"""Service orchestration above the generic RunPod host-claim facade."""

from __future__ import annotations

import dataclasses
import os
import stat
import threading
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from model_session.attachment import ServiceEndpoint

from .configuration import LabConfiguration
from .errors import ModelLabError
from .lifecycle import (
    Deployment,
    DeploymentStore,
    UseLease,
    format_timestamp,
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
    ) -> ServiceEndpoint:
        """Stage, cache, start, tunnel, and attest one exact service."""
        ...

    def attest_ready(
        self,
        service: ServiceDefinition,
        claim: HostClaim,
        deployment: Deployment,
    ) -> ServiceEndpoint:
        """Prove an existing deployment is still the exact ready service."""
        ...

    def stop(
        self,
        service: ServiceDefinition,
        claim: HostClaim,
        deployment: Deployment,
    ) -> None:
        """Stop only the deployment-owned process and revoke its endpoint."""
        ...

    def cleanup_lost_claim(
        self,
        service: ServiceDefinition,
        deployment: Deployment,
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
    ) -> None:
        self.hosts = hosts
        self.deployments = deployments
        self.deployment = deployment
        self.renewal_ttl_seconds = renewal_ttl_seconds
        self.interval_seconds = interval_seconds
        self.wait_for_interval = wait_for_interval
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
                )
                self.deployments.renew_claim_generation(
                    current.service_id,
                    deployment_id=current.deployment_id,
                    expected_generation=current.claim_generation,
                    generation=claim.generation,
                )
        except BaseException as error:
            self.failure = error


