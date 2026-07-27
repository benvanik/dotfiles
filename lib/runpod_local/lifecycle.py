"""Crash-reconcilable Runpod launch, verification, and termination."""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Callable

from .allocation import select_launch_placement, verify_allocated_pod
from .api import NO_INSTANCES_AVAILABLE_ERROR, RunpodApi
from .errors import HttpRequestError, RunpodLocalError
from .instances import (
    INSTANCE_SCHEMA,
    INTENT_TTL_SECONDS,
    InstanceStore,
    activate_lease,
    append_event,
    build_pod_payload,
    lease_expiry_reasons,
    json_document_hash,
    instance_lock_scope,
    profile_hash,
    transition_instance,
    validate_lease_request,
)
from .profile import (
    validate_profile,
    validate_profile_ssh_files,
    validate_ssh_identity_file,
    validate_ssh_key_pair,
    validate_ssh_public_key,
)
from .state import StateStore, validate_record_name
from .timeutil import parse_utc_timestamp, utc_timestamp


TERMINAL_PHASES = {"rolled_back", "terminated", "aborted"}
LAUNCH_PHASES = {"intent", "submitting", "provisioning"}
MAX_OPERATION_HISTORY = 20


def _operation_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation_id": record.get("operation_id"),
        "remote_name": record.get("remote_name"),
        "pod_id": record.get("pod_id"),
        "phase": record.get("phase"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    }


