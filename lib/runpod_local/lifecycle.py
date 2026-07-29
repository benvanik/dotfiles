"""Crash-reconcilable Runpod launch, verification, and termination."""

from __future__ import annotations

import datetime
import math
import time
import uuid
from collections.abc import Callable
from typing import Any

from .allocation import select_launch_placement, verify_allocated_pod
from .api import (
    GRAPHQL_NO_CAPACITY_ERROR_CODE,
    RunpodApi,
    provider_pod_snapshot,
)
from .errors import HttpRequestError, RunpodLocalError
from .instances import (
    INSTANCE_SCHEMA,
    INTENT_TTL_SECONDS,
    InstanceStore,
    activate_lease,
    append_event,
    build_pod_payload,
    instance_lock_scope,
    json_document_hash,
    lease_expiry_reasons,
    profile_hash,
    transition_instance,
    validate_lease_request,
)
from .profile import (
    provider_effective_environment_summary,
    validate_profile,
    validate_profile_ssh_files,
    validate_ssh_identity_file,
    validate_ssh_key_pair,
    validate_ssh_public_key,
)
from .state import StateStore, validate_record_name
from .template import template_contract_violations
from .timeutil import parse_utc_timestamp, utc_timestamp

TERMINAL_PHASES = {"rolled_back", "terminated", "aborted"}
LAUNCH_PHASES = {"intent", "submitting", "provisioning"}
MAX_OPERATION_HISTORY = 20
LIFECYCLE_CLEANUP_TIMEOUT_SECONDS = 60.0


def _host_retention(
    profile: dict[str, Any],
    *,
    retention_mode: str | None,
    empty_grace_seconds: int | None,
) -> dict[str, Any]:
    profile_retention = profile["retention"]
    mode = (
        profile_retention["mode"]
        if retention_mode is None
        else retention_mode
    )
    grace = (
        profile_retention["empty_grace_seconds"]
        if empty_grace_seconds is None
        else empty_grace_seconds
    )
    if mode not in {"manual", "while-claimed"}:
        raise RunpodLocalError(
            "host retention must be manual or while-claimed",
            code="invalid_host_retention",
        )
    if (
        not isinstance(grace, int)
        or isinstance(grace, bool)
        or grace < 0
        or grace > 30 * 24 * 60 * 60
    ):
        raise RunpodLocalError(
            "empty-host grace must be from 0 seconds through 30 days",
            code="invalid_host_retention",
        )
    return {"mode": mode, "empty_grace_seconds": grace}


def _provider_termination_deadline(
    record: dict[str, Any],
) -> datetime.datetime:
    timestamp = record.get("provider_termination_at")
    if not isinstance(timestamp, str):
        raise RunpodLocalError(
            f"instance {record.get('name')} has no provider-owned "
            "termination deadline",
            code="invalid_instance_record",
        )
    return parse_utc_timestamp(timestamp)


def _durable_provider_snapshot(
    record: dict[str, Any],
    pod: dict[str, Any],
) -> dict[str, Any]:
    expected = record["expected"]
    payload = record["pod_payload"]
    return provider_pod_snapshot(
        pod,
        expected={
            "id": record["pod_id"],
            "name": record["remote_name"],
            "desired_status": "RUNNING",
            "template_id": payload.get("templateId"),
            "volume_mount_path": expected["volume_mount_path"],
            "environment_names": expected["environment_names"],
            "environment_sha256": expected["environment_sha256"],
            "gpu_id": expected["gpu_id"],
            "data_center_id": expected["data_center_id"],
            "network_volume_id": expected["network_volume_id"],
            "network_volume_data_center_id": (
                expected["data_center_id"]
                if expected["network_volume_id"] is not None
                else None
            ),
            "ports": expected["ports"],
            "image": expected["image"],
            "docker_entrypoint": expected["docker_entrypoint"],
            "docker_start_cmd": expected["docker_start_cmd"],
        },
    )


def _operation_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation_id": record.get("operation_id"),
        "remote_name": record.get("remote_name"),
        "pod_id": record.get("pod_id"),
        "phase": record.get("phase"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    }


def _recorded_conflict_pod_ids(record: dict[str, Any]) -> list[str]:
    pod_ids = record.get("conflict_pod_ids")
    if (
        not isinstance(pod_ids, list)
        or len(pod_ids) < 2
        or not all(isinstance(pod_id, str) and pod_id for pod_id in pod_ids)
        or pod_ids != sorted(set(pod_ids))
    ):
        raise RunpodLocalError(
            "conflicted launch has no canonical set of Pod IDs",
            code="conflict_identity_unavailable",
        )
    return pod_ids


def _has_conflict_identity(record: dict[str, Any]) -> bool:
    return record.get("conflict_pod_ids") is not None


def _submission_may_have_been_sent(record: dict[str, Any]) -> bool:
    return isinstance(record.get("submission_started_at"), str)


def _durable_current_pod_ids(record: dict[str, Any]) -> list[str]:
    pod_ids = set()
    pod_id = record.get("pod_id")
    if isinstance(pod_id, str):
        pod_ids.add(pod_id)
    if _has_conflict_identity(record):
        pod_ids.update(_recorded_conflict_pod_ids(record))
    return sorted(pod_ids)


