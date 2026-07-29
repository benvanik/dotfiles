"""Stable generic host/claim facade for Runpod consumers."""

from __future__ import annotations

import datetime
import hashlib
import time
import uuid
from collections.abc import Callable
from typing import Any

from .claim_acquisition import ClaimAcquisitionStore
from .claims import (
    ClaimReleaseResult,
    ClaimStore,
    HostClaim,
    HostClaimRequest,
    attest_claim_ledger_receipt,
    claim_admission_reasons,
    claim_id_from_uuid,
    default_allocation_from_host,
    normalize_host_claim_request,
)
from .errors import RunpodLocalError
from .instances import InstanceStore, profile_hash
from .lifecycle import (
    HOST_CONTROLLER_LOCK_SCOPE,
    LifecycleManager,
    TERMINAL_PHASES,
)
from .profile import ProfileStore
from .state import StateStore, validate_record_name
from .timeutil import parse_utc_timestamp, utc_timestamp


DEFAULT_EMPTY_GRACE_SECONDS = 5 * 60
READINESS_POLL_SECONDS = 1.0


def _automatic_host_name(
    request: HostClaimRequest,
    profile_name: str,
) -> str:
    digest = hashlib.sha256(
        (
            request.owner_system
            + "\0"
            + request.owner_instance
            + "\0"
            + request.owner_operation_id
        ).encode("utf-8")
    ).hexdigest()[:12]
    prefix = f"auto-{profile_name}"
    maximum_prefix_length = 63 - 1 - len(digest)
    return f"{prefix[:maximum_prefix_length].rstrip('-')}-{digest}"


def _profile_retention(
    profile: dict[str, Any],
    *,
    requested_mode: str,
) -> tuple[str, int]:
    policy = profile.get("retention")
    if not isinstance(policy, dict):
        policy = profile.get("lifecycle")
    grace = (
        policy.get("empty_grace_seconds", DEFAULT_EMPTY_GRACE_SECONDS)
        if isinstance(policy, dict)
        else DEFAULT_EMPTY_GRACE_SECONDS
    )
    if (
        not isinstance(grace, int)
        or isinstance(grace, bool)
        or grace < 0
    ):
        raise RunpodLocalError(
            "host profile has an invalid empty-host grace",
            code="invalid_profile",
        )
    return requested_mode, grace


def _static_profile_admission_reasons(
    profile: dict[str, Any],
    request: HostClaimRequest,
) -> list[str]:
    """Reject requests that no allocation from this profile could satisfy."""

    pod = profile["pod"]
    gpu_count = pod["gpu_count"]
    reasons: list[str] = []
    if any(device >= gpu_count for device in request.gpu_devices):
        reasons.append("requested GPU device does not exist in the profile")
    if request.gpu_devices:
        requested_per_device = (
            request.gpu_memory_gb / len(request.gpu_devices)
        )
        capacities = pod["gpu_memory_gb_by_type"].values()
        # A profile is one reusable host contract. Every GPU type it may
        # select must satisfy the opaque reservation; otherwise provider stock
        # could turn admission into a billable lottery.
        if any(requested_per_device > capacity for capacity in capacities):
            reasons.append("profile GPU memory may be insufficient")
    if request.ephemeral_disk_gb > pod["container_disk_gb"]:
        reasons.append("profile ephemeral disk is insufficient")
    if request.minimum_remaining_seconds >= request.new_host_hard_ttl_seconds:
        reasons.append("new-host hard lifetime is too short")
    minimum_cpu = pod["min_vcpu_per_gpu"] * gpu_count
    if request.cpu_count > minimum_cpu:
        reasons.append("profile CPU guarantee is insufficient")
    minimum_ram = pod["min_ram_per_gpu"] * gpu_count
    if request.ram_gb > minimum_ram:
        reasons.append("profile RAM guarantee is insufficient")
    return sorted(set(reasons))


def _is_definitive_no_capacity_receipt(
    host: dict[str, Any] | None,
    target: dict[str, Any],
) -> bool:
    if (
        host is None
        or host.get("name") != target["host_name"]
        or host.get("operation_id") != target["host_operation_id"]
        or host.get("profile") != target["profile"]
        or host.get("phase") != "aborted"
        or host.get("pod_id") is not None
        or host.get("provider") is not None
    ):
        return False
    events = host.get("events")
    return isinstance(events, list) and any(
        isinstance(event, dict)
        and event.get("event") == "submission_rejected_no_capacity"
        for event in events
    )


