"""Boot-local single writer for model services, claims, and Pi use leases."""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import fcntl
import io
import os
import pathlib
import socket
import stat
import threading
from collections.abc import Callable
from typing import Any

from model_session.errors import ModelSessionError
from model_session.launcher import resolve_resume_selection
from model_session.profile import load_profile

from .catalog import load_profile_route, load_service_id
from .controller import ModelLabController, ServiceUse
from .deployed_service import DeployedServiceStore
from .errors import ModelLabError
from .lifecycle import Deployment
from .paths import ensure_private_directory, profile_path
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
    require_canonical_input_modalities,
    require_exact_fields,
    require_identifier,
    require_monotonic_deadline,
    require_nullable_opaque_identifier,
    require_process_identity,
    require_sha256,
    require_timestamp,
    send_document,
    supervisor_lock_path,
    supervisor_socket_path,
)


@dataclasses.dataclass(frozen=True)
class SupervisorFailure:
    operation: str
    code: str
    message: str


@dataclasses.dataclass(frozen=True)
class _ImmediateDownFence:
    sequence: int
    completed: threading.Event


@dataclasses.dataclass(frozen=True)
class _PiOperation:
    sequence: int
    preceding_down: _ImmediateDownFence | None


@dataclasses.dataclass(frozen=True)
class _PendingUseRelease:
    service: ServiceDefinition
    use: ServiceUse
    now: bool
    stop_if_final: bool


