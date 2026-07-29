"""Concrete model-owned service backend over generic RunPod host receipts."""

from __future__ import annotations

import dataclasses
import datetime
import os
import pathlib
import socket
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

from model_session.errors import ModelSessionError
from model_session.service_endpoint import (
    inspect_service_publication,
    publish_service_endpoint,
    revoke_service_endpoint,
)
from runpod_local.api import RunpodApi
from runpod_local.errors import RunpodLocalError
from runpod_local.instances import InstanceStore
from runpod_local.remote import (
    SshEndpoint,
    build_ssh_argv,
    build_tunnel_argv,
    ensure_known_hosts_file,
    prepare_local_tunnel_socket,
    resolve_endpoint,
    run_with_activity,
    sanitized_subprocess_environment,
)
from runpod_local.state import StateStore

from .cache import JsonCache
from .errors import ModelLabError
from .huggingface_credentials import (
    build_remote_hf_credential_argv,
    build_remote_hf_probe_argv,
    configured_huggingface_token,
    open_huggingface_token_file,
)
from .huggingface_model import HuggingFaceClient
from .proxy import MeteredUnixProxy
from .runpod_backend import HostClaim, HostClaimRequest
from .runtime_catalog import load_runtime
from .service_definition import ServiceDefinition
from .service_deployment import (
    build_service_push_plan,
    push_service_materialization,
)
from .service_execution import (
    build_service_runtime_plan,
    execute_service_runtime_capture,
)
from .service_huggingface import (
    default_huggingface_closure_path,
    resolve_huggingface_closure,
    write_huggingface_closure,
)
from .service_installation import (
    InstalledService,
    ServiceInstallationStore,
    require_current_instance,
)
from .service_materialization import (
    MaterializedService,
    build_service_materialization_plan,
    materialize_service,
)
from .service_runtime import PreparedService, TransportBinding

TUNNEL_START_SECONDS = 20.0
TUNNEL_POLL_SECONDS = 0.05


def _translate(error: BaseException) -> ModelLabError:
    if isinstance(error, ModelLabError):
        return error
    code = getattr(error, "code", "model_lab_backend_error")
    return ModelLabError(str(error), code=code)


class RunpodHostControlAdapter:
    """Translate generic RunPod failures without interpreting host policy."""

    def __init__(self, control: Any) -> None:
        self.control = control

    def _call(self, method: str, *arguments: Any, **keywords: Any) -> Any:
        try:
            return getattr(self.control, method)(*arguments, **keywords)
        except RunpodLocalError as error:
            raise _translate(error) from error

    def acquire(self, request: HostClaimRequest) -> HostClaim:
        return self._call("acquire", request)

    def find(self, request: HostClaimRequest) -> HostClaim | None:
        return self._call("find", request)

    def renew(
        self,
        host_name: str,
        claim_id: str,
        expected_generation: int,
        renewal_ttl_seconds: int,
    ) -> HostClaim:
        return self._call(
            "renew",
            host_name,
            claim_id,
            expected_generation,
            renewal_ttl_seconds,
        )

    def release(
        self,
        host_name: str,
        claim_id: str,
        expected_generation: int,
        *,
        now: bool = False,
    ) -> Any:
        return self._call(
            "release",
            host_name,
            claim_id,
            expected_generation,
            now=now,
        )

    def get(self, host_name: str, claim_id: str) -> HostClaim:
        return self._call("get", host_name, claim_id)

    def list(self, host_name: str | None = None) -> Any:
        return self._call("list", host_name)

    def status(self, host_name: str) -> Any:
        return self._call("status", host_name)

    def enforce_retirement(self, *, execute: bool) -> Any:
        return self._call("enforce_retirement", execute=execute)


@dataclasses.dataclass
class _LiveTransport:
    binding: TransportBinding
    prepared: PreparedService
    endpoint: SshEndpoint
    upstream_path: pathlib.Path
    upstream_socket_device: int
    upstream_socket_inode: int
    public_path: pathlib.Path
    public_socket_device: int
    public_socket_inode: int
    tunnel: Any
    proxy: MeteredUnixProxy
    proxy_thread: threading.Thread