class HostControl:
    """The only supported workload-independent consumer API for Runpod hosts.

    The global controller lock covers host selection, optional creation, claim
    admission, renewal, release, and empty-host retirement. Provider lifecycle
    operations retain their own instance locks underneath this serialization.
    """

    def __init__(
        self,
        *,
        state: StateStore,
        lifecycle: LifecycleManager,
        profiles: ProfileStore,
        clock: Callable[[], datetime.datetime] | None = None,
        uuid_factory: Callable[[], uuid.UUID] | None = None,
        readiness_waiter: Callable[[float], None] | None = None,
    ) -> None:
        self.state = state
        self.lifecycle = lifecycle
        self.profiles = profiles
        self.instances = InstanceStore(state)
        self.claims = ClaimStore(state)
        self.acquisitions = ClaimAcquisitionStore(state)
        self.clock = clock or (
            lambda: datetime.datetime.now(datetime.timezone.utc)
        )
        self.uuid_factory = uuid_factory or uuid.uuid4
        self.readiness_waiter = readiness_waiter or time.sleep

    def _now(self) -> datetime.datetime:
        now = self.clock()
        utc_timestamp(now)
        return now

    def _active_host(self, host_name: str) -> dict[str, Any] | None:
        record = self.instances.load(host_name, required=False)
        if record is None or record["phase"] in TERMINAL_PHASES:
            return None
        if record["phase"] not in {"provisioning", "active"}:
            return None
        if not isinstance(record.get("pod_id"), str):
            return None
        return record

    def _allocation(self, host: dict[str, Any]) -> dict[str, Any]:
        return default_allocation_from_host(host)

    def _attest_ledger_receipt(
        self,
        host: dict[str, Any],
        ledger: dict[str, Any],
    ) -> dict[str, Any]:
        return attest_claim_ledger_receipt(host, ledger)

    def _current_host_ledger(
        self,
        host_name: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        host = self._active_host(host_name)
        ledger = self.claims.load(host_name)
        if ledger is None:
            raise AssertionError("required claim ledger unexpectedly absent")
        if host is None:
            raise RunpodLocalError(
                f"host {host_name} operation is no longer live",
                code="host_claim_host_changed",
            )
        return host, self._attest_ledger_receipt(host, ledger)

    def _record_closed_claims(
        self,
        ledger: dict[str, Any],
        claim_ids: list[str],
        *,
        now: datetime.datetime,
    ) -> None:
        if not claim_ids:
            return
        closed_by_id = {
            claim["claim_id"]: claim
            for claim in ledger["closed_claims"]
        }
        for claim_id in claim_ids:
            closed_claim = closed_by_id.get(claim_id)
            if closed_claim is None:
                raise RunpodLocalError(
                    f"closed host claim is missing from its ledger: "
                    f"{claim_id}",
                    code="invalid_host_claim_record",
                )
            self.acquisitions.close_claim(
                closed_claim,
                ledger=ledger,
                now=now,
            )

    def _expire_claims(
        self,
        ledger: dict[str, Any],
        *,
        now: datetime.datetime,
    ) -> tuple[dict[str, Any], list[str]]:
        ledger, expired_claim_ids = self.claims.expire_claims(
            ledger,
            now=now,
        )
        self.acquisitions.reconcile_closed_claims(
            [ledger],
            now=now,
        )
        return ledger, expired_claim_ids

    def _expire_current_ledger(
        self,
        ledger: dict[str, Any],
        *,
        now: datetime.datetime,
    ) -> list[str]:
        """Reconcile one ledger without assigning it fleet-wide authority."""

        if ledger["operation_end"] is not None:
            self.acquisitions.reconcile_closed_claims(
                [ledger],
                now=now,
            )
            return []
        host = self.instances.load(
            ledger["host_name"],
            required=False,
        )
        exact_operation = (
            host is not None
            and host["operation_id"] == ledger["host_operation_id"]
            and host.get("pod_id") == ledger["pod_id"]
        )
        operation_ended = (
            host is not None
            and (
                host["operation_id"] != ledger["host_operation_id"]
                or (
                    host["operation_id"]
                    == ledger["host_operation_id"]
                    and host["phase"] in TERMINAL_PHASES
                )
            )
        )
        if operation_ended:
            ledger, _ = self.claims.close_host_operation(
                ledger,
                now=now,
            )
            # The ledger is the durable closure outbox. Reconcile every
            # entry, not just claims closed in this pass, so an earlier
            # expiry-to-journal crash is repaired at the same terminal
            # boundary.
            self.acquisitions.reconcile_closed_claims(
                [ledger],
                now=now,
            )
            return []
        if ledger["claims"]:
            if (
                not exact_operation
                or host is None
                or host["phase"] not in {"provisioning", "active"}
            ):
                # A missing or nonterminal recovery receipt is not proof
                # that the exact Pod stopped. Keep its claims isolated from
                # fleet-wide placement; exact callers still receive
                # host_claim_host_changed.
                return []
            ledger = self._attest_ledger_receipt(host, ledger)
        _, expired_claim_ids = self._expire_claims(
            ledger,
            now=now,
        )
        return expired_claim_ids

    def _expire_all_current_claims(
        self,
        *,
        now: datetime.datetime,
    ) -> tuple[
        dict[str, list[str]],
        dict[str, RunpodLocalError],
    ]:
        expired_by_host: dict[str, list[str]] = {}
        errors_by_host: dict[str, RunpodLocalError] = {}
        for scanned in self.claims.scan():
            if scanned.error is not None or scanned.value is None:
                # Fleet-wide placement must not let one unrelated record hide
                # every healthy host. Exact access and doctor retain the
                # malformed-record error.
                continue
            ledger = scanned.value
            try:
                expired_claim_ids = self._expire_current_ledger(
                    ledger,
                    now=now,
                )
            except RunpodLocalError as error:
                errors_by_host[ledger["host_name"]] = error
                continue
            if expired_claim_ids:
                expired_by_host[ledger["host_name"]] = expired_claim_ids
        return expired_by_host, errors_by_host

    def _ledger_for_host(
        self,
        host: dict[str, Any],
        *,
        requested_retention: str | None,
        now: datetime.datetime,
    ) -> dict[str, Any]:
        receipt_retention = host.get("retention")
        if not isinstance(receipt_retention, dict):
            raise RunpodLocalError(
                f"host {host['name']} has no retention receipt",
                code="invalid_host_receipt",
            )
        retention_mode = receipt_retention.get("mode")
        grace = receipt_retention.get("empty_grace_seconds")
        if requested_retention is not None and (
            requested_retention != retention_mode
        ):
            raise RunpodLocalError(
                f"host {host['name']} has a different retention policy",
                code="host_retention_conflict",
            )
        allocation = self._allocation(host)
        existing = self.claims.load(host["name"], required=False)
        if existing is not None:
            if (
                existing["host_operation_id"] == host["operation_id"]
                and existing["pod_id"] == host["pod_id"]
            ):
                return self._attest_ledger_receipt(host, existing)
            if existing["claims"]:
                raise RunpodLocalError(
                    f"host {host['name']} changed operation while claims remain",
                    code="host_operation_conflict",
                )
            # The old ledger is a durable closure outbox. Flush it before a
            # replacement operation can carry and eventually prune the bounded
            # closed-claim collection.
            self._record_closed_claims(
                existing,
                [
                    claim["claim_id"]
                    for claim in existing["closed_claims"]
                ],
                now=now,
            )
        return self.claims.initialize(
            host=host,
            allocation=allocation,
            retention=retention_mode,
            empty_grace_seconds=grace,
            now=(
                parse_utc_timestamp(host["created_at"])
                if retention_mode == "while-claimed"
                else now
            ),
        )

    def _find_idempotent(
        self,
        request: HostClaimRequest,
    ) -> HostClaim | None:
        acquisition = self.acquisitions.load(request)
        strict_host_names: set[str] = set()
        if request.host_name is not None:
            strict_host_names.add(request.host_name)
        if acquisition is not None:
            strict_host_names.update(
                binding["host_name"]
                for binding in (
                    acquisition["target"],
                    acquisition["host"],
                    acquisition["claim"],
                )
                if binding is not None
            )
        if acquisition is not None and acquisition["claim"] is not None:
            binding = acquisition["claim"]
            exact_ledger = self.claims.load(
                binding["host_name"],
                required=True,
            )
            if exact_ledger is None:
                raise AssertionError(
                    "bound claim ledger unexpectedly absent"
                )
            if (
                exact_ledger["host_operation_id"]
                != binding["host_operation_id"]
                or exact_ledger["pod_id"] != binding["pod_id"]
            ):
                raise RunpodLocalError(
                    "owner operation claim ledger differs from its "
                    "acquisition journal",
                    code="host_claim_acquisition_drift",
                )
        closed = self.claims.find_closed_owner_operation(
            request,
            strict_host_names=strict_host_names,
        )
        if closed is not None:
            closed_ledger, claim = closed
            if claim["request_sha256"] != request.sha256():
                raise RunpodLocalError(
                    "owner operation already names a different closed claim",
                    code="host_claim_operation_conflict",
                )
            self.acquisitions.close_claim(
                claim,
                ledger=closed_ledger,
                now=self._now(),
            )
            raise RunpodLocalError(
                "owner operation already completed a claim",
                code="host_claim_operation_closed",
            )
        existing = self.claims.find_owner_operation(
            request,
            strict_host_names=strict_host_names,
        )
        if existing is None:
            if (
                acquisition is not None
                and acquisition["claim"] is not None
            ):
                raise RunpodLocalError(
                    "owner operation already completed or lost its bound claim",
                    code="host_claim_operation_closed",
                )
            return None
        ledger, claim = existing
        if claim["request_sha256"] != request.sha256():
            raise RunpodLocalError(
                "owner operation already names a different claim request",
                code="host_claim_operation_conflict",
            )
        if ledger["quarantine"] is not None:
            raise RunpodLocalError(
                f"host {ledger['host_name']} is quarantined and cannot reuse "
                "an existing claim acquisition",
                code="host_claim_quarantined",
            )
        host = self._active_host(ledger["host_name"])
        if (
            host is None
            or host["operation_id"] != ledger["host_operation_id"]
            or host["pod_id"] != ledger["pod_id"]
        ):
            raise RunpodLocalError(
                "idempotent claim no longer has its exact live host",
                code="host_claim_host_changed",
            )
        self._attest_ledger_receipt(host, ledger)
        result = HostClaim.from_documents(ledger, claim)
        if acquisition is not None:
            expected_target = {
                "host_name": result.host_name,
                "host_operation_id": result.operation_id,
                "predecessor_operation_id": (
                    acquisition["target"]["predecessor_operation_id"]
                    if acquisition["target"] is not None
                    else None
                ),
                "profile": {
                    "name": result.profile_name,
                    "sha256": result.profile_sha256,
                },
            }
            expected_host = {
                "host_name": result.host_name,
                "host_operation_id": result.operation_id,
                "pod_id": result.provider_resource_id,
            }
            expected_claim = {
                "host_name": result.host_name,
                "host_operation_id": result.operation_id,
                "pod_id": result.provider_resource_id,
                "claim_id": result.claim_id,
            }
            if (
                acquisition["target"] is not None
                and acquisition["target"] != expected_target
            ) or (
                acquisition["host"] is not None
                and acquisition["host"] != expected_host
            ) or (
                acquisition["claim"] is not None
                and acquisition["claim"] != expected_claim
            ):
                raise RunpodLocalError(
                    "owner operation acquisition names a different claim",
                    code="host_claim_acquisition_drift",
                )
        return result

    def _candidate_ledgers(
        self,
        request: HostClaimRequest,
        *,
        now: datetime.datetime,
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        records = (
            [self._active_host(request.host_name)]
            if request.host_name is not None
            else [
                scanned.value
                for scanned in self.instances.scan()
                if scanned.error is None
                and scanned.value is not None
                and scanned.value["phase"] == "active"
                and scanned.value["profile"]["name"]
                in request.allowed_profile_names
            ]
        )
        if request.host_name is None:
            records = [
                record
                for record in records
                if record is not None
                and record["profile"]["name"]
                in request.allowed_profile_names
            ]
        if request.host_name is None:
            recorded_names = {
                record["name"]
                for record in records
                if record is not None
            }
            for profile_name in request.allowed_profile_names:
                automatic_name = _automatic_host_name(
                    request,
                    profile_name,
                )
                automatic_host = self._active_host(automatic_name)
                if (
                    automatic_host is not None
                    and automatic_name not in recorded_names
                ):
                    records.append(automatic_host)
                    recorded_names.add(automatic_name)
        candidates = []
        for host in records:
            if host is None:
                continue
            if (
                host["profile"]["name"]
                not in request.allowed_profile_names
            ):
                if request.host_name is not None:
                    raise RunpodLocalError(
                        f"host {host['name']} profile is not allowed by this "
                        "claim request",
                        code="host_profile_not_allowed",
                    )
                continue
            try:
                current_profile = self.profiles.load(
                    host["profile"]["name"]
                )
            except RunpodLocalError:
                if request.host_name is None:
                    continue
                raise
            if profile_hash(current_profile) != host["profile"]["sha256"]:
                if request.host_name is not None:
                    raise RunpodLocalError(
                        f"host {host['name']} profile differs from its "
                        "current authored definition",
                        code="host_profile_drift",
                    )
                continue
            try:
                ledger = self._ledger_for_host(
                    host,
                    requested_retention=None,
                    now=now,
                )
                ledger, _ = self._expire_claims(ledger, now=now)
            except RunpodLocalError:
                if request.host_name is not None:
                    raise
                continue
            admission_reasons = claim_admission_reasons(
                ledger,
                request,
                now=now,
            )
            if (
                request.host_name is not None
                and ledger["quarantine"] is not None
            ):
                raise RunpodLocalError(
                    f"host {host['name']} is quarantined because cleanup for "
                    "an expired claim is unproven; retire this exact host "
                    "operation before reusing the name",
                    code="host_claim_quarantined",
                )
            if not admission_reasons:
                candidates.append((host, ledger))
        return sorted(
            candidates,
            key=lambda item: (
                item[0].get("quoted_total_price_per_hour", float("inf")),
                item[0]["name"],
            ),
        )

    def _finish_rejected_host_cleanup(
        self,
        *,
        host_name: str,
        operation_id: str,
    ) -> None:
        try:
            self.lifecycle.terminate(
                host_name,
                execute=True,
                reason="claim_launch_allocation_rejected",
                expected_operation_id=operation_id,
            )
        except RunpodLocalError as error:
            raise RunpodLocalError(
                f"rejected host {host_name} still requires exact Pod cleanup",
                code="rollback_required",
            ) from error
        raise RunpodLocalError(
            f"new host {host_name} violated its allocation policy and was "
            "deleted",
            code="allocation_rejected",
        )

    def _advance_no_capacity_target(
        self,
        request: HostClaimRequest,
        acquisition: dict[str, Any],
        host: dict[str, Any] | None,
    ) -> dict[str, Any]:
        target = acquisition["target"]
        if (
            target is None
            or acquisition["host"] is not None
            or acquisition["claim"] is not None
            or not _is_definitive_no_capacity_receipt(host, target)
        ):
            raise RunpodLocalError(
                "no-capacity rejection does not match the exact unbound "
                "acquisition target",
                code="host_claim_acquisition_drift",
            )
        return self.acquisitions.advance_rejected_target(
            request,
            rejected_host_operation_id=target["host_operation_id"],
            new_host_operation_id=self.lifecycle.new_operation_id(),
            now=self._now(),
        )

    def _create_host(
        self,
        request: HostClaimRequest,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        acquisition = self.acquisitions.load(request, required=True)
        if acquisition is None:
            raise AssertionError(
                "required claim acquisition unexpectedly absent"
            )
        target = acquisition["target"]
        profile_names = request.allowed_profile_names
        if target is not None:
            target_profile_name = target["profile"]["name"]
            if target_profile_name not in profile_names:
                raise RunpodLocalError(
                    "acquisition target profile is no longer allowed",
                    code="host_claim_acquisition_drift",
                )
            profile_names = profile_names[
                profile_names.index(target_profile_name) :
            ]
        ineligible_profiles: list[str] = []
        for profile_name in profile_names:
            profile = self.profiles.load(profile_name)
            profile_identity = {
                "name": profile_name,
                "sha256": profile_hash(profile),
            }
            if target is not None and target["profile"] != profile_identity:
                raise RunpodLocalError(
                    "acquisition target profile differs from its current "
                    "authored definition",
                    code="host_profile_drift",
                )
            static_reasons = _static_profile_admission_reasons(
                profile,
                request,
            )
            if static_reasons:
                if target is not None:
                    raise RunpodLocalError(
                        "acquisition target can no longer admit its exact "
                        "request: "
                        + "; ".join(static_reasons),
                        code="no_eligible_host_profile",
                    )
                ineligible_profiles.append(
                    f"{profile_name} ({'; '.join(static_reasons)})"
                )
                continue
            if target is None:
                host_name = request.host_name or _automatic_host_name(
                    request,
                    profile_name,
                )
                existing_host = self.instances.load(
                    host_name,
                    required=False,
                )
                if (
                    existing_host is not None
                    and existing_host["phase"] not in TERMINAL_PHASES
                ):
                    ineligible_profiles.append(
                        f"{profile_name} (named host is already live but "
                        "cannot admit this request)"
                    )
                    continue
                acquisition = self.acquisitions.select_target(
                    request,
                    host_name=host_name,
                    host_operation_id=self.lifecycle.new_operation_id(),
                    predecessor_operation_id=(
                        existing_host["operation_id"]
                        if existing_host is not None
                        else None
                    ),
                    profile=profile_identity,
                    now=self._now(),
                )
                target = acquisition["target"]
            else:
                host_name = target["host_name"]
            _, grace = _profile_retention(
                profile,
                requested_mode=request.new_host_retention,
            )
            current_host = self.instances.load(
                host_name,
                required=False,
            )
            bound_host = acquisition["host"]
            if (
                bound_host is None
                and target is not None
                and _is_definitive_no_capacity_receipt(
                    current_host,
                    target,
                )
            ):
                acquisition = self._advance_no_capacity_target(
                    request,
                    acquisition,
                    current_host,
                )
                target = acquisition["target"]
            if bound_host is not None and (
                current_host is None
                or current_host["operation_id"]
                != bound_host["host_operation_id"]
                or current_host.get("pod_id") != bound_host["pod_id"]
            ):
                raise RunpodLocalError(
                    "acquisition provider host no longer has its exact receipt",
                    code="host_claim_acquisition_terminal",
                )
            if bound_host is None:
                exact_target = (
                    current_host is not None
                    and current_host["operation_id"]
                    == target["host_operation_id"]
                )
                exact_predecessor = (
                    current_host is not None
                    and target["predecessor_operation_id"] is not None
                    and current_host["operation_id"]
                    == target["predecessor_operation_id"]
                    and current_host["phase"] in TERMINAL_PHASES
                )
                exact_absence = (
                    current_host is None
                    and target["predecessor_operation_id"] is None
                )
                if not (
                    exact_target or exact_predecessor or exact_absence
                ):
                    raise RunpodLocalError(
                        "acquisition launch boundary no longer names its "
                        "exact target or predecessor operation",
                        code="host_claim_acquisition_terminal",
                    )
            if (
                current_host is not None
                and current_host["operation_id"]
                == target["host_operation_id"]
                and current_host["phase"] == "rollback_required"
            ):
                self._finish_rejected_host_cleanup(
                    host_name=host_name,
                    operation_id=target["host_operation_id"],
                )
            if (
                current_host is not None
                and current_host["operation_id"]
                == target["host_operation_id"]
                and current_host["profile"] != profile_identity
            ):
                raise RunpodLocalError(
                    "acquisition target names a host with another profile",
                    code="host_claim_acquisition_drift",
                )
            if (
                current_host is not None
                and current_host["operation_id"]
                == target["host_operation_id"]
                and current_host["phase"] in TERMINAL_PHASES
            ):
                raise RunpodLocalError(
                    "acquisition provider host is already terminal; a new "
                    "owner operation is required for another Pod",
                    code="host_claim_acquisition_terminal",
                )
            exact_target_host = (
                current_host is not None
                and current_host["operation_id"]
                == target["host_operation_id"]
                and isinstance(current_host.get("pod_id"), str)
            )
            if bound_host is None and exact_target_host:
                acquisition = self.acquisitions.bind_host(
                    request,
                    current_host,
                    now=self._now(),
                )
                bound_host = acquisition["host"]
            resumed_active_host = (
                bound_host is not None
                and exact_target_host
                and current_host["phase"] == "active"
            )
            if resumed_active_host:
                host = current_host
            else:
                try:
                    host = self.lifecycle.launch(
                        host_name,
                        profile,
                        ttl_seconds=request.new_host_hard_ttl_seconds,
                        idle_timeout_seconds=None,
                        retention_mode=request.new_host_retention,
                        empty_grace_seconds=grace,
                        target_operation_id=target["host_operation_id"],
                        predecessor_operation_id=target[
                            "predecessor_operation_id"
                        ],
                    )
                except RunpodLocalError as error:
                    if error.code == "rollback_required":
                        self._finish_rejected_host_cleanup(
                            host_name=host_name,
                            operation_id=target["host_operation_id"],
                        )
                    if error.code == "no_provider_capacity":
                        rejected_host = self.instances.load(
                            host_name,
                            required=False,
                        )
                        self._advance_no_capacity_target(
                            request,
                            acquisition,
                            rejected_host,
                        )
                        raise
                    if error.code != "no_eligible_gpu":
                        raise
                    ineligible_profiles.append(profile_name)
                    acquisition = (
                        self.acquisitions.clear_unsubmitted_target(
                            request,
                            now=self._now(),
                        )
                    )
                    target = acquisition["target"]
                    continue
            if isinstance(host.get("pod_id"), str):
                acquisition = self.acquisitions.bind_host(
                    request,
                    host,
                    now=self._now(),
                )
            if host["phase"] not in {"provisioning", "active"} or not isinstance(
                host.get("pod_id"), str
            ):
                raise RunpodLocalError(
                    f"new host {host_name} has not acquired a Pod identity",
                    code="host_not_claimable_yet",
                )
            now = self._now()
            ledger = self._ledger_for_host(
                host,
                requested_retention=(
                    None
                    if resumed_active_host
                    else request.new_host_retention
                ),
                now=now,
            )
            admission_reasons = claim_admission_reasons(
                ledger,
                request,
                now=now,
            )
            if admission_reasons:
                if resumed_active_host:
                    raise RunpodLocalError(
                        "bound acquisition host can no longer admit its exact "
                        "request: "
                        + "; ".join(admission_reasons),
                        code="host_claim_not_admitted",
                    )
                self.lifecycle.terminate(
                    host_name,
                    execute=True,
                    reason="new_host_claim_not_admitted",
                    expected_operation_id=host["operation_id"],
                )
                raise RunpodLocalError(
                    "new host cannot admit its exact acquisition request: "
                    + "; ".join(admission_reasons),
                    code="no_eligible_host_profile",
                )
            return host, ledger
        raise RunpodLocalError(
            "no allowed host profile has an eligible live GPU: "
            + ", ".join(ineligible_profiles),
            code="no_eligible_host_profile",
        )

    def _await_active(
        self,
        request: HostClaimRequest,
        claim: HostClaim,
    ) -> HostClaim:
        """Reconcile one claimed Pod to active without monopolizing claim CAS."""

        while True:
            with self.state.locked(HOST_CONTROLLER_LOCK_SCOPE):
                now = self._now()
                host, ledger = self._current_host_ledger(claim.host_name)
                ledger, expired_claim_ids = self._expire_claims(
                    ledger,
                    now=now,
                )
                if claim.claim_id in expired_claim_ids:
                    raise RunpodLocalError(
                        f"host claim already expired: {claim.claim_id}",
                        code="host_claim_expired",
                    )
                if ledger["quarantine"] is not None:
                    raise RunpodLocalError(
                        f"host {claim.host_name} became quarantined before "
                        "claim acquisition completed",
                        code="host_claim_quarantined",
                    )
                matches = [
                    value
                    for value in ledger["claims"]
                    if value["claim_id"] == claim.claim_id
                ]
                if len(matches) != 1:
                    raise RunpodLocalError(
                        f"host claim does not exist: {claim.claim_id}",
                        code="host_claim_not_found",
                    )
                claim = HostClaim.from_documents(ledger, matches[0])
                if host["phase"] == "active":
                    provider_remaining = (
                        parse_utc_timestamp(
                            ledger["provider_termination_at"]
                        )
                        - now
                    ).total_seconds()
                    if (
                        provider_remaining
                        < request.minimum_remaining_seconds
                    ):
                        release = self.claims.release(
                            claim.host_name,
                            claim.claim_id,
                            expected_generation=claim.generation,
                            now=now,
                            retire_now=False,
                        )
                        closed_ledger = self.claims.load(claim.host_name)
                        if closed_ledger is None:
                            raise AssertionError(
                                "released claim ledger unexpectedly absent"
                            )
                        self._record_closed_claims(
                            closed_ledger,
                            [claim.claim_id],
                            now=now,
                        )
                        if release.retirement_due:
                            self.lifecycle.terminate(
                                claim.host_name,
                                execute=True,
                                reason="claim_minimum_lifetime_elapsed",
                                expected_operation_id=claim.operation_id,
                            )
                        raise RunpodLocalError(
                            f"host {claim.host_name} became active with less "
                            "than the requested useful hard lifetime",
                            code="host_minimum_lifetime_elapsed",
                        )
                    return claim
                if host["phase"] != "provisioning":
                    raise RunpodLocalError(
                        f"claimed host {host['name']} cannot become ready from "
                        f"phase {host['phase']}",
                        code="host_not_claimable_yet",
                    )
                renewal_remaining = (
                    parse_utc_timestamp(claim.renewal_deadline) - now
                ).total_seconds()
                renewal_threshold = max(
                    1.0,
                    request.renewal_ttl_seconds / 2,
                )
                if renewal_remaining <= renewal_threshold:
                    claim = self.claims.renew(
                        claim.host_name,
                        claim.claim_id,
                        expected_generation=claim.generation,
                        renewal_ttl_seconds=request.renewal_ttl_seconds,
                        now=now,
                    )
                profile_identity = dict(host["profile"])
                lease_request = host.get("lease_request")
                retention = host.get("retention")
                if (
                    not isinstance(lease_request, dict)
                    or type(lease_request.get("ttl_seconds")) is not int
                    or (
                        lease_request.get("idle_timeout_seconds") is not None
                        and type(
                            lease_request.get("idle_timeout_seconds")
                        )
                        is not int
                    )
                    or not isinstance(retention, dict)
                ):
                    raise RunpodLocalError(
                        f"claimed host {host['name']} has incomplete launch "
                        "receipts",
                        code="invalid_host_receipt",
                    )

            profile = self.profiles.load(profile_identity["name"])
            if profile_hash(profile) != profile_identity["sha256"]:
                raise RunpodLocalError(
                    f"host {claim.host_name} profile differs from its "
                    "current authored definition",
                    code="host_profile_drift",
                )
            advanced = self.lifecycle.launch(
                claim.host_name,
                profile,
                ttl_seconds=lease_request["ttl_seconds"],
                idle_timeout_seconds=lease_request[
                    "idle_timeout_seconds"
                ],
                retention_mode=retention["mode"],
                empty_grace_seconds=retention["empty_grace_seconds"],
                expected_operation_id=claim.operation_id,
            )
            if advanced["phase"] == "active":
                continue
            if advanced["phase"] != "provisioning":
                raise RunpodLocalError(
                    f"claimed host {claim.host_name} entered "
                    f"{advanced['phase']} while provisioning",
                    code="host_not_claimable_yet",
                )
            self.readiness_waiter(READINESS_POLL_SECONDS)

    def acquire(self, request: HostClaimRequest | Any) -> HostClaim:
        request = normalize_host_claim_request(request)
        with self.state.locked(HOST_CONTROLLER_LOCK_SCOPE):
            now = self._now()
            self.acquisitions.begin(request, now=now)
            self._expire_all_current_claims(now=now)
            existing = self._find_idempotent(request)
            if existing is not None:
                claim = existing
            else:
                acquisition = self.acquisitions.load(
                    request,
                    required=True,
                )
                if acquisition is None:
                    raise AssertionError(
                        "required claim acquisition unexpectedly absent"
                    )
                candidates = (
                    []
                    if acquisition["target"] is not None
                    else self._candidate_ledgers(request, now=now)
                )
                if candidates:
                    host, ledger = candidates[0]
                    self.acquisitions.select_target(
                        request,
                        host_name=host["name"],
                        host_operation_id=host["operation_id"],
                        predecessor_operation_id=None,
                        profile=host["profile"],
                        now=self._now(),
                    )
                    self.acquisitions.bind_host(
                        request,
                        host,
                        now=self._now(),
                    )
                else:
                    if not request.create_if_missing:
                        target = (
                            f"host {request.host_name}"
                            if request.host_name is not None
                            else "any allowed host"
                        )
                        raise RunpodLocalError(
                            f"{target} cannot admit the requested claim",
                            code="no_compatible_host",
                        )
                    _, ledger = self._create_host(request)
                now = self._now()
                ledger, _ = self._expire_claims(ledger, now=now)
                claim = self.claims.admit(
                    ledger,
                    request,
                    now=now,
                    claim_id=claim_id_from_uuid(self.uuid_factory()),
                )
            self.acquisitions.bind(
                request,
                claim,
                now=self._now(),
            )
        return self._await_active(request, claim)

    def find(self, request: HostClaimRequest | Any) -> HostClaim | None:
        """Return one exact active owner operation without creating a claim."""

        request = normalize_host_claim_request(request)
        with self.state.locked(HOST_CONTROLLER_LOCK_SCOPE):
            now = self._now()
            self._expire_all_current_claims(now=now)
            return self._find_idempotent(request)

    def renew(
        self,
        host_name: str,
        claim_id: str,
        expected_generation: int,
        renewal_ttl_seconds: int,
    ) -> HostClaim:
        with self.state.locked(HOST_CONTROLLER_LOCK_SCOPE):
            now = self._now()
            _, ledger = self._current_host_ledger(host_name)
            ledger, expired_claim_ids = self._expire_claims(
                ledger,
                now=now,
            )
            if claim_id in expired_claim_ids:
                raise RunpodLocalError(
                    f"host claim already expired: {claim_id}",
                    code="host_claim_expired",
                )
            return self.claims.renew(
                host_name,
                claim_id,
                expected_generation=expected_generation,
                renewal_ttl_seconds=renewal_ttl_seconds,
                now=now,
            )

    def release(
        self,
        host_name: str,
        claim_id: str,
        expected_generation: int,
        *,
        now: bool = False,
    ) -> ClaimReleaseResult:
        with self.state.locked(HOST_CONTROLLER_LOCK_SCOPE):
            current_time = self._now()
            _, ledger = self._current_host_ledger(host_name)
            ledger, expired_claim_ids = self._expire_claims(
                ledger,
                now=current_time,
            )
            if claim_id in expired_claim_ids:
                raise RunpodLocalError(
                    f"host claim already expired: {claim_id}",
                    code="host_claim_expired",
                )
            already_closed = [
                claim
                for claim in ledger["closed_claims"]
                if claim["claim_id"] == claim_id
            ]
            if already_closed:
                self._record_closed_claims(
                    ledger,
                    [claim_id],
                    now=current_time,
                )
            result = self.claims.release(
                host_name,
                claim_id,
                expected_generation=expected_generation,
                now=current_time,
                retire_now=now,
            )
            closed_ledger = self.claims.load(host_name)
            if closed_ledger is None:
                raise AssertionError(
                    "released claim ledger unexpectedly absent"
                )
            self._record_closed_claims(
                closed_ledger,
                [claim_id],
                now=current_time,
            )
            if result.retirement_due:
                self.lifecycle.terminate(
                    host_name,
                    execute=True,
                    reason=(
                        "last_claim_released_now"
                        if now
                        else "quarantined_host_claims_closed"
                    ),
                    expected_operation_id=ledger["host_operation_id"],
                )
            return result

    def get(self, host_name: str, claim_id: str) -> HostClaim:
        validate_record_name(host_name)
        with self.state.locked(HOST_CONTROLLER_LOCK_SCOPE):
            _, ledger = self._current_host_ledger(host_name)
            ledger, expired_claim_ids = self._expire_claims(
                ledger,
                now=self._now(),
            )
            if claim_id in expired_claim_ids:
                raise RunpodLocalError(
                    f"host claim already expired: {claim_id}",
                    code="host_claim_expired",
                )
            matches = [
                claim
                for claim in ledger["claims"]
                if claim["claim_id"] == claim_id
            ]
            if len(matches) != 1:
                raise RunpodLocalError(
                    f"host claim does not exist: {claim_id}",
                    code="host_claim_not_found",
                )
            return HostClaim.from_documents(ledger, matches[0])

    def list(self, host_name: str | None = None) -> list[HostClaim]:
        if host_name is not None:
            validate_record_name(host_name)
        with self.state.locked(HOST_CONTROLLER_LOCK_SCOPE):
            now = self._now()
            ledgers = (
                [self.claims.load(host_name)]
                if host_name is not None
                else [
                    scanned.value
                    for scanned in self.claims.scan()
                    if scanned.error is None
                    and scanned.value is not None
                ]
            )
            current_ledgers = []
            for ledger in ledgers:
                if ledger is not None:
                    try:
                        if ledger["claims"]:
                            _, ledger = self._current_host_ledger(
                                ledger["host_name"]
                            )
                        ledger, _ = self._expire_claims(
                            ledger,
                            now=now,
                        )
                    except RunpodLocalError:
                        if host_name is not None:
                            raise
                        continue
                    current_ledgers.append(ledger)
            return [
                HostClaim.from_documents(ledger, claim)
                for ledger in current_ledgers
                for claim in ledger["claims"]
            ]

    def status(self, host_name: str) -> dict[str, Any]:
        validate_record_name(host_name)
        with self.state.locked(HOST_CONTROLLER_LOCK_SCOPE):
            host = self.instances.load(host_name, required=False)
            ledger = self.claims.load(host_name, required=False)
            if ledger is not None:
                if host is not None:
                    self._attest_ledger_receipt(host, ledger)
                if ledger["claims"]:
                    _, ledger = self._current_host_ledger(host_name)
                ledger, expired_claim_ids = self._expire_claims(
                    ledger,
                    now=self._now(),
                )
            else:
                expired_claim_ids = []
            return {
                "schema_version": "runpod.host-status.v1",
                "host_name": host_name,
                "host": host,
                "claim_ledger": ledger,
                "expired_claim_ids": expired_claim_ids,
            }

    def enforce_retirement(self, *, execute: bool) -> dict[str, Any]:
        """Expire stale claims and plan or execute empty-host retirement."""

        if type(execute) is not bool:
            raise RunpodLocalError(
                "retirement execution selector must be boolean",
                code="invalid_host_claim",
            )
        with self.state.locked(HOST_CONTROLLER_LOCK_SCOPE):
            now = self._now()
            previously_expired: dict[str, list[str]] = {}
            sweep_errors: dict[str, RunpodLocalError] = {}
            if execute:
                (
                    previously_expired,
                    sweep_errors,
                ) = self._expire_all_current_claims(
                    now=now,
                )
            actions: list[dict[str, Any]] = []
            instance_errors = set()
            hosts = []
            for scanned in self.instances.scan():
                if scanned.error is None:
                    if scanned.value is not None:
                        hosts.append(scanned.value)
                    continue
                instance_errors.add(scanned.name)
                actions.append(
                    {
                        "host_name": scanned.name,
                        "host_operation_id": None,
                        "expired_claim_ids": [],
                        "remaining_claim_count": None,
                        "quarantine": None,
                        "operation_end": None,
                        "manual_action_required": False,
                        "retire_at": None,
                        "due": False,
                        "executed": False,
                        "error": {
                            "code": scanned.error.code,
                            "message": str(scanned.error),
                        },
                    }
                )
            ledgers = []
            invalid_claim_record_names = set()
            claim_ledgers_by_name: dict[str, dict[str, Any]] = {}
            for scanned in self.claims.scan():
                if scanned.error is None:
                    if (
                        scanned.value is not None
                        and scanned.name not in instance_errors
                    ):
                        ledgers.append(scanned.value)
                        claim_ledgers_by_name[scanned.name] = scanned.value
                    continue
                invalid_claim_record_names.add(scanned.name)
                actions.append(
                    {
                        "host_name": scanned.name,
                        "host_operation_id": None,
                        "expired_claim_ids": [],
                        "remaining_claim_count": None,
                        "quarantine": None,
                        "operation_end": None,
                        "manual_action_required": False,
                        "retire_at": None,
                        "due": False,
                        "executed": False,
                        "error": {
                            "code": scanned.error.code,
                            "message": str(scanned.error),
                        },
                    }
                )
            # A process can die after the provider returns a Pod but before
            # the first claim is admitted. Recover those exact receipts into
            # an already-aging empty ledger so the crash cannot defer grace
            # until an observer happens to notice it.
            for host in hosts:
                recorded_ledger = claim_ledgers_by_name.get(host["name"])
                has_current_ledger = (
                    recorded_ledger is not None
                    and recorded_ledger["host_operation_id"]
                    == host["operation_id"]
                    and recorded_ledger["pod_id"] == host.get("pod_id")
                )
                if (
                    host["phase"] not in {"provisioning", "active"}
                    or not isinstance(host.get("pod_id"), str)
                    or host["retention"]["mode"] != "while-claimed"
                    or host["name"] in invalid_claim_record_names
                    or has_current_ledger
                ):
                    continue
                created_at = parse_utc_timestamp(host["created_at"])
                if not execute:
                    retire_at = created_at + datetime.timedelta(
                        seconds=host["retention"][
                            "empty_grace_seconds"
                        ]
                    )
                    actions.append(
                        {
                            "host_name": host["name"],
                            "host_operation_id": host["operation_id"],
                            "expired_claim_ids": [],
                            "remaining_claim_count": 0,
                            "quarantine": None,
                            "operation_end": None,
                            "manual_action_required": False,
                            "retire_at": utc_timestamp(retire_at),
                            "due": now >= retire_at,
                            "executed": False,
                        }
                    )
                    continue
                try:
                    recovered = self._ledger_for_host(
                        host,
                        requested_retention="while-claimed",
                        now=created_at,
                    )
                except RunpodLocalError as error:
                    actions.append(
                        {
                            "host_name": host["name"],
                            "host_operation_id": host["operation_id"],
                            "expired_claim_ids": [],
                            "remaining_claim_count": 0,
                            "quarantine": None,
                            "operation_end": None,
                            "manual_action_required": False,
                            "retire_at": None,
                            "due": False,
                            "executed": False,
                            "error": {
                                "code": error.code,
                                "message": str(error),
                            },
                        }
                    )
                    continue
                ledgers = [
                    ledger
                    for ledger in ledgers
                    if ledger["host_name"] != host["name"]
                ]
                claim_ledgers_by_name[host["name"]] = recovered
                ledgers.append(recovered)
            for ledger in ledgers:
                action: dict[str, Any] = {
                    "host_name": ledger["host_name"],
                    "host_operation_id": ledger["host_operation_id"],
                    "expired_claim_ids": previously_expired.get(
                        ledger["host_name"],
                        [],
                    ),
                    "remaining_claim_count": len(ledger["claims"]),
                    "quarantine": ledger["quarantine"],
                    "operation_end": ledger["operation_end"],
                    "manual_action_required": (
                        ledger["quarantine"] is not None
                        and ledger["retention"]["mode"] == "manual"
                        and ledger.get("retire_at") is None
                    ),
                    "retire_at": ledger.get("retire_at"),
                    "due": False,
                    "executed": False,
                }
                if ledger["operation_end"] is not None:
                    sweep_error = sweep_errors.get(ledger["host_name"])
                    if sweep_error is not None:
                        action["error"] = {
                            "code": sweep_error.code,
                            "message": str(sweep_error),
                        }
                    actions.append(action)
                    continue
                sweep_error = sweep_errors.get(ledger["host_name"])
                try:
                    host = self.instances.load(
                        ledger["host_name"],
                        required=False,
                    )
                    if (
                        host is None
                        or host["operation_id"]
                        != ledger["host_operation_id"]
                        or host.get("pod_id") != ledger["pod_id"]
                    ):
                        raise RunpodLocalError(
                            f"host {ledger['host_name']} operation changed "
                            "before retirement enforcement",
                            code="host_claim_host_changed",
                        )
                    self._attest_ledger_receipt(host, ledger)
                    if ledger["claims"] and (
                        host["phase"] in TERMINAL_PHASES
                    ):
                        raise RunpodLocalError(
                            f"host {ledger['host_name']} terminated while "
                            "claims remain",
                            code="host_claim_host_changed",
                        )
                    if execute and sweep_error is None:
                        ledger, expired_claim_ids = self._expire_claims(
                            ledger,
                            now=now,
                        )
                    elif execute:
                        # Expiry persists before its acquisition-journal
                        # closure is replayed. Keep enforcing the resulting
                        # exact-host quarantine even when that replay needs
                        # operator repair.
                        expired_claim_ids = []
                    else:
                        (
                            ledger,
                            expired_claim_ids,
                        ) = self.claims.preview_expire_claims(
                            ledger,
                            now=now,
                        )
                    retire_at = ledger.get("retire_at")
                    due = (
                        not ledger["claims"]
                        and isinstance(retire_at, str)
                        and now >= parse_utc_timestamp(retire_at)
                    )
                    action["expired_claim_ids"] = sorted(
                        {
                            *action["expired_claim_ids"],
                            *expired_claim_ids,
                        }
                    )
                    action["remaining_claim_count"] = len(
                        ledger["claims"]
                    )
                    action["quarantine"] = ledger["quarantine"]
                    action["operation_end"] = ledger["operation_end"]
                    action["manual_action_required"] = (
                        ledger["quarantine"] is not None
                        and ledger["retention"]["mode"] == "manual"
                        and retire_at is None
                    )
                    action["retire_at"] = retire_at
                    action["due"] = due
                    if due and execute:
                        termination = self.lifecycle.terminate(
                            ledger["host_name"],
                            execute=True,
                            reason=(
                                "quarantined_host_retirement"
                                if ledger["quarantine"] is not None
                                else "empty_host_retention_expired"
                            ),
                            expected_operation_id=ledger[
                                "host_operation_id"
                            ],
                        )
                        action["termination"] = termination
                        action["executed"] = termination.get(
                            "executed",
                            False,
                        )
                except RunpodLocalError as error:
                    action["error"] = {
                        "code": error.code,
                        "message": str(error),
                    }
                if "error" not in action and sweep_error is not None:
                    action["error"] = {
                        "code": sweep_error.code,
                        "message": str(sweep_error),
                    }
                actions.append(action)
            return {
                "schema_version": "runpod.host-retirement.v1",
                "evaluated_at": utc_timestamp(now),
                "executed": execute,
                "actions": actions,
            }