@dataclasses.dataclass(frozen=True)
class _FrozenProfileSelection:
    """Exact provider-free profile authority admitted by one Pi request."""

    profile_id: str
    project_id: str
    service_id: str
    required_input_modalities: tuple[str, ...]


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
        self._service_locks_guard = threading.Lock()
        self._service_locks: dict[str, threading.RLock] = {}
        self._service_operations_guard = threading.Lock()
        self._service_operation_sequences: dict[str, int] = {}
        self._immediate_down_fences: dict[
            str,
            _ImmediateDownFence,
        ] = {}
        self._rollback_lock = threading.Lock()
        self._pending_release_lock = threading.Lock()
        self._pending_use_releases: dict[
            tuple[str, str],
            _PendingUseRelease,
        ] = {}
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self._listener: socket.socket | None = None
        self._singleton_descriptor: int | None = None
        self._threads: set[threading.Thread] = set()
        self._connections: set[socket.socket] = set()
        self._connection_services: dict[socket.socket, str] = {}
        self._connections_lock = threading.Lock()
        # A newly created deployment is rollback-owned only while its creating
        # ``up`` response is the latest service operation. A later up, down, or
        # Pi admission invalidates that ownership even when it leaves the
        # durable deployment fields byte-for-byte unchanged.
        self._pending_up_rollbacks: dict[str, tuple[str, object]] = {}
        self._supervisor_pid = os.getpid()
        self._supervisor_start_time = process_start_time(self._supervisor_pid)

    @contextlib.contextmanager
    def _service_mutation(
        self,
        service_id: str,
        *,
        deadline: float | None = None,
        deadline_error_code: str = "service_startup_timeout",
    ):
        """Serialize one service, optionally within an absolute deadline."""

        with self._service_locks_guard:
            lock = self._service_locks.setdefault(
                service_id,
                threading.RLock(),
            )
        if deadline is None:
            lock.acquire()
        else:
            remaining_seconds = deadline - self.controller.monotonic()
            if remaining_seconds <= 0 or not lock.acquire(
                timeout=remaining_seconds
            ):
                raise ModelLabError(
                    "service mutation did not begin within its absolute "
                    "deadline",
                    code=deadline_error_code,
                )
        try:
            if (
                deadline is not None
                and self.controller.monotonic() >= deadline
            ):
                raise ModelLabError(
                    "service mutation did not begin within its absolute "
                    "deadline",
                    code=deadline_error_code,
                )
            yield
        finally:
            lock.release()

    def _next_service_operation_sequence(self, service_id: str) -> int:
        sequence = self._service_operation_sequences.get(service_id, 0) + 1
        self._service_operation_sequences[service_id] = sequence
        return sequence

    def _begin_pi_operation(self, service_id: str) -> _PiOperation:
        with self._service_operations_guard:
            return _PiOperation(
                sequence=self._next_service_operation_sequence(service_id),
                preceding_down=self._immediate_down_fences.get(service_id),
            )

    def _begin_immediate_down(self, service_id: str) -> _ImmediateDownFence:
        with self._service_operations_guard:
            fence = _ImmediateDownFence(
                sequence=self._next_service_operation_sequence(service_id),
                completed=threading.Event(),
            )
            self._immediate_down_fences[service_id] = fence
            return fence

    def _wait_for_preceding_down(
        self,
        operation: _PiOperation,
        startup_deadline: float,
    ) -> None:
        fence = operation.preceding_down
        if fence is None or fence.completed.is_set():
            return
        remaining_seconds = startup_deadline - self.controller.monotonic()
        if remaining_seconds <= 0 or not fence.completed.wait(
            remaining_seconds
        ):
            self.controller.require_startup_budget(startup_deadline)
            raise AssertionError("expired startup budget was accepted")
        self.controller.require_startup_budget(startup_deadline)

    def _require_current_pi_operation(
        self,
        service_id: str,
        operation: _PiOperation,
    ) -> None:
        with self._service_operations_guard:
            fence = self._immediate_down_fences.get(service_id)
        if fence is not None and fence.sequence > operation.sequence:
            raise ModelLabError(
                "Pi startup was superseded by a newer immediate service "
                "shutdown",
                code="service_startup_superseded",
            )

    def _clear_pending_up_rollback(self, service_id: str) -> None:
        with self._rollback_lock:
            self._pending_up_rollbacks.pop(service_id, None)

    def _record_pending_up_rollback(
        self,
        service_id: str,
        deployment_id: str,
        marker: object,
    ) -> None:
        with self._rollback_lock:
            self._pending_up_rollbacks[service_id] = (
                deployment_id,
                marker,
            )

    def _claim_pending_up_rollback(
        self,
        service_id: str,
        deployment_id: str,
        marker: object,
    ) -> bool:
        expected = (deployment_id, marker)
        with self._rollback_lock:
            if self._pending_up_rollbacks.get(service_id) != expected:
                return False
            self._pending_up_rollbacks.pop(service_id)
            return True

    @staticmethod
    def _pending_release_key(
        pending: _PendingUseRelease,
    ) -> tuple[str, str]:
        return (
            pending.service.service_id,
            pending.use.lease.lease_id,
        )

    def _queue_use_release(self, pending: _PendingUseRelease) -> None:
        key = self._pending_release_key(pending)
        with self._pending_release_lock:
            retained = self._pending_use_releases.get(key)
            if retained is not None and retained != pending:
                raise ModelLabError(
                    "queued use release changed identity",
                    code="use_release_identity_changed",
                )
            self._pending_use_releases[key] = pending

    def _complete_use_release(self, pending: _PendingUseRelease) -> None:
        key = self._pending_release_key(pending)
        with self._pending_release_lock:
            if self._pending_use_releases.get(key) == pending:
                self._pending_use_releases.pop(key)

    def _pending_release_snapshot(self) -> tuple[_PendingUseRelease, ...]:
        with self._pending_release_lock:
            return tuple(self._pending_use_releases.values())

    def _attempt_use_release(self, pending: _PendingUseRelease) -> None:
        try:
            self.controller.release_profile_use(
                pending.service,
                pending.use,
                now=pending.now,
                stop_if_final=pending.stop_if_final,
            )
        except ModelLabError as error:
            if error.code != "use_lease_not_found":
                raise
        self._complete_use_release(pending)

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

    def _deployment_snapshot(
        self,
        service_id: str,
    ) -> Deployment:
        deployment = self.controller.deployments.load(service_id)
        if deployment is None:
            raise ModelLabError(
                f"service {service_id} has no deployment",
                code="deployment_not_found",
            )
        return deployment

    def _serve_up(
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
                    "service_id",
                    "host_name",
                    "startup_expires_at",
                    "startup_deadline",
                }
            ),
        )
        service_id = require_identifier(request["service_id"], label="service ID")
        host_name = request["host_name"]
        if host_name is not None and not isinstance(host_name, str):
            raise ModelLabError(
                "host name must be null or text",
                code="invalid_supervisor_protocol",
            )
        startup_expires_at = self.controller.canonical_startup_expiration(
            require_timestamp(
                request["startup_expires_at"],
                label="service startup expiration",
            )
        )
        startup_deadline = self.controller.startup_deadline_from_expiration(
            startup_expires_at,
            startup_deadline=require_monotonic_deadline(
                request["startup_deadline"],
                label="service startup monotonic deadline",
            ),
        )
        service = load_service_id(service_id, root=self.authored_root)
        with self._service_mutation(
            service.service_id,
            deadline=startup_deadline,
        ):
            self.deployed_services.publish(service)
            self._clear_pending_up_rollback(service.service_id)
            prior = self.controller.deployments.load(service.service_id)
            rollback_deployment_id: str | None = None
            rollback_marker: object | None = None
            try:
                deployment, endpoint = self.controller.ensure_ready(
                    service,
                    host_name=host_name,
                    startup_expires_at=startup_expires_at,
                    startup_deadline=startup_deadline,
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
                    startup_expires_at=startup_expires_at,
                    startup_deadline=startup_deadline,
                )
            if (
                prior is None
                or prior.phase == "released"
                or prior.deployment_id != deployment.deployment_id
            ):
                rollback_deployment_id = deployment.deployment_id
            try:
                self.controller.require_startup_budget(startup_deadline)
                deployment = self.controller.down(
                    service,
                    now=False,
                    cleanup_deadline=startup_deadline,
                    deadline_error_code="service_startup_timeout",
                )
                self.controller.require_startup_budget(startup_deadline)
                if rollback_deployment_id is not None:
                    rollback_marker = object()
                    self._record_pending_up_rollback(
                        service.service_id,
                        rollback_deployment_id,
                        rollback_marker,
                    )
            except BaseException:
                current = self.controller.deployments.load(
                    service.service_id
                )
                if (
                    rollback_deployment_id is not None
                    and current is not None
                    and current.deployment_id == rollback_deployment_id
                ):
                    self.controller.down(service, now=True)
                raise
        try:
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
                deadline=startup_deadline,
                monotonic=self.controller.monotonic,
                deadline_error_code="service_startup_timeout",
            )
            self.controller.require_startup_budget(startup_deadline)
        except BaseException:
            with self._service_mutation(service.service_id):
                rollback_owned = (
                    rollback_deployment_id is not None
                    and rollback_marker is not None
                    and self._claim_pending_up_rollback(
                        service.service_id,
                        rollback_deployment_id,
                        rollback_marker,
                    )
                )
                current = self.controller.deployments.load(
                    service.service_id
                )
                if (
                    rollback_owned
                    and current is not None
                    and current == deployment
                    and current.deployment_id == rollback_deployment_id
                    and current.phase in {"ready", "idle"}
                    and not current.use_leases
                ):
                    self.controller.down(service, now=True)
            raise
        else:
            if (
                rollback_deployment_id is not None
                and rollback_marker is not None
            ):
                self._claim_pending_up_rollback(
                    service.service_id,
                    rollback_deployment_id,
                    rollback_marker,
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
        fence = self._begin_immediate_down(service_id) if now else None
        try:
            with self._service_mutation(service_id):
                if now:
                    self._close_service_connections(service_id)
                current = self._deployment_snapshot(service_id)
                self._clear_pending_up_rollback(service_id)
                if now and current.phase == "released":
                    deployment = current
                else:
                    service = self.deployed_services.load(
                        service_id,
                        current.service_sha256,
                    )
                    if now and current.phase in {
                        "quiescing",
                        "stopping",
                        "failed",
                    }:
                        current = (
                            self.controller.deployments.escalate_cleanup_now(
                                service_id
                            )
                        )
                        deployment = self.controller.reconcile_cleanup(
                            service,
                            current,
                        )
                    else:
                        deployment = self.controller.down(service, now=now)
        finally:
            if fence is not None:
                fence.completed.set()
        send_document(
            connection,
            {
                "schema": SUPERVISOR_RESULT_SCHEMA,
                "operation": "down",
                "result": {"deployment": deployment.normalized()},
            },
        )

    def _validate_pi_identity(
        self,
        *,
        profile_id: str,
        project_id: str,
        service_id: str,
        service_sha256: str,
        workload_sha256: str,
        required_input_modalities: tuple[str, ...],
        session_id: str | None,
    ) -> tuple[_FrozenProfileSelection, ServiceDefinition]:
        """Revalidate the caller's exact provider-free selection."""

        route = load_profile_route(profile_id, root=self.authored_root)
        if (
            route.project_id != project_id
            or route.service_id != service_id
        ):
            raise ModelLabError(
                "Pi profile route changed after local validation",
                code="pi_identity_changed",
            )
        profile_root = profile_path(profile_id, self.authored_root).parent
        if session_id is None:
            try:
                loaded_profile = load_profile(profile_root)
            except ModelSessionError as error:
                raise ModelLabError(str(error), code=error.code) from error
            contract = loaded_profile.contract
            if (
                contract.profile_id != profile_id
                or contract.project_id != project_id
                or contract.service_id != service_id
                or contract.endpoint is None
            ):
                raise ModelLabError(
                    "Pi profile changed after local validation",
                    code="pi_identity_changed",
                )
            selected_modalities = tuple(
                sorted(contract.endpoint.required_input_modalities)
            )
        else:
            try:
                resumed = resolve_resume_selection(
                    profile_root,
                    session_id,
                    input_stream=io.StringIO(""),
                    output=io.StringIO(),
                )
            except ModelSessionError as error:
                raise ModelLabError(str(error), code=error.code) from error
            if (
                resumed.session_id != session_id
                or resumed.service_id != service_id
                or resumed.workload_sha256 != workload_sha256
            ):
                raise ModelLabError(
                    "Pi resume selection changed after local validation",
                    code="pi_identity_changed",
                )
            selected_modalities = tuple(sorted(resumed.input_modalities))
        if selected_modalities != required_input_modalities:
            raise ModelLabError(
                "Pi input requirements changed after local validation",
                code="pi_identity_changed",
            )
        service = load_service_id(service_id, root=self.authored_root)
        if (
            service.service_sha256 != service_sha256
            or service.workload_sha256 != workload_sha256
            or not set(required_input_modalities).issubset(
                service.endpoint.input_modalities
            )
        ):
            raise ModelLabError(
                "Pi service changed after local validation",
                code="pi_identity_changed",
            )
        return (
            _FrozenProfileSelection(
                profile_id=profile_id,
                project_id=project_id,
                service_id=service_id,
                required_input_modalities=required_input_modalities,
            ),
            service,
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
                    "project_id",
                    "service_id",
                    "service_sha256",
                    "workload_sha256",
                    "required_input_modalities",
                    "session_id",
                    "host_name",
                    "stop_on_release",
                    "startup_expires_at",
                    "startup_deadline",
                }
            ),
        )
        profile_id = require_identifier(request["profile_id"], label="profile ID")
        project_id = require_identifier(request["project_id"], label="project ID")
        service_id = require_identifier(request["service_id"], label="service ID")
        service_sha256 = require_sha256(
            request["service_sha256"],
            label="service document",
        )
        workload_sha256 = require_sha256(
            request["workload_sha256"],
            label="service workload",
        )
        required_input_modalities = require_canonical_input_modalities(
            request["required_input_modalities"],
            label="required input modalities",
        )
        session_id = require_nullable_opaque_identifier(
            request["session_id"],
            label="session ID",
        )
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
        startup_expires_at = self.controller.canonical_startup_expiration(
            require_timestamp(
                request["startup_expires_at"],
                label="Pi startup expiration",
            )
        )
        startup_deadline = self.controller.startup_deadline_from_expiration(
            startup_expires_at,
            startup_deadline=require_monotonic_deadline(
                request["startup_deadline"],
                label="Pi startup monotonic deadline",
            ),
        )
        operation = self._begin_pi_operation(service_id)
        profile, service = self._validate_pi_identity(
            profile_id=profile_id,
            project_id=project_id,
            service_id=service_id,
            service_sha256=service_sha256,
            workload_sha256=workload_sha256,
            required_input_modalities=required_input_modalities,
            session_id=session_id,
        )
        self._wait_for_preceding_down(operation, startup_deadline)
        use: ServiceUse | None = None
        admitted = False
        try:
            with self._service_mutation(
                service.service_id,
                deadline=startup_deadline,
            ):
                self._require_current_pi_operation(
                    service.service_id,
                    operation,
                )
                self.deployed_services.publish(service)
                self._clear_pending_up_rollback(service.service_id)
                try:
                    use = self.controller.acquire_for_profile(
                        profile,
                        service,
                        host_name=host_name,
                        owner_pid=self._supervisor_pid,
                        owner_start_time=self._supervisor_start_time,
                        startup_expires_at=startup_expires_at,
                        startup_deadline=startup_deadline,
                        stop_on_release=stop_on_release,
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
                        profile,
                        service,
                        host_name=host_name,
                        owner_pid=self._supervisor_pid,
                        owner_start_time=self._supervisor_start_time,
                        startup_expires_at=startup_expires_at,
                        startup_deadline=startup_deadline,
                        stop_on_release=stop_on_release,
                    )
                self.controller.require_startup_budget(startup_deadline)
                with self._connections_lock:
                    self._connection_services[connection] = (
                        service.service_id
                    )
            enable_sender_credentials(connection)
            send_document(
                connection,
                {
                    "schema": PI_PENDING_SCHEMA,
                    "profile_id": profile.profile_id,
                    "project_id": profile.project_id,
                    "service_id": service.service_id,
                    "service_sha256": service.service_sha256,
                    "workload_sha256": use.deployment.workload_sha256,
                    "required_input_modalities": list(
                        profile.required_input_modalities
                    ),
                    "session_id": session_id,
                    "deployment_id": use.deployment.deployment_id,
                    "use_lease_id": use.lease.lease_id,
                },
                deadline=startup_deadline,
                monotonic=self.controller.monotonic,
                deadline_error_code="service_startup_timeout",
            )
            self.controller.require_startup_budget(startup_deadline)
            remaining_seconds = startup_deadline - self.controller.monotonic()
            if remaining_seconds <= 0:
                self.controller.require_startup_budget(startup_deadline)
                raise AssertionError("expired startup budget was accepted")
            admission_deadline = min(
                startup_deadline,
                self.controller.monotonic()
                + self.admission_timeout_seconds,
            )
            admission, sender_credentials = (
                receive_document_with_credentials(
                    connection,
                    deadline=admission_deadline,
                    monotonic=self.controller.monotonic,
                    deadline_error_code="service_startup_timeout",
                )
            )
            self.controller.require_startup_budget(startup_deadline)
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
                admission["profile_id"] != profile.profile_id
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
            with self._service_mutation(
                service.service_id,
                deadline=startup_deadline,
            ):
                self.controller.require_startup_budget(startup_deadline)
                lease = self.controller.deployments.transfer_use_owner(
                    service.service_id,
                    use.lease.lease_id,
                    expected_owner_pid=self._supervisor_pid,
                    expected_owner_start_time=self._supervisor_start_time,
                    owner_pid=session_pid,
                    owner_start_time=session_start,
                    startup_deadline=startup_deadline,
                    monotonic=self.controller.monotonic,
                )
            use = dataclasses.replace(use, lease=lease)
            self.controller.require_startup_budget(startup_deadline)
            send_document(
                connection,
                {
                    "schema": SESSION_USE_ACCEPTED_SCHEMA,
                    "profile_id": profile.profile_id,
                    "service_id": service.service_id,
                    "workload_sha256": use.deployment.workload_sha256,
                    "deployment_id": use.deployment.deployment_id,
                    "use_lease_id": use.lease.lease_id,
                    "supervisor_pid": self._supervisor_pid,
                    "supervisor_start_time": self._supervisor_start_time,
                    "session_pid": session_pid,
                    "session_start_time": session_start,
                },
                deadline=startup_deadline,
                monotonic=self.controller.monotonic,
                deadline_error_code="service_startup_timeout",
            )
            self.controller.require_startup_budget(startup_deadline)
            admitted = True
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
            if use is not None:
                pending_release = _PendingUseRelease(
                    service=service,
                    use=use,
                    now=stop_on_release,
                    stop_if_final=not admitted,
                )
                self._queue_use_release(pending_release)
                with self._service_mutation(service.service_id):
                    try:
                        self._attempt_use_release(pending_release)
                    except Exception as error:
                        self._report_operation_failure(
                            "session-use-release:"
                            f"{service.service_id}:"
                            f"{use.lease.lease_id}",
                            error,
                        )

    def maintain_once(self) -> None:
        """Renew exact claims, stop due services, and retire empty hosts."""

        for pending in self._pending_release_snapshot():
            with self._service_mutation(pending.service.service_id):
                try:
                    self._attempt_use_release(pending)
                except Exception as error:
                    self._report_operation_failure(
                        "maintenance:use-release:"
                        f"{pending.service.service_id}:"
                        f"{pending.use.lease.lease_id}",
                        error,
                    )

        try:
            intents = self.controller.preparations.list()
        except Exception as error:
            self._report_operation_failure("maintenance:intents", error)
            intents = ()
        for intent in intents:
            with self._service_mutation(intent.service_id):
                try:
                    current_intent = self.controller.preparations.load(
                        intent.service_id
                    )
                    if current_intent is not None:
                        self.controller.reconcile_acquire_intent(
                            current_intent
                        )
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
        for observed in deployments:
            with self._service_mutation(observed.service_id):
                deployment = self.controller.deployments.load(
                    observed.service_id
                )
                if deployment is None or deployment.phase == "released":
                    continue
                try:
                    service = self.deployed_services.load(
                        deployment.service_id,
                        deployment.service_sha256,
                    )
                    if (
                        deployment.phase == "ready"
                        and self.controller.release_expired_pending_uses(
                            service
                        )
                    ):
                        deployment = self.controller.deployments.load(
                            observed.service_id
                        )
                        if (
                            deployment is None
                            or deployment.phase == "released"
                        ):
                            continue
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
                        self.controller.renew_deployment_claim(
                            deployment
                        )
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