class LifecycleManager:
    def __init__(
        self,
        api: RunpodApi | None,
        state: StateStore,
        *,
        clock: Callable[[], datetime.datetime] | None = None,
        uuid_factory: Callable[[], uuid.UUID] | None = None,
        profile_ssh_validator: Callable[[dict[str, Any]], Any] | None = None,
        key_pair_validator: Callable[[str, str], None] | None = None,
    ) -> None:
        self.api = api
        self.state = state
        self.instances = InstanceStore(state)
        self.clock = clock or (
            lambda: datetime.datetime.now(datetime.timezone.utc)
        )
        self.uuid_factory = uuid_factory or uuid.uuid4
        self.profile_ssh_validator = (
            profile_ssh_validator or validate_profile_ssh_files
        )
        self.key_pair_validator = key_pair_validator or validate_ssh_key_pair

    def _api(self) -> RunpodApi:
        if self.api is None:
            raise RunpodLocalError(
                "this operation requires a configured Runpod credential",
                code="credential_required",
            )
        return self.api

    def _now(self) -> datetime.datetime:
        now = self.clock()
        utc_timestamp(now)
        return now

    def _validate_record_ssh_identity(self, record: dict[str, Any]) -> None:
        connection = record.get("connection")
        payload = record.get("pod_payload")
        environment = payload.get("env") if isinstance(payload, dict) else None
        if not isinstance(connection, dict) or not isinstance(
            environment, dict
        ):
            raise RunpodLocalError(
                "launch receipt has no SSH identity policy",
                code="invalid_instance_record",
            )
        public_key = environment.get("SSH_PUBLIC_KEY")
        if not isinstance(public_key, str):
            raise RunpodLocalError(
                "launch receipt has no injected SSH public key",
                code="invalid_instance_record",
            )
        public_key = validate_ssh_public_key(public_key)
        identity_path = validate_ssh_identity_file(
            connection.get("identity_file")
        )
        self.key_pair_validator(str(identity_path), public_key)

    def _volume_for_profile(
        self, profile: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None]:
        volume_id = profile["pod"]["network_volume_id"]
        if volume_id is None:
            return None, None
        volume = self._api().get_network_volume(volume_id)
        if volume.get("id") != volume_id:
            raise RunpodLocalError(
                "Runpod returned the wrong network volume",
                code="volume_identity_mismatch",
            )
        data_center_id = volume.get("data_center_id")
        if not isinstance(data_center_id, str) or not data_center_id:
            raise RunpodLocalError(
                f"network volume {volume_id} has no data-center identity",
                code="invalid_provider_response",
            )
        return volume, data_center_id

    def _placement(
        self,
        profile: dict[str, Any],
        *,
        allowed_gpu_ids: set[str] | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        volume, data_center_id = self._volume_for_profile(profile)
        stock = self._api().stock(
            gpu_count=profile["pod"]["gpu_count"],
            secure_cloud=True,
            include_data_centers=data_center_id is not None,
        )
        placement = select_launch_placement(
            profile,
            stock,
            data_center_id=data_center_id,
            allowed_gpu_ids=allowed_gpu_ids,
        )
        return volume, placement

    def plan_launch(
        self,
        name: str,
        profile: dict[str, Any],
        *,
        ttl_seconds: int,
        idle_timeout_seconds: int | None,
        allowed_gpu_ids: set[str] | None = None,
        model: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        validate_record_name(name)
        profile = validate_profile(profile)
        validate_lease_request(ttl_seconds, idle_timeout_seconds)
        existing = self.instances.load(name, required=False)
        if existing is not None and existing["phase"] not in TERMINAL_PHASES:
            if existing["profile"]["sha256"] != profile_hash(profile):
                raise RunpodLocalError(
                    f"instance {name} has unfinished work for another profile",
                    code="instance_profile_conflict",
                )
            if existing["phase"] == "intent":
                self._validate_record_ssh_identity(existing)
            return {
                "schema_version": "runpod.launch-plan.v1",
                "action": "reconcile_existing_launch",
                "ready": existing["phase"] in LAUNCH_PHASES,
                "instance": existing,
                "executed": False,
            }
        self.profile_ssh_validator(profile)
        if (
            existing is not None
            and self._find_owned_remote(existing) is not None
        ):
            raise RunpodLocalError(
                f"terminal receipt {name} still has a live Pod",
                code="terminal_pod_leak",
            )
        volume, placement = self._placement(
            profile, allowed_gpu_ids=allowed_gpu_ids
        )
        return {
            "schema_version": "runpod.launch-plan.v1",
            "action": "create_pod",
            "ready": placement["selected"] is not None,
            "instance_name": name,
            "profile": {
                "name": profile["name"],
                "sha256": profile_hash(profile),
            },
            "volume": volume,
            "placement": placement,
            "lease_request": {
                "ttl_seconds": ttl_seconds,
                "idle_timeout_seconds": idle_timeout_seconds,
            },
            "model": model,
            "executed": False,
        }

    def launch(
        self,
        name: str,
        profile: dict[str, Any],
        *,
        ttl_seconds: int,
        idle_timeout_seconds: int | None,
        allowed_gpu_ids: set[str] | None = None,
        model: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        validate_record_name(name)
        profile = validate_profile(profile)
        validate_lease_request(ttl_seconds, idle_timeout_seconds)
        with self.state.locked(instance_lock_scope(name)):
            record = self.instances.load(name, required=False)
            if record is not None and record["phase"] not in TERMINAL_PHASES:
                if record["profile"]["sha256"] != profile_hash(profile):
                    raise RunpodLocalError(
                        f"instance {name} has unfinished work for another profile",
                        code="instance_profile_conflict",
                    )
                if record["phase"] == "intent":
                    self._validate_record_ssh_identity(record)
                return self._advance_launch(record)
            if record is not None and self._find_owned_remote(record) is not None:
                raise RunpodLocalError(
                    f"terminal receipt {name} still has a live Pod",
                    code="terminal_pod_leak",
                )

            self.profile_ssh_validator(profile)
            volume, placement = self._placement(
                profile, allowed_gpu_ids=allowed_gpu_ids
            )
            selected = placement["selected"]
            if selected is None:
                raise RunpodLocalError(
                    "no allowed GPU satisfies live stock, datacenter, model, "
                    "and hourly-price constraints",
                    code="no_eligible_gpu",
                )
            operation_id = str(self.uuid_factory())
            operation_uuid = uuid.UUID(operation_id)
            now = self._now()
            remote_name = f"rp-{name}-{operation_uuid.hex[:12]}"
            gpu_id = selected["gpu_id"]
            history = []
            if record is not None:
                history = list(record.get("history", []))
                history.append(_operation_summary(record))
                history = history[-MAX_OPERATION_HISTORY:]
            record = {
                "schema_version": INSTANCE_SCHEMA,
                "name": name,
                "operation_id": operation_id,
                "remote_name": remote_name,
                "phase": "intent",
                "created_at": utc_timestamp(now),
                "updated_at": utc_timestamp(now),
                "intent_expires_at": utc_timestamp(
                    now + datetime.timedelta(seconds=INTENT_TTL_SECONDS)
                ),
                "profile": {
                    "name": profile["name"],
                    "sha256": profile_hash(profile),
                },
                "expected": {
                    "gpu_id": gpu_id,
                    "gpu_count": profile["pod"]["gpu_count"],
                    "network_volume_id": (
                        volume.get("id") if volume is not None else None
                    ),
                    "data_center_id": placement["data_center_id"],
                    "max_hourly_usd": profile["limits"]["max_hourly_usd"],
                },
                "quoted_total_price_per_hour": selected[
                    "total_price_per_hour"
                ],
                "pod_payload": build_pod_payload(
                    profile, remote_name=remote_name, gpu_id=gpu_id
                ),
                "connection": {
                    "user": profile["ssh"]["user"],
                    "identity_file": profile["ssh"]["identity_file"],
                    "internal_ssh_port": 22,
                },
                "lease_request": {
                    "ttl_seconds": ttl_seconds,
                    "idle_timeout_seconds": idle_timeout_seconds,
                },
                "lease": None,
                "pod_id": None,
                "provider": None,
                "model": model,
                "events": [],
                "history": history,
            }
            record["pod_payload_sha256"] = json_document_hash(
                record["pod_payload"]
            )
            append_event(record, "launch_intent_saved", at=now)
            self.instances.save(record)
            return self._advance_launch(record)

    def _advance_launch(self, record: dict[str, Any]) -> dict[str, Any]:
        phase = record["phase"]
        if phase in {"submitting", "provisioning"}:
            reasons = lease_expiry_reasons(record, now=self._now())
            if reasons:
                raise RunpodLocalError(
                    "launch can no longer activate because its safety deadline "
                    f"expired: {', '.join(reasons)}",
                    code="launch_expired",
                )
        just_marked_submitting = False
        if phase == "intent":
            self._validate_record_ssh_identity(record)
            now = self._now()
            transition_instance(
                record,
                "submitting",
                at=now,
                event="submission_started",
            )
            record["submission_started_at"] = utc_timestamp(now)
            activate_lease(
                record,
                ttl_seconds=record["lease_request"]["ttl_seconds"],
                idle_timeout_seconds=record["lease_request"][
                    "idle_timeout_seconds"
                ],
                hard_started_at=now,
                now=now,
            )
            self.instances.save(record)
            just_marked_submitting = True
            phase = "submitting"

        if phase == "submitting":
            matches = [
                pod
                for pod in self._api().list_pods()
                if pod.get("name") == record["remote_name"]
            ]
            if len(matches) > 1:
                now = self._now()
                transition_instance(
                    record,
                    "conflict",
                    at=now,
                    event="duplicate_remote_name",
                    details={
                        "pod_ids": sorted(
                            str(pod.get("id")) for pod in matches
                        )
                    },
                )
                self.instances.save(record)
                raise RunpodLocalError(
                    f"multiple Pods use reconciliation name "
                    f"{record['remote_name']}",
                    code="duplicate_remote_name",
                )
            if len(matches) == 1:
                pod = matches[0]
            elif not just_marked_submitting:
                raise RunpodLocalError(
                    "submission outcome is ambiguous and no matching Pod is "
                    "visible yet; retry reconciliation later",
                    code="submission_ambiguous",
                )
            else:
                try:
                    pod = self._api().create_pod(record["pod_payload"])
                except HttpRequestError as error:
                    if error.provider_error == NO_INSTANCES_AVAILABLE_ERROR:
                        transition_instance(
                            record,
                            "aborted",
                            at=self._now(),
                            event="submission_rejected_no_capacity",
                        )
                        self.instances.save(record)
                        raise RunpodLocalError(
                            "Runpod reported no instances available for the "
                            "selected launch constraints",
                            code="no_provider_capacity",
                        ) from error
                    append_event(
                        record, "submission_result_unknown", at=self._now()
                    )
                    self.instances.save(record)
                    raise
                except RunpodLocalError:
                    append_event(
                        record, "submission_result_unknown", at=self._now()
                    )
                    self.instances.save(record)
                    raise
            pod_id = pod.get("id")
            if not isinstance(pod_id, str) or not pod_id:
                append_event(
                    record, "submission_missing_pod_id", at=self._now()
                )
                self.instances.save(record)
                raise RunpodLocalError(
                    "Pod submission returned no durable Pod ID; reconcile later",
                    code="submission_ambiguous",
                )
            now = self._now()
            record["pod_id"] = pod_id
            record["provider"] = pod
            transition_instance(
                record,
                "provisioning",
                at=now,
                event="pod_identity_saved",
                details={"pod_id": pod_id},
            )
            self.instances.save(record)
            phase = "provisioning"

        if phase == "provisioning":
            try:
                pod = self._api().get_pod(record["pod_id"])
            except HttpRequestError as error:
                if error.status == 404:
                    append_event(
                        record, "pod_not_visible_yet", at=self._now()
                    )
                    self.instances.save(record)
                    return record
                raise
            violations, pending = verify_allocated_pod(record, pod)
            record["provider"] = pod
            if violations:
                return self._rollback(record, violations)
            if pending:
                append_event(
                    record,
                    "allocation_pending",
                    at=self._now(),
                    details={"fields": pending},
                )
                self.instances.save(record)
                return record
            reasons = lease_expiry_reasons(record, now=self._now())
            if reasons:
                raise RunpodLocalError(
                    "allocation became ready after its safety deadline: "
                    + ", ".join(reasons),
                    code="launch_expired",
                )
            now = self._now()
            hard_started = parse_utc_timestamp(
                record["submission_started_at"]
            )
            activate_lease(
                record,
                ttl_seconds=record["lease_request"]["ttl_seconds"],
                idle_timeout_seconds=record["lease_request"][
                    "idle_timeout_seconds"
                ],
                hard_started_at=hard_started,
                now=now,
            )
            transition_instance(
                record,
                "active",
                at=now,
                event="allocation_verified",
            )
            self.instances.save(record)
        return record

    def _rollback(
        self, record: dict[str, Any], violations: list[str]
    ) -> dict[str, Any]:
        now = self._now()
        record["rollback_reasons"] = violations
        transition_instance(
            record,
            "rollback_required",
            at=now,
            event="allocation_rejected",
            details={"violations": violations},
        )
        self.instances.save(record)
        try:
            self._api().delete_pod(record["pod_id"])
        except HttpRequestError as error:
            if error.status != 404:
                append_event(
                    record, "rollback_delete_failed", at=self._now()
                )
                self.instances.save(record)
                raise RunpodLocalError(
                    f"Pod {record['pod_id']} violates launch policy and "
                    "requires deletion retry",
                    code="rollback_required",
                ) from error
        now = self._now()
        transition_instance(
            record,
            "rolled_back",
            at=now,
            event="rollback_completed",
        )
        self.instances.save(record)
        raise RunpodLocalError(
            "created Pod violated launch policy and was deleted: "
            + "; ".join(violations),
            code="allocation_rejected",
        )

    def _find_owned_remote(
        self, record: dict[str, Any]
    ) -> dict[str, Any] | None:
        pod_id = record.get("pod_id")
        if isinstance(pod_id, str):
            try:
                pod = self._api().get_pod(pod_id)
            except HttpRequestError as error:
                if error.status != 404:
                    raise
                pod = None
            if pod is not None:
                if pod.get("id") != pod_id or pod.get("name") != record["remote_name"]:
                    raise RunpodLocalError(
                        "live Pod identity does not match the local receipt; "
                        "refusing destructive action",
                        code="pod_identity_conflict",
                    )
                return pod
        matches = [
            pod
            for pod in self._api().list_pods()
            if pod.get("name") == record["remote_name"]
        ]
        if len(matches) > 1:
            raise RunpodLocalError(
                f"multiple Pods use reconciliation name {record['remote_name']}",
                code="duplicate_remote_name",
            )
        if len(matches) == 1:
            candidate = matches[0]
            if pod_id is not None and candidate.get("id") != pod_id:
                raise RunpodLocalError(
                    "reconciliation name belongs to another Pod ID",
                    code="pod_identity_conflict",
                )
            return candidate
        return None

    def terminate(
        self,
        name: str,
        *,
        execute: bool,
        reason: str,
        expected_operation_id: str | None = None,
        require_expired: bool = False,
    ) -> dict[str, Any]:
        validate_record_name(name)
        with self.state.locked(instance_lock_scope(name)):
            record = self.instances.load(name)
            if record is None:
                raise AssertionError("required instance unexpectedly absent")
            if (
                expected_operation_id is not None
                and record["operation_id"] != expected_operation_id
            ):
                raise RunpodLocalError(
                    f"instance {name} no longer owns the expected operation",
                    code="instance_identity_changed",
                )
            if require_expired:
                current_reasons = lease_expiry_reasons(
                    record, now=self._now()
                )
                if not current_reasons:
                    return {
                        "schema_version": "runpod.termination-plan.v1",
                        "instance_name": name,
                        "phase": record["phase"],
                        "action": "lease_no_longer_expired",
                        "executed": False,
                    }
                reason = "+".join(current_reasons)
            if record["phase"] in TERMINAL_PHASES:
                remote = self._find_owned_remote(record)
                if remote is not None:
                    result = {
                        "schema_version": "runpod.termination-plan.v1",
                        "instance_name": name,
                        "phase": record["phase"],
                        "action": "delete_terminal_pod_leak",
                        "pod_id": remote["id"],
                        "remote_name": remote["name"],
                        "network_volume_id": record["expected"][
                            "network_volume_id"
                        ],
                        "volume_action": "preserve",
                        "executed": execute,
                    }
                    if not execute:
                        return result
                    append_event(
                        record,
                        "terminal_leak_delete_started",
                        at=self._now(),
                        details={"reason": reason},
                    )
                    self.instances.save(record)
                    try:
                        self._api().delete_pod(remote["id"])
                    except HttpRequestError as error:
                        if error.status != 404:
                            append_event(
                                record,
                                "terminal_leak_delete_failed",
                                at=self._now(),
                            )
                            self.instances.save(record)
                            raise
                    append_event(
                        record,
                        "terminal_leak_delete_completed",
                        at=self._now(),
                    )
                    self.instances.save(record)
                    return result
                return {
                    "schema_version": "runpod.termination-plan.v1",
                    "instance_name": name,
                    "phase": record["phase"],
                    "action": "none",
                    "executed": execute,
                }
            if record["phase"] == "conflict":
                raise RunpodLocalError(
                    "conflicted launch cannot be terminated automatically",
                    code="instance_conflict",
                )
            if record["phase"] == "intent":
                result = {
                    "schema_version": "runpod.termination-plan.v1",
                    "instance_name": name,
                    "action": "abort_unsubmitted_intent",
                    "pod_id": None,
                    "executed": execute,
                }
                if execute:
                    transition_instance(
                        record,
                        "aborted",
                        at=self._now(),
                        event="unsubmitted_intent_aborted",
                        details={"reason": reason},
                    )
                    self.instances.save(record)
                return result
            remote = self._find_owned_remote(record)
            if remote is None:
                if record["phase"] == "submitting":
                    raise RunpodLocalError(
                        "submission remains ambiguous; no Pod is visible to "
                        "delete",
                        code="submission_ambiguous",
                    )
                terminal_phase = (
                    "rolled_back"
                    if record["phase"] == "rollback_required"
                    else "terminated"
                )
                result = {
                    "schema_version": "runpod.termination-plan.v1",
                    "instance_name": name,
                    "action": "record_remote_absence",
                    "pod_id": record.get("pod_id"),
                    "executed": execute,
                }
                if execute:
                    if record["phase"] not in {
                        "termination_pending",
                        "rollback_required",
                    }:
                        transition_instance(
                            record,
                            "termination_pending",
                            at=self._now(),
                            event="termination_started",
                            details={"reason": reason},
                        )
                    transition_instance(
                        record,
                        terminal_phase,
                        at=self._now(),
                        event="remote_absence_confirmed",
                    )
                    self.instances.save(record)
                return result

            result = {
                "schema_version": "runpod.termination-plan.v1",
                "instance_name": name,
                "action": "delete_pod",
                "pod_id": remote["id"],
                "remote_name": remote["name"],
                "network_volume_id": record["expected"]["network_volume_id"],
                "volume_action": "preserve",
                "executed": execute,
            }
            if not execute:
                return result
            rollback = record["phase"] == "rollback_required"
            if record["phase"] not in {
                "termination_pending",
                "rollback_required",
            }:
                if record["phase"] == "submitting":
                    record["pod_id"] = remote["id"]
                    transition_instance(
                        record,
                        "provisioning",
                        at=self._now(),
                        event="pod_identity_saved_for_termination",
                    )
                transition_instance(
                    record,
                    "termination_pending",
                    at=self._now(),
                    event="termination_started",
                    details={"reason": reason},
                )
            self.instances.save(record)
            try:
                self._api().delete_pod(remote["id"])
            except HttpRequestError as error:
                if error.status != 404:
                    append_event(
                        record, "termination_delete_failed", at=self._now()
                    )
                    self.instances.save(record)
                    raise
            transition_instance(
                record,
                "rolled_back" if rollback else "terminated",
                at=self._now(),
                event="termination_completed",
            )
            self.instances.save(record)
            return result

    def status(
        self, name: str | None = None, *, live: bool = True
    ) -> dict[str, Any]:
        local = (
            [self.instances.load(name)]
            if name is not None
            else self.instances.list()
        )
        local = [record for record in local if record is not None]
        remote = self._api().list_pods() if live else []
        remote_by_id = {
            pod.get("id"): pod for pod in remote if isinstance(pod.get("id"), str)
        }
        remote_by_name: dict[str, list[dict[str, Any]]] = {}
        for pod in remote:
            if isinstance(pod.get("name"), str):
                remote_by_name.setdefault(pod["name"], []).append(pod)
        managed_ids = set()
        instances = []
        now = self._now()
        for record in local:
            pod_id = record.get("pod_id")
            pod = remote_by_id.get(pod_id)
            if isinstance(pod_id, str):
                managed_ids.add(pod_id)
            drift = []
            name_matches = remote_by_name.get(record["remote_name"], [])
            if pod_id is None and record["phase"] in {
                "intent",
                "submitting",
                "conflict",
            }:
                for candidate in name_matches:
                    candidate_id = candidate.get("id")
                    if isinstance(candidate_id, str):
                        managed_ids.add(candidate_id)
                if len(name_matches) == 1:
                    pod = name_matches[0]
                elif len(name_matches) > 1:
                    drift.append("duplicate_remote_name")
            if live and pod is None and record["phase"] in {
                "provisioning",
                "active",
                "termination_pending",
                "rollback_required",
            }:
                drift.append("managed_pod_missing")
            if pod is not None and pod.get("name") != record["remote_name"]:
                drift.append("pod_name_mismatch")
            if pod is not None and record["phase"] in TERMINAL_PHASES:
                drift.append("terminal_receipt_has_live_pod")
            if record["phase"] in {
                "termination_pending",
                "rollback_required",
            }:
                drift.append("cleanup_pending")
            if record["phase"] == "intent" and pod is not None:
                drift.append("unsubmitted_intent_has_live_pod")
            allocation_violations = []
            allocation_pending = []
            if pod is not None and record["phase"] in {
                "provisioning",
                "active",
            }:
                allocation_violations, allocation_pending = (
                    verify_allocated_pod(record, pod)
                )
                if allocation_violations:
                    drift.append("allocation_policy_mismatch")
            instances.append(
                {
                    "local": record,
                    "remote": pod,
                    "expiry_reasons": lease_expiry_reasons(record, now=now),
                    "drift": drift,
                    "allocation_violations": allocation_violations,
                    "allocation_pending": allocation_pending,
                }
            )
        unmanaged = [
            pod for pod in remote if pod.get("id") not in managed_ids
        ]
        return {
            "schema_version": "runpod.status.v1",
            "generated_at": utc_timestamp(now),
            "provider_checked": live,
            "instances": instances,
            "unmanaged_pods": unmanaged,
        }

    def enforce_ttl(self, *, execute: bool) -> dict[str, Any]:
        now = self._now()
        actions = []
        for record in self.instances.list():
            reasons = lease_expiry_reasons(record, now=now)
            if not reasons:
                continue
            action: dict[str, Any] = {
                "instance_name": record["name"],
                "phase": record["phase"],
                "reasons": reasons,
                "executed": False,
            }
            if execute:
                try:
                    action["termination"] = self.terminate(
                        record["name"],
                        execute=True,
                        reason="+".join(reasons),
                        expected_operation_id=record["operation_id"],
                        require_expired=True,
                    )
                    action["executed"] = action["termination"]["executed"]
                except RunpodLocalError as error:
                    action["error"] = {
                        "code": error.code,
                        "message": str(error),
                    }
            actions.append(action)
        return {
            "schema_version": "runpod.ttl-enforcement.v1",
            "evaluated_at": utc_timestamp(now),
            "executed": execute,
            "actions": actions,
        }