def build_claim_request(
    service: ServiceDefinition,
    lab: LabConfiguration,
    *,
    operation_id: str,
    host_name: str | None,
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
        minimum_remaining_seconds=lab.lease.minimum_useful_seconds,
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
    ) -> None:
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
        self.preparations = (
            PreparationIntentStore(deployments.root)
            if preparations is None
            else preparations
        )

    def _claim_request(
        self,
        service: ServiceDefinition,
        *,
        operation_id: str,
        host_name: str | None,
    ) -> HostClaimRequest:
        return build_claim_request(
            service,
            self.lab,
            operation_id=operation_id,
            host_name=host_name,
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
    ) -> tuple[Deployment, ServiceEndpoint]:
        existing = self.deployments.load(service.service_id)
        if existing is not None and existing.phase in {"ready", "idle"}:
            intent = self.preparations.load(service.service_id)
            if intent is not None:
                if intent.deployment_id != existing.deployment_id:
                    raise ModelLabError(
                        "preparation intent conflicts with active deployment",
                        code="preparation_intent_conflict",
                    )
                self.preparations.complete(intent)
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
                existing, claim = self._renew_current_claim(existing)
            except Exception as error:
                if self.is_claim_gone(error):
                    return self._recover_lost_claim_for_ensure(
                        service,
                        existing,
                        host_name=host_name,
                    )
                if self.is_claim_quarantined(error):
                    return self._drain_quarantined_claim_for_ensure(
                        service,
                        existing,
                        host_name=host_name,
                    )
                raise
            try:
                endpoint = self.runtime.attest_ready(
                    service,
                    claim,
                    existing,
                )
                self._attest_endpoint(service, endpoint)
            except Exception as error:
                if self.is_claim_gone(error):
                    return self._recover_lost_claim_for_ensure(
                        service,
                        existing,
                        host_name=host_name,
                    )
                if getattr(error, "code", None) == "service_transport_replaced":
                    raise
                return self._recover_unhealthy_runtime_for_ensure(
                    service,
                    existing,
                    host_name=host_name,
                    attestation_error=error,
                )
            return existing, endpoint

        if existing is not None and existing.phase == "preparing":
            intent = self.preparations.load(service.service_id)
            if intent is not None:
                if intent.deployment_id != existing.deployment_id:
                    raise ModelLabError(
                        "preparation intent conflicts with active deployment",
                        code="preparation_intent_conflict",
                    )
                self.preparations.complete(intent)
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
                existing, claim = self._current_claim(existing)
            except Exception as error:
                if not self.is_claim_gone(error):
                    raise
                return self._recover_lost_claim_for_ensure(
                    service,
                    existing,
                    host_name=host_name,
                )
            return self._complete_preparation(service, claim, existing)

        if existing is not None and existing.phase != "released":
            raise ModelLabError(
                f"service {service.service_id} requires reconciliation from "
                f"phase {existing.phase}",
                code="service_cleanup_required",
            )
        stale_intent = self.preparations.load(service.service_id)
        if stale_intent is not None:
            self.reconcile_acquire_intent(stale_intent)
        intent = self.preparations.begin(
            service_id=service.service_id,
            workload_sha256=service.workload_sha256,
            service_sha256=service.service_sha256,
            claim_request_factory=lambda operation_id: self._claim_request(
                service,
                operation_id=operation_id,
                host_name=host_name,
            ),
        )
        claim = self.hosts.acquire(intent.claim_request)
        try:
            claim = self.hosts.wait_ready(
                claim,
                renewal_ttl_seconds=self.lab.lease.renewal_ttl_seconds,
            )
            now_text = format_timestamp(utc_now())
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
            self.deployments.publish_preparing(preparing)
            self.preparations.complete(intent)
        except BaseException as original:
            try:
                current_claim = self.hosts.get(
                    claim.host_name,
                    claim.claim_id,
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
                )
                self.preparations.complete(intent)
            except Exception as cleanup_error:
                raise ModelLabError(
                    "service bring-up failed and its RunPod claim could not "
                    f"be released: bring-up={original}; cleanup={cleanup_error}",
                    code="service_cleanup_required",
                ) from original
            raise
        return self._complete_preparation(service, claim, preparing)

    def reconcile_acquire_intent(
        self,
        intent: PreparationIntent,
    ) -> None:
        """Close an orphan pre-acquire window without admitting a new claim."""

        deployment = self.deployments.load(intent.service_id)
        if deployment is not None:
            if deployment.deployment_id == intent.deployment_id:
                self.preparations.complete(intent)
                return
            if deployment.phase != "released":
                raise ModelLabError(
                    "preparation intent conflicts with durable deployment",
                    code="preparation_intent_conflict",
                )
        claim = self.hosts.find(intent.claim_request)
        if claim is not None:
            self.hosts.release(
                claim.host_name,
                claim.claim_id,
                claim.generation,
                now=True,
            )
        self.preparations.complete(intent)

    def _complete_preparation(
        self,
        service: ServiceDefinition,
        claim: HostClaim,
        preparing: Deployment,
    ) -> tuple[Deployment, ServiceEndpoint]:
        renewer = _PreparationClaimRenewer(
            hosts=self.hosts,
            deployments=self.deployments,
            deployment=preparing,
            renewal_ttl_seconds=self.lab.lease.renewal_ttl_seconds,
            interval_seconds=self.preparation_renewal_interval_seconds,
            wait_for_interval=self.preparation_waiter,
        )
        endpoint: ServiceEndpoint | None = None
        renewer.start()
        try:
            endpoint = self.runtime.ensure_ready(
                service,
                claim,
                deployment_id=preparing.deployment_id,
            )
            renewal_failure = renewer.stop()
            if renewal_failure is not None:
                raise ModelLabError(
                    f"host claim renewal failed during service start: "
                    f"{renewal_failure}",
                    code="service_claim_renewal_failed",
                ) from renewal_failure
            self._attest_endpoint(service, endpoint)
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
            now_text = format_timestamp(utc_now())
            ready = dataclasses.replace(
                current,
                endpoint_receipt_path=str(endpoint.receipt_path),
                phase="ready",
                updated_at=now_text,
                last_inference_at=now_text,
            )
            self.deployments.publish_ready(ready)
            return ready, endpoint
        except BaseException as original:
            renewal_failure = renewer.stop()
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
                    updated_at=format_timestamp(utc_now()),
                    host_release_mode="now",
                    use_leases=(),
                )
                self.deployments.save(failed)
                self.reconcile_cleanup(service, failed)
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

    def acquire_for_profile(
        self,
        profile: ProfileSelection,
        service: ServiceDefinition,
        *,
        host_name: str | None = None,
        owner_pid: int | None = None,
        owner_start_time: str = "unknown",
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
        self.bindings.attest(profile, service)
        deployment, endpoint = self.ensure_ready(
            service,
            host_name=host_name,
        )
        lease = self.deployments.acquire_use(
            service.service_id,
            expected_workload_sha256=service.workload_sha256,
            owner_pid=owner_pid if owner_pid is not None else os.getpid(),
            owner_start_time=owner_start_time,
        )
        self._attest_endpoint(
            service,
            endpoint,
            required_modalities=profile.required_input_modalities,
        )
        return ServiceUse(
            deployment=dataclasses.replace(
                deployment,
                phase="ready",
                idle_deadline=None,
                use_leases=(*deployment.use_leases, lease),
            ),
            endpoint=endpoint,
            lease=lease,
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
        try:
            deployment, claim = self._current_claim(deployment)
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
        )
        renewer.start()
        endpoint: ServiceEndpoint | None = None
        attestation_error: BaseException | None = None
        try:
            endpoint = self.runtime.attest_ready(service, claim, deployment)
        except BaseException as error:
            attestation_error = error
        renewal_failure = renewer.stop()
        if attestation_error is None and renewal_failure is None:
            assert endpoint is not None
            self._attest_endpoint(service, endpoint)
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
                updated_at=format_timestamp(utc_now()),
            )
            self.deployments.publish_ready(ready)
            return ready

        current = self.deployments.load(service.service_id)
        if current is None or current.deployment_id != deployment.deployment_id:
            raise ModelLabError(
                "preparing deployment changed during recovery",
                code="service_cleanup_required",
            )
        failed = dataclasses.replace(
            current,
            phase="failed",
            updated_at=format_timestamp(utc_now()),
            host_release_mode="now",
            use_leases=(),
        )
        self.deployments.save(failed)
        try:
            return self.reconcile_cleanup(service, failed)
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
    ) -> None:
        result = self.deployments.release_use(
            service.service_id,
            use.lease.lease_id,
            idle_ttl_seconds=self.lab.lease.service_idle_ttl_seconds,
            now=now,
        )
        if result.stop_now:
            self._stop(service, result.deployment, now=True)

    def down(self, service: ServiceDefinition, *, now: bool = False) -> Deployment:
        deployment = self.deployments.begin_idle(
            service.service_id,
            idle_ttl_seconds=self.lab.lease.service_idle_ttl_seconds,
            now=now,
        )
        if now:
            return self._stop(service, deployment, now=True)
        return deployment

    def stop_if_idle_due(self, service: ServiceDefinition) -> bool:
        deployment = self.deployments.begin_idle_cleanup_if_due(
            service.service_id
        )
        if deployment is None:
            return False
        self.reconcile_cleanup(service, deployment)
        return True

    def _stop(
        self,
        service: ServiceDefinition,
        deployment: Deployment,
        *,
        now: bool,
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
        return self.reconcile_cleanup(service, deployment)

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
    ) -> tuple[Deployment, HostClaim]:
        claim = self.hosts.get(deployment.host_name, deployment.claim_id)
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
            )
        return deployment, claim

    def _renew_current_claim(
        self,
        deployment: Deployment,
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
        retained, current_claim = self._current_claim(retained)
        renewed = self.hosts.renew(
            retained.host_name,
            retained.claim_id,
            retained.claim_generation,
            self.lab.lease.renewal_ttl_seconds,
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
    ) -> tuple[Deployment, ServiceEndpoint]:
        active_users = bool(deployment.use_leases)
        self.reconcile_claim_gone(service, deployment)
        if active_users:
            raise ModelLabError(
                "host claim vanished while Pi sessions were active; "
                "their supervisor channels must close before retry",
                code="service_claim_lost",
            )
        return self.ensure_ready(service, host_name=host_name)

    def _recover_unhealthy_runtime_for_ensure(
        self,
        service: ServiceDefinition,
        deployment: Deployment,
        *,
        host_name: str | None,
        attestation_error: BaseException,
    ) -> tuple[Deployment, ServiceEndpoint]:
        active_users = bool(deployment.use_leases)
        try:
            self.down(service, now=True)
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
        return self.ensure_ready(service, host_name=host_name)

    def _drain_quarantined_claim_for_ensure(
        self,
        service: ServiceDefinition,
        deployment: Deployment,
        *,
        host_name: str | None,
    ) -> tuple[Deployment, ServiceEndpoint]:
        active_users = bool(deployment.use_leases)
        self.drain_quarantined_claim(service, deployment)
        if active_users:
            raise ModelLabError(
                "host was quarantined while Pi sessions were active; "
                "their supervisor channels must close before retry",
                code="service_claim_drained",
            )
        return self.ensure_ready(service, host_name=host_name)

    def drain_quarantined_claim(
        self,
        service: ServiceDefinition,
        deployment: Deployment,
    ) -> Deployment:
        """Stop remotely while a quarantined claim still grants authority."""

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
        )
        return self.reconcile_cleanup(service, quiescing)

    def reconcile_claim_gone(
        self,
        service: ServiceDefinition,
        deployment: Deployment,
    ) -> Deployment:
        """Revoke local authority and retire state for a vanished claim."""

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
        )
        if current.phase == "released":
            return current
        try:
            self.runtime.cleanup_lost_claim(service, current)
        except Exception as error:
            retained = dataclasses.replace(
                current,
                updated_at=format_timestamp(utc_now()),
            )
            self.deployments.save(retained)
            raise ModelLabError(
                f"lost host-claim cleanup requires reconciliation: {error}",
                code="service_cleanup_required",
            ) from error
        released = dataclasses.replace(
            current,
            phase="released",
            updated_at=format_timestamp(utc_now()),
        )
        self.deployments.save(released)
        return released

    def reconcile_cleanup(
        self,
        service: ServiceDefinition,
        deployment: Deployment,
    ) -> Deployment:
        """Resume the durable stop-runtime then release-claim transaction."""

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
            return self.reconcile_claim_gone(service, current)

        if current.phase in {"quiescing", "failed"}:
            try:
                current, claim = self._current_claim(current)
                self.runtime.stop(service, claim, current)
            except Exception as error:
                if self.is_claim_gone(error):
                    return self.reconcile_claim_gone(service, current)
                retained = dataclasses.replace(
                    current,
                    updated_at=format_timestamp(utc_now()),
                )
                self.deployments.save(retained)
                raise ModelLabError(
                    f"service runtime cleanup requires reconciliation: {error}",
                    code="service_cleanup_required",
                ) from error
            current = dataclasses.replace(
                current,
                phase="stopping",
                updated_at=format_timestamp(utc_now()),
            )
            self.deployments.save(current)

        try:
            current, claim = self._current_claim(current)
        except Exception as error:
            if not self.is_claim_gone(error):
                raise ModelLabError(
                    f"host claim lookup during cleanup failed: {error}",
                    code="service_cleanup_required",
                ) from error
            return self.reconcile_claim_gone(service, current)

        try:
            result = self.hosts.release(
                claim.host_name,
                claim.claim_id,
                claim.generation,
                now=current.host_release_mode == "now",
            )
            if not result.released:
                raise ModelLabError(
                    "host controller did not release the exact claim",
                    code="host_claim_release_incomplete",
                )
        except Exception as error:
            if self.is_claim_gone(error):
                return self.reconcile_claim_gone(service, current)
            retained = dataclasses.replace(
                current,
                updated_at=format_timestamp(utc_now()),
            )
            self.deployments.save(retained)
            raise ModelLabError(
                f"host claim release requires reconciliation: {error}",
                code="service_cleanup_required",
            ) from error

        released = dataclasses.replace(
            current,
            phase="released",
            updated_at=format_timestamp(utc_now()),
        )
        self.deployments.save(released)
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
