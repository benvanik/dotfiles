"""Boot-local single writer for model services, claims, and Pi use leases."""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import fcntl
import os
import pathlib
import socket
import stat
import threading
from collections.abc import Callable
from typing import Any

from .catalog import load_profile_route, load_service_id
from .controller import ModelLabController, ServiceUse
from .deployed_service import DeployedServiceStore
from .errors import ModelLabError
from .paths import ensure_private_directory
from .service_definition import ServiceDefinition
from .supervisor_protocol import (
    PI_PENDING_SCHEMA,
    SESSION_USE_ACCEPTED_SCHEMA,
    SESSION_USE_ADMIT_SCHEMA,
    SUPERVISOR_ERROR_SCHEMA,
    SUPERVISOR_REQUEST_SCHEMA,
    SUPERVISOR_RESULT_SCHEMA,
    enable_sender_credentials,
    peer_credentials,
    process_start_time,
    receive_document,
    receive_document_with_credentials,
    require_exact_fields,
    require_identifier,
    require_process_identity,
    send_document,
    supervisor_lock_path,
    supervisor_socket_path,
)


@dataclasses.dataclass(frozen=True)
class SupervisorFailure:
    operation: str
    code: str
    message: str


FailureReporter = Callable[[SupervisorFailure], None]


def _no_report(_failure: SupervisorFailure) -> None:
    return


