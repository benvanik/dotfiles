"""Service orchestration above the generic RunPod host-claim facade."""

from __future__ import annotations

import dataclasses
import datetime
import math
import os
import secrets
import stat
import threading
import time
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from model_session.attachment import ServiceEndpoint

from .cleanup import CleanupBudget
from .configuration import LabConfiguration
from .errors import ModelLabError
from .lifecycle import (
    Deployment,
    DeploymentStore,
    UseLease,
    format_timestamp,
    parse_timestamp,
    utc_now,
)
from .profile_binding import ProfileBindingStore
from .preparation_intent import (
    PreparationIntent,
    PreparationIntentStore,
)
from .runpod_backend import HostClaim, HostClaimRequest, HostControl
from .service_definition import ServiceDefinition


@runtime_checkable
class ServiceRuntime(Protocol):
    """Model-specific remote work hidden completely from RunPod."""

    def ensure_ready(
        self,
        service: ServiceDefinition,
        claim: HostClaim,
        *,
        deployment_id: str,
        startup_deadline: float,
        cleanup_budget: CleanupBudget | None = None,
    ) -> ServiceEndpoint:
        """Stage, cache, start, tunnel, and attest one exact service."""
        ...

    def attest_ready(
        self,
        service: ServiceDefinition,
        claim: HostClaim,
        deployment: Deployment,
        *,
        startup_deadline: float | None = None,
    ) -> ServiceEndpoint:
        """Prove an existing deployment is still the exact ready service."""
        ...

    def stop(
        self,
        service: ServiceDefinition,
        claim: HostClaim,
        deployment: Deployment,
        *,
        cleanup_deadline: float | None = None,
    ) -> None:
        """Stop only the deployment-owned process and revoke its endpoint."""
        ...

    def cleanup_lost_claim(
        self,
        service: ServiceDefinition,
        deployment: Deployment,
        *,
        cleanup_deadline: float | None = None,
    ) -> None:
        """Revoke local authority after the provider claim has vanished."""
        ...


@runtime_checkable
class ProfileSelection(Protocol):
    """Narrow route exported by the authoritative model-session v3 parser."""

    profile_id: str
    project_id: str
    service_id: str
    required_input_modalities: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class ServiceUse:
    deployment: Deployment
    endpoint: ServiceEndpoint
    lease: UseLease


