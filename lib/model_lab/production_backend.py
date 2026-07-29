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
from .http import JsonHttpTransport
from .lifecycle import format_timestamp, utc_now
from .proxy import MeteredUnixProxy
from .runpod_backend import (
    ClaimReleaseResult,
    HostClaim,
    HostClaimRequest,
)
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
HOST_SSH_READINESS_SECONDS = 5 * 60.0
HOST_PROVIDER_RECONCILIATION_ALLOWANCE_SECONDS = 2 * 30.0
HOST_SSH_CONNECT_ALLOWANCE_SECONDS = 15.0
HOST_FINAL_READINESS_ATTEMPT_SECONDS = (
    HOST_PROVIDER_RECONCILIATION_ALLOWANCE_SECONDS
    + HOST_SSH_CONNECT_ALLOWANCE_SECONDS
)
HOST_SSH_READINESS_POLL_SECONDS = 1.0


def _translate(error: BaseException) -> ModelLabError:
    if isinstance(error, ModelLabError):
        return error
    code = getattr(error, "code", "model_lab_backend_error")
    return ModelLabError(str(error), code=code)


class RunpodHostControlAdapter:
    """Translate generic RunPod failures without interpreting host policy."""

    def __init__(
        self,
        control: Any,
        *,
        runpod_state: StateStore,
        api: RunpodApi,
        instances: InstanceStore,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], datetime.datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        poll_waiter: Callable[[float], None] = time.sleep,
        ssh_readiness_seconds: float = HOST_SSH_READINESS_SECONDS,
        ssh_probe: Callable[[SshEndpoint], bool] | None = None,
    ) -> None:
        if ssh_readiness_seconds <= HOST_FINAL_READINESS_ATTEMPT_SECONDS:
            raise ValueError(
                "SSH readiness budget must exceed one bounded provider and "
                "SSH attempt"
            )
        self.control = control
        self.runpod_state = runpod_state
        self.api = api
        self.instances = instances
        self.popen_factory = popen_factory
        self.clock = clock
        self.monotonic = monotonic
        self.poll_waiter = poll_waiter
        self.ssh_readiness_seconds = ssh_readiness_seconds
        self.ssh_probe = ssh_probe

    def _call(self, method: str, *arguments: Any, **keywords: Any) -> Any:
        try:
            return getattr(self.control, method)(*arguments, **keywords)
        except RunpodLocalError as error:
            raise _translate(error) from error

    def acquire(
        self,
        request: HostClaimRequest,
        *,
        startup_deadline: float,
        cleanup_deadline_factory: Callable[[], float] | None = None,
    ) -> HostClaim:
        return self._call(
            "acquire",
            request,
            startup_deadline=startup_deadline,
            cleanup_deadline_factory=cleanup_deadline_factory,
        )

    def cancel(
        self,
        request: HostClaimRequest,
        *,
        cleanup_deadline: float | None = None,
    ) -> None:
        self._call(
            "cancel",
            request,
            cleanup_deadline=cleanup_deadline,
        )

    @staticmethod
    def _attest_wait_identity(
        expected: HostClaim,
        observed: HostClaim,
    ) -> None:
        if (
            observed.host_name != expected.host_name
            or observed.claim_id != expected.claim_id
            or observed.operation_id != expected.operation_id
            or observed.provider_resource_id != expected.provider_resource_id
            or observed.generation < expected.generation
        ):
            raise ModelLabError(
                "host claim identity changed while waiting for SSH",
                code="service_host_claim_mismatch",
            )

    def _probe_ssh(
        self,
        endpoint: SshEndpoint,
        *,
        startup_deadline: float,
    ) -> bool:
        try:
            ensure_known_hosts_file(endpoint.known_hosts_file)
            return (
                run_with_activity(
                    build_ssh_argv(endpoint, ["/usr/bin/true"]),
                    instances=self.instances,
                    name=endpoint.instance_name,
                    expected_operation_id=endpoint.operation_id,
                    expected_pod_id=endpoint.pod_id,
                    source="host-ssh-readiness",
                    popen_factory=self.popen_factory,
                    deadline=startup_deadline,
                    monotonic=self.monotonic,
                )
                == 0
            )
        except RunpodLocalError as error:
            if error.code == "remote_client_timeout":
                raise ModelLabError(
                    "SSH readiness probe exceeded the endpoint startup "
                    "deadline",
                    code="service_startup_timeout",
                ) from error
            raise _translate(error) from error

    def wait_ready(
        self,
        claim: HostClaim,
        *,
        renewal_ttl_seconds: int,
        startup_deadline: float | None = None,
    ) -> HostClaim:
        if (
            not isinstance(renewal_ttl_seconds, int)
            or isinstance(renewal_ttl_seconds, bool)
            or renewal_ttl_seconds <= 0
        ):
            raise ModelLabError(
                "host SSH wait requires a positive claim renewal TTL",
                code="invalid_host_claim",
            )
        started = self.monotonic()
        if startup_deadline is None:
            startup_deadline = started + self.ssh_readiness_seconds
        # One exact provider reconciliation is a 30-second REST request plus a
        # 30-second GraphQL policy attestation. OpenSSH then has a 15-second
        # ConnectTimeout. Stop starting attempts one full allowance early so
        # the final provider observation and handshake stay in the five-minute
        # controller budget.
        probe_deadline = (
            min(
                started + self.ssh_readiness_seconds,
                startup_deadline,
            )
            - HOST_FINAL_READINESS_ATTEMPT_SECONDS
        )
        renewal_interval = max(1.0, renewal_ttl_seconds / 3)
        next_renewal = started + renewal_interval
        current = claim
        last_observation = "provider SSH routing is not populated"
        while True:
            remaining = probe_deadline - self.monotonic()
            if remaining <= 0:
                raise ModelLabError(
                    "RunPod host did not become SSH-ready within the shared "
                    "service startup budget: "
                    f"{last_observation}",
                    code="service_host_ssh_not_ready",
                )
            observed = self.get(
                current.host_name,
                current.claim_id,
                startup_deadline=startup_deadline,
            )
            self._attest_wait_identity(claim, observed)
            current = observed
            now = self.monotonic()
            if now >= next_renewal:
                current = self.renew(
                    current.host_name,
                    current.claim_id,
                    current.generation,
                    renewal_ttl_seconds,
                    startup_deadline=startup_deadline,
                )
                self._attest_wait_identity(claim, current)
                next_renewal = now + renewal_interval
            try:
                endpoint = resolve_endpoint(
                    current.host_name,
                    instances=self.instances,
                    api=self.api,
                    state=self.runpod_state,
                    deadline=startup_deadline,
                    monotonic=self.monotonic,
                )
            except RunpodLocalError as error:
                if error.code != "pod_not_ready":
                    raise _translate(error) from error
                last_observation = str(error)
            else:
                if (
                    endpoint.operation_id != claim.operation_id
                    or endpoint.pod_id != claim.provider_resource_id
                ):
                    raise ModelLabError(
                        "resolved SSH endpoint differs from the exact host claim",
                        code="service_host_claim_mismatch",
                    )
                ssh_ready = (
                    self._probe_ssh(
                        endpoint,
                        startup_deadline=startup_deadline,
                    )
                    if self.ssh_probe is None
                    else self.ssh_probe(endpoint)
                )
                if ssh_ready:
                    observed = self.get(
                        current.host_name,
                        current.claim_id,
                        startup_deadline=startup_deadline,
                    )
                    self._attest_wait_identity(current, observed)
                    current = observed
                    if self.monotonic() >= next_renewal:
                        current = self.renew(
                            current.host_name,
                            current.claim_id,
                            current.generation,
                            renewal_ttl_seconds,
                            startup_deadline=startup_deadline,
                        )
                        self._attest_wait_identity(claim, current)
                    return current
                last_observation = "SSH endpoint did not accept /usr/bin/true"
            remaining = probe_deadline - self.monotonic()
            if remaining <= 0:
                raise ModelLabError(
                    "RunPod host did not become SSH-ready within the shared "
                    "service startup budget: "
                    f"{last_observation}",
                    code="service_host_ssh_not_ready",
                )
            self.poll_waiter(min(HOST_SSH_READINESS_POLL_SECONDS, remaining))

    def find(self, request: HostClaimRequest) -> HostClaim | None:
        return self._call("find", request)

    def renew(
        self,
        host_name: str,
        claim_id: str,
        expected_generation: int,
        renewal_ttl_seconds: int,
        *,
        startup_deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> HostClaim:
        return self._call(
            "renew",
            host_name,
            claim_id,
            expected_generation,
            renewal_ttl_seconds,
            startup_deadline=startup_deadline,
            cancel_event=cancel_event,
        )

    def release(
        self,
        host_name: str,
        claim_id: str,
        expected_generation: int,
        *,
        now: bool = False,
        cleanup_deadline: float | None = None,
    ) -> ClaimReleaseResult:
        released = self._call(
            "release",
            host_name,
            claim_id,
            expected_generation,
            now=now,
            cleanup_deadline=cleanup_deadline,
        )
        if (
            getattr(released, "host_name", None) != host_name
            or getattr(released, "claim_id", None) != claim_id
            or getattr(released, "released_generation", None)
            != expected_generation
            or isinstance(
                getattr(released, "remaining_claim_count", None),
                bool,
            )
            or not isinstance(
                getattr(released, "remaining_claim_count", None),
                int,
            )
            or released.remaining_claim_count < 0
            or not isinstance(getattr(released, "retention", None), str)
            or (
                getattr(released, "retire_at", None) is not None
                and not isinstance(released.retire_at, str)
            )
        ):
            raise ModelLabError(
                "generic RunPod claim release returned a mismatched result",
                code="host_claim_release_mismatch",
            )
        return ClaimReleaseResult(
            host_name=host_name,
            claim_id=claim_id,
            released=True,
            final_claim=released.remaining_claim_count == 0,
            retirement=released.retention,
            empty_deadline=released.retire_at,
        )

    def get(
        self,
        host_name: str,
        claim_id: str,
        *,
        startup_deadline: float | None = None,
    ) -> HostClaim:
        return self._call(
            "get",
            host_name,
            claim_id,
            startup_deadline=startup_deadline,
        )

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
        clock: Callable[[], datetime.datetime] | None = None,
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
        self.clock = clock or (
            lambda: datetime.datetime.now(datetime.timezone.utc)
        )
        self.poll_waiter = poll_waiter
        self._transport_lock = threading.Lock()
        self._transports: dict[str, _LiveTransport] = {}

    def _require_startup_budget(self, deadline: float | None) -> None:
        if deadline is not None and self.monotonic() >= deadline:
            raise ModelLabError(
                "service exceeded its absolute endpoint startup deadline",
                code="service_startup_timeout",
            )

    def _remote_startup_expiration(
        self,
        deadline: float | None,
    ) -> str | None:
        if deadline is None:
            return None
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            self._require_startup_budget(deadline)
            raise AssertionError("expired startup deadline was accepted")
        return format_timestamp(
            self.clock() + datetime.timedelta(seconds=remaining)
        )

    def _endpoint_for_claim(
        self,
        claim: HostClaim,
        *,
        startup_deadline: float | None = None,
    ) -> SshEndpoint:
        self._require_startup_budget(startup_deadline)
        try:
            self.instances.check_active_lease(
                claim.host_name,
                now=self.clock(),
                expected_operation_id=claim.operation_id,
                expected_pod_id=claim.provider_resource_id,
                deadline=startup_deadline,
                monotonic=self.monotonic,
            )
            endpoint = resolve_endpoint(
                claim.host_name,
                instances=self.instances,
                api=self.api,
                state=self.runpod_state,
                deadline=startup_deadline,
                monotonic=self.monotonic,
            )
            self._require_startup_budget(startup_deadline)
            record = self.instances.check_active_lease(
                claim.host_name,
                now=self.clock(),
                expected_operation_id=claim.operation_id,
                expected_pod_id=claim.provider_resource_id,
                deadline=startup_deadline,
                monotonic=self.monotonic,
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
        *,
        startup_deadline: float | None,
    ) -> None:
        try:
            record = self.instances.check_active_lease(
                endpoint.instance_name,
                now=self.clock(),
                expected_operation_id=endpoint.operation_id,
                expected_pod_id=endpoint.pod_id,
                deadline=startup_deadline,
                monotonic=self.monotonic,
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

    def _resolve_closure(
        self,
        service: ServiceDefinition,
        *,
        startup_deadline: float | None,
    ) -> Any:
        self._require_startup_budget(startup_deadline)
        token = configured_huggingface_token()
        client = HuggingFaceClient(
            cache=JsonCache(self.state_root / "cache" / "huggingface-metadata"),
            token=token,
            transport=JsonHttpTransport(
                deadline=startup_deadline,
                monotonic=self.monotonic,
            ),
        )
        try:
            closure = resolve_huggingface_closure(service, client=client)
        except BaseException as error:
            if (
                startup_deadline is not None
                and self.monotonic() >= startup_deadline
            ):
                raise ModelLabError(
                    "Hugging Face metadata resolution exceeded the absolute "
                    "service startup deadline",
                    code="service_startup_timeout",
                ) from error
            raise
        self._require_startup_budget(startup_deadline)
        write_huggingface_closure(
            default_huggingface_closure_path(self.state_root, closure),
            closure,
        )
        return closure

    def _desired_materialization(
        self,
        service: ServiceDefinition,
        claim: HostClaim,
        *,
        startup_deadline: float | None,
    ) -> tuple[MaterializedService, pathlib.Path]:
        try:
            remote_port = claim.endpoints["openai"]
        except (KeyError, TypeError) as error:
            raise ModelLabError(
                "host claim has no OpenAI endpoint allocation",
                code="service_host_claim_mismatch",
            ) from error
        runtime = load_runtime(service.runtime_id)
        closure = self._resolve_closure(
            service,
            startup_deadline=startup_deadline,
        )
        plan = build_service_materialization_plan(
            service,
            source_root=self.source_root,
            state_root=self.state_root,
            runtime=runtime,
            closure=closure,
            remote_port=remote_port,
        )
        materialized = materialize_service(plan)
        self._require_startup_budget(startup_deadline)
        return materialized, plan.installer_path

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
        startup_deadline: float | None = None,
    ) -> PreparedService:
        self._require_startup_budget(startup_deadline)
        endpoint = self._endpoint_for_claim(
            claim,
            startup_deadline=startup_deadline,
        )
        runtime = load_runtime(service.runtime_id)
        self._attest_runtime_image(
            endpoint,
            runtime.image,
            startup_deadline=startup_deadline,
        )
        materialization, installer_path = self._desired_materialization(
            service,
            claim,
            startup_deadline=startup_deadline,
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
                    deadline=startup_deadline,
                    monotonic=self.monotonic,
                )
                installed, _ = self.installations.publish(
                    materialization=materialization,
                    endpoint=endpoint,
                    instances=self.instances,
                    deadline=startup_deadline,
                    monotonic=self.monotonic,
                )
            except (ModelLabError, RunpodLocalError) as error:
                if error.code == "remote_client_timeout":
                    raise ModelLabError(
                        "service installation exceeded the endpoint startup "
                        "deadline",
                        code="service_startup_timeout",
                    ) from error
                raise _translate(error) from error
        self._require_startup_budget(startup_deadline)
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
        *,
        startup_deadline: float | None = None,
    ) -> tuple[HostClaim, SshEndpoint, InstalledService]:
        self._require_startup_budget(startup_deadline)
        claim = self.hosts.get(
            prepared.host_name,
            prepared.claim_id,
            startup_deadline=startup_deadline,
        )
        endpoint = self._endpoint_for_claim(
            claim,
            startup_deadline=startup_deadline,
        )
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
        *,
        startup_deadline: float | None = None,
    ) -> PreparedService:
        self._require_startup_budget(startup_deadline)
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
        endpoint = self._endpoint_for_claim(
            claim,
            startup_deadline=startup_deadline,
        )
        self._require_startup_budget(startup_deadline)
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
        self._attest_runtime_image(
            endpoint,
            runtime.image,
            startup_deadline=startup_deadline,
        )
        self._require_startup_budget(startup_deadline)
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
        startup_deadline: float | None = None,
    ) -> int:
        self._require_startup_budget(startup_deadline)
        _, endpoint, _ = self._context(
            prepared,
            startup_deadline=startup_deadline,
        )
        self._require_startup_budget(startup_deadline)
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
                deadline=startup_deadline,
                monotonic=self.monotonic,
            )
        except RunpodLocalError as error:
            if error.code == "remote_client_timeout":
                raise ModelLabError(
                    "remote Hugging Face action exceeded the endpoint startup "
                    "deadline",
                    code="service_startup_timeout",
                ) from error
            raise _translate(error) from error
        if return_code not in accepted_return_codes:
            raise ModelLabError(
                f"remote Hugging Face credential action exited {return_code}",
                code="remote_hf_credential_failed",
            )
        return return_code

    def push_huggingface_credential(
        self,
        prepared: PreparedService,
        *,
        startup_deadline: float | None = None,
    ) -> None:
        if configured_huggingface_token() is None:
            self.clear_huggingface_credential(
                prepared,
                startup_deadline=startup_deadline,
            )
            return
        self._run_remote_hf(
            prepared,
            build_remote_hf_probe_argv(),
            source="service-hf-probe",
            startup_deadline=startup_deadline,
        )
        with open_huggingface_token_file() as token:
            self._run_remote_hf(
                prepared,
                build_remote_hf_credential_argv("push"),
                source="service-hf-push",
                stdin=token,
                startup_deadline=startup_deadline,
            )

    def clear_huggingface_credential(
        self,
        prepared: PreparedService,
        *,
        startup_deadline: float | None = None,
    ) -> None:
        self._run_remote_hf(
            prepared,
            build_remote_hf_credential_argv("clear"),
            source="service-hf-clear",
            accepted_return_codes=(0, 3),
            startup_deadline=startup_deadline,
        )

    def execute(
        self,
        prepared: PreparedService,
        action: str,
        *,
        cache_mode: str | None = None,
        startup_deadline: float | None = None,
    ) -> dict[str, Any]:
        self._require_startup_budget(startup_deadline)
        _, endpoint, installed = self._context(
            prepared,
            startup_deadline=startup_deadline,
        )
        self._require_startup_budget(startup_deadline)
        try:
            result = execute_service_runtime_capture(
                build_service_runtime_plan(
                    installed.materialization,
                    endpoint=endpoint,
                    action=action,
                    cache_mode=cache_mode,
                    startup_expires_at=self._remote_startup_expiration(
                        startup_deadline
                    ),
                ),
                resolved_endpoint=endpoint,
                instances=self.instances,
                popen_factory=self.popen_factory,
                deadline=startup_deadline,
                monotonic=self.monotonic,
            )
        except RunpodLocalError as error:
            raise _translate(error) from error
        if result.get("service_id") != prepared.service_id:
            raise ModelLabError(
                "remote runtime result belongs to another service",
                code="invalid_service_runtime_output",
            )
        return result

    def inspect_cache(
        self,
        prepared: PreparedService,
        *,
        startup_deadline: float | None = None,
    ) -> str:
        result = self.execute(
            prepared,
            "cache-status",
            startup_deadline=startup_deadline,
        )
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
    def _socket_accepting(
        path: pathlib.Path,
        *,
        timeout_seconds: float = 0.25,
    ) -> bool:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(timeout_seconds)
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
        startup_deadline: float | None,
    ) -> TransportBinding:
        self._require_startup_budget(startup_deadline)
        claim, endpoint, installed = self._context(
            prepared,
            startup_deadline=startup_deadline,
        )
        self._require_startup_budget(startup_deadline)
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
            if startup_deadline is not None:
                deadline = min(deadline, startup_deadline)
            while True:
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    self._raise_transport_start_timeout(
                        deadline=deadline,
                        startup_deadline=startup_deadline,
                    )
                accepting = self._socket_accepting(
                    upstream_path,
                    timeout_seconds=min(0.25, remaining),
                )
                if self.monotonic() >= deadline:
                    self._raise_transport_start_timeout(
                        deadline=deadline,
                        startup_deadline=startup_deadline,
                    )
                if accepting:
                    break
                if tunnel.poll() is not None:
                    raise ModelLabError(
                        "SSH inference tunnel exited before becoming ready",
                        code="service_transport_start_failed",
                    )
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    self._raise_transport_start_timeout(
                        deadline=deadline,
                        startup_deadline=startup_deadline,
                    )
                self.poll_waiter(min(TUNNEL_POLL_SECONDS, remaining))
            self._require_startup_budget(startup_deadline)
            self.instances.touch(
                endpoint.instance_name,
                now=self.clock(),
                source="service-transport-open",
                expected_operation_id=endpoint.operation_id,
                expected_pod_id=endpoint.pod_id,
                deadline=startup_deadline,
                monotonic=self.monotonic,
            )
            self._require_startup_budget(startup_deadline)
            proxy = MeteredUnixProxy(
                listen_path=public_path,
                upstream_path=upstream_path,
                completed=completed,
            )
            proxy.bind()
            self._require_startup_budget(startup_deadline)
            proxy_thread = threading.Thread(
                target=proxy.serve,
                name=f"model-lab-proxy-{prepared.service_id}",
                daemon=True,
            )
            proxy_thread.start()
            self._require_startup_budget(startup_deadline)
            upstream_device, upstream_inode = self._bound_socket_identity(
                upstream_path
            )
            public_device, public_inode = self._bound_socket_identity(
                public_path
            )
            self._require_startup_budget(startup_deadline)
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
                    remaining = self._remaining_startup_cleanup(startup_deadline)
                    if remaining is None:
                        proxy.close()
                    else:
                        proxy.close(timeout_seconds=remaining)
                    if proxy_thread is not None:
                        remaining = self._remaining_startup_cleanup(startup_deadline)
                        proxy_thread.join(remaining)
                except Exception as error:
                    cleanup_errors.append(f"proxy={error}")
            if tunnel is not None:
                try:
                    remaining = self._remaining_startup_cleanup(startup_deadline)
                    self._reap_tunnel(
                        tunnel,
                        wait_timeout_seconds=(
                            5.0 if remaining is None else min(5.0, remaining)
                        ),
                    )
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
        startup_deadline: float | None = None,
    ) -> TransportBinding:
        return self._start_transport(
            prepared,
            completed=completed,
            startup_deadline=startup_deadline,
        )

    def restore_transport(
        self,
        prepared: PreparedService,
        *,
        completed: Any,
        startup_deadline: float | None = None,
    ) -> TransportBinding:
        return self._start_transport(
            prepared,
            completed=completed,
            startup_deadline=startup_deadline,
        )

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

    def _raise_transport_start_timeout(
        self,
        *,
        deadline: float,
        startup_deadline: float | None,
    ) -> None:
        if startup_deadline is not None and deadline == startup_deadline:
            raise ModelLabError(
                "SSH inference tunnel exceeded the endpoint startup deadline",
                code="service_startup_timeout",
            )
        raise ModelLabError(
            "SSH inference tunnel did not become ready",
            code="service_transport_start_failed",
        )

    def _remaining_startup_cleanup(
        self,
        startup_deadline: float | None,
    ) -> float | None:
        if startup_deadline is None:
            return None
        return max(0.0, startup_deadline - self.monotonic())

    @staticmethod
    def _background_reap_tunnel(tunnel: Any) -> None:
        def reap() -> None:
            try:
                tunnel.wait()
            except BaseException:
                return

        threading.Thread(target=reap, daemon=True).start()

    @classmethod
    def _reap_tunnel(
        cls,
        tunnel: Any,
        *,
        wait_timeout_seconds: float = 5.0,
    ) -> None:
        if tunnel.poll() is not None:
            try:
                tunnel.wait(timeout=0)
            except subprocess.TimeoutExpired:
                cls._background_reap_tunnel(tunnel)
            return
        tunnel.terminate()
        try:
            tunnel.wait(timeout=max(0.0, wait_timeout_seconds))
        except subprocess.TimeoutExpired:
            tunnel.kill()
            try:
                tunnel.wait(timeout=0)
            except subprocess.TimeoutExpired:
                cls._background_reap_tunnel(tunnel)

    def _remove_stale_socket(self, path: pathlib.Path) -> None:
        try:
            prepare_local_tunnel_socket(path)
        except RunpodLocalError as error:
            raise _translate(error) from error

    def close_transport(
        self,
        prepared: PreparedService,
        transport: TransportBinding | None,
        *,
        startup_deadline: float | None = None,
    ) -> None:
        with self._transport_lock:
            live = self._transports.get(prepared.deployment_id)
        cleanup_errors: list[str] = []
        if live is not None:
            if transport is not None and live.binding != transport:
                cleanup_errors.append("binding identity changed")
            try:
                remaining = self._remaining_startup_cleanup(startup_deadline)
                workers_closed = live.proxy.close(
                    timeout_seconds=remaining,
                )
                if not workers_closed:
                    cleanup_errors.append(
                        "proxy workers did not close before the cleanup deadline"
                    )
                remaining = self._remaining_startup_cleanup(startup_deadline)
                live.proxy_thread.join(remaining)
                if live.proxy_thread.is_alive():
                    cleanup_errors.append(
                        "proxy listener did not close before the cleanup deadline"
                    )
            except Exception as error:
                cleanup_errors.append(f"proxy={error}")
            try:
                remaining = self._remaining_startup_cleanup(startup_deadline)
                self._reap_tunnel(
                    live.tunnel,
                    wait_timeout_seconds=(
                        5.0 if remaining is None else min(5.0, remaining)
                    ),
                )
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

    def __init__(
        self,
        runtime_root: pathlib.Path,
        *,
        clock: Callable[[], datetime.datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.runtime_root = runtime_root
        self.clock = clock
        self.monotonic = monotonic

    @staticmethod
    def _translate_model_session(error: ModelSessionError) -> ModelLabError:
        return ModelLabError(str(error), code=error.code)

    def publish(
        self,
        service: ServiceDefinition,
        transport: TransportBinding,
        *,
        ttl_seconds: int,
        startup_deadline: float | None = None,
        deadline_error_code: str = "service_startup_timeout",
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
                deadline=startup_deadline,
                monotonic=self.monotonic,
                deadline_error_code=deadline_error_code,
            )
        except ModelSessionError as error:
            raise self._translate_model_session(error) from error

    def inspect(
        self,
        service: ServiceDefinition,
        *,
        startup_deadline: float | None = None,
        deadline_error_code: str = "service_startup_timeout",
    ) -> Any | None:
        try:
            return inspect_service_publication(
                service.service_id,
                runtime_root=self.runtime_root,
                deadline=startup_deadline,
                monotonic=self.monotonic,
                deadline_error_code=deadline_error_code,
            )
        except ModelSessionError as error:
            raise self._translate_model_session(error) from error

    def load(
        self,
        service: ServiceDefinition,
        *,
        startup_deadline: float | None = None,
        deadline_error_code: str = "service_startup_timeout",
    ) -> Any | None:
        endpoint = self.inspect(
            service,
            startup_deadline=startup_deadline,
            deadline_error_code=deadline_error_code,
        )
        if endpoint is None:
            return None
        if endpoint.admission_expires_at <= self.clock():
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

    def revoke(
        self,
        endpoint: Any,
        *,
        startup_deadline: float | None = None,
        deadline_error_code: str = "service_startup_timeout",
    ) -> None:
        try:
            revoke_service_endpoint(
                endpoint.binding.service_id,
                endpoint.publication_id,
                runtime_root=self.runtime_root,
                deadline=startup_deadline,
                monotonic=self.monotonic,
                deadline_error_code=deadline_error_code,
            )
        except ModelSessionError as error:
            if error.code == "service_endpoint_missing":
                return
            raise self._translate_model_session(error) from error