class ModelLabSupervisor:
    """Own every controller mutation and every live session-use channel."""

    def __init__(
        self,
        *,
        controller: ModelLabController,
        authored_root: pathlib.Path,
        state_root: pathlib.Path,
        runtime_root: pathlib.Path,
        report_failure: FailureReporter = _no_report,
        admission_timeout_seconds: float = 60.0,
        maintenance_interval_seconds: float | None = None,
    ) -> None:
        self.controller = controller
        self.authored_root = authored_root
        self.state_root = state_root
        self.runtime_root = runtime_root
        self.report_failure = report_failure
        self.admission_timeout_seconds = admission_timeout_seconds
        self.maintenance_interval_seconds = (
            max(
                1.0,
                min(30.0, controller.lab.lease.renewal_ttl_seconds / 3),
            )
            if maintenance_interval_seconds is None
            else maintenance_interval_seconds
        )
        self.deployed_services = DeployedServiceStore(state_root)
        self.mutation_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self._listener: socket.socket | None = None
        self._singleton_descriptor: int | None = None
        self._threads: set[threading.Thread] = set()
        self._connections: set[socket.socket] = set()
        self._connection_services: dict[socket.socket, str] = {}
        self._connections_lock = threading.Lock()
        self._supervisor_pid = os.getpid()
        self._supervisor_start_time = process_start_time(self._supervisor_pid)

    @property
    def socket_path(self) -> pathlib.Path:
        return supervisor_socket_path(self.runtime_root)

    def _acquire_singleton(self) -> None:
        ensure_private_directory(self.runtime_root)
        path = supervisor_lock_path(self.runtime_root)
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o077
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            os.close(descriptor)
            raise ModelLabError(
                "model-lab supervisor lock has an unsafe identity",
                code="unsafe_supervisor_runtime",
            )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            raise ModelLabError(
                "a model-lab supervisor already owns this runtime",
                code="supervisor_already_running",
            ) from error
        self._singleton_descriptor = descriptor

    def _remove_stale_socket(self) -> None:
        path = self.socket_path
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if (
            not stat.S_ISSOCK(metadata.st_mode)
            or path.is_symlink()
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            raise ModelLabError(
                f"refusing unsafe stale supervisor path: {path}",
                code="unsafe_supervisor_runtime",
            )
        path.unlink()

    def _open_listener(self) -> socket.socket:
        self._remove_stale_socket()
        listener = socket.socket(
            socket.AF_UNIX,
            socket.SOCK_STREAM | getattr(socket, "SOCK_CLOEXEC", 0),
        )
        try:
            listener.bind(os.fspath(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            listener.listen(64)
            listener.settimeout(0.5)
        except BaseException:
            listener.close()
            raise
        self._listener = listener
        return listener

    def serve_forever(self) -> None:
        """Acquire the singleton, reconcile old channels, and serve requests."""

        self._acquire_singleton()
        listener: socket.socket | None = None
        maintenance: threading.Thread | None = None
        try:
            with self.mutation_lock:
                try:
                    self.controller.deployments.reconcile_orphaned_uses(
                        idle_ttl_seconds=(
                            self.controller.lab.lease.service_idle_ttl_seconds
                        )
                    )
                except Exception as error:
                    self._report_operation_failure(
                        "startup:orphaned-uses",
                        error,
                    )
                self._reconcile_startup_deployments()
            listener = self._open_listener()
            maintenance = threading.Thread(
                target=self._maintenance_loop,
                name="model-lab-maintenance",
                daemon=True,
            )
            maintenance.start()
            self.ready_event.set()
            while not self.stop_event.is_set():
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError as error:
                    if self.stop_event.is_set() or error.errno in {
                        errno.EBADF,
                        errno.EINVAL,
                    }:
                        break
                    raise
                worker = threading.Thread(
                    target=self._serve_connection,
                    args=(connection,),
                    name="model-lab-client",
                    daemon=True,
                )
                with self._connections_lock:
                    self._connections.add(connection)
                    self._threads.add(worker)
                worker.start()
        finally:
            self.ready_event.set()
            self.stop_event.set()
            if listener is not None:
                listener.close()
            self._close_all_connections()
            if maintenance is not None:
                maintenance.join()
            with self._connections_lock:
                workers = tuple(self._threads)
            for worker in workers:
                worker.join()
            self._close_runtime()

    def _reconcile_startup_deployments(self) -> None:
        try:
            intents = self.controller.preparations.list()
        except Exception as error:
            self._report_operation_failure("startup:intents", error)
            intents = ()
        for intent in intents:
            try:
                self.controller.reconcile_acquire_intent(intent)
            except Exception as error:
                self._report_operation_failure(
                    f"startup:intent:{intent.service_id}",
                    error,
                )

        try:
            deployments = self.controller.deployments.list()
        except Exception as error:
            self._report_operation_failure("startup:deployments", error)
            deployments = ()
        for deployment in deployments:
            if deployment.phase == "released":
                continue
            try:
                service = self.deployed_services.load(
                    deployment.service_id,
                    deployment.service_sha256,
                )
                if deployment.phase == "preparing":
                    deployment = self.controller.reconcile_preparing(
                        service,
                        deployment,
                    )
                    if deployment.phase == "ready":
                        self.controller.down(service, now=False)
                elif (
                    deployment.phase == "ready"
                    and not deployment.use_leases
                ):
                    self.controller.down(service, now=False)
                elif deployment.phase in {
                    "quiescing",
                    "stopping",
                    "failed",
                }:
                    self.controller.reconcile_cleanup(service, deployment)
            except Exception as error:
                self._report_operation_failure(
                    f"startup:deployment:{deployment.service_id}",
                    error,
                )

    def stop(self) -> None:
        self.stop_event.set()
        listener = self._listener
        if listener is not None:
            listener.close()
        self._close_all_connections()

    def _close_all_connections(self) -> None:
        with self._connections_lock:
            connections = tuple(self._connections)
        for connection in connections:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)

    def _close_service_connections(self, service_id: str) -> None:
        with self._connections_lock:
            connections = tuple(
                connection
                for connection, current_service in self._connection_services.items()
                if current_service == service_id
            )
        for connection in connections:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)

    def _close_runtime(self) -> None:
        path = self.socket_path
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            metadata = None
        if (
            metadata is not None
            and stat.S_ISSOCK(metadata.st_mode)
            and not path.is_symlink()
            and (not hasattr(os, "getuid") or metadata.st_uid == os.getuid())
        ):
            path.unlink()
        if self._singleton_descriptor is not None:
            os.close(self._singleton_descriptor)
            self._singleton_descriptor = None

    def _serve_connection(self, connection: socket.socket) -> None:
        operation = "unknown"
        try:
            _, peer_uid, _ = peer_credentials(connection)
            if hasattr(os, "getuid") and peer_uid != os.getuid():
                raise ModelLabError(
                    "supervisor client is owned by a different user",
                    code="supervisor_peer_mismatch",
                )
            request = receive_document(connection)
            operation = request.get("operation", "unknown")
            if operation == "pi-acquire":
                self._serve_pi(connection, request)
            elif operation == "up":
                self._serve_up(connection, request)
            elif operation == "down":
                self._serve_down(connection, request)
            elif operation == "ping":
                require_exact_fields(
                    request,
                    schema=SUPERVISOR_REQUEST_SCHEMA,
                    fields=frozenset({"operation"}),
                )
                send_document(
                    connection,
                    {
                        "schema": SUPERVISOR_RESULT_SCHEMA,
                        "operation": "ping",
                        "result": {
                            "pid": self._supervisor_pid,
                            "start_time": self._supervisor_start_time,
                        },
                    },
                )
            else:
                raise ModelLabError(
                    "supervisor operation is unsupported",
                    code="unsupported_supervisor_operation",
                )
        except ModelLabError as error:
            self._send_error(connection, error)
            self.report_failure(
                SupervisorFailure(operation, error.code, str(error))
            )
        except Exception as error:
            failure = ModelLabError(
                f"supervisor operation failed: {error}",
                code="supervisor_operation_failed",
            )
            self._send_error(connection, failure)
            self.report_failure(
                SupervisorFailure(operation, failure.code, str(failure))
            )
        finally:
            with self._connections_lock:
                self._connections.discard(connection)
                self._connection_services.pop(connection, None)
                self._threads.discard(threading.current_thread())
            connection.close()

    @staticmethod
    def _send_error(connection: socket.socket, error: ModelLabError) -> None:
        with contextlib.suppress(ModelLabError):
            send_document(
                connection,
                {
                    "schema": SUPERVISOR_ERROR_SCHEMA,
                    "code": error.code,
                    "message": str(error),
                },
            )

    def _service_snapshot_for_deployment(
        self,
        service_id: str,
    ) -> ServiceDefinition:
        deployment = self.controller.deployments.load(service_id)
        if deployment is None:
            raise ModelLabError(
                f"service {service_id} has no deployment",
                code="deployment_not_found",
            )
        return self.deployed_services.load(
            service_id,
            deployment.service_sha256,
        )

    def _serve_up(
        self,
        connection: socket.socket,
        request: dict[str, Any],
    ) -> None:
        require_exact_fields(
            request,
            schema=SUPERVISOR_REQUEST_SCHEMA,
            fields=frozenset({"operation", "service_id", "host_name"}),
        )
        service_id = require_identifier(request["service_id"], label="service ID")
        host_name = request["host_name"]
        if host_name is not None and not isinstance(host_name, str):
            raise ModelLabError(
                "host name must be null or text",
                code="invalid_supervisor_protocol",
            )
        service = load_service_id(service_id, root=self.authored_root)
        self.deployed_services.publish(service)
        with self.mutation_lock:
            try:
                deployment, endpoint = self.controller.ensure_ready(
                    service,
                    host_name=host_name,
                )
            except ModelLabError as error:
                if error.code not in {
                    "service_claim_lost",
                    "service_claim_drained",
                    "service_runtime_replaced",
                    "service_transport_replaced",
                }:
                    if error.code == "service_cleanup_required":
                        self._close_service_connections(
                            service.service_id
                        )
                    raise
                self._close_service_connections(service.service_id)
                deployment, endpoint = self.controller.ensure_ready(
                    service,
                    host_name=host_name,
                )
            deployment = self.controller.down(service, now=False)
        send_document(
            connection,
            {
                "schema": SUPERVISOR_RESULT_SCHEMA,
                "operation": "up",
                "result": {
                    "deployment": deployment.normalized(),
                    "endpoint": endpoint.as_dict(),
                },
            },
        )

    def _serve_down(
        self,
        connection: socket.socket,
        request: dict[str, Any],
    ) -> None:
        require_exact_fields(
            request,
            schema=SUPERVISOR_REQUEST_SCHEMA,
            fields=frozenset({"operation", "service_id", "now"}),
        )
        service_id = require_identifier(request["service_id"], label="service ID")
        now = request["now"]
        if type(now) is not bool:
            raise ModelLabError(
                "down now selector must be boolean",
                code="invalid_supervisor_protocol",
            )
        service = self._service_snapshot_for_deployment(service_id)
        if now:
            self._close_service_connections(service_id)
        with self.mutation_lock:
            deployment = self.controller.down(service, now=now)
        send_document(
            connection,
            {
                "schema": SUPERVISOR_RESULT_SCHEMA,
                "operation": "down",
                "result": {"deployment": deployment.normalized()},
            },
        )

    def _serve_pi(
        self,
        connection: socket.socket,
        request: dict[str, Any],
    ) -> None:
        require_exact_fields(
            request,
            schema=SUPERVISOR_REQUEST_SCHEMA,
            fields=frozenset(
                {
                    "operation",
                    "profile_id",
                    "host_name",
                    "stop_on_release",
                }
            ),
        )
        profile_id = require_identifier(request["profile_id"], label="profile ID")
        host_name = request["host_name"]
        if host_name is not None and not isinstance(host_name, str):
            raise ModelLabError(
                "host name must be null or text",
                code="invalid_supervisor_protocol",
            )
        stop_on_release = request["stop_on_release"]
        if type(stop_on_release) is not bool:
            raise ModelLabError(
                "stop_on_release must be boolean",
                code="invalid_supervisor_protocol",
            )
        route = load_profile_route(profile_id, root=self.authored_root)
        service = load_service_id(route.service_id, root=self.authored_root)
        self.deployed_services.publish(service)
        use: ServiceUse | None = None
        released = False
        try:
            with self.mutation_lock:
                try:
                    use = self.controller.acquire_for_profile(
                        route,
                        service,
                        host_name=host_name,
                        owner_pid=self._supervisor_pid,
                        owner_start_time=self._supervisor_start_time,
                    )
                except ModelLabError as error:
                    if error.code not in {
                        "service_claim_lost",
                        "service_claim_drained",
                        "service_runtime_replaced",
                        "service_transport_replaced",
                    }:
                        if error.code == "service_cleanup_required":
                            self._close_service_connections(
                                service.service_id
                            )
                        raise
                    self._close_service_connections(
                        service.service_id
                    )
                    use = self.controller.acquire_for_profile(
                        route,
                        service,
                        host_name=host_name,
                        owner_pid=self._supervisor_pid,
                        owner_start_time=self._supervisor_start_time,
                    )
            with self._connections_lock:
                self._connection_services[connection] = service.service_id
            enable_sender_credentials(connection)
            send_document(
                connection,
                {
                    "schema": PI_PENDING_SCHEMA,
                    "profile_id": route.profile_id,
                    "service_id": service.service_id,
                    "workload_sha256": use.deployment.workload_sha256,
                    "deployment_id": use.deployment.deployment_id,
                    "use_lease_id": use.lease.lease_id,
                },
            )
            connection.settimeout(self.admission_timeout_seconds)
            admission, sender_credentials = (
                receive_document_with_credentials(connection)
            )
            require_exact_fields(
                admission,
                schema=SESSION_USE_ADMIT_SCHEMA,
                fields=frozenset(
                    {
                        "profile_id",
                        "service_id",
                        "pid",
                        "start_time",
                    }
                ),
            )
            if (
                admission["profile_id"] != route.profile_id
                or admission["service_id"] != service.service_id
            ):
                raise ModelLabError(
                    "session admission does not match its pending grant",
                    code="session_use_admission_mismatch",
                )
            session_pid, session_start = require_process_identity(
                admission["pid"],
                admission["start_time"],
            )
            sender_pid, sender_uid, _ = sender_credentials
            if (
                sender_pid != session_pid
                or (hasattr(os, "getuid") and sender_uid != os.getuid())
                or process_start_time(session_pid) != session_start
            ):
                raise ModelLabError(
                    "session process does not own the pending lease channel",
                    code="session_use_admission_mismatch",
                )
            with self.mutation_lock:
                lease = self.controller.deployments.transfer_use_owner(
                    service.service_id,
                    use.lease.lease_id,
                    expected_owner_pid=self._supervisor_pid,
                    expected_owner_start_time=self._supervisor_start_time,
                    owner_pid=session_pid,
                    owner_start_time=session_start,
                )
            use = dataclasses.replace(use, lease=lease)
            send_document(
                connection,
                {
                    "schema": SESSION_USE_ACCEPTED_SCHEMA,
                    "profile_id": route.profile_id,
                    "service_id": service.service_id,
                    "workload_sha256": use.deployment.workload_sha256,
                    "deployment_id": use.deployment.deployment_id,
                    "use_lease_id": use.lease.lease_id,
                    "supervisor_pid": self._supervisor_pid,
                    "supervisor_start_time": self._supervisor_start_time,
                    "session_pid": session_pid,
                    "session_start_time": session_start,
                },
            )
            connection.settimeout(1.0)
            while not self.stop_event.is_set():
                try:
                    unexpected = connection.recv(1)
                except TimeoutError:
                    continue
                except OSError:
                    break
                if not unexpected:
                    break
                raise ModelLabError(
                    "session-use channel carried bytes after admission",
                    code="invalid_supervisor_protocol",
                )
        finally:
            if use is not None and not released:
                with self.mutation_lock:
                    try:
                        self.controller.release_profile_use(
                            service,
                            use,
                            now=stop_on_release,
                        )
                    except ModelLabError as error:
                        if error.code != "use_lease_not_found":
                            raise
                released = True

    def maintain_once(self) -> None:
        """Renew exact claims, stop due services, and retire empty hosts."""

        with self.mutation_lock:
            try:
                intents = self.controller.preparations.list()
            except Exception as error:
                self._report_operation_failure("maintenance:intents", error)
                intents = ()
            for intent in intents:
                try:
                    self.controller.reconcile_acquire_intent(intent)
                except Exception as error:
                    self._report_operation_failure(
                        f"maintenance:intent:{intent.service_id}",
                        error,
                    )

            try:
                deployments = self.controller.deployments.list()
            except Exception as error:
                self._report_operation_failure(
                    "maintenance:deployments",
                    error,
                )
                deployments = ()
            for deployment in deployments:
                if deployment.phase == "released":
                    continue
                try:
                    service = self.deployed_services.load(
                        deployment.service_id,
                        deployment.service_sha256,
                    )
                    if deployment.phase in {
                        "quiescing",
                        "stopping",
                        "failed",
                    }:
                        self._close_service_connections(
                            deployment.service_id
                        )
                        self.controller.reconcile_cleanup(
                            service,
                            deployment,
                        )
                        continue
                    if deployment.phase not in {"ready", "idle"}:
                        continue
                    if (
                        deployment.phase == "idle"
                        and self.controller.stop_if_idle_due(service)
                    ):
                        continue
                    try:
                        self.controller.renew_deployment_claim(deployment)
                    except Exception as error:
                        if self.controller.is_claim_quarantined(error):
                            self._close_service_connections(
                                deployment.service_id
                            )
                            self.controller.drain_quarantined_claim(
                                service,
                                deployment,
                            )
                        elif self.controller.is_claim_gone(error):
                            self._close_service_connections(
                                deployment.service_id
                            )
                            self.controller.reconcile_claim_gone(
                                service,
                                deployment,
                            )
                        else:
                            raise
                except Exception as error:
                    self._report_operation_failure(
                        f"maintenance:deployment:{deployment.service_id}",
                        error,
                    )
            try:
                self.controller.hosts.enforce_retirement(execute=True)
            except Exception as error:
                self._report_operation_failure(
                    "maintenance:host-retirement",
                    error,
                )

    def _report_operation_failure(
        self,
        operation: str,
        error: BaseException,
    ) -> None:
        code = getattr(error, "code", None)
        if not isinstance(code, str) or not code:
            code = "supervisor_operation_failed"
        self.report_failure(
            SupervisorFailure(operation, code, str(error))
        )

    def _maintenance_loop(self) -> None:
        while not self.stop_event.wait(self.maintenance_interval_seconds):
            try:
                self.maintain_once()
            except ModelLabError as error:
                self.report_failure(
                    SupervisorFailure("maintenance", error.code, str(error))
                )
            except Exception as error:
                self.report_failure(
                    SupervisorFailure(
                        "maintenance",
                        "supervisor_maintenance_failed",
                        str(error),
                    )
                )