class ProductionModelServiceBackend:
    """HF resolution, exact installation, runtime control, and local transport."""

    def __init__(
        self,
        *,
        source_root: pathlib.Path,
        state_root: pathlib.Path,
        runtime_root: pathlib.Path,
        runpod_state: StateStore,
        api: RunpodApi,
        hosts: RunpodHostControlAdapter,
        instances: InstanceStore,
        installations: ServiceInstallationStore,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        monotonic: Callable[[], float] = time.monotonic,
        poll_waiter: Callable[[float], None] = time.sleep,
    ) -> None:
        self.source_root = source_root
        self.state_root = state_root
        self.runtime_root = runtime_root
        self.runpod_state = runpod_state
        self.api = api
        self.hosts = hosts
        self.instances = instances
        self.installations = installations
        self.popen_factory = popen_factory
        self.monotonic = monotonic
        self.poll_waiter = poll_waiter
        self._transport_lock = threading.Lock()
        self._transports: dict[str, _LiveTransport] = {}

    def _endpoint_for_claim(self, claim: HostClaim) -> SshEndpoint:
        try:
            endpoint = resolve_endpoint(
                claim.host_name,
                instances=self.instances,
                api=self.api,
                state=self.runpod_state,
            )
            record = self.instances.check_active_lease(
                claim.host_name,
                now=datetime.datetime.now(datetime.timezone.utc),
                expected_operation_id=claim.operation_id,
                expected_pod_id=claim.provider_resource_id,
            )
        except RunpodLocalError as error:
            raise _translate(error) from error
        if (
            endpoint.operation_id != claim.operation_id
            or endpoint.pod_id != claim.provider_resource_id
        ):
            raise ModelLabError(
                "resolved SSH endpoint differs from the exact host claim",
                code="service_host_claim_mismatch",
            )
        expected = record.get("expected")
        if not isinstance(expected, dict):
            raise ModelLabError(
                "active host receipt has no exact image identity",
                code="service_runtime_host_image_mismatch",
            )
        return endpoint

    def _attest_runtime_image(
        self,
        endpoint: SshEndpoint,
        runtime_image: str,
    ) -> None:
        try:
            record = self.instances.check_active_lease(
                endpoint.instance_name,
                now=datetime.datetime.now(datetime.timezone.utc),
                expected_operation_id=endpoint.operation_id,
                expected_pod_id=endpoint.pod_id,
            )
        except RunpodLocalError as error:
            raise _translate(error) from error
        expected = record.get("expected")
        image = expected.get("image") if isinstance(expected, dict) else None
        if image != runtime_image:
            raise ModelLabError(
                "generic host image does not match the service's exact "
                f"runtime image: host={image!r}; service={runtime_image!r}",
                code="service_runtime_host_image_mismatch",
            )

    def _resolve_closure(self, service: ServiceDefinition) -> Any:
        token = configured_huggingface_token()
        client = HuggingFaceClient(
            cache=JsonCache(self.state_root / "cache" / "huggingface-metadata"),
            token=token,
        )
        closure = resolve_huggingface_closure(service, client=client)
        write_huggingface_closure(
            default_huggingface_closure_path(self.state_root, closure),
            closure,
        )
        return closure

    def _desired_materialization(
        self,
        service: ServiceDefinition,
        claim: HostClaim,
    ) -> tuple[MaterializedService, pathlib.Path]:
        try:
            remote_port = claim.endpoints["openai"]
        except (KeyError, TypeError) as error:
            raise ModelLabError(
                "host claim has no OpenAI endpoint allocation",
                code="service_host_claim_mismatch",
            ) from error
        runtime = load_runtime(service.runtime_id)
        closure = self._resolve_closure(service)
        plan = build_service_materialization_plan(
            service,
            source_root=self.source_root,
            state_root=self.state_root,
            runtime=runtime,
            closure=closure,
            remote_port=remote_port,
        )
        return materialize_service(plan), plan.installer_path

    @staticmethod
    def _installation_matches(
        installed: InstalledService,
        *,
        service: ServiceDefinition,
        materialization: MaterializedService,
        endpoint: SshEndpoint,
        remote_port: int,
    ) -> bool:
        try:
            require_current_instance(installed, endpoint=endpoint)
        except ModelLabError:
            return False
        return (
            installed.request.service_id == service.service_id
            and installed.request.service_plan_sha256 == service.plan_sha256
            and installed.request.remote_port == remote_port
            and installed.materialization.materialization_sha256
            == materialization.materialization_sha256
        )

    def prepare(
        self,
        service: ServiceDefinition,
        claim: HostClaim,
        *,
        deployment_id: str,
    ) -> PreparedService:
        endpoint = self._endpoint_for_claim(claim)
        runtime = load_runtime(service.runtime_id)
        self._attest_runtime_image(endpoint, runtime.image)
        materialization, installer_path = self._desired_materialization(
            service,
            claim,
        )
        installed: InstalledService | None
        try:
            installed = self.installations.load(
                instance_name=endpoint.instance_name,
                service_id=service.service_id,
                required=False,
            )
        except (ModelLabError, RunpodLocalError):
            installed = None
        remote_port = claim.endpoints["openai"]
        if installed is None or not self._installation_matches(
            installed,
            service=service,
            materialization=materialization,
            endpoint=endpoint,
            remote_port=remote_port,
        ):
            try:
                push_service_materialization(
                    build_service_push_plan(
                        materialization,
                        endpoint=endpoint,
                        installer_path=installer_path,
                    ),
                    resolved_endpoint=endpoint,
                    instances=self.instances,
                    popen_factory=self.popen_factory,
                )
                installed, _ = self.installations.publish(
                    materialization=materialization,
                    endpoint=endpoint,
                    instances=self.instances,
                )
            except RunpodLocalError as error:
                raise _translate(error) from error
        if installed is None:
            raise AssertionError("successful service installation is absent")
        return PreparedService(
            service_id=service.service_id,
            deployment_id=deployment_id,
            host_name=claim.host_name,
            claim_id=claim.claim_id,
            handle=materialization.materialization_sha256,
        )

    def _context(
        self,
        prepared: PreparedService,
    ) -> tuple[HostClaim, SshEndpoint, InstalledService]:
        claim = self.hosts.get(prepared.host_name, prepared.claim_id)
        endpoint = self._endpoint_for_claim(claim)
        try:
            installed = self.installations.load(
                instance_name=endpoint.instance_name,
                service_id=prepared.service_id,
            )
        except RunpodLocalError as error:
            raise _translate(error) from error
        if installed is None:
            raise AssertionError("required installation unexpectedly absent")
        require_current_instance(installed, endpoint=endpoint)
        if (
            installed.materialization.materialization_sha256
            != prepared.handle
        ):
            raise ModelLabError(
                "deployment handle differs from its installation receipt",
                code="service_installation_changed",
            )
        return claim, endpoint, installed

    def load(
        self,
        service: ServiceDefinition,
        claim: HostClaim,
        deployment: Any,
    ) -> PreparedService:
        if (
            claim.host_name != deployment.host_name
            or claim.claim_id != deployment.claim_id
        ):
            raise ModelLabError(
                "deployment and host claim identities differ",
                code="service_host_claim_mismatch",
            )
        prepared = PreparedService(
            service_id=service.service_id,
            deployment_id=deployment.deployment_id,
            host_name=claim.host_name,
            claim_id=claim.claim_id,
            handle="",
        )
        endpoint = self._endpoint_for_claim(claim)
        try:
            installed = self.installations.load(
                instance_name=endpoint.instance_name,
                service_id=service.service_id,
            )
        except RunpodLocalError as error:
            raise _translate(error) from error
        if installed is None:
            raise AssertionError("required installation unexpectedly absent")
        require_current_instance(installed, endpoint=endpoint)
        if (
            installed.request.service_plan_sha256 != service.plan_sha256
            or installed.request.remote_port != claim.endpoints.get("openai")
        ):
            raise ModelLabError(
                "installed service differs from the exact retained deployment",
                code="service_installation_changed",
            )
        runtime = load_runtime(service.runtime_id)
        self._attest_runtime_image(endpoint, runtime.image)
        return dataclasses.replace(
            prepared,
            handle=installed.materialization.materialization_sha256,
        )

    def _run_remote_hf(
        self,
        prepared: PreparedService,
        remote_arguments: list[str],
        *,
        source: str,
        stdin: Any = None,
        accepted_return_codes: tuple[int, ...] = (0,),
    ) -> int:
        _, endpoint, _ = self._context(prepared)
        try:
            ensure_known_hosts_file(endpoint.known_hosts_file)
            return_code = run_with_activity(
                build_ssh_argv(endpoint, remote_arguments),
                instances=self.instances,
                name=endpoint.instance_name,
                expected_operation_id=endpoint.operation_id,
                expected_pod_id=endpoint.pod_id,
                source=source,
                stdin=stdin,
                popen_factory=self.popen_factory,
            )
        except RunpodLocalError as error:
            raise _translate(error) from error
        if return_code not in accepted_return_codes:
            raise ModelLabError(
                f"remote Hugging Face credential action exited {return_code}",
                code="remote_hf_credential_failed",
            )
        return return_code

    def push_huggingface_credential(self, prepared: PreparedService) -> None:
        if configured_huggingface_token() is None:
            self.clear_huggingface_credential(prepared)
            return
        self._run_remote_hf(
            prepared,
            build_remote_hf_probe_argv(),
            source="service-hf-probe",
        )
        with open_huggingface_token_file() as token:
            self._run_remote_hf(
                prepared,
                build_remote_hf_credential_argv("push"),
                source="service-hf-push",
                stdin=token,
            )

    def clear_huggingface_credential(self, prepared: PreparedService) -> None:
        self._run_remote_hf(
            prepared,
            build_remote_hf_credential_argv("clear"),
            source="service-hf-clear",
            accepted_return_codes=(0, 3),
        )

    def execute(
        self,
        prepared: PreparedService,
        action: str,
        *,
        cache_mode: str | None = None,
    ) -> dict[str, Any]:
        _, endpoint, installed = self._context(prepared)
        try:
            result = execute_service_runtime_capture(
                build_service_runtime_plan(
                    installed.materialization,
                    endpoint=endpoint,
                    action=action,
                    cache_mode=cache_mode,
                ),
                resolved_endpoint=endpoint,
                instances=self.instances,
                popen_factory=self.popen_factory,
            )
        except RunpodLocalError as error:
            raise _translate(error) from error
        if result.get("service_id") != prepared.service_id:
            raise ModelLabError(
                "remote runtime result belongs to another service",
                code="invalid_service_runtime_output",
            )
        return result

    def inspect_cache(self, prepared: PreparedService) -> str:
        result = self.execute(prepared, "cache-status")
        state = result.get("state")
        if (
            result.get("schema_version")
            != "model-lab.service-cache-status.v1"
            or state not in {"absent", "candidate", "accepted"}
            or not isinstance(result.get("cache_id"), str)
        ):
            raise ModelLabError(
                "remote compile-cache status is malformed",
                code="invalid_service_runtime_output",
            )
        return state

    def _transport_paths(
        self,
        prepared: PreparedService,
    ) -> tuple[pathlib.Path, pathlib.Path]:
        return (
            self.runtime_root
            / "transports"
            / f"{prepared.deployment_id}.sock",
            self.runtime_root / "services" / f"{prepared.service_id}.sock",
        )

    @staticmethod
    def _socket_accepting(path: pathlib.Path) -> bool:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.25)
        try:
            probe.connect(os.fspath(path))
            return True
        except OSError:
            return False
        finally:
            probe.close()

    @staticmethod
    def _bound_socket_identity(path: pathlib.Path) -> tuple[int, int]:
        try:
            metadata = path.lstat()
        except OSError as error:
            raise ModelLabError(
                f"inference transport socket is unavailable: {path}",
                code="service_transport_start_failed",
            ) from error
        if not stat.S_ISSOCK(metadata.st_mode):
            raise ModelLabError(
                f"inference transport path is not a socket: {path}",
                code="service_transport_start_failed",
            )
        return metadata.st_dev, metadata.st_ino

    def _start_transport(
        self,
        prepared: PreparedService,
        *,
        completed: Any,
    ) -> TransportBinding:
        claim, endpoint, installed = self._context(prepared)
        upstream_path, public_path = self._transport_paths(prepared)
        tunnel: Any | None = None
        proxy: MeteredUnixProxy | None = None
        proxy_thread: threading.Thread | None = None
        try:
            prepare_local_tunnel_socket(upstream_path)
            prepare_local_tunnel_socket(public_path)
            ensure_known_hosts_file(endpoint.known_hosts_file)
            tunnel = self.popen_factory(
                build_tunnel_argv(
                    endpoint,
                    local_socket=upstream_path,
                    remote_port=claim.endpoints["openai"],
                ),
                env=sanitized_subprocess_environment(),
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = self.monotonic() + TUNNEL_START_SECONDS
            while not self._socket_accepting(upstream_path):
                if tunnel.poll() is not None:
                    raise ModelLabError(
                        "SSH inference tunnel exited before becoming ready",
                        code="service_transport_start_failed",
                    )
                if self.monotonic() >= deadline:
                    raise ModelLabError(
                        "SSH inference tunnel did not become ready",
                        code="service_transport_start_failed",
                    )
                self.poll_waiter(TUNNEL_POLL_SECONDS)
            self.instances.touch(
                endpoint.instance_name,
                now=datetime.datetime.now(datetime.timezone.utc),
                source="service-transport-open",
                expected_operation_id=endpoint.operation_id,
                expected_pod_id=endpoint.pod_id,
            )
            proxy = MeteredUnixProxy(
                listen_path=public_path,
                upstream_path=upstream_path,
                completed=completed,
            )
            proxy.bind()
            proxy_thread = threading.Thread(
                target=proxy.serve,
                name=f"model-lab-proxy-{prepared.service_id}",
                daemon=True,
            )
            proxy_thread.start()
            upstream_device, upstream_inode = self._bound_socket_identity(
                upstream_path
            )
            public_device, public_inode = self._bound_socket_identity(
                public_path
            )
            binding = TransportBinding(
                socket_path=str(public_path),
                handle=(
                    f"{prepared.deployment_id}:"
                    f"{installed.installation_sha256}"
                ),
            )
            live = _LiveTransport(
                binding=binding,
                prepared=prepared,
                endpoint=endpoint,
                upstream_path=upstream_path,
                upstream_socket_device=upstream_device,
                upstream_socket_inode=upstream_inode,
                public_path=public_path,
                public_socket_device=public_device,
                public_socket_inode=public_inode,
                tunnel=tunnel,
                proxy=proxy,
                proxy_thread=proxy_thread,
            )
            with self._transport_lock:
                if prepared.deployment_id in self._transports:
                    raise ModelLabError(
                        "deployment already owns an inference transport",
                        code="service_transport_already_open",
                    )
                self._transports[prepared.deployment_id] = live
            return binding
        except BaseException as original:
            cleanup_errors: list[str] = []
            if proxy is not None:
                try:
                    proxy.close()
                    if proxy_thread is not None:
                        proxy_thread.join()
                except Exception as error:
                    cleanup_errors.append(f"proxy={error}")
            if tunnel is not None:
                try:
                    self._reap_tunnel(tunnel)
                except Exception as error:
                    cleanup_errors.append(f"tunnel={error}")
            for label, path in (
                ("public-socket", public_path),
                ("upstream-socket", upstream_path),
            ):
                try:
                    self._remove_stale_socket(path)
                except Exception as error:
                    cleanup_errors.append(f"{label}={error}")
            if cleanup_errors:
                raise ModelLabError(
                    "inference transport start failed and rollback is "
                    f"incomplete: start={original}; "
                    + "; ".join(cleanup_errors),
                    code="service_transport_cleanup_failed",
                ) from original
            if isinstance(original, (RunpodLocalError, OSError)):
                raise _translate(original) from original
            raise

    def open_transport(
        self,
        prepared: PreparedService,
        *,
        completed: Any,
    ) -> TransportBinding:
        return self._start_transport(prepared, completed=completed)

    def restore_transport(
        self,
        prepared: PreparedService,
        *,
        completed: Any,
    ) -> TransportBinding:
        return self._start_transport(prepared, completed=completed)

    def transport_is_live(
        self,
        prepared: PreparedService,
        transport: TransportBinding,
    ) -> bool:
        with self._transport_lock:
            live = self._transports.get(prepared.deployment_id)
        if live is None:
            return False
        if live.prepared != prepared or live.binding != transport:
            raise ModelLabError(
                "inference transport identity changed within a deployment",
                code="service_transport_changed",
            )
        if live.tunnel.poll() is not None or not live.proxy_thread.is_alive():
            return False
        for path, expected_device, expected_inode in (
            (
                live.upstream_path,
                live.upstream_socket_device,
                live.upstream_socket_inode,
            ),
            (
                live.public_path,
                live.public_socket_device,
                live.public_socket_inode,
            ),
        ):
            try:
                metadata = path.lstat()
            except OSError:
                return False
            if (
                not stat.S_ISSOCK(metadata.st_mode)
                or metadata.st_dev != expected_device
                or metadata.st_ino != expected_inode
            ):
                return False
        return True

    @staticmethod
    def _reap_tunnel(tunnel: Any) -> None:
        if tunnel.poll() is not None:
            tunnel.wait()
            return
        tunnel.terminate()
        try:
            tunnel.wait(timeout=5)
        except subprocess.TimeoutExpired:
            tunnel.kill()
            tunnel.wait()

    def _remove_stale_socket(self, path: pathlib.Path) -> None:
        try:
            prepare_local_tunnel_socket(path)
        except RunpodLocalError as error:
            raise _translate(error) from error

    def close_transport(
        self,
        prepared: PreparedService,
        transport: TransportBinding | None,
    ) -> None:
        with self._transport_lock:
            live = self._transports.get(prepared.deployment_id)
        cleanup_errors: list[str] = []
        if live is not None:
            if transport is not None and live.binding != transport:
                cleanup_errors.append("binding identity changed")
            try:
                live.proxy.close()
                live.proxy_thread.join()
            except Exception as error:
                cleanup_errors.append(f"proxy={error}")
            try:
                self._reap_tunnel(live.tunnel)
            except Exception as error:
                cleanup_errors.append(f"tunnel={error}")
        upstream_path, public_path = self._transport_paths(prepared)
        for label, path in (
            ("public-socket", public_path),
            ("upstream-socket", upstream_path),
        ):
            try:
                self._remove_stale_socket(path)
            except Exception as error:
                cleanup_errors.append(f"{label}={error}")
        if cleanup_errors:
            raise ModelLabError(
                "inference transport cleanup is incomplete: "
                + "; ".join(cleanup_errors),
                code="service_transport_cleanup_failed",
            )
        if live is not None:
            with self._transport_lock:
                if self._transports.get(prepared.deployment_id) is not live:
                    raise ModelLabError(
                        "inference transport identity changed during cleanup",
                        code="service_transport_cleanup_failed",
                    )
                self._transports.pop(prepared.deployment_id)


class ServiceEndpointPublisher:
    """Publish live admission offers and separately inspect cleanup state."""

    def __init__(self, runtime_root: pathlib.Path) -> None:
        self.runtime_root = runtime_root

    @staticmethod
    def _translate_model_session(error: ModelSessionError) -> ModelLabError:
        return ModelLabError(str(error), code=error.code)

    def publish(
        self,
        service: ServiceDefinition,
        transport: TransportBinding,
        *,
        ttl_seconds: int,
    ) -> Any:
        try:
            return publish_service_endpoint(
                service.service_id,
                service_sha256=service.service_sha256,
                workload=service.service_workload(),
                input_modalities=service.endpoint.input_modalities,
                ttl_seconds=ttl_seconds,
                socket_path=transport.socket_path,
                runtime_root=self.runtime_root,
            )
        except ModelSessionError as error:
            raise self._translate_model_session(error) from error

    def inspect(self, service: ServiceDefinition) -> Any | None:
        try:
            return inspect_service_publication(
                service.service_id,
                runtime_root=self.runtime_root,
            )
        except ModelSessionError as error:
            raise self._translate_model_session(error) from error

    def load(self, service: ServiceDefinition) -> Any | None:
        endpoint = self.inspect(service)
        if endpoint is None:
            return None
        if endpoint.admission_expires_at <= datetime.datetime.now(
            datetime.timezone.utc
        ):
            return None
        try:
            metadata = endpoint.socket_path.stat()
        except OSError:
            return None
        if (
            not stat.S_ISSOCK(metadata.st_mode)
            or metadata.st_dev != endpoint.socket_device
            or metadata.st_ino != endpoint.socket_inode
        ):
            return None
        return endpoint

    def revoke(self, endpoint: Any) -> None:
        try:
            revoke_service_endpoint(
                endpoint.binding.service_id,
                endpoint.publication_id,
                runtime_root=self.runtime_root,
            )
        except ModelSessionError as error:
            if error.code == "service_endpoint_missing":
                return
            raise self._translate_model_session(error) from error
