"""Model-owned HF/vLLM lifecycle over one opaque generic host claim."""

from __future__ import annotations

import dataclasses
import math
import time
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from model_session.attachment import ServiceEndpoint

from .cleanup import CleanupBudget
from .errors import ModelLabError
from .lifecycle import Deployment, DeploymentStore
from .runpod_backend import HostClaim
from .service_definition import ServiceDefinition

CACHE_STATES = frozenset({"accepted", "candidate", "absent"})
SERVICE_CLEANUP_TIMEOUT_SECONDS = 60.0


@dataclasses.dataclass(frozen=True)
class PreparedService:
    """Opaque exact remote installation selected by a model-owned backend."""

    service_id: str
    deployment_id: str
    host_name: str
    claim_id: str
    handle: str


@dataclasses.dataclass(frozen=True)
class TransportBinding:
    """One live metered local proxy over a private remote transport."""

    socket_path: str
    handle: str


@runtime_checkable
class ModelServiceBackend(Protocol):
    """Mechanism adapter for metadata, SSH, staging, cache, and vLLM."""

    def prepare(
        self,
        service: ServiceDefinition,
        claim: HostClaim,
        *,
        deployment_id: str,
        startup_deadline: float | None = None,
    ) -> PreparedService: ...

    def load(
        self,
        service: ServiceDefinition,
        claim: HostClaim,
        deployment: Deployment,
        *,
        startup_deadline: float | None = None,
    ) -> PreparedService: ...

    def push_huggingface_credential(
        self,
        prepared: PreparedService,
        *,
        startup_deadline: float | None = None,
    ) -> None: ...

    def clear_huggingface_credential(
        self,
        prepared: PreparedService,
        *,
        startup_deadline: float | None = None,
    ) -> None: ...

    def execute(
        self,
        prepared: PreparedService,
        action: str,
        *,
        cache_mode: str | None = None,
        startup_deadline: float | None = None,
    ) -> dict[str, Any]: ...

    def inspect_cache(
        self,
        prepared: PreparedService,
        *,
        startup_deadline: float | None = None,
    ) -> str: ...

    def open_transport(
        self,
        prepared: PreparedService,
        *,
        completed: Any,
        startup_deadline: float | None = None,
    ) -> TransportBinding: ...

    def restore_transport(
        self,
        prepared: PreparedService,
        *,
        completed: Any,
        startup_deadline: float | None = None,
    ) -> TransportBinding: ...

    def transport_is_live(
        self,
        prepared: PreparedService,
        transport: TransportBinding,
    ) -> bool: ...

    def close_transport(
        self,
        prepared: PreparedService,
        transport: TransportBinding | None,
        *,
        startup_deadline: float | None = None,
    ) -> None: ...


@runtime_checkable
class EndpointPublisher(Protocol):
    def publish(
        self,
        service: ServiceDefinition,
        transport: TransportBinding,
        *,
        ttl_seconds: int,
        startup_deadline: float | None = None,
        deadline_error_code: str = "service_startup_timeout",
    ) -> ServiceEndpoint: ...

    def revoke(
        self,
        endpoint: ServiceEndpoint,
        *,
        startup_deadline: float | None = None,
        deadline_error_code: str = "service_startup_timeout",
    ) -> None: ...

    def load(
        self,
        service: ServiceDefinition,
        *,
        startup_deadline: float | None = None,
        deadline_error_code: str = "service_startup_timeout",
    ) -> ServiceEndpoint | None: ...

    def inspect(
        self,
        service: ServiceDefinition,
        *,
        startup_deadline: float | None = None,
        deadline_error_code: str = "service_startup_timeout",
    ) -> ServiceEndpoint | None:
        """Authenticate retained publication state for cleanup only."""
        ...


def cache_mode_for_state(state: str) -> str:
    if state not in CACHE_STATES:
        raise ModelLabError(
            f"compiled cache has an unsafe or unsupported state: {state}",
            code="compiled_cache_requires_repair",
        )
    return {
        "accepted": "accepted",
        "candidate": "candidate-proof",
        "absent": "author",
    }[state]