class _PreparationClaimRenewer:
    """Renew one persisted preparing claim independently of controller RPCs."""

    def __init__(
        self,
        *,
        hosts: HostControl,
        deployments: DeploymentStore,
        deployment: Deployment,
        renewal_ttl_seconds: int,
        interval_seconds: float,
        wait_for_interval: Callable[[threading.Event, float], bool],
        startup_deadline: float,
        monotonic: Callable[[], float],
    ) -> None:
        self.hosts = hosts
        self.deployments = deployments
        self.deployment = deployment
        self.renewal_ttl_seconds = renewal_ttl_seconds
        self.interval_seconds = interval_seconds
        self.wait_for_interval = wait_for_interval
        self.startup_deadline = startup_deadline
        self.monotonic = monotonic
        self.stop_event = threading.Event()
        self.failure: BaseException | None = None
        self.thread = threading.Thread(
            target=self._run,
            name=f"model-lab-renew-{deployment.service_id}",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> BaseException | None:
        self.stop_event.set()
        self.thread.join()
        return self.failure

    def _run(self) -> None:
        try:
            while not self.wait_for_interval(
                self.stop_event,
                self.interval_seconds,
            ):
                current = self.deployments.load(self.deployment.service_id)
                if (
                    current is None
                    or current.deployment_id != self.deployment.deployment_id
                    or current.phase != "preparing"
                ):
                    return
                claim = self.hosts.renew(
                    current.host_name,
                    current.claim_id,
                    current.claim_generation,
                    self.renewal_ttl_seconds,
                    startup_deadline=self.startup_deadline,
                    cancel_event=self.stop_event,
                )
                self.deployments.renew_claim_generation(
                    current.service_id,
                    deployment_id=current.deployment_id,
                    expected_generation=current.claim_generation,
                    generation=claim.generation,
                    startup_deadline=self.startup_deadline,
                    monotonic=self.monotonic,
                )
        except BaseException as error:
            if (
                self.stop_event.is_set()
                and getattr(error, "code", None)
                == "state_lock_cancelled"
            ):
                return
            self.failure = error


def build_claim_request(
    service: ServiceDefinition,
    lab: LabConfiguration,
    *,
    operation_id: str,
    host_name: str | None,
    acquisition_expires_at: str | None = None,
) -> HostClaimRequest:
    """Translate model resources into the complete opaque RunPod request."""

    resources = service.resources
    return HostClaimRequest(
        owner_system="model-lab",
        owner_instance=service.service_id,
        operation_id=operation_id,
        host_name=host_name,
        allowed_profile_names=lab.allowed_runpod_profiles,
        create_if_missing=host_name is None,
        mode=resources.claim_mode,
        gpu_device_count=resources.gpu_count,
        gpu_memory_bytes=resources.gpu_memory_gib * 1024**3,
        cpu_count=resources.cpu_count,
        memory_bytes=resources.memory_gib * 1024**3,
        ephemeral_disk_bytes=resources.ephemeral_disk_gib * 1024**3,
        endpoint_names=("openai",),
        minimum_remaining_seconds=(
            lab.lease.startup_timeout_seconds
            + lab.lease.minimum_useful_seconds
        ),
        acquisition_timeout_seconds=lab.lease.startup_timeout_seconds,
        acquisition_expires_at=acquisition_expires_at,
        renewal_ttl_seconds=lab.lease.renewal_ttl_seconds,
        new_host_hard_ttl_seconds=lab.lease.hard_ttl_seconds,
        new_host_retention="while-claimed",
    )


class ModelLabController:
    """Coordinates service identity, one host claim, and many Pi use leases."""

    def __init__(
        self,
        *,
        hosts: HostControl,
        runtime: ServiceRuntime,
        deployments: DeploymentStore,
        bindings: ProfileBindingStore,
        lab: LabConfiguration,
        preparation_renewal_interval_seconds: float | None = None,
        preparation_waiter: (
            Callable[[threading.Event, float], bool] | None
        ) = None,
        preparations: PreparationIntentStore | None = None,
        cleanup_timeout_seconds: float = 60.0,
        clock: Callable[[], datetime.datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            isinstance(cleanup_timeout_seconds, bool)
            or not isinstance(cleanup_timeout_seconds, (int, float))
            or not math.isfinite(float(cleanup_timeout_seconds))
            or cleanup_timeout_seconds <= 0
        ):
            raise ValueError(
                "service cleanup timeout must be positive and finite"
            )
        self.hosts = hosts
        self.runtime = runtime
        self.deployments = deployments
        self.bindings = bindings
        self.lab = lab
        self.preparation_renewal_interval_seconds = (
            max(1.0, lab.lease.renewal_ttl_seconds / 3)
            if preparation_renewal_interval_seconds is None
            else preparation_renewal_interval_seconds
        )
        self.preparation_waiter = (
            (lambda event, interval: event.wait(interval))
            if preparation_waiter is None
            else preparation_waiter
        )
        self.clock = clock
        self.monotonic = monotonic
        self.cleanup_timeout_seconds = float(cleanup_timeout_seconds)
        self.preparations = (
            PreparationIntentStore(deployments.root, clock=clock)
            if preparations is None
            else preparations
        )

    def canonical_startup_expiration(self, expires_at: str) -> str:
        """Bound a caller deadline to the server-authored startup policy."""

        requested = parse_timestamp(
            expires_at,
            "service startup expiration",
        )
        now = self.clock()
        authored = now + datetime.timedelta(
            seconds=self.lab.lease.startup_timeout_seconds
        )
        expiration = min(requested, authored)
        if expiration <= now:
            raise self._startup_timeout_error()
        return format_timestamp(expiration)

    def startup_deadline_from_expiration(
        self,
        expires_at: str,
        *,
        startup_deadline: float | None = None,
    ) -> float:
        """Bind a wall expiration without expanding an existing monotonic cap."""

        expiration = parse_timestamp(
            self.canonical_startup_expiration(expires_at),
            "canonical service startup expiration",
        )
        requested_remaining = (expiration - self.clock()).total_seconds()
        remaining_seconds = min(
            float(self.lab.lease.startup_timeout_seconds),
            requested_remaining,
        )
        if remaining_seconds <= 0:
            raise self._startup_timeout_error()
        wall_deadline = self.monotonic() + remaining_seconds
        if startup_deadline is None:
            return wall_deadline
        if (
            isinstance(startup_deadline, bool)
            or not isinstance(startup_deadline, (int, float))
            or not math.isfinite(float(startup_deadline))
        ):
            raise ModelLabError(
                "service startup monotonic deadline is invalid",
                code="invalid_supervisor_protocol",
            )
        bounded_deadline = min(float(startup_deadline), wall_deadline)
        self._require_startup_budget(bounded_deadline)
        return bounded_deadline

    def new_startup_expiration(self) -> str:
        return format_timestamp(
            self.clock()
            + datetime.timedelta(
                seconds=self.lab.lease.startup_timeout_seconds
            )
        )

    def _startup_timeout_error(self) -> ModelLabError:
        return ModelLabError(
            "service did not become ready within the configured "
            f"{self.lab.lease.startup_timeout_seconds}-second startup budget",
            code="service_startup_timeout",
        )

    def _require_startup_budget(self, deadline: float) -> None:
        if self.monotonic() >= deadline:
            raise self._startup_timeout_error()

    def require_startup_budget(self, deadline: float) -> None:
        self._require_startup_budget(deadline)

    def _new_cleanup_deadline(self) -> float:
        return self._new_cleanup_budget().deadline()

    def _new_cleanup_budget(self) -> CleanupBudget:
        return CleanupBudget(
            timeout_seconds=self.cleanup_timeout_seconds,
            monotonic=self.monotonic,
        )

    def _require_cleanup_budget(self, deadline: float) -> None:
        if self.monotonic() >= deadline:
            raise ModelLabError(
                "service cleanup exceeded its absolute deadline",
                code="service_cleanup_required",
            )

    def _claim_request(
        self,
        service: ServiceDefinition,
        *,
        operation_id: str,
        host_name: str | None,
        acquisition_expires_at: str | None = None,
    ) -> HostClaimRequest:
        return build_claim_request(
            service,
            self.lab,
            operation_id=operation_id,
            host_name=host_name,
            acquisition_expires_at=acquisition_expires_at,
        )

    def plan_claim(
        self,
        service: ServiceDefinition,
        *,
        host_name: str | None = None,
    ) -> HostClaimRequest:
        """Builds the complete generic claim request without provider access."""
        return self._claim_request(
            service,
            operation_id="available-at-execution",
            host_name=host_name,
        )

    def ensure_ready(
        self,
        service: ServiceDefinition,
        *,
        host_name: str | None = None,
        startup_expires_at: str | None = None,
        startup_deadline: float | None = None,
        _cleanup_budget: CleanupBudget | None = None,
    ) -> tuple[Deployment, ServiceEndpoint]:
        cleanup_budget = (
            self._new_cleanup_budget()
            if _cleanup_budget is None
            else _cleanup_budget
        )
        if startup_expires_at is None:
            startup_expires_at = self.new_startup_expiration()
        else:
            startup_expires_at = self.canonical_startup_expiration(
                startup_expires_at
            )
        startup_deadline = self.startup_deadline_from_expiration(
            startup_expires_at,
            startup_deadline=startup_deadline,
        )
        self._require_startup_budget(startup_deadline)
        existing = self.deployments.load(service.service_id)
        if existing is not None and existing.phase in {"ready", "idle"}:
            intent = self.preparations.load(service.service_id)
            if intent is not None:
                if intent.deployment_id != existing.deployment_id:
                    raise ModelLabError(
                        "preparation intent conflicts with active deployment",
                        code="preparation_intent_conflict",
                    )
                self.preparations.complete(
                    intent,
                    deadline=startup_deadline,
                    monotonic=self.monotonic,
                    deadline_error_code="service_startup_timeout",
                )
            if existing.workload_sha256 != service.workload_sha256:
                raise ModelLabError(
                    "a different workload is already deployed for "
                    f"{service.service_id}; stop it before changing the model",
                    code="service_workload_drift",
                )
            if existing.service_sha256 != service.service_sha256:
                raise ModelLabError(
                    "the active service configuration differs from its "
                    "authored definition; stop it before applying changes",
                    code="service_configuration_drift",
                )
            if host_name is not None and host_name != existing.host_name:
                raise ModelLabError(
                    f"service is already deployed on {existing.host_name}",
                    code="service_host_mismatch",
                )
            try:
                existing, claim = self._renew_current_claim(
                    existing,
                    startup_deadline=startup_deadline,
                )
            except Exception as error:
                if self.is_claim_gone(error):
                    return self._recover_lost_claim_for_ensure(
                        service,
                        existing,
                        host_name=host_name,
                        startup_deadline=startup_deadline,
                        startup_expires_at=startup_expires_at,
                        cleanup_budget=cleanup_budget,
                    )
                if self.is_claim_quarantined(error):
                    return self._drain_quarantined_claim_for_ensure(
                        service,
                        existing,
                        host_name=host_name,
                        startup_deadline=startup_deadline,
                        startup_expires_at=startup_expires_at,
                        cleanup_budget=cleanup_budget,
                    )
                raise
            try:
                endpoint = self.runtime.attest_ready(
                    service,
                    claim,
                    existing,
                    startup_deadline=startup_deadline,
                )
                self._require_startup_budget(startup_deadline)
                self._attest_endpoint(service, endpoint)
                self._require_startup_budget(startup_deadline)
            except Exception as error:
                if self.is_claim_gone(error):
                    return self._recover_lost_claim_for_ensure(
                        service,
                        existing,
                        host_name=host_name,
                        startup_deadline=startup_deadline,
                        startup_expires_at=startup_expires_at,
                        cleanup_budget=cleanup_budget,
                    )
                if getattr(error, "code", None) == "service_transport_replaced":
                    raise
                return self._recover_unhealthy_runtime_for_ensure(
                    service,
                    existing,
                    host_name=host_name,
                    attestation_error=error,
                    startup_deadline=startup_deadline,
                    startup_expires_at=startup_expires_at,
                    cleanup_budget=cleanup_budget,
                )
            return existing, endpoint

        if existing is not None and existing.phase == "preparing":
            intent = self.preparations.load(service.service_id)
            if intent is None:
                missing_intent = ModelLabError(
                    "preparing deployment has no durable preparation intent",
                    code="preparation_intent_missing",
                )
                self._cleanup_failed_preparation(
                    service,
                    existing,
                    intent=None,
                    cause=missing_intent,
                    cleanup_budget=cleanup_budget,
                )
                raise missing_intent
            if intent.deployment_id != existing.deployment_id:
                raise ModelLabError(
                    "preparation intent conflicts with active deployment",
                    code="preparation_intent_conflict",
                )
            if (
                existing.workload_sha256 != service.workload_sha256
                or existing.service_sha256 != service.service_sha256
            ):
                raise ModelLabError(
                    "a different service is already preparing",
                    code="service_configuration_drift",
                )
            if host_name is not None and host_name != existing.host_name:
                raise ModelLabError(
                    f"service is already preparing on {existing.host_name}",
                    code="service_host_mismatch",
                )
            try:
                startup_deadline = min(
                    startup_deadline,
                    self.startup_deadline_from_expiration(
                        intent.startup_expires_at,
                        startup_deadline=startup_deadline,
                    ),
                )
            except ModelLabError as error:
                if error.code != "service_startup_timeout":
                    raise
                self._cleanup_failed_preparation(
                    service,
                    existing,
                    intent=intent,
                    cause=error,
                    cleanup_budget=cleanup_budget,
                )
                raise
            try:
                existing, claim = self._current_claim(
                    existing,
                    startup_deadline=startup_deadline,
                )
            except Exception as error:
                if not self.is_claim_gone(error):
                    raise
                return self._recover_lost_claim_for_ensure(
                    service,
                    existing,
                    host_name=host_name,
                    startup_deadline=startup_deadline,
                    startup_expires_at=startup_expires_at,
                    cleanup_budget=cleanup_budget,
                )
            return self._complete_preparation(
                service,
                claim,
                existing,
                intent=intent,
                startup_deadline=startup_deadline,
                cleanup_budget=cleanup_budget,
            )

        if existing is not None and existing.phase != "released":
            raise ModelLabError(
                f"service {service.service_id} requires reconciliation from "
                f"phase {existing.phase}",
                code="service_cleanup_required",
            )
        stale_intent = self.preparations.load(service.service_id)
        if stale_intent is not None:
            self.reconcile_acquire_intent(
                stale_intent,
                cleanup_deadline=cleanup_budget.deadline(),
            )
        # Cleanup above belongs to an older provider identity. A newly acquired
        # claim must retain its own lazily started rollback budget even when
        # reconciliation consumed the complete allowance for that older claim.
        cleanup_budget = self._new_cleanup_budget()
        intent = self.preparations.begin(
            service_id=service.service_id,
            workload_sha256=service.workload_sha256,
            service_sha256=service.service_sha256,
            startup_expires_at=startup_expires_at,
            claim_request_factory=lambda operation_id: self._claim_request(
                service,
                operation_id=operation_id,
                host_name=host_name,
                acquisition_expires_at=startup_expires_at,
            ),
        )
        startup_deadline = min(
            startup_deadline,
            self.startup_deadline_from_expiration(
                intent.startup_expires_at,
                startup_deadline=startup_deadline,
            ),
        )
        try:
            claim = self.hosts.acquire(
                intent.claim_request,
                startup_deadline=startup_deadline,
                cleanup_deadline_factory=cleanup_budget.deadline,
            )
        except BaseException as original:
            try:
                self.reconcile_acquire_intent(
                    intent,
                    cleanup_deadline=cleanup_budget.deadline(),
                )
            except Exception as cleanup_error:
                raise ModelLabError(
                    "service host acquisition failed and its durable intent "
                    f"could not be reconciled: acquire={original}; "
                    f"cleanup={cleanup_error}",
                    code="service_cleanup_required",
                ) from original
            raise
        try:
            claim = self.hosts.wait_ready(
                claim,
                renewal_ttl_seconds=self.lab.lease.renewal_ttl_seconds,
                startup_deadline=startup_deadline,
            )
            now_text = format_timestamp(self.clock())
            preparing = Deployment(
                service_id=service.service_id,
                deployment_id=intent.deployment_id,
                workload_sha256=service.workload_sha256,
                service_sha256=service.service_sha256,
                host_name=claim.host_name,
                claim_id=claim.claim_id,
                claim_generation=claim.generation,
                endpoint_receipt_path=None,
                phase="preparing",
                created_at=now_text,
                updated_at=now_text,
                last_inference_at=now_text,
                idle_deadline=None,
                host_release_mode=None,
                use_leases=(),
            )
            self.deployments.publish_preparing(
                preparing,
                startup_deadline=startup_deadline,
                monotonic=self.monotonic,
            )
        except BaseException as original:
            cleanup_deadline = cleanup_budget.deadline()
            try:
                current_claim = self.hosts.get(
                    claim.host_name,
                    claim.claim_id,
                    startup_deadline=cleanup_deadline,
                )
                if (
                    current_claim.host_name != claim.host_name
                    or current_claim.claim_id != claim.claim_id
                    or current_claim.operation_id != claim.operation_id
                    or current_claim.provider_resource_id
                    != claim.provider_resource_id
                    or current_claim.generation < claim.generation
                ):
                    raise ModelLabError(
                        "host claim identity changed before failed readiness "
                        "could release it",
                        code="service_host_claim_mismatch",
                    )
                claim = current_claim
                self.hosts.release(
                    claim.host_name,
                    claim.claim_id,
                    claim.generation,
                    now=True,
                    cleanup_deadline=cleanup_deadline,
                )
                self.preparations.complete(
                    intent,
                    deadline=cleanup_deadline,
                    monotonic=self.monotonic,
                    deadline_error_code="service_cleanup_required",
                )
            except Exception as cleanup_error:
                raise ModelLabError(
                    "service bring-up failed and its RunPod claim could not "
                    f"be released: bring-up={original}; cleanup={cleanup_error}",
                    code="service_cleanup_required",
                ) from original
            raise
        return self._complete_preparation(
            service,
            claim,
            preparing,
            intent=intent,
            startup_deadline=startup_deadline,
            cleanup_budget=cleanup_budget,
        )

    def reconcile_acquire_intent(
        self,
        intent: PreparationIntent,
        *,
        cleanup_deadline: float | None = None,
    ) -> None:
        """Close an orphan pre-acquire window without admitting a new claim."""

        if cleanup_deadline is None:
            cleanup_deadline = self._new_cleanup_deadline()
        self._require_cleanup_budget(cleanup_deadline)
        deployment = self.deployments.load(intent.service_id)
        if deployment is not None:
            if deployment.deployment_id == intent.deployment_id:
                if deployment.phase == "preparing":
                    return
                self.preparations.complete(
                    intent,
                    deadline=cleanup_deadline,
                    monotonic=self.monotonic,
                    deadline_error_code="service_cleanup_required",
                )
                return
            if deployment.phase != "released":
                raise ModelLabError(
                    "preparation intent conflicts with durable deployment",
                    code="preparation_intent_conflict",
                )
        self.hosts.cancel(
            intent.claim_request,
            cleanup_deadline=cleanup_deadline,
        )
        self.preparations.complete(
            intent,
            deadline=cleanup_deadline,
            monotonic=self.monotonic,
            deadline_error_code="service_cleanup_required",
        )

    def _complete_preparation(
        self,
        service: ServiceDefinition,
        claim: HostClaim,
        preparing: Deployment,
        *,
        intent: PreparationIntent | None,
        startup_deadline: float,
        cleanup_budget: CleanupBudget,
    ) -> tuple[Deployment, ServiceEndpoint]:
        renewer = _PreparationClaimRenewer(
            hosts=self.hosts,
            deployments=self.deployments,
            deployment=preparing,
            renewal_ttl_seconds=self.lab.lease.renewal_ttl_seconds,
            interval_seconds=self.preparation_renewal_interval_seconds,
            wait_for_interval=self.preparation_waiter,
            startup_deadline=startup_deadline,
            monotonic=self.monotonic,
        )
        endpoint: ServiceEndpoint | None = None
        renewer.start()
        try:
            self._require_startup_budget(startup_deadline)
            endpoint = self.runtime.ensure_ready(
                service,
                claim,
                deployment_id=preparing.deployment_id,
                startup_deadline=startup_deadline,
                cleanup_budget=cleanup_budget,
            )
            renewal_failure = renewer.stop()
            self._require_startup_budget(startup_deadline)
            if renewal_failure is not None:
                raise ModelLabError(
                    f"host claim renewal failed during service start: "
                    f"{renewal_failure}",
                    code="service_claim_renewal_failed",
                ) from renewal_failure
            self._require_startup_budget(startup_deadline)
            self._attest_endpoint(service, endpoint)
            self._require_startup_budget(startup_deadline)
            current = self.deployments.load(service.service_id)
            if (
                current is None
                or current.deployment_id != preparing.deployment_id
                or current.phase != "preparing"
            ):
                raise ModelLabError(
                    "preparing deployment changed before publication",
                    code="deployment_changed",
                )
            now_text = format_timestamp(self.clock())
            ready = dataclasses.replace(
                current,
                endpoint_receipt_path=str(endpoint.receipt_path),
                phase="ready",
                updated_at=now_text,
                last_inference_at=now_text,
            )
            self.deployments.publish_ready(
                ready,
                startup_deadline=startup_deadline,
                monotonic=self.monotonic,
            )
            self._require_startup_budget(startup_deadline)
            if intent is not None:
                self.preparations.complete(
                    intent,
                    deadline=startup_deadline,
                    monotonic=self.monotonic,
                    deadline_error_code="service_startup_timeout",
                )
            return ready, endpoint
        except BaseException as original:
            renewal_failure = renewer.stop()
            cleanup_deadline = cleanup_budget.deadline()
            current = self.deployments.load(service.service_id)
            if current is None or current.deployment_id != preparing.deployment_id:
                raise ModelLabError(
                    "service bring-up failed after deployment state changed",
                    code="service_cleanup_required",
                ) from original
            try:
                failed = dataclasses.replace(
                    current,
                    phase="failed",
                    updated_at=format_timestamp(self.clock()),
                    host_release_mode="now",
                    use_leases=(),
                )
                self.deployments.publish_cleanup_transition(
                    current,
                    failed,
                    cleanup_deadline=cleanup_deadline,
                    monotonic=self.monotonic,
                )
                self.reconcile_cleanup(
                    service,
                    failed,
                    cleanup_deadline=cleanup_deadline,
                )
                if intent is not None:
                    self.preparations.complete(
                        intent,
                        deadline=cleanup_deadline,
                        monotonic=self.monotonic,
                        deadline_error_code="service_cleanup_required",
                    )
            except Exception as cleanup_error:
                renewal_text = (
                    ""
                    if renewal_failure is None
                    else f"; renewal={renewal_failure}"
                )
                raise ModelLabError(
                    "service bring-up failed and cleanup requires "
                    f"reconciliation: bring-up={original}"
                    f"{renewal_text}; cleanup={cleanup_error}",
                    code="service_cleanup_required",
                ) from original
            if renewal_failure is not None and renewal_failure is not original:
                raise ModelLabError(
                    "service bring-up failed after claim renewal failed: "
                    f"bring-up={original}; renewal={renewal_failure}",
                    code="service_claim_renewal_failed",
                ) from original
            raise

    def _cleanup_failed_preparation(
        self,
        service: ServiceDefinition,
        deployment: Deployment,
        *,
        intent: PreparationIntent | None,
        cause: ModelLabError,
        cleanup_budget: CleanupBudget | None = None,
    ) -> Deployment:
        """Stop and release one preparation that cannot safely continue."""

        if cleanup_budget is None:
            cleanup_budget = self._new_cleanup_budget()
        current = self.deployments.load(service.service_id)
        if (
            current is None
            or current.deployment_id != deployment.deployment_id
            or current.phase != "preparing"
        ):
            raise ModelLabError(
                "expired preparation changed before cleanup",
                code="service_cleanup_required",
            ) from cause
        failed = dataclasses.replace(
            current,
            phase="failed",
            updated_at=format_timestamp(self.clock()),
            host_release_mode="now",
            use_leases=(),
        )
        cleanup_deadline = cleanup_budget.deadline()
        self.deployments.publish_cleanup_transition(
            current,
            failed,
            cleanup_deadline=cleanup_deadline,
            monotonic=self.monotonic,
        )
        try:
            released = self.reconcile_cleanup(
                service,
                failed,
                cleanup_deadline=cleanup_deadline,
            )
            if intent is not None:
                self.preparations.complete(
                    intent,
                    deadline=cleanup_deadline,
                    monotonic=self.monotonic,
                    deadline_error_code="service_cleanup_required",
                )
            return released
        except Exception as cleanup_error:
            raise ModelLabError(
                "unrecoverable service preparation requires cleanup "
                f"reconciliation: {cleanup_error}",
                code="service_cleanup_required",
            ) from cause

    def acquire_for_profile(
        self,
        profile: ProfileSelection,
        service: ServiceDefinition,
        *,
        host_name: str | None = None,
        owner_pid: int | None = None,
        owner_start_time: str = "unknown",
        startup_expires_at: str | None = None,
        startup_deadline: float | None = None,
        stop_on_release: bool = False,
    ) -> ServiceUse:
        if profile.service_id != service.service_id:
            raise ModelLabError(
                "profile and service identifiers do not agree",
                code="profile_service_mismatch",
            )
        missing = sorted(
            set(profile.required_input_modalities).difference(
                service.endpoint.input_modalities
            )
        )
        if missing:
            raise ModelLabError(
                "service lacks profile-required modalities: " + ", ".join(missing),
                code="service_capability_mismatch",
            )
        if startup_expires_at is None:
            startup_expires_at = self.new_startup_expiration()
        else:
            startup_expires_at = self.canonical_startup_expiration(
                startup_expires_at
            )
        startup_deadline = self.startup_deadline_from_expiration(
            startup_expires_at,
            startup_deadline=startup_deadline,
        )
        self.bindings.attest(
            profile,
            service,
            startup_deadline=startup_deadline,
            monotonic=self.monotonic,
        )
        lease_id = f"use-{secrets.token_hex(16)}"
        lease_owner_pid = (
            owner_pid if owner_pid is not None else os.getpid()
        )
        prior = self.deployments.load(service.service_id)
        deployment, endpoint = self.ensure_ready(
            service,
            host_name=host_name,
            startup_expires_at=startup_expires_at,
            startup_deadline=startup_deadline,
        )
        acquired_new_identity = (
            prior is None
            or prior.phase == "released"
            or prior.deployment_id != deployment.deployment_id
        )
        admission_release_mode = (
            "now"
            if stop_on_release
            else "stop-if-final"
            if acquired_new_identity
            else "idle"
        )
        try:
            lease = self.deployments.acquire_use(
                service.service_id,
                lease_id=lease_id,
                admission_expires_at=startup_expires_at,
                admission_release_mode=admission_release_mode,
                expected_workload_sha256=service.workload_sha256,
                owner_pid=lease_owner_pid,
                owner_start_time=owner_start_time,
                stop_on_release=stop_on_release,
                startup_deadline=startup_deadline,
                monotonic=self.monotonic,
            )
        except BaseException as original:
            try:
                self._recover_failed_use_acquisition(
                    service,
                    deployment,
                    prior=prior,
                    lease_id=lease_id,
                    lease_owner_pid=lease_owner_pid,
                    lease_owner_start_time=owner_start_time,
                    stop_on_release=stop_on_release,
                )
            except Exception as cleanup_error:
                raise ModelLabError(
                    "Pi use acquisition failed and lease-free service cleanup "
                    f"requires reconciliation: acquisition={original}; "
                    f"cleanup={cleanup_error}",
                    code="service_cleanup_required",
                ) from original
            raise
        use = ServiceUse(
            deployment=dataclasses.replace(
                deployment,
                phase="ready",
                idle_deadline=None,
                host_release_mode=(
                    "now"
                    if stop_on_release
                    or deployment.host_release_mode == "now"
                    else None
                ),
                use_leases=(*deployment.use_leases, lease),
            ),
            endpoint=endpoint,
            lease=lease,
        )
        try:
            self._attest_endpoint(
                service,
                endpoint,
                required_modalities=profile.required_input_modalities,
            )
            if startup_deadline is not None:
                self._require_startup_budget(startup_deadline)
            return use
        except BaseException as original:
            try:
                self.release_profile_use(
                    service,
                    use,
                    now=stop_on_release,
                    stop_if_final=True,
                )
            except Exception as cleanup_error:
                raise ModelLabError(
                    "Pi use admission failed and immediate service cleanup "
                    f"requires reconciliation: admission={original}; "
                    f"cleanup={cleanup_error}",
                    code="service_cleanup_required",
                ) from original
            raise

    def _recover_failed_use_acquisition(
        self,
        service: ServiceDefinition,
        deployment: Deployment,
        *,
        prior: Deployment | None,
        lease_id: str,
        lease_owner_pid: int,
        lease_owner_start_time: str,
        stop_on_release: bool,
    ) -> None:
        """Release exactly this admission or retire its lease-free capacity."""

        current = self.deployments.load(service.service_id)
        if (
            current is None
            or current.deployment_id != deployment.deployment_id
            or current.service_sha256 != service.service_sha256
            or current.workload_sha256 != service.workload_sha256
            or current.phase in {"quiescing", "stopping", "failed", "released"}
        ):
            return
        matching_leases = tuple(
            lease
            for lease in current.use_leases
            if lease.lease_id == lease_id
        )
        if matching_leases:
            lease = matching_leases[0]
            if (
                lease.owner_pid != lease_owner_pid
                or lease.owner_start_time != lease_owner_start_time
            ):
                return
            acquired_new_identity = (
                prior is None
                or prior.phase == "released"
                or prior.deployment_id != current.deployment_id
            )
            cleanup_deadline = self._new_cleanup_deadline()
            self._release_exact_use(
                service,
                deployment_id=current.deployment_id,
                lease_id=lease_id,
                now=stop_on_release,
                stop_if_final=acquired_new_identity,
                cleanup_deadline=cleanup_deadline,
            )
            return
        if current.use_leases:
            return
        if current.phase == "idle" and not stop_on_release:
            return
        if current.phase not in {"ready", "idle"}:
            raise ModelLabError(
                "failed Pi use acquisition left an unsupported deployment phase",
                code="invalid_deployment_transition",
            )
        acquired_new_identity = (
            prior is None
            or prior.phase == "released"
            or prior.deployment_id != current.deployment_id
        )
        self.down(
            service,
            now=stop_on_release or acquired_new_identity,
            cleanup_deadline=self._new_cleanup_deadline(),
        )

    def reconcile_preparing(
        self,
        service: ServiceDefinition,
        deployment: Deployment,
    ) -> Deployment:
        """Adopt an exact ready runtime or tear down an orphaned preparation."""

        if (
            deployment.phase != "preparing"
            or deployment.service_id != service.service_id
            or deployment.workload_sha256 != service.workload_sha256
            or deployment.service_sha256 != service.service_sha256
        ):
            raise ModelLabError(
                "preparing deployment does not match its retained service",
                code="service_configuration_drift",
            )
        intent = self.preparations.load(service.service_id)
        if intent is None:
            missing_intent = ModelLabError(
                "preparing deployment has no durable preparation intent",
                code="preparation_intent_missing",
            )
            return self._cleanup_failed_preparation(
                service,
                deployment,
                intent=None,
                cause=missing_intent,
            )
        if intent.deployment_id != deployment.deployment_id:
            raise ModelLabError(
                "preparation intent conflicts with retained deployment",
                code="preparation_intent_conflict",
            )
        try:
            startup_deadline = self.startup_deadline_from_expiration(
                intent.startup_expires_at
            )
        except ModelLabError as error:
            if error.code != "service_startup_timeout":
                raise
            return self._cleanup_failed_preparation(
                service,
                deployment,
                intent=intent,
                cause=error,
            )
        try:
            deployment, claim = self._current_claim(
                deployment,
                startup_deadline=startup_deadline,
            )
        except Exception as error:
            if not self.is_claim_gone(error):
                raise
            return self.reconcile_claim_gone(service, deployment)
        renewer = _PreparationClaimRenewer(
            hosts=self.hosts,
            deployments=self.deployments,
            deployment=deployment,
            renewal_ttl_seconds=self.lab.lease.renewal_ttl_seconds,
            interval_seconds=self.preparation_renewal_interval_seconds,
            wait_for_interval=self.preparation_waiter,
            startup_deadline=startup_deadline,
            monotonic=self.monotonic,
        )
        renewer.start()
        endpoint: ServiceEndpoint | None = None
        attestation_error: BaseException | None = None
        try:
            endpoint = self.runtime.attest_ready(
                service,
                claim,
                deployment,
                startup_deadline=startup_deadline,
            )
            self._require_startup_budget(startup_deadline)
        except BaseException as error:
            attestation_error = error
        renewal_failure = renewer.stop()
        try:
            self._require_startup_budget(startup_deadline)
        except BaseException as error:
            if attestation_error is None:
                attestation_error = error
        if attestation_error is None and renewal_failure is None:
            try:
                assert endpoint is not None
                self._attest_endpoint(service, endpoint)
                self._require_startup_budget(startup_deadline)
                current = self.deployments.load(service.service_id)
                if (
                    current is None
                    or current.deployment_id != deployment.deployment_id
                    or current.phase != "preparing"
                ):
                    raise ModelLabError(
                        "preparing deployment changed during recovery",
                        code="deployment_changed",
                    )
                ready = dataclasses.replace(
                    current,
                    endpoint_receipt_path=str(endpoint.receipt_path),
                    phase="ready",
                    updated_at=format_timestamp(self.clock()),
                )
                self.deployments.publish_ready(
                    ready,
                    startup_deadline=startup_deadline,
                    monotonic=self.monotonic,
                )
                self._require_startup_budget(startup_deadline)
                if intent is not None:
                    self.preparations.complete(
                        intent,
                        deadline=startup_deadline,
                        monotonic=self.monotonic,
                        deadline_error_code="service_startup_timeout",
                    )
                return ready
            except BaseException as error:
                attestation_error = error

        current = self.deployments.load(service.service_id)
        if current is None or current.deployment_id != deployment.deployment_id:
            raise ModelLabError(
                "preparing deployment changed during recovery",
                code="service_cleanup_required",
            )
        failed = dataclasses.replace(
            current,
            phase="failed",
            updated_at=format_timestamp(self.clock()),
            host_release_mode="now",
            use_leases=(),
        )
        cleanup_deadline = self._new_cleanup_deadline()
        self.deployments.publish_cleanup_transition(
            current,
            failed,
            cleanup_deadline=cleanup_deadline,
            monotonic=self.monotonic,
        )
        try:
            released = self.reconcile_cleanup(
                service,
                failed,
                cleanup_deadline=cleanup_deadline,
            )
            if intent is not None:
                self.preparations.complete(
                    intent,
                    deadline=cleanup_deadline,
                    monotonic=self.monotonic,
                    deadline_error_code="service_cleanup_required",
                )
            return released
        except Exception as cleanup_error:
            cause = attestation_error or renewal_failure
            raise ModelLabError(
                "orphaned preparation cleanup requires reconciliation: "
                f"{cleanup_error}",
                code="service_cleanup_required",
            ) from cause

    def release_profile_use(
        self,
        service: ServiceDefinition,
        use: ServiceUse,
        *,
        now: bool = False,
        stop_if_final: bool = False,
    ) -> None:
        cleanup_deadline = self._new_cleanup_deadline()
        self._release_exact_use(
            service,
            deployment_id=use.deployment.deployment_id,
            lease_id=use.lease.lease_id,
            now=now,
            stop_if_final=stop_if_final,
            cleanup_deadline=cleanup_deadline,
        )

    def _release_exact_use(
        self,
        service: ServiceDefinition,
        *,
        deployment_id: str,
        lease_id: str,
        now: bool,
        stop_if_final: bool,
        cleanup_deadline: float,
    ) -> None:
        """Release and durably confirm one exact use before reporting success."""

        result = self.deployments.release_use_exact(
            service.service_id,
            lease_id,
            expected_deployment_id=deployment_id,
            idle_ttl_seconds=self.lab.lease.service_idle_ttl_seconds,
            now=now,
            stop_if_final=stop_if_final,
            cleanup_deadline=cleanup_deadline,
            monotonic=self.monotonic,
        )
        if result.stop_now:
            if result.deployment.phase == "quiescing":
                self._stop(
                    service,
                    result.deployment,
                    now=True,
                    cleanup_deadline=cleanup_deadline,
                )
            else:
                self.reconcile_cleanup(
                    service,
                    result.deployment,
                    cleanup_deadline=cleanup_deadline,
                )

    def release_expired_pending_uses(
        self,
        service: ServiceDefinition,
    ) -> bool:
        """Reap admissions that never transferred to a live session process."""

        deployment = self.deployments.load(service.service_id)
        if deployment is None or deployment.phase != "ready":
            return False
        current_time = self.clock()
        expired = tuple(
            lease
            for lease in deployment.use_leases
            if (
                lease.admission_expires_at is not None
                and parse_timestamp(
                    lease.admission_expires_at,
                    "use lease admission expiration",
                )
                <= current_time
            )
        )
        if not expired:
            return False
        cleanup_deadline = self._new_cleanup_deadline()
        for lease in expired:
            release_mode = lease.admission_release_mode
            if release_mode is None:
                raise ModelLabError(
                    "pending admission lost its durable release policy",
                    code="invalid_deployment_state",
                )
            self._release_exact_use(
                service,
                deployment_id=deployment.deployment_id,
                lease_id=lease.lease_id,
                now=release_mode == "now",
                stop_if_final=release_mode == "stop-if-final",
                cleanup_deadline=cleanup_deadline,
            )
            deployment = self.deployments.load(service.service_id)
            if deployment is None or deployment.phase == "released":
                break
        return True

    def down(
        self,
        service: ServiceDefinition,
        *,
        now: bool = False,
        cleanup_deadline: float | None = None,
        deadline_error_code: str = "service_cleanup_required",
    ) -> Deployment:
        if cleanup_deadline is None:
            cleanup_deadline = self._new_cleanup_deadline()
        deployment = self.deployments.begin_idle(
            service.service_id,
            idle_ttl_seconds=self.lab.lease.service_idle_ttl_seconds,
            now=now,
            cleanup_deadline=cleanup_deadline,
            monotonic=self.monotonic,
            deadline_error_code=deadline_error_code,
        )
        if now:
            return self._stop(
                service,
                deployment,
                now=True,
                cleanup_deadline=cleanup_deadline,
            )
        return deployment

    def stop_if_idle_due(self, service: ServiceDefinition) -> bool:
        cleanup_deadline = self._new_cleanup_deadline()
        deployment = self.deployments.begin_idle_cleanup_if_due(
            service.service_id,
            cleanup_deadline=cleanup_deadline,
            monotonic=self.monotonic,
        )
        if deployment is None:
            return False
        self.reconcile_cleanup(
            service,
            deployment,
            cleanup_deadline=cleanup_deadline,
        )
        return True

    def _stop(
        self,
        service: ServiceDefinition,
        deployment: Deployment,
        *,
        now: bool,
        cleanup_deadline: float | None = None,
    ) -> Deployment:
        expected_release_mode = "now" if now else "empty-grace"
        if (
            deployment.phase != "quiescing"
            or deployment.host_release_mode != expected_release_mode
            or deployment.idle_deadline is not None
            or deployment.use_leases
        ):
            raise ModelLabError(
                "service stop requires an atomically quiesced deployment",
                code="invalid_deployment_transition",
            )
        return self.reconcile_cleanup(
            service,
            deployment,
            cleanup_deadline=cleanup_deadline,
        )

    @staticmethod
    def is_claim_gone(error: BaseException) -> bool:
        return getattr(error, "code", None) in {
            "host_claim_not_found",
            "host_claim_expired",
            "host_claim_host_changed",
        }

    @staticmethod
    def is_claim_quarantined(error: BaseException) -> bool:
        return getattr(error, "code", None) == "host_claim_quarantined"

    def _current_claim(
        self,
        deployment: Deployment,
        *,
        startup_deadline: float | None = None,
    ) -> tuple[Deployment, HostClaim]:
        claim = self.hosts.get(
            deployment.host_name,
            deployment.claim_id,
            startup_deadline=startup_deadline,
        )
        if (
            claim.host_name != deployment.host_name
            or claim.claim_id != deployment.claim_id
        ):
            raise ModelLabError(
                "host controller returned a different claim identity",
                code="host_claim_identity_mismatch",
            )
        if claim.generation < deployment.claim_generation:
            raise ModelLabError(
                "host claim generation moved backwards",
                code="host_claim_generation_mismatch",
            )
        if claim.generation > deployment.claim_generation:
            deployment = self.deployments.renew_claim_generation(
                deployment.service_id,
                deployment_id=deployment.deployment_id,
                expected_generation=deployment.claim_generation,
                generation=claim.generation,
                startup_deadline=startup_deadline,
                monotonic=self.monotonic,
            )
        return deployment, claim

    def _renew_current_claim(
        self,
        deployment: Deployment,
        *,
        startup_deadline: float | None = None,
    ) -> tuple[Deployment, HostClaim]:
        """Adopt a committed provider generation before renewing it again."""

        retained = self.deployments.load(deployment.service_id)
        if (
            retained is None
            or retained.deployment_id != deployment.deployment_id
            or retained.claim_id != deployment.claim_id
            or retained.phase not in {"ready", "idle"}
        ):
            raise ModelLabError(
                "deployment changed before host-claim renewal",
                code="deployment_changed",
            )
        retained, current_claim = self._current_claim(
            retained,
            startup_deadline=startup_deadline,
        )
        renewed = self.hosts.renew(
            retained.host_name,
            retained.claim_id,
            retained.claim_generation,
            self.lab.lease.renewal_ttl_seconds,
            startup_deadline=startup_deadline,
        )
        if (
            renewed.host_name != retained.host_name
            or renewed.claim_id != retained.claim_id
        ):
            raise ModelLabError(
                "host renewal returned a different claim identity",
                code="host_claim_identity_mismatch",
            )
        if renewed.generation <= current_claim.generation:
            raise ModelLabError(
                "host renewal did not advance the claim generation",
                code="host_claim_generation_mismatch",
            )
        updated = self.deployments.renew_claim_generation(
            retained.service_id,
            deployment_id=retained.deployment_id,
            expected_generation=retained.claim_generation,
            generation=renewed.generation,
            startup_deadline=startup_deadline,
            monotonic=self.monotonic,
        )
        return updated, renewed

    def renew_deployment_claim(
        self,
        deployment: Deployment,
    ) -> Deployment:
        renewed, _ = self._renew_current_claim(deployment)
        return renewed

    def _recover_lost_claim_for_ensure(
        self,
        service: ServiceDefinition,
        deployment: Deployment,
        *,
        host_name: str | None,
        startup_deadline: float,
        startup_expires_at: str,
        cleanup_budget: CleanupBudget,
    ) -> tuple[Deployment, ServiceEndpoint]:
        active_users = bool(deployment.use_leases)
        self.reconcile_claim_gone(
            service,
            deployment,
            cleanup_deadline=cleanup_budget.deadline(),
        )
        if active_users:
            raise ModelLabError(
                "host claim vanished while Pi sessions were active; "
                "their supervisor channels must close before retry",
                code="service_claim_lost",
            )
        self._require_startup_budget(startup_deadline)
        return self.ensure_ready(
            service,
            host_name=host_name,
            startup_expires_at=startup_expires_at,
            startup_deadline=startup_deadline,
            _cleanup_budget=cleanup_budget,
        )

    def _recover_unhealthy_runtime_for_ensure(
        self,
        service: ServiceDefinition,
        deployment: Deployment,
        *,
        host_name: str | None,
        attestation_error: BaseException,
        startup_deadline: float,
        startup_expires_at: str,
        cleanup_budget: CleanupBudget,
    ) -> tuple[Deployment, ServiceEndpoint]:
        active_users = bool(deployment.use_leases)
        try:
            self.down(
                service,
                now=True,
                cleanup_deadline=cleanup_budget.deadline(),
            )
        except Exception as cleanup_error:
            raise ModelLabError(
                "unhealthy service cleanup requires reconciliation: "
                f"attestation={attestation_error}; cleanup={cleanup_error}",
                code="service_cleanup_required",
            ) from attestation_error
        if active_users:
            raise ModelLabError(
                "service runtime failed while Pi sessions were active; "
                "their supervisor channels must close before retry",
                code="service_runtime_replaced",
            ) from attestation_error
        self._require_startup_budget(startup_deadline)
        return self.ensure_ready(
            service,
            host_name=host_name,
            startup_expires_at=startup_expires_at,
            startup_deadline=startup_deadline,
            _cleanup_budget=cleanup_budget,
        )

    def _drain_quarantined_claim_for_ensure(
        self,
        service: ServiceDefinition,
        deployment: Deployment,
        *,
        host_name: str | None,
        startup_deadline: float,
        startup_expires_at: str,
        cleanup_budget: CleanupBudget,
    ) -> tuple[Deployment, ServiceEndpoint]:
        active_users = bool(deployment.use_leases)
        self.drain_quarantined_claim(
            service,
            deployment,
            cleanup_deadline=cleanup_budget.deadline(),
        )
        if active_users:
            raise ModelLabError(
                "host was quarantined while Pi sessions were active; "
                "their supervisor channels must close before retry",
                code="service_claim_drained",
            )
        self._require_startup_budget(startup_deadline)
        return self.ensure_ready(
            service,
            host_name=host_name,
            startup_expires_at=startup_expires_at,
            startup_deadline=startup_deadline,
            _cleanup_budget=cleanup_budget,
        )

    def drain_quarantined_claim(
        self,
        service: ServiceDefinition,
        deployment: Deployment,
        *,
        cleanup_deadline: float | None = None,
    ) -> Deployment:
        """Stop remotely while a quarantined claim still grants authority."""

        if cleanup_deadline is None:
            cleanup_deadline = self._new_cleanup_deadline()
        self._require_cleanup_budget(cleanup_deadline)
        current = self.deployments.load(service.service_id)
        if (
            current is None
            or current.deployment_id != deployment.deployment_id
            or current.claim_id != deployment.claim_id
            or current.phase not in {"ready", "idle"}
        ):
            raise ModelLabError(
                "deployment changed before quarantined-claim drain",
                code="deployment_changed",
            )
        quiescing = self.deployments.begin_idle(
            service.service_id,
            idle_ttl_seconds=self.lab.lease.service_idle_ttl_seconds,
            now=True,
            cleanup_deadline=cleanup_deadline,
            monotonic=self.monotonic,
        )
        return self.reconcile_cleanup(
            service,
            quiescing,
            cleanup_deadline=cleanup_deadline,
        )

    def reconcile_claim_gone(
        self,
        service: ServiceDefinition,
        deployment: Deployment,
        *,
        cleanup_deadline: float | None = None,
    ) -> Deployment:
        """Revoke local authority and retire state for a vanished claim."""

        if cleanup_deadline is None:
            cleanup_deadline = self._new_cleanup_deadline()
        self._require_cleanup_budget(cleanup_deadline)
        retained = self.deployments.load(service.service_id)
        if (
            retained is None
            or retained.deployment_id != deployment.deployment_id
            or retained.claim_id != deployment.claim_id
        ):
            raise ModelLabError(
                "deployment changed while reconciling a vanished claim",
                code="deployment_changed",
            )
        current = self.deployments.begin_claim_gone_cleanup(
            service.service_id,
            deployment_id=retained.deployment_id,
            claim_id=retained.claim_id,
            expected_generation=retained.claim_generation,
            cleanup_deadline=cleanup_deadline,
            monotonic=self.monotonic,
        )
        if current.phase == "released":
            return current
        try:
            self.runtime.cleanup_lost_claim(
                service,
                current,
                cleanup_deadline=cleanup_deadline,
            )
            released = dataclasses.replace(
                current,
                phase="released",
                updated_at=format_timestamp(self.clock()),
            )
            self.deployments.publish_cleanup_transition(
                current,
                released,
                cleanup_deadline=cleanup_deadline,
                monotonic=self.monotonic,
            )
        except Exception as error:
            raise ModelLabError(
                f"lost host-claim cleanup requires reconciliation: {error}",
                code="service_cleanup_required",
            ) from error
        return released

    def reconcile_cleanup(
        self,
        service: ServiceDefinition,
        deployment: Deployment,
        *,
        cleanup_deadline: float | None = None,
    ) -> Deployment:
        """Resume the durable stop-runtime then release-claim transaction."""

        if cleanup_deadline is None:
            cleanup_deadline = self._new_cleanup_deadline()
        self._require_cleanup_budget(cleanup_deadline)
        current = self.deployments.load(service.service_id)
        if (
            current is None
            or current.deployment_id != deployment.deployment_id
            or current.phase not in {"quiescing", "stopping", "failed"}
            or current.host_release_mode
            not in {"now", "empty-grace", "claim-gone"}
        ):
            raise ModelLabError(
                "deployment is not in a reconcilable cleanup phase",
                code="invalid_deployment_transition",
            )

        if current.host_release_mode == "claim-gone":
            return self.reconcile_claim_gone(
                service,
                current,
                cleanup_deadline=cleanup_deadline,
            )

        if current.phase in {"quiescing", "failed"}:
            try:
                current, claim = self._current_claim(
                    current,
                    startup_deadline=cleanup_deadline,
                )
                self.runtime.stop(
                    service,
                    claim,
                    current,
                    cleanup_deadline=cleanup_deadline,
                )
                stopping = dataclasses.replace(
                    current,
                    phase="stopping",
                    updated_at=format_timestamp(self.clock()),
                )
                current = self.deployments.publish_cleanup_transition(
                    current,
                    stopping,
                    cleanup_deadline=cleanup_deadline,
                    monotonic=self.monotonic,
                )
            except Exception as error:
                if self.is_claim_gone(error):
                    return self.reconcile_claim_gone(
                        service,
                        current,
                        cleanup_deadline=cleanup_deadline,
                    )
                raise ModelLabError(
                    f"service runtime cleanup requires reconciliation: {error}",
                    code="service_cleanup_required",
                ) from error

        try:
            current, claim = self._current_claim(
                current,
                startup_deadline=cleanup_deadline,
            )
        except Exception as error:
            if not self.is_claim_gone(error):
                raise ModelLabError(
                    f"host claim lookup during cleanup failed: {error}",
                    code="service_cleanup_required",
                ) from error
            return self.reconcile_claim_gone(
                service,
                current,
                cleanup_deadline=cleanup_deadline,
            )

        try:
            result = self.hosts.release(
                claim.host_name,
                claim.claim_id,
                claim.generation,
                now=current.host_release_mode == "now",
                cleanup_deadline=cleanup_deadline,
            )
            if not result.released:
                raise ModelLabError(
                    "host controller did not release the exact claim",
                    code="host_claim_release_incomplete",
                )
            released = dataclasses.replace(
                current,
                phase="released",
                updated_at=format_timestamp(self.clock()),
            )
            self.deployments.publish_cleanup_transition(
                current,
                released,
                cleanup_deadline=cleanup_deadline,
                monotonic=self.monotonic,
            )
        except Exception as error:
            if self.is_claim_gone(error):
                return self.reconcile_claim_gone(
                    service,
                    current,
                    cleanup_deadline=cleanup_deadline,
                )
            raise ModelLabError(
                f"host claim release requires reconciliation: {error}",
                code="service_cleanup_required",
            ) from error
        return released

    @staticmethod
    def _attest_endpoint(
        service: ServiceDefinition,
        endpoint: ServiceEndpoint,
        *,
        required_modalities: tuple[str, ...] = (),
    ) -> None:
        binding = endpoint.binding
        if (
            binding.service_id != service.service_id
            or binding.service_sha256 != service.service_sha256
            or binding.workload_sha256 != service.workload_sha256
        ):
            raise ModelLabError(
                "published endpoint does not match the exact service",
                code="endpoint_service_mismatch",
            )
        missing = sorted(set(required_modalities).difference(binding.input_modalities))
        if missing:
            raise ModelLabError(
                "endpoint lacks required input modalities: " + ", ".join(missing),
                code="endpoint_capability_mismatch",
            )
        try:
            metadata = endpoint.socket_path.stat()
        except OSError as error:
            raise ModelLabError(
                "published endpoint socket is unavailable",
                code="endpoint_socket_unavailable",
            ) from error
        if (
            not stat.S_ISSOCK(metadata.st_mode)
            or metadata.st_dev != endpoint.socket_device
            or metadata.st_ino != endpoint.socket_inode
        ):
            raise ModelLabError(
                "published endpoint socket identity changed",
                code="endpoint_socket_mismatch",
            )
