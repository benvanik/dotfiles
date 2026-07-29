"""Model-owned HF/vLLM lifecycle over one opaque generic host claim."""

from __future__ import annotations

import dataclasses
from typing import Any, Protocol, runtime_checkable

from model_session.attachment import ServiceEndpoint

from .errors import ModelLabError
from .lifecycle import Deployment, DeploymentStore
from .runpod_backend import HostClaim
from .service_definition import ServiceDefinition

CACHE_STATES = frozenset({"accepted", "candidate", "absent"})


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
    ) -> PreparedService: ...

    def load(
        self,
        service: ServiceDefinition,
        claim: HostClaim,
        deployment: Deployment,
    ) -> PreparedService: ...

    def push_huggingface_credential(self, prepared: PreparedService) -> None: ...

    def clear_huggingface_credential(self, prepared: PreparedService) -> None: ...

    def execute(
        self,
        prepared: PreparedService,
        action: str,
        *,
        cache_mode: str | None = None,
    ) -> dict[str, Any]: ...

    def inspect_cache(self, prepared: PreparedService) -> str: ...

    def open_transport(
        self,
        prepared: PreparedService,
        *,
        completed: Any,
    ) -> TransportBinding: ...

    def restore_transport(
        self,
        prepared: PreparedService,
        *,
        completed: Any,
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
    ) -> None: ...


@runtime_checkable
class EndpointPublisher(Protocol):
    def publish(
        self,
        service: ServiceDefinition,
        transport: TransportBinding,
        *,
        ttl_seconds: int,
    ) -> ServiceEndpoint: ...

    def revoke(self, endpoint: ServiceEndpoint) -> None: ...

    def load(self, service: ServiceDefinition) -> ServiceEndpoint | None: ...

    def inspect(self, service: ServiceDefinition) -> ServiceEndpoint | None:
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
    ) -> None:
        self.backend = backend
        self.publisher = publisher
        self.deployments = deployments
        self.endpoint_ttl_seconds = endpoint_ttl_seconds
        self.service_idle_ttl_seconds = service_idle_ttl_seconds
        self.transports: dict[str, TransportBinding] = {}

    def ensure_ready(
        self,
        service: ServiceDefinition,
        claim: HostClaim,
        *,
        deployment_id: str,
    ) -> ServiceEndpoint:
        prepared = self.backend.prepare(
            service,
            claim,
            deployment_id=deployment_id,
        )
        credential_attempted = False
        transport: TransportBinding | None = None
        endpoint: ServiceEndpoint | None = None
        try:
            credential_attempted = True
            self.backend.push_huggingface_credential(prepared)
            self.backend.execute(prepared, "stage-snapshot")
            if credential_attempted:
                self.backend.clear_huggingface_credential(prepared)
                credential_attempted = False
            cache_mode = cache_mode_for_state(
                self.backend.inspect_cache(prepared)
            )
            for action in ("prepare-cache", "setup", "start"):
                self.backend.execute(
                    prepared,
                    action,
                    cache_mode=cache_mode,
                )
            status = self.backend.execute(prepared, "status")
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
            )
            endpoint = self.publisher.publish(
                service,
                transport,
                ttl_seconds=self.endpoint_ttl_seconds,
            )
            self.transports[deployment_id] = transport
            return endpoint
        except BaseException as original:
            cleanup_errors: list[str] = []
            if credential_attempted:
                try:
                    self.backend.clear_huggingface_credential(prepared)
                except Exception as error:
                    cleanup_errors.append(f"credential={error}")
            cleanup_endpoint = endpoint
            if cleanup_endpoint is None:
                try:
                    cleanup_endpoint = self.publisher.inspect(service)
                except Exception as error:
                    cleanup_errors.append(f"endpoint-load={error}")
            if cleanup_endpoint is not None:
                try:
                    self.publisher.revoke(cleanup_endpoint)
                except Exception as error:
                    cleanup_errors.append(f"endpoint={error}")
            try:
                self.backend.close_transport(prepared, transport)
            except Exception as error:
                cleanup_errors.append(f"transport={error}")
            try:
                self.backend.execute(prepared, "stop")
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
    ) -> ServiceEndpoint:
        prepared = self.backend.load(service, claim, deployment)
        status = self.backend.execute(prepared, "status")
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
            stale = self.publisher.inspect(service)
            if stale is not None:
                self.publisher.revoke(stale)
            self.transports.pop(deployment.deployment_id, None)
            self.backend.close_transport(prepared, transport)
            transport = None
            transport_replaced = True
        if transport is None:
            stale = self.publisher.inspect(service)
            if stale is not None:
                self.publisher.revoke(stale)
            transport = self.backend.restore_transport(
                prepared,
                completed=lambda: self.deployments.note_inference(
                    service.service_id,
                    idle_ttl_seconds=self.service_idle_ttl_seconds,
                ),
            )
            self.transports[deployment.deployment_id] = transport
        endpoint = self.publisher.load(service)
        if endpoint is None:
            endpoint = self.publisher.publish(
                service,
                transport,
                ttl_seconds=self.endpoint_ttl_seconds,
            )
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
    ) -> None:
        prepared = self.backend.load(service, claim, deployment)
        cleanup_errors: list[str] = []
        try:
            self.backend.clear_huggingface_credential(prepared)
        except Exception as error:
            cleanup_errors.append(f"credential={error}")
        try:
            endpoint = self.publisher.inspect(service)
            if endpoint is not None:
                self.publisher.revoke(endpoint)
        except Exception as error:
            cleanup_errors.append(f"endpoint={error}")
        transport = self.transports.pop(deployment.deployment_id, None)
        try:
            self.backend.close_transport(prepared, transport)
        except Exception as error:
            cleanup_errors.append(f"transport={error}")
        try:
            self.backend.execute(prepared, "stop")
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
    ) -> None:
        """Close only local authority when no remote claim remains."""

        prepared = PreparedService(
            service_id=service.service_id,
            deployment_id=deployment.deployment_id,
            host_name=deployment.host_name,
            claim_id=deployment.claim_id,
            handle="claim-gone",
        )
        cleanup_errors: list[str] = []
        try:
            endpoint = self.publisher.inspect(service)
            if endpoint is not None:
                self.publisher.revoke(endpoint)
        except Exception as error:
            cleanup_errors.append(f"endpoint={error}")
        transport = self.transports.pop(deployment.deployment_id, None)
        try:
            self.backend.close_transport(prepared, transport)
        except Exception as error:
            cleanup_errors.append(f"transport={error}")
        if cleanup_errors:
            raise ModelLabError(
                "lost-claim cleanup did not revoke every local authority: "
                + "; ".join(cleanup_errors),
                code="service_cleanup_required",
            )