class ProductionServiceRuntime:
    """Preserves the proven exact-revision/cache/start sequence."""

    def __init__(
        self,
        *,
        backend: ModelServiceBackend,
        publisher: EndpointPublisher,
        deployments: DeploymentStore,
        endpoint_ttl_seconds: int,
        service_idle_ttl_seconds: int,
        cleanup_timeout_seconds: float = SERVICE_CLEANUP_TIMEOUT_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            isinstance(cleanup_timeout_seconds, bool)
            or not isinstance(cleanup_timeout_seconds, (int, float))
            or not math.isfinite(float(cleanup_timeout_seconds))
            or cleanup_timeout_seconds <= 0
        ):
            raise ValueError("service cleanup timeout must be positive and finite")
        self.backend = backend
        self.publisher = publisher
        self.deployments = deployments
        self.endpoint_ttl_seconds = endpoint_ttl_seconds
        self.service_idle_ttl_seconds = service_idle_ttl_seconds
        self.cleanup_timeout_seconds = float(cleanup_timeout_seconds)
        self.monotonic = monotonic
        self.transports: dict[str, TransportBinding] = {}

    def _new_cleanup_deadline(self) -> float:
        """Bound one complete cleanup attempt independently of startup."""

        return self.monotonic() + self.cleanup_timeout_seconds

    def _require_startup_budget(self, deadline: float | None) -> None:
        if deadline is not None and self.monotonic() >= deadline:
            raise ModelLabError(
                "service exceeded its absolute endpoint startup deadline",
                code="service_startup_timeout",
            )

    def ensure_ready(
        self,
        service: ServiceDefinition,
        claim: HostClaim,
        *,
        deployment_id: str,
        startup_deadline: float | None = None,
        cleanup_budget: CleanupBudget | None = None,
    ) -> ServiceEndpoint:
        if cleanup_budget is None:
            cleanup_budget = CleanupBudget(
                timeout_seconds=self.cleanup_timeout_seconds,
                monotonic=self.monotonic,
            )
        self._require_startup_budget(startup_deadline)
        deadline_arguments = (
            {}
            if startup_deadline is None
            else {"startup_deadline": startup_deadline}
        )
        prepared = self.backend.prepare(
            service,
            claim,
            deployment_id=deployment_id,
            **deadline_arguments,
        )
        credential_attempted = False
        transport: TransportBinding | None = None
        endpoint: ServiceEndpoint | None = None
        try:
            credential_attempted = True
            self.backend.push_huggingface_credential(
                prepared,
                **deadline_arguments,
            )
            self.backend.execute(
                prepared,
                "stage-snapshot",
                **deadline_arguments,
            )
            if credential_attempted:
                self.backend.clear_huggingface_credential(
                    prepared,
                    **deadline_arguments,
                )
                credential_attempted = False
            cache_mode = cache_mode_for_state(
                self.backend.inspect_cache(
                    prepared,
                    **deadline_arguments,
                )
            )
            for action in ("prepare-cache", "setup", "start"):
                self.backend.execute(
                    prepared,
                    action,
                    cache_mode=cache_mode,
                    **deadline_arguments,
                )
            status = self.backend.execute(
                prepared,
                "status",
                **deadline_arguments,
            )
            if (
                status.get("ready") is not True
                or status.get("phase") != "ready"
            ):
                raise ModelLabError(
                    "remote vLLM service did not attest ready after start",
                    code="service_not_ready",
                )
            transport = self.backend.open_transport(
                prepared,
                completed=lambda: self.deployments.note_inference(
                    service.service_id,
                    idle_ttl_seconds=self.service_idle_ttl_seconds,
                ),
                **deadline_arguments,
            )
            self._require_startup_budget(startup_deadline)
            endpoint = self.publisher.publish(
                service,
                transport,
                ttl_seconds=self.endpoint_ttl_seconds,
                **deadline_arguments,
            )
            self._require_startup_budget(startup_deadline)
            self.transports[deployment_id] = transport
            return endpoint
        except BaseException as original:
            cleanup_deadline = cleanup_budget.deadline()
            cleanup_arguments = {"startup_deadline": cleanup_deadline}
            cleanup_errors: list[str] = []
            if credential_attempted:
                try:
                    self.backend.clear_huggingface_credential(
                        prepared,
                        **cleanup_arguments,
                    )
                except Exception as error:
                    cleanup_errors.append(f"credential={error}")
            cleanup_endpoint = endpoint
            if cleanup_endpoint is None:
                try:
                    cleanup_endpoint = self.publisher.inspect(
                        service,
                        **cleanup_arguments,
                        deadline_error_code="service_cleanup_required",
                    )
                except Exception as error:
                    cleanup_errors.append(f"endpoint-load={error}")
            if cleanup_endpoint is not None:
                try:
                    self.publisher.revoke(
                        cleanup_endpoint,
                        **cleanup_arguments,
                        deadline_error_code="service_cleanup_required",
                    )
                except Exception as error:
                    cleanup_errors.append(f"endpoint={error}")
            try:
                self.backend.close_transport(
                    prepared,
                    transport,
                    **cleanup_arguments,
                )
            except Exception as error:
                cleanup_errors.append(f"transport={error}")
            try:
                self.backend.execute(
                    prepared,
                    "stop",
                    **cleanup_arguments,
                )
            except Exception as error:
                cleanup_errors.append(f"runtime={error}")
            if cleanup_errors:
                raise ModelLabError(
                    "service bring-up failed and partial runtime cleanup "
                    f"requires reconciliation: bring-up={original}; "
                    + "; ".join(cleanup_errors),
                    code="service_cleanup_required",
                ) from original
            raise

    def attest_ready(
        self,
        service: ServiceDefinition,
        claim: HostClaim,
        deployment: Deployment,
        *,
        startup_deadline: float | None = None,
    ) -> ServiceEndpoint:
        self._require_startup_budget(startup_deadline)
        deadline_arguments = (
            {}
            if startup_deadline is None
            else {"startup_deadline": startup_deadline}
        )
        prepared = self.backend.load(
            service,
            claim,
            deployment,
            **deadline_arguments,
        )
        status = self.backend.execute(
            prepared,
            "status",
            **deadline_arguments,
        )
        if status.get("ready") is not True or status.get("phase") != "ready":
            raise ModelLabError(
                "remote vLLM service is not ready",
                code="service_not_ready",
            )
        transport = self.transports.get(deployment.deployment_id)
        transport_replaced = False
        if (
            transport is not None
            and not self.backend.transport_is_live(prepared, transport)
        ):
            stale = self.publisher.inspect(
                service,
                **deadline_arguments,
            )
            if stale is not None:
                self.publisher.revoke(
                    stale,
                    **deadline_arguments,
                )
            self.transports.pop(deployment.deployment_id, None)
            self.backend.close_transport(
                prepared,
                transport,
                startup_deadline=startup_deadline,
            )
            transport = None
            transport_replaced = True
        if transport is None:
            stale = self.publisher.inspect(
                service,
                **deadline_arguments,
            )
            if stale is not None:
                self.publisher.revoke(
                    stale,
                    **deadline_arguments,
                )
            transport = self.backend.restore_transport(
                prepared,
                completed=lambda: self.deployments.note_inference(
                    service.service_id,
                    idle_ttl_seconds=self.service_idle_ttl_seconds,
                ),
                **deadline_arguments,
            )
            self.transports[deployment.deployment_id] = transport
        self._require_startup_budget(startup_deadline)
        endpoint = self.publisher.load(
            service,
            **deadline_arguments,
        )
        if endpoint is None:
            endpoint = self.publisher.publish(
                service,
                transport,
                ttl_seconds=self.endpoint_ttl_seconds,
                **deadline_arguments,
            )
        self._require_startup_budget(startup_deadline)
        if transport_replaced and deployment.use_leases:
            raise ModelLabError(
                "inference transport changed while Pi sessions were active",
                code="service_transport_replaced",
            )
        return endpoint

    def stop(
        self,
        service: ServiceDefinition,
        claim: HostClaim,
        deployment: Deployment,
        *,
        cleanup_deadline: float | None = None,
    ) -> None:
        if cleanup_deadline is None:
            cleanup_deadline = self._new_cleanup_deadline()
        cleanup_arguments = {"startup_deadline": cleanup_deadline}
        cleanup_errors: list[str] = []
        installation_present = True
        try:
            prepared = self.backend.load(
                service,
                claim,
                deployment,
                **cleanup_arguments,
            )
        except Exception as error:
            # A missing or unreadable installation is not evidence that its
            # remote process is absent. Revoke the independently owned local
            # authority below, but retain the host claim for reconciliation.
            cleanup_errors.append(f"installation={error}")
            installation_present = False
            prepared = PreparedService(
                service_id=service.service_id,
                deployment_id=deployment.deployment_id,
                host_name=claim.host_name,
                claim_id=claim.claim_id,
                handle="installation-absent",
            )
        if installation_present:
            try:
                self.backend.clear_huggingface_credential(
                    prepared,
                    **cleanup_arguments,
                )
            except Exception as error:
                cleanup_errors.append(f"credential={error}")
        try:
            endpoint = self.publisher.inspect(
                service,
                **cleanup_arguments,
                deadline_error_code="service_cleanup_required",
            )
            if endpoint is not None:
                self.publisher.revoke(
                    endpoint,
                    **cleanup_arguments,
                    deadline_error_code="service_cleanup_required",
                )
        except Exception as error:
            cleanup_errors.append(f"endpoint={error}")
        transport = self.transports.pop(deployment.deployment_id, None)
        try:
            self.backend.close_transport(
                prepared,
                transport,
                **cleanup_arguments,
            )
        except Exception as error:
            cleanup_errors.append(f"transport={error}")
        if installation_present:
            try:
                self.backend.execute(
                    prepared,
                    "stop",
                    **cleanup_arguments,
                )
            except Exception as error:
                cleanup_errors.append(f"runtime={error}")
        if cleanup_errors:
            raise ModelLabError(
                "service shutdown did not complete every owned cleanup: "
                + "; ".join(cleanup_errors),
                code="service_cleanup_required",
            )

    def cleanup_lost_claim(
        self,
        service: ServiceDefinition,
        deployment: Deployment,
        *,
        cleanup_deadline: float | None = None,
    ) -> None:
        """Close only local authority when no remote claim remains."""

        if cleanup_deadline is None:
            cleanup_deadline = self._new_cleanup_deadline()
        prepared = PreparedService(
            service_id=service.service_id,
            deployment_id=deployment.deployment_id,
            host_name=deployment.host_name,
            claim_id=deployment.claim_id,
            handle="claim-gone",
        )
        cleanup_errors: list[str] = []
        try:
            endpoint = self.publisher.inspect(
                service,
                startup_deadline=cleanup_deadline,
                deadline_error_code="service_cleanup_required",
            )
            if endpoint is not None:
                self.publisher.revoke(
                    endpoint,
                    startup_deadline=cleanup_deadline,
                    deadline_error_code="service_cleanup_required",
                )
        except Exception as error:
            cleanup_errors.append(f"endpoint={error}")
        transport = self.transports.pop(deployment.deployment_id, None)
        try:
            self.backend.close_transport(
                prepared,
                transport,
                startup_deadline=cleanup_deadline,
            )
        except Exception as error:
            cleanup_errors.append(f"transport={error}")
        if cleanup_errors:
            raise ModelLabError(
                "lost-claim cleanup did not revoke every local authority: "
                + "; ".join(cleanup_errors),
                code="service_cleanup_required",
            )