class LifecycleManager:
    def __init__(
        self,
        api: RunpodApi | None,
        state: StateStore,
        *,
        clock: Callable[[], datetime.datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
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
        self.monotonic = monotonic or time.monotonic
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

    def _provider_deadline(
        self,
        acquisition_expires_at: datetime.datetime | None,
        *,
        acquisition_deadline: float | None = None,
    ) -> float | None:
        if acquisition_deadline is not None and (
            isinstance(acquisition_deadline, bool)
            or not isinstance(acquisition_deadline, (int, float))
            or not math.isfinite(float(acquisition_deadline))
        ):
            raise RunpodLocalError(
                "RunPod launch monotonic deadline is invalid",
                code="invalid_operation_identity",
            )
        deadline = (
            None
            if acquisition_deadline is None
            else float(acquisition_deadline)
        )
        if acquisition_expires_at is not None:
            utc_timestamp(acquisition_expires_at)
            remaining = (
                acquisition_expires_at - self._now()
            ).total_seconds()
            wall_deadline = self.monotonic() + remaining
            deadline = (
                wall_deadline
                if deadline is None
                else min(deadline, wall_deadline)
            )
        if deadline is not None and self.monotonic() >= deadline:
            raise RunpodLocalError(
                "RunPod launch cannot make another provider request after "
                "its acquisition deadline",
                code="remote_client_timeout",
            )
        return deadline

    def _provider_call_arguments(
        self,
        deadline: float | None,
    ) -> dict[str, Any]:
        if deadline is None:
            return {}
        return {"deadline": deadline, "monotonic": self.monotonic}

    def new_operation_id(self) -> str:
        """Return one canonical identity for an operation not yet launched."""

        operation_id = str(self.uuid_factory())
        try:
            operation_uuid = uuid.UUID(operation_id)
        except (AttributeError, TypeError, ValueError) as error:
            raise RunpodLocalError(
                "generated launch operation ID must be a UUID",
                code="invalid_operation_identity",
            ) from error
        if str(operation_uuid) != operation_id:
            raise RunpodLocalError(
                "generated launch operation ID must be canonical UUID text",
                code="invalid_operation_identity",
            )
        return operation_id

    def _validate_record_ssh_identity(self, record: dict[str, Any]) -> str:
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
        return public_key

    def _attest_record_template(
        self,
        record: dict[str, Any],
        *,
        deadline: float | None = None,
    ) -> None:
        contract = record["expected"].get("template_contract")
        if contract is None:
            return
        observed = self._api().get_template(
            contract["id"],
            **self._provider_call_arguments(deadline),
        )
        violations = template_contract_violations(observed, contract)
        if violations:
            raise RunpodLocalError(
                "Runpod template drifted from the launch receipt: "
                + "; ".join(violations),
                code="template_contract_drift",
            )

    def _volume_for_profile(
        self,
        profile: dict[str, Any],
        *,
        deadline: float | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        volume_id = profile["pod"]["network_volume_id"]
        if volume_id is None:
            return None, None
        volume = self._api().get_network_volume(
            volume_id,
            **self._provider_call_arguments(deadline),
        )
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
        deadline: float | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        volume, data_center_id = self._volume_for_profile(
            profile,
            deadline=deadline,
        )
        stock = self._api().stock(
            gpu_count=profile["pod"]["gpu_count"],
            secure_cloud=True,
            include_data_centers=data_center_id is not None,
            **self._provider_call_arguments(deadline),
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
        retention_mode: str | None = None,
        empty_grace_seconds: int | None = None,
    ) -> dict[str, Any]:
        validate_record_name(name)
        profile = validate_profile(profile)
        retention = _host_retention(
            profile,
            retention_mode=retention_mode,
            empty_grace_seconds=empty_grace_seconds,
        )
        validate_lease_request(ttl_seconds, idle_timeout_seconds)
        existing = self.instances.load(name, required=False)
        if existing is not None and existing["phase"] not in TERMINAL_PHASES:
            if existing["profile"]["sha256"] != profile_hash(profile):
                raise RunpodLocalError(
                    f"instance {name} has unfinished work for another profile",
                    code="instance_profile_conflict",
                )
            if existing["retention"] != retention:
                raise RunpodLocalError(
                    f"instance {name} has unfinished work for another "
                    "retention policy",
                    code="instance_retention_conflict",
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
            and existing.get("conflict_review_required_at") is not None
        ):
            raise RunpodLocalError(
                f"terminal receipt {name} has a conflict set awaiting review",
                code="conflict_review_required",
            )
        if (
            existing is not None
            and _submission_may_have_been_sent(existing)
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
            "retention": retention,
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
        retention_mode: str | None = None,
        empty_grace_seconds: int | None = None,
        expected_operation_id: str | None = None,
        require_absent: bool = False,
        target_operation_id: str | None = None,
        predecessor_operation_id: str | None = None,
        acquisition_expires_at: datetime.datetime | None = None,
        acquisition_deadline: float | None = None,
        cleanup_deadline_factory: Callable[[], float] | None = None,
    ) -> dict[str, Any]:
        validate_record_name(name)
        profile = validate_profile(profile)
        provider_deadline = self._provider_deadline(
            acquisition_expires_at,
            acquisition_deadline=acquisition_deadline,
        )
        if type(require_absent) is not bool:
            raise RunpodLocalError(
                "launch absence guard must be boolean",
                code="invalid_operation_identity",
            )
        operation_guards = sum(
            (
                expected_operation_id is not None,
                require_absent,
                target_operation_id is not None,
            )
        )
        if operation_guards > 1:
            raise RunpodLocalError(
                "launch operation guards are mutually exclusive",
                code="invalid_operation_identity",
            )
        if (
            predecessor_operation_id is not None
            and target_operation_id is None
        ):
            raise RunpodLocalError(
                "launch predecessor requires an exact target operation",
                code="invalid_operation_identity",
            )
        for label, operation_id in (
            ("expected", expected_operation_id),
            ("target", target_operation_id),
            ("predecessor", predecessor_operation_id),
        ):
            if operation_id is None:
                continue
            try:
                operation_uuid = uuid.UUID(operation_id)
            except (AttributeError, TypeError, ValueError) as error:
                raise RunpodLocalError(
                    f"{label} launch operation ID must be a UUID",
                    code="invalid_operation_identity",
                ) from error
            if str(operation_uuid) != operation_id:
                raise RunpodLocalError(
                    f"{label} launch operation ID must be canonical UUID text",
                    code="invalid_operation_identity",
                )
        if (
            target_operation_id is not None
            and target_operation_id == predecessor_operation_id
        ):
            raise RunpodLocalError(
                "launch target operation repeats its predecessor",
                code="invalid_operation_identity",
            )
        retention = _host_retention(
            profile,
            retention_mode=retention_mode,
            empty_grace_seconds=empty_grace_seconds,
        )
        validate_lease_request(ttl_seconds, idle_timeout_seconds)
        with self.state.locked(
            instance_lock_scope(name),
            deadline=provider_deadline,
            monotonic=self.monotonic,
            deadline_error_code="remote_client_timeout",
        ):
            record = self.instances.load(name, required=False)
            if require_absent and record is not None:
                raise RunpodLocalError(
                    f"instance {name} appeared before guarded launch",
                    code="instance_operation_changed",
                )
            if expected_operation_id is not None and (
                record is None
                or record["operation_id"] != expected_operation_id
                or record["phase"] in TERMINAL_PHASES
            ):
                raise RunpodLocalError(
                    f"instance {name} no longer names the expected live "
                    "operation",
                    code="instance_operation_changed",
                )
            if target_operation_id is not None:
                target_is_live = (
                    record is not None
                    and record["operation_id"] == target_operation_id
                    and record["phase"] not in TERMINAL_PHASES
                )
                predecessor_is_terminal = (
                    predecessor_operation_id is not None
                    and record is not None
                    and record["operation_id"]
                    == predecessor_operation_id
                    and record["phase"] in TERMINAL_PHASES
                )
                target_can_start_absent = (
                    predecessor_operation_id is None and record is None
                )
                if not (
                    target_is_live
                    or predecessor_is_terminal
                    or target_can_start_absent
                ):
                    raise RunpodLocalError(
                        f"instance {name} no longer names the target "
                        "operation boundary",
                        code="instance_operation_changed",
                    )
            if record is not None and record["phase"] not in TERMINAL_PHASES:
                if record["profile"]["sha256"] != profile_hash(profile):
                    raise RunpodLocalError(
                        f"instance {name} has unfinished work for another profile",
                        code="instance_profile_conflict",
                    )
                if record["retention"] != retention:
                    raise RunpodLocalError(
                        f"instance {name} has unfinished work for another "
                        "retention policy",
                        code="instance_retention_conflict",
                    )
                _provider_termination_deadline(record)
                if record["phase"] == "intent":
                    self._validate_record_ssh_identity(record)
                return self._advance_launch(
                    record,
                    deadline=provider_deadline,
                    cleanup_deadline_factory=cleanup_deadline_factory,
                )
            if (
                record is not None
                and _submission_may_have_been_sent(record)
            ):
                name_matches = self._exact_remote_name_matches(
                    record["remote_name"],
                    deadline=provider_deadline,
                )
                conflict_observed = (
                    self._persist_terminal_exact_name_ids(
                        record,
                        [pod["id"] for pod in name_matches],
                        event="terminal_reuse_identity_observed",
                    )
                )
                if conflict_observed:
                    raise RunpodLocalError(
                        f"terminal receipt {name} has conflicting live Pod "
                        "identities",
                        code="pod_identity_conflict",
                    )
                if record.get("conflict_review_required_at") is not None:
                    raise RunpodLocalError(
                        f"terminal receipt {name} has a conflict set "
                        "awaiting review",
                        code="conflict_review_required",
                    )
                if (
                    self._find_owned_remote(
                        record,
                        name_matches=name_matches,
                        deadline=provider_deadline,
                    )
                    is not None
                ):
                    raise RunpodLocalError(
                        f"terminal receipt {name} still has a live Pod",
                        code="terminal_pod_leak",
                    )

            self.profile_ssh_validator(profile)
            volume, placement = self._placement(
                profile,
                allowed_gpu_ids=allowed_gpu_ids,
                deadline=provider_deadline,
            )
            selected = placement["selected"]
            if selected is None:
                raise RunpodLocalError(
                    "no allowed GPU satisfies live stock, datacenter, "
                    "and hourly-price constraints",
                    code="no_eligible_gpu",
                )
            operation_id = target_operation_id or self.new_operation_id()
            operation_uuid = uuid.UUID(operation_id)
            remote_name = f"rp-{name}-{operation_uuid.hex[:12]}"
            if record is not None:
                previous_operation_ids = {
                    record["operation_id"],
                    *(
                        summary.get("operation_id")
                        for summary in record.get("history", [])
                    ),
                }
                previous_remote_names = {
                    record["remote_name"],
                    *(
                        summary.get("remote_name")
                        for summary in record.get("history", [])
                    ),
                }
                if (
                    operation_id in previous_operation_ids
                    or remote_name in previous_remote_names
                ):
                    raise RunpodLocalError(
                        "the operation identity generator repeated a retained "
                        "UUID or remote-name prefix; refusing name reuse",
                        code="operation_identity_collision",
                    )
            now = self._now()
            created_at = utc_timestamp(now)
            provider_termination_at = utc_timestamp(
                now + datetime.timedelta(seconds=ttl_seconds)
            )
            gpu_id = selected["gpu_id"]
            history = []
            if record is not None:
                history = list(record.get("history", []))
                history.append(_operation_summary(record))
                history = history[-MAX_OPERATION_HISTORY:]
            pod_payload = build_pod_payload(
                profile,
                remote_name=remote_name,
                gpu_id=gpu_id,
                data_center_id=placement["data_center_id"],
                provider_termination_at=provider_termination_at,
            )
            expected_environment = provider_effective_environment_summary(
                pod_payload.get("env")
            )
            if expected_environment is None:
                raise AssertionError(
                    "validated profile produced an invalid effective Pod "
                    "environment"
                )
            record = {
                "schema_version": INSTANCE_SCHEMA,
                "name": name,
                "operation_id": operation_id,
                "remote_name": remote_name,
                "phase": "intent",
                "created_at": created_at,
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
                    "gpu_memory_gb": profile["pod"][
                        "gpu_memory_gb_by_type"
                    ][gpu_id],
                    "network_volume_id": (
                        volume.get("id") if volume is not None else None
                    ),
                    "data_center_id": placement["data_center_id"],
                    "max_hourly_usd": profile["limits"]["max_hourly_usd"],
                    "image": profile["pod"]["image_name"],
                    "container_disk_gb": profile["pod"][
                        "container_disk_gb"
                    ],
                    "min_vcpu_count": (
                        profile["pod"]["min_vcpu_per_gpu"]
                        * profile["pod"]["gpu_count"]
                    ),
                    "min_ram_gb": (
                        profile["pod"]["min_ram_per_gpu"]
                        * profile["pod"]["gpu_count"]
                    ),
                    "volume_in_gb": (
                        0
                        if profile["pod"]["network_volume_id"] is not None
                        else 20
                    ),
                    "volume_mount_path": profile["pod"][
                        "volume_mount_path"
                    ],
                    **expected_environment,
                    "has_registry_auth": False,
                    "docker_entrypoint": (
                        profile["pod"]["template_contract"][
                            "docker_entrypoint"
                        ]
                        if profile["pod"]["template_contract"] is not None
                        else None
                    ),
                    "docker_start_cmd": (
                        profile["pod"]["template_contract"][
                            "docker_start_cmd"
                        ]
                        if profile["pod"]["template_contract"] is not None
                        else None
                    ),
                    "ports": profile["pod"]["ports"],
                    "template_contract": profile["pod"][
                        "template_contract"
                    ],
                },
                "quoted_total_price_per_hour": selected[
                    "total_price_per_hour"
                ],
                "provider_termination_at": provider_termination_at,
                "pod_payload": pod_payload,
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
                "retention": retention,
                "events": [],
                "history": history,
            }
            record["pod_payload_sha256"] = json_document_hash(
                record["pod_payload"]
            )
            append_event(record, "launch_intent_saved", at=now)
            if (
                provider_deadline is not None
                and self.monotonic() >= provider_deadline
            ):
                raise RunpodLocalError(
                    "RunPod launch intent exceeded its acquisition deadline",
                    code="remote_client_timeout",
                )
            self.instances.save(record)
            return self._advance_launch(
                record,
                deadline=provider_deadline,
                cleanup_deadline_factory=cleanup_deadline_factory,
            )

    def _advance_launch(
        self,
        record: dict[str, Any],
        *,
        deadline: float | None = None,
        cleanup_deadline_factory: Callable[[], float] | None = None,
    ) -> dict[str, Any]:
        phase = record["phase"]
        if phase in LAUNCH_PHASES:
            _provider_termination_deadline(record)
            reasons = lease_expiry_reasons(record, now=self._now())
            if reasons:
                raise RunpodLocalError(
                    "launch can no longer activate because its safety deadline "
                    f"expired: {', '.join(reasons)}",
                    code="launch_expired",
                )
        just_marked_submitting = False
        account_ssh_attestation: Any = None
        matches: list[dict[str, Any]] | None = None
        if phase == "intent":
            self._attest_record_template(record, deadline=deadline)
            public_key = self._validate_record_ssh_identity(record)
            account_ssh_attestation = self._api().attest_account_ssh_key(
                public_key,
                **self._provider_call_arguments(deadline),
            )
            now = self._now()
            reasons = lease_expiry_reasons(record, now=now)
            if reasons:
                raise RunpodLocalError(
                    "launch can no longer submit because its safety deadline "
                    f"expired: {', '.join(reasons)}",
                    code="launch_expired",
                )
            matches = self._exact_remote_name_matches(
                record["remote_name"],
                deadline=deadline,
            )
            if matches:
                now = self._now()
                transition_instance(
                    record,
                    "aborted",
                    at=now,
                    event="unsubmitted_remote_name_collision",
                    details={"match_count": len(matches)},
                )
                self.instances.save(record)
                raise RunpodLocalError(
                    "an unsubmitted launch name already belongs to a Pod; "
                    "refusing to adopt or mutate it",
                    code="remote_name_collision",
                )
            now = self._now()
            reasons = lease_expiry_reasons(record, now=now)
            if reasons:
                raise RunpodLocalError(
                    "launch can no longer submit because its safety deadline "
                    f"expired: {', '.join(reasons)}",
                    code="launch_expired",
                )
            # This is deliberately the final provider read before the create.
            # The following receipt transition/fsync records ambiguity before
            # the billable request without opening another network TOCTOU.
            self._attest_record_template(record, deadline=deadline)
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
                hard_started_at=parse_utc_timestamp(record["created_at"]),
                hard_expires_at=_provider_termination_deadline(record),
                now=now,
            )
            self.instances.save(record)
            just_marked_submitting = True
            phase = "submitting"

        if phase == "submitting":
            if matches is None:
                matches = self._exact_remote_name_matches(
                    record["remote_name"],
                    deadline=deadline,
                )
            if len(matches) > 1:
                conflict_pod_ids = sorted(
                    {
                        pod["id"]
                        for pod in matches
                        if isinstance(pod.get("id"), str) and pod["id"]
                    }
                )
                if len(conflict_pod_ids) != len(matches):
                    raise RunpodLocalError(
                        "duplicate reconciliation results do not have distinct "
                        "durable Pod IDs",
                        code="invalid_provider_response",
                    )
                now = self._now()
                record["conflict_pod_ids"] = conflict_pod_ids
                record["conflict_review_required_at"] = utc_timestamp(now)
                transition_instance(
                    record,
                    "conflict",
                    at=now,
                    event="duplicate_remote_name",
                    details={"pod_ids": conflict_pod_ids},
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
                reasons = lease_expiry_reasons(record, now=self._now())
                if reasons:
                    raise RunpodLocalError(
                        "launch can no longer submit because its safety "
                        f"deadline expired: {', '.join(reasons)}",
                        code="launch_expired",
                    )
                try:
                    pod = self._api().create_pod(
                        record["pod_payload"],
                        account_ssh_attestation=account_ssh_attestation,
                        **self._provider_call_arguments(deadline),
                    )
                except HttpRequestError:
                    append_event(
                        record, "submission_result_unknown", at=self._now()
                    )
                    self.instances.save(record)
                    raise
                except RunpodLocalError as error:
                    if error.code == GRAPHQL_NO_CAPACITY_ERROR_CODE:
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
            record["provider"] = _durable_provider_snapshot(record, pod)
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
                pod = self._api().get_pod(
                    record["pod_id"],
                    **self._provider_call_arguments(deadline),
                )
            except HttpRequestError as error:
                if error.status == 404:
                    append_event(
                        record, "pod_not_visible_yet", at=self._now()
                    )
                    self.instances.save(record)
                    return record
                raise
            violations, pending = verify_allocated_pod(record, pod)
            record["provider"] = _durable_provider_snapshot(record, pod)
            if violations:
                return self._rollback(
                    record,
                    violations,
                    cleanup_deadline_factory=cleanup_deadline_factory,
                )
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
                record["created_at"]
            )
            activate_lease(
                record,
                ttl_seconds=record["lease_request"]["ttl_seconds"],
                idle_timeout_seconds=record["lease_request"][
                    "idle_timeout_seconds"
                ],
                hard_started_at=hard_started,
                hard_expires_at=_provider_termination_deadline(record),
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
        self,
        record: dict[str, Any],
        violations: list[str],
        *,
        cleanup_deadline_factory: Callable[[], float] | None = None,
    ) -> dict[str, Any]:
        cleanup_deadline = (
            self.monotonic() + LIFECYCLE_CLEANUP_TIMEOUT_SECONDS
            if cleanup_deadline_factory is None
            else cleanup_deadline_factory()
        )
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
            self._api().delete_pod(
                record["pod_id"],
                **self._provider_call_arguments(cleanup_deadline),
            )
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

    def _exact_remote_name_matches(
        self,
        remote_name: str,
        *,
        deadline: float | None = None,
    ) -> list[dict[str, Any]]:
        matches = [
            pod
            for pod in self._api().list_pods(
                **self._provider_call_arguments(deadline)
            )
            if pod.get("name") == remote_name
        ]
        pod_ids = [pod.get("id") for pod in matches]
        if (
            not all(
                isinstance(pod_id, str) and pod_id for pod_id in pod_ids
            )
            or len(set(pod_ids)) != len(pod_ids)
        ):
            raise RunpodLocalError(
                "exact-name Pod results have missing or duplicate identities",
                code="invalid_provider_response",
            )
        return matches

    def _owned_remote_candidates(
        self,
        record: dict[str, Any],
        *,
        name_matches: list[dict[str, Any]] | None = None,
        deadline: float | None = None,
    ) -> list[dict[str, Any]]:
        matches = (
            self._exact_remote_name_matches(
                record["remote_name"],
                deadline=deadline,
            )
            if name_matches is None
            else name_matches
        )
        live_by_id: dict[str, dict[str, Any]] = {}
        for pod_id in _durable_current_pod_ids(record):
            try:
                pod = self._api().get_pod(
                    pod_id,
                    **self._provider_call_arguments(deadline),
                )
            except HttpRequestError as error:
                if error.status != 404:
                    raise
                pod = None
            if pod is not None:
                if (
                    pod.get("id") != pod_id
                    or pod.get("name") != record["remote_name"]
                ):
                    raise RunpodLocalError(
                        "live Pod identity does not match the local receipt; "
                        "refusing destructive action",
                        code="pod_identity_conflict",
                    )
                live_by_id[pod_id] = pod
        for candidate in matches:
            live_by_id[candidate["id"]] = candidate
        return [
            live_by_id[pod_id]
            for pod_id in sorted(live_by_id)
        ]

    def _has_exact_name_identity_conflict(
        self,
        record: dict[str, Any],
        name_matches: list[dict[str, Any]],
    ) -> bool:
        exact_name_ids = {pod["id"] for pod in name_matches}
        durable_ids = set(_durable_current_pod_ids(record))
        return len(exact_name_ids) > 1 or bool(
            durable_ids and exact_name_ids - durable_ids
        )

    def _has_remote_identity_conflict(
        self,
        record: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> bool:
        candidate_ids = {candidate["id"] for candidate in candidates}
        durable_ids = set(_durable_current_pod_ids(record))
        return len(candidate_ids) > 1 or bool(
            durable_ids and candidate_ids - durable_ids
        )

    def _find_owned_remote(
        self,
        record: dict[str, Any],
        *,
        name_matches: list[dict[str, Any]] | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any] | None:
        matches = (
            self._exact_remote_name_matches(
                record["remote_name"],
                deadline=deadline,
            )
            if name_matches is None
            else name_matches
        )
        if self._has_exact_name_identity_conflict(record, matches):
            raise RunpodLocalError(
                "live Pod identities conflict with the local receipt",
                code="pod_identity_conflict",
            )
        candidates = self._owned_remote_candidates(
            record,
            name_matches=matches,
            deadline=deadline,
        )
        if self._has_remote_identity_conflict(record, candidates):
            raise RunpodLocalError(
                "live Pod identities conflict with the local receipt",
                code="pod_identity_conflict",
            )
        return candidates[0] if candidates else None

    def _persist_terminal_exact_name_ids(
        self,
        record: dict[str, Any],
        observed_pod_ids: list[str] | tuple[str, ...],
        *,
        event: str,
    ) -> bool:
        if record["phase"] not in TERMINAL_PHASES:
            raise RunpodLocalError(
                "terminal identity observations require a terminal receipt",
                code="instance_identity_changed",
            )
        if not all(
            isinstance(pod_id, str) and pod_id
            for pod_id in observed_pod_ids
        ):
            raise RunpodLocalError(
                "terminal Pod observations have invalid identities",
                code="invalid_provider_response",
            )
        observed_ids = set(observed_pod_ids)
        durable_ids = set(_durable_current_pod_ids(record))
        new_ids = observed_ids - durable_ids
        if not new_ids:
            return False

        now = self._now()
        if not durable_ids and len(observed_ids) == 1:
            pod_id = next(iter(observed_ids))
            record["pod_id"] = pod_id
            append_event(
                record,
                event,
                at=now,
                details={"pod_id": pod_id},
            )
            self.instances.save(record)
            return False

        expanded_ids = sorted(durable_ids | observed_ids)
        record["conflict_pod_ids"] = expanded_ids
        record.pop("conflict_cleanup_requested_at", None)
        record["conflict_review_required_at"] = utc_timestamp(now)
        append_event(
            record,
            event,
            at=now,
            details={
                "previous_pod_ids": sorted(durable_ids),
                "pod_ids": expanded_ids,
            },
        )
        self.instances.save(record)
        return True

    def _capture_remote_conflict(
        self,
        record: dict[str, Any],
        *,
        matches: list[dict[str, Any]],
        execute: bool,
        reason: str,
        authorize_conflict_cleanup: bool,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        conflict_pod_id_set = {pod["id"] for pod in matches}
        conflict_pod_id_set.update(_durable_current_pod_ids(record))
        conflict_pod_ids = sorted(conflict_pod_id_set)
        if len(conflict_pod_ids) < 2:
            raise RunpodLocalError(
                "duplicate reconciliation results do not have distinct "
                "durable Pod IDs",
                code="invalid_provider_response",
            )
        if not execute:
            preview = dict(record)
            preview["conflict_pod_ids"] = conflict_pod_ids
            return self._terminate_conflict(
                preview,
                execute=False,
                reason=reason,
                authorize_conflict_cleanup=authorize_conflict_cleanup,
                deadline=deadline,
            )

        record["conflict_pod_ids"] = conflict_pod_ids
        now = self._now()
        details = {"pod_ids": conflict_pod_ids, "reason": reason}
        if authorize_conflict_cleanup:
            record.pop("conflict_review_required_at", None)
            record["conflict_cleanup_requested_at"] = utc_timestamp(now)
        else:
            record.pop("conflict_cleanup_requested_at", None)
            record["conflict_review_required_at"] = utc_timestamp(now)
        if record["phase"] in TERMINAL_PHASES:
            append_event(
                record,
                "late_duplicate_remote_name",
                at=now,
                details=details,
            )
        else:
            transition_instance(
                record,
                "conflict",
                at=now,
                event="duplicate_remote_name",
                details=details,
            )
        self.instances.save(record)
        return self._terminate_conflict(
            record,
            execute=True,
            reason=reason,
            authorize_conflict_cleanup=authorize_conflict_cleanup,
            deadline=deadline,
        )

    def _terminate_conflict(
        self,
        record: dict[str, Any],
        *,
        execute: bool,
        reason: str,
        authorize_conflict_cleanup: bool,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        recorded_pod_ids = _recorded_conflict_pod_ids(record)
        recorded_pod_id_set = set(recorded_pod_ids)
        remote_name = record["remote_name"]
        listed_pods = self._api().list_pods(
            **self._provider_call_arguments(deadline)
        )
        listed_name_matches = [
            pod for pod in listed_pods if pod.get("name") == remote_name
        ]
        listed_name_ids = [
            pod.get("id") for pod in listed_name_matches
        ]
        if (
            not all(
                isinstance(pod_id, str) and pod_id
                for pod_id in listed_name_ids
            )
            or len(set(listed_name_ids)) != len(listed_name_ids)
        ):
            raise RunpodLocalError(
                "conflicted Pod list has missing or duplicate identities",
                code="invalid_provider_response",
            )
        unrecorded_name_ids = sorted(
            set(listed_name_ids) - recorded_pod_id_set
        )
        if unrecorded_name_ids:
            expanded_pod_ids = sorted(
                recorded_pod_id_set | set(unrecorded_name_ids)
            )
            if execute:
                now = self._now()
                record["conflict_pod_ids"] = expanded_pod_ids
                record.pop("conflict_cleanup_requested_at", None)
                record["conflict_review_required_at"] = utc_timestamp(now)
                append_event(
                    record,
                    "conflict_identity_expanded",
                    at=now,
                    details={
                        "previous_pod_ids": recorded_pod_ids,
                        "pod_ids": expanded_pod_ids,
                    },
                )
                self.instances.save(record)
            raise RunpodLocalError(
                "new Pod identities were found for the conflicted name; "
                "review the expanded set and execute cleanup again",
                code="conflict_identity_expanded",
            )
        for pod in listed_pods:
            if (
                pod.get("id") in recorded_pod_id_set
                and pod.get("name") != remote_name
            ):
                raise RunpodLocalError(
                    "a recorded conflicted Pod ID now has another name",
                    code="conflict_identity_changed",
                )
        if (
            execute
            and record.get("conflict_cleanup_requested_at") is None
            and not authorize_conflict_cleanup
        ):
            if record.get("conflict_review_required_at") is None:
                now = self._now()
                record["conflict_review_required_at"] = utc_timestamp(now)
                append_event(
                    record,
                    "conflict_cleanup_review_required",
                    at=now,
                    details={"pod_ids": recorded_pod_ids, "reason": reason},
                )
                self.instances.save(record)
            raise RunpodLocalError(
                "the conflicted Pod identity set requires explicit cleanup "
                "after review",
                code="conflict_review_required",
            )
        if (
            execute
            and record.get("conflict_cleanup_requested_at") is None
        ):
            now = self._now()
            record.pop("conflict_review_required_at", None)
            record["conflict_cleanup_requested_at"] = utc_timestamp(now)
            append_event(
                record,
                "conflict_cleanup_authorized",
                at=now,
                details={"pod_ids": recorded_pod_ids, "reason": reason},
            )
            self.instances.save(record)

        live_by_id: dict[str, dict[str, Any]] = {}
        for pod_id in recorded_pod_ids:
            try:
                pod = self._api().get_pod(
                    pod_id,
                    **self._provider_call_arguments(deadline),
                )
            except HttpRequestError as error:
                if error.status == 404:
                    continue
                raise
            if pod.get("id") != pod_id or pod.get("name") != remote_name:
                raise RunpodLocalError(
                    "a recorded conflicted Pod no longer has its exact identity",
                    code="conflict_identity_changed",
                )
            live_by_id[pod_id] = pod
        if set(live_by_id) != set(listed_name_ids):
            raise RunpodLocalError(
                "Runpod Pod-ID and name lookups disagree for the conflict set",
                code="conflict_identity_changed",
            )

        target_pod_ids = sorted(live_by_id)
        terminal = record["phase"] in TERMINAL_PHASES
        result = {
            "schema_version": "runpod.termination-plan.v1",
            "instance_name": record["name"],
            "phase": record["phase"],
            "action": (
                "none"
                if terminal and not target_pod_ids
                else (
                    "delete_terminal_conflicted_pods"
                    if terminal
                    else (
                        "delete_conflicted_pods"
                        if target_pod_ids
                        else "record_conflict_absence"
                    )
                )
            ),
            "pod_ids": target_pod_ids,
            "recorded_pod_ids": recorded_pod_ids,
            "remote_name": remote_name,
            "network_volume_id": record["expected"]["network_volume_id"],
            "volume_action": "preserve",
            "executed": execute,
        }
        if not execute or (terminal and not target_pod_ids):
            return result

        if record.get("conflict_pod_ids") is None:
            record["conflict_pod_ids"] = recorded_pod_ids
        append_event(
            record,
            "conflict_cleanup_started",
            at=self._now(),
            details={"pod_ids": target_pod_ids, "reason": reason},
        )
        self.instances.save(record)
        for pod_id in target_pod_ids:
            try:
                self._api().delete_pod(
                    pod_id,
                    **self._provider_call_arguments(deadline),
                )
            except HttpRequestError as error:
                if error.status == 404:
                    continue
                append_event(
                    record,
                    "conflict_cleanup_failed",
                    at=self._now(),
                    details={"pod_id": pod_id},
                )
                self.instances.save(record)
                raise
        if terminal:
            append_event(
                record,
                "terminal_conflict_cleanup_completed",
                at=self._now(),
                details={"pod_ids": target_pod_ids, "reason": reason},
            )
        else:
            transition_instance(
                record,
                "terminated",
                at=self._now(),
                event="conflict_cleanup_completed",
                details={"pod_ids": target_pod_ids, "reason": reason},
            )
        self.instances.save(record)
        return result

    def terminate(
        self,
        name: str,
        *,
        execute: bool,
        reason: str,
        expected_operation_id: str | None = None,
        require_expired: bool = False,
        observed_terminal_pod_ids: tuple[str, ...] | None = None,
        authorize_conflict_cleanup: bool = True,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        validate_record_name(name)
        with self.state.locked(
            instance_lock_scope(name),
            deadline=deadline,
            monotonic=self.monotonic,
            deadline_error_code="instance_termination_timeout",
        ):
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
            if observed_terminal_pod_ids is not None:
                if expected_operation_id is None:
                    raise RunpodLocalError(
                        "terminal observations require an operation identity",
                        code="instance_identity_changed",
                    )
                if not _submission_may_have_been_sent(record):
                    raise RunpodLocalError(
                        "an unsubmitted receipt cannot own remote Pod "
                        "observations",
                        code="unsubmitted_name_collision",
                    )
                if self._persist_terminal_exact_name_ids(
                    record,
                    observed_terminal_pod_ids,
                    event="ttl_terminal_identity_observed",
                ):
                    raise RunpodLocalError(
                        "the terminal Pod identity set expanded; review the "
                        "durable set and execute cleanup explicitly",
                        code="conflict_identity_expanded",
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
            if (
                record["phase"] in TERMINAL_PHASES
                and not _submission_may_have_been_sent(record)
            ):
                return {
                    "schema_version": "runpod.termination-plan.v1",
                    "instance_name": name,
                    "phase": record["phase"],
                    "action": "none",
                    "executed": execute,
                }
            if record["phase"] == "conflict" or (
                record["phase"] in TERMINAL_PHASES
                and _has_conflict_identity(record)
            ):
                return self._terminate_conflict(
                    record,
                    execute=execute,
                    reason=reason,
                    authorize_conflict_cleanup=(
                        authorize_conflict_cleanup
                    ),
                    deadline=deadline,
                )
            if record["phase"] in TERMINAL_PHASES:
                name_matches = self._exact_remote_name_matches(
                    record["remote_name"],
                    deadline=deadline,
                )
                if self._has_exact_name_identity_conflict(
                    record, name_matches
                ):
                    return self._capture_remote_conflict(
                        record,
                        matches=name_matches,
                        execute=execute,
                        reason=reason,
                        authorize_conflict_cleanup=(
                            authorize_conflict_cleanup
                        ),
                        deadline=deadline,
                    )
                candidates = self._owned_remote_candidates(
                    record,
                    name_matches=name_matches,
                    deadline=deadline,
                )
                if self._has_remote_identity_conflict(
                    record, candidates
                ):
                    return self._capture_remote_conflict(
                        record,
                        matches=candidates,
                        execute=execute,
                        reason=reason,
                        authorize_conflict_cleanup=(
                            authorize_conflict_cleanup
                        ),
                        deadline=deadline,
                    )
                remote = candidates[0] if candidates else None
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
                    if record.get("pod_id") is None:
                        record["pod_id"] = remote["id"]
                        append_event(
                            record,
                            "terminal_leak_identity_saved",
                            at=self._now(),
                            details={"pod_id": remote["id"]},
                        )
                    append_event(
                        record,
                        "terminal_leak_delete_started",
                        at=self._now(),
                        details={
                            "pod_id": remote["id"],
                            "reason": reason,
                        },
                    )
                    self.instances.save(record)
                    try:
                        self._api().delete_pod(
                            remote["id"],
                            **self._provider_call_arguments(deadline),
                        )
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
            name_matches = self._exact_remote_name_matches(
                record["remote_name"],
                deadline=deadline,
            )
            if self._has_exact_name_identity_conflict(
                record, name_matches
            ):
                return self._capture_remote_conflict(
                    record,
                    matches=name_matches,
                    execute=execute,
                    reason=reason,
                    authorize_conflict_cleanup=authorize_conflict_cleanup,
                    deadline=deadline,
                )
            candidates = self._owned_remote_candidates(
                record,
                name_matches=name_matches,
                deadline=deadline,
            )
            if self._has_remote_identity_conflict(record, candidates):
                return self._capture_remote_conflict(
                    record,
                    matches=candidates,
                    execute=execute,
                    reason=reason,
                    authorize_conflict_cleanup=authorize_conflict_cleanup,
                    deadline=deadline,
                )
            remote = candidates[0] if candidates else None
            if remote is None:
                if record["phase"] == "submitting":
                    now = self._now()
                    if now < _provider_termination_deadline(record):
                        raise RunpodLocalError(
                            "submission remains ambiguous; no Pod is visible "
                            "to delete before its provider deadline",
                            code="submission_ambiguous",
                        )
                    result = {
                        "schema_version": "runpod.termination-plan.v1",
                        "instance_name": name,
                        "action": "close_expired_submission",
                        "pod_id": None,
                        "executed": execute,
                    }
                    if execute:
                        transition_instance(
                            record,
                            "aborted",
                            at=now,
                            event="provider_deadline_absence_confirmed",
                            details={"reason": reason},
                        )
                        self.instances.save(record)
                    return result
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
                self._api().delete_pod(
                    remote["id"],
                    **self._provider_call_arguments(deadline),
                )
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
            durable_pod_ids = _durable_current_pod_ids(record)
            durable_id_matches = [
                remote_by_id[durable_pod_id]
                for durable_pod_id in durable_pod_ids
                if durable_pod_id in remote_by_id
            ]
            managed_ids.update(durable_pod_ids)
            drift = []
            name_matches = remote_by_name.get(record["remote_name"], [])
            owns_remote_name = (
                record["phase"] != "intent"
                and _submission_may_have_been_sent(record)
            )
            for candidate in name_matches:
                candidate_id = candidate.get("id")
                if (
                    owns_remote_name
                    and isinstance(candidate_id, str)
                ):
                    managed_ids.add(candidate_id)
            if len(name_matches) > 1:
                drift.append("duplicate_remote_name")
            if pod_id is None and record["phase"] in (
                {"intent", "submitting", "conflict"} | TERMINAL_PHASES
            ):
                if len(name_matches) == 1:
                    pod = name_matches[0]
            elif isinstance(pod_id, str) and any(
                candidate.get("id") != pod_id
                for candidate in name_matches
            ):
                drift.append("pod_identity_conflict")
            if any(
                candidate.get("name") != record["remote_name"]
                for candidate in durable_id_matches
            ):
                drift.append("pod_identity_conflict")
            if live and pod is None and record["phase"] in {
                "provisioning",
                "active",
                "termination_pending",
                "rollback_required",
            }:
                drift.append("managed_pod_missing")
            if pod is not None and pod.get("name") != record["remote_name"]:
                drift.append("pod_name_mismatch")
            if (
                (pod is not None or name_matches or durable_id_matches)
                and record["phase"] in TERMINAL_PHASES
                and _submission_may_have_been_sent(record)
            ):
                drift.append("terminal_receipt_has_live_pod")
            if record["phase"] in {
                "termination_pending",
                "rollback_required",
            }:
                drift.append("cleanup_pending")
            if record["phase"] == "intent" and pod is not None:
                drift.append("unsubmitted_intent_has_live_pod")
            if (
                record["phase"] in TERMINAL_PHASES
                and not _submission_may_have_been_sent(record)
                and name_matches
            ):
                drift.append("unsubmitted_receipt_name_collision")
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
                    "durable_id_matches": durable_id_matches,
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

    def enforce_ttl(
        self,
        *,
        execute: bool,
        protected_instance_names: set[str] | None = None,
    ) -> dict[str, Any]:
        protected_names = set(protected_instance_names or ())
        for name in protected_names:
            validate_record_name(name)
        now = self._now()
        actions = []
        records = []
        for scanned in self.instances.scan():
            if scanned.error is not None:
                actions.append(
                    {
                        "instance_name": scanned.name,
                        "phase": None,
                        "reasons": ["invalid_instance_state"],
                        "executed": False,
                        "blocked_by_active_claims": (
                            scanned.name in protected_names
                        ),
                        "error": {
                            "code": scanned.error.code,
                            "message": str(scanned.error),
                        },
                    }
                )
                continue
            if scanned.value is not None:
                records.append(scanned.value)
        terminal_live_operations: dict[
            tuple[str, str], tuple[str, ...]
        ] = {}
        if execute:
            remote_pods = self._api().list_pods()
            remote_pod_ids = [pod.get("id") for pod in remote_pods]
            if (
                not all(
                    isinstance(pod_id, str) and pod_id
                    for pod_id in remote_pod_ids
                )
                or len(set(remote_pod_ids)) != len(remote_pod_ids)
            ):
                raise RunpodLocalError(
                    "TTL Pod scan has missing or duplicate identities",
                    code="invalid_provider_response",
                )
            remote_ids = set(remote_pod_ids)
            for record in records:
                if record["phase"] not in TERMINAL_PHASES:
                    continue
                if not _submission_may_have_been_sent(record):
                    continue
                observed_name_ids = tuple(
                    sorted(
                        {
                            pod["id"]
                            for pod in remote_pods
                            if pod.get("name") == record["remote_name"]
                            and isinstance(pod.get("id"), str)
                        }
                    )
                )
                durable_id_live = any(
                    pod_id in remote_ids
                    for pod_id in _durable_current_pod_ids(record)
                )
                if observed_name_ids or durable_id_live:
                    terminal_live_operations[
                        (record["name"], record["operation_id"])
                    ] = observed_name_ids
        for record in records:
            reasons = lease_expiry_reasons(record, now=now)
            operation_identity = (
                record["name"],
                record["operation_id"],
            )
            terminal_leak = operation_identity in terminal_live_operations
            if terminal_leak:
                reasons.append("terminal_pod_leak")
            if not reasons:
                continue
            action: dict[str, Any] = {
                "instance_name": record["name"],
                "phase": record["phase"],
                "reasons": reasons,
                "executed": False,
                "blocked_by_active_claims": (
                    record["name"] in protected_names
                ),
            }
            if execute and record["name"] in protected_names:
                action["error"] = {
                    "code": "host_has_active_claims",
                    "message": (
                        f"instance {record['name']} has active host claims"
                    ),
                }
            elif execute:
                try:
                    action["termination"] = self.terminate(
                        record["name"],
                        execute=True,
                        reason="+".join(reasons),
                        expected_operation_id=record["operation_id"],
                        require_expired=not terminal_leak,
                        observed_terminal_pod_ids=(
                            terminal_live_operations[operation_identity]
                            if terminal_leak
                            else None
                        ),
                        authorize_conflict_cleanup=False,
                    )
                    action["executed"] = action["termination"]["executed"]
                except RunpodLocalError as error:
                    action["error"] = {
                        "code": error.code,
                        "message": str(error),
                    }
            actions.append(action)
        actions.sort(key=lambda action: action["instance_name"])
        return {
            "schema_version": "runpod.ttl-enforcement.v1",
            "evaluated_at": utc_timestamp(now),
            "executed": execute,
            "actions": actions,
        }
