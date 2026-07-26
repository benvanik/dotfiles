"""Local instance receipts, launch payloads, and lease arithmetic."""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import re
from typing import Any

from .errors import RunpodLocalError
from .profile import validate_profile
from .state import StateStore, validate_record_name
from .timeutil import (
    MAX_DURATION_SECONDS,
    parse_utc_timestamp,
    utc_timestamp,
)


INSTANCE_SCHEMA = "runpod.instance.v1"
INSTANCE_PHASES = {
    "intent",
    "submitting",
    "provisioning",
    "active",
    "termination_pending",
    "rollback_required",
    "conflict",
    "rolled_back",
    "terminated",
    "aborted",
}
POD_OWNING_PHASES = {
    "provisioning",
    "active",
    "termination_pending",
    "rollback_required",
}
MAX_EVENTS = 100
INTENT_TTL_SECONDS = 15 * 60
OPERATION_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
INSTANCE_TRANSITIONS = {
    "intent": {"submitting", "aborted"},
    "submitting": {"provisioning", "conflict"},
    "provisioning": {"active", "termination_pending", "rollback_required"},
    "active": {"termination_pending"},
    "termination_pending": {"terminated"},
    "rollback_required": {"rolled_back"},
    "conflict": set(),
    "rolled_back": set(),
    "terminated": set(),
    "aborted": set(),
}


def _positive_duration(value: Any, *, label: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
        or value > MAX_DURATION_SECONDS
    ):
        raise RunpodLocalError(
            f"{label} must be between 1 second and 30 days",
            code="invalid_lease",
        )
    return value


def validate_lease_request(
    ttl_seconds: Any, idle_timeout_seconds: Any
) -> None:
    _positive_duration(ttl_seconds, label="lease TTL")
    if idle_timeout_seconds is not None:
        _positive_duration(idle_timeout_seconds, label="idle timeout")


def json_document_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def profile_hash(profile: dict[str, Any]) -> str:
    return json_document_hash(profile)


def build_pod_payload(
    profile: dict[str, Any],
    *,
    remote_name: str,
    gpu_id: str,
) -> dict[str, Any]:
    profile = validate_profile(profile)
    pod = profile["pod"]
    if gpu_id not in pod["gpu_type_ids"]:
        raise RunpodLocalError(
            f"GPU is not allowed by profile {profile['name']}: {gpu_id}",
            code="gpu_not_allowed",
        )
    payload: dict[str, Any] = {
        "name": remote_name,
        "cloudType": "SECURE",
        "computeType": "GPU",
        "gpuTypeIds": [gpu_id],
        "gpuTypePriority": "custom",
        "gpuCount": pod["gpu_count"],
        "containerDiskInGb": pod["container_disk_gb"],
        "volumeMountPath": pod["volume_mount_path"],
        "ports": pod["ports"],
        "env": pod["environment"],
        "interruptible": False,
        "locked": False,
        "minVCPUPerGPU": pod["min_vcpu_per_gpu"],
        "minRAMPerGPU": pod["min_ram_per_gpu"],
    }
    if pod["allowed_cuda_versions"]:
        payload["allowedCudaVersions"] = pod["allowed_cuda_versions"]
    if pod["template_id"] is not None:
        payload["templateId"] = pod["template_id"]
    else:
        payload["imageName"] = pod["image_name"]
    if pod["network_volume_id"] is not None:
        payload["networkVolumeId"] = pod["network_volume_id"]
    else:
        payload["volumeInGb"] = 20
    return payload


def append_event(
    record: dict[str, Any],
    event: str,
    *,
    at: datetime.datetime | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    events = record.setdefault("events", [])
    if not isinstance(events, list):
        raise RunpodLocalError(
            "instance event history is not a list",
            code="invalid_instance_record",
        )
    entry: dict[str, Any] = {
        "at": utc_timestamp(at),
        "event": event,
    }
    if details:
        entry["details"] = details
    events.append(entry)
    if len(events) > MAX_EVENTS:
        del events[:-MAX_EVENTS]


def transition_instance(
    record: dict[str, Any],
    phase: str,
    *,
    at: datetime.datetime,
    event: str,
    details: dict[str, Any] | None = None,
) -> None:
    current = record.get("phase")
    if current not in INSTANCE_TRANSITIONS or phase not in INSTANCE_TRANSITIONS[current]:
        raise RunpodLocalError(
            f"illegal instance transition: {current!r} -> {phase!r}",
            code="invalid_instance_transition",
        )
    record["phase"] = phase
    record["updated_at"] = utc_timestamp(at)
    append_event(record, event, at=at, details=details)


def activate_lease(
    record: dict[str, Any],
    *,
    ttl_seconds: int,
    idle_timeout_seconds: int | None,
    hard_started_at: datetime.datetime,
    now: datetime.datetime,
) -> None:
    validate_lease_request(ttl_seconds, idle_timeout_seconds)
    utc_timestamp(hard_started_at)
    utc_timestamp(now)
    if now < hard_started_at:
        raise RunpodLocalError(
            "lease activity time precedes its hard start",
            code="invalid_lease",
        )
    record["lease"] = {
        "activated_at": utc_timestamp(hard_started_at),
        "expires_at": utc_timestamp(
            hard_started_at + datetime.timedelta(seconds=ttl_seconds)
        ),
        "ttl_seconds": ttl_seconds,
        "idle_timeout_seconds": idle_timeout_seconds,
        "last_activity_at": utc_timestamp(now),
        "activity_source": "explicit_heartbeat",
        "expiry_action": "terminate",
    }


def lease_expiry_reasons(
    record: dict[str, Any], *, now: datetime.datetime
) -> list[str]:
    utc_timestamp(now)
    if record.get("phase") == "intent":
        deadline = record.get("intent_expires_at")
        if not isinstance(deadline, str):
            raise RunpodLocalError(
                f"instance {record.get('name')} has no launch deadline",
                code="invalid_instance_record",
            )
        if now >= parse_utc_timestamp(deadline):
            return ["launch_intent_timeout"]
        return []
    if record.get("phase") in {"submitting", "provisioning"}:
        reasons = []
        lease = record.get("lease")
        if not isinstance(lease, dict) or not isinstance(
            lease.get("expires_at"), str
        ):
            raise RunpodLocalError(
                f"instance {record.get('name')} has no submission lease",
                code="invalid_instance_record",
            )
        if now >= parse_utc_timestamp(lease["expires_at"]):
            reasons.append("hard_ttl")
        deadline = record.get("intent_expires_at")
        if not isinstance(deadline, str):
            raise RunpodLocalError(
                f"instance {record.get('name')} has no launch deadline",
                code="invalid_instance_record",
            )
        if now >= parse_utc_timestamp(deadline):
            reasons.append("launch_reconciliation_timeout")
        return reasons
    if record.get("phase") != "active":
        return []
    lease = record.get("lease")
    if not isinstance(lease, dict):
        raise RunpodLocalError(
            f"active instance {record.get('name')} has no lease",
            code="invalid_instance_record",
        )
    reasons = []
    expires_at = lease.get("expires_at")
    if not isinstance(expires_at, str):
        raise RunpodLocalError(
            f"instance {record.get('name')} has no absolute lease deadline",
            code="invalid_instance_record",
        )
    if now >= parse_utc_timestamp(expires_at):
        reasons.append("hard_ttl")
    idle_timeout = lease.get("idle_timeout_seconds")
    if idle_timeout is not None:
        if not isinstance(idle_timeout, int) or idle_timeout <= 0:
            raise RunpodLocalError(
                f"instance {record.get('name')} has an invalid idle timeout",
                code="invalid_instance_record",
            )
        activity = lease.get("last_activity_at")
        if not isinstance(activity, str):
            raise RunpodLocalError(
                f"instance {record.get('name')} has no activity timestamp",
                code="invalid_instance_record",
            )
        idle_deadline = parse_utc_timestamp(activity) + datetime.timedelta(
            seconds=idle_timeout
        )
        if now >= idle_deadline:
            reasons.append("explicit_heartbeat_idle_timeout")
    return reasons


def validate_instance_record(record: dict[str, Any]) -> dict[str, Any]:
    if record.get("schema_version") != INSTANCE_SCHEMA:
        raise RunpodLocalError(
            "instance record has an unsupported schema version",
            code="invalid_instance_record",
        )
    name = record.get("name")
    phase = record.get("phase")
    if not isinstance(name, str):
        raise RunpodLocalError(
            "instance record has no local name",
            code="invalid_instance_record",
        )
    validate_record_name(name)
    if phase not in INSTANCE_PHASES:
        raise RunpodLocalError(
            f"instance {name} has an invalid phase: {phase!r}",
            code="invalid_instance_record",
        )
    operation_id = record.get("operation_id")
    if (
        not isinstance(operation_id, str)
        or not OPERATION_ID_PATTERN.fullmatch(operation_id)
    ):
        raise RunpodLocalError(
            f"instance {name} has an invalid operation ID",
            code="invalid_instance_record",
        )
    remote_name = record.get("remote_name")
    if (
        not isinstance(remote_name, str)
        or not remote_name
        or len(remote_name) > 191
        or any(ord(character) < 32 for character in remote_name)
    ):
        raise RunpodLocalError(
            f"instance {name} has no remote name",
            code="invalid_instance_record",
        )
    for timestamp_name in ("created_at", "updated_at", "intent_expires_at"):
        timestamp = record.get(timestamp_name)
        if not isinstance(timestamp, str):
            raise RunpodLocalError(
                f"instance {name} has no {timestamp_name}",
                code="invalid_instance_record",
            )
        parse_utc_timestamp(timestamp)
    profile = record.get("profile")
    if (
        not isinstance(profile, dict)
        or not isinstance(profile.get("name"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", str(profile.get("sha256", "")))
    ):
        raise RunpodLocalError(
            f"instance {name} has an invalid profile identity",
            code="invalid_instance_record",
        )
    expected = record.get("expected")
    if not isinstance(expected, dict):
        raise RunpodLocalError(
            f"instance {name} has no expected placement",
            code="invalid_instance_record",
        )
    price_cap = expected.get("max_hourly_usd")
    gpu_count = expected.get("gpu_count")
    if (
        not isinstance(expected.get("gpu_id"), str)
        or not isinstance(gpu_count, int)
        or isinstance(gpu_count, bool)
        or gpu_count <= 0
        or not isinstance(price_cap, (int, float))
        or isinstance(price_cap, bool)
        or not math.isfinite(price_cap)
        or price_cap <= 0
    ):
        raise RunpodLocalError(
            f"instance {name} has an invalid expected placement",
            code="invalid_instance_record",
        )
    lease_request = record.get("lease_request")
    if not isinstance(lease_request, dict):
        raise RunpodLocalError(
            f"instance {name} has no lease request",
            code="invalid_instance_record",
        )
    _positive_duration(
        lease_request.get("ttl_seconds"), label="requested lease TTL"
    )
    idle_timeout = lease_request.get("idle_timeout_seconds")
    if idle_timeout is not None:
        _positive_duration(idle_timeout, label="requested idle timeout")
    if not isinstance(record.get("pod_payload"), dict):
        raise RunpodLocalError(
            f"instance {name} has no Pod request payload",
            code="invalid_instance_record",
        )
    payload_hash = record.get("pod_payload_sha256")
    if (
        not isinstance(payload_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", payload_hash)
        or payload_hash != json_document_hash(record["pod_payload"])
    ):
        raise RunpodLocalError(
            f"instance {name} Pod request hash does not match",
            code="invalid_instance_record",
        )
    events = record.get("events")
    if (
        not isinstance(events, list)
        or len(events) > MAX_EVENTS
        or not all(
            isinstance(event, dict)
            and isinstance(event.get("event"), str)
            and isinstance(event.get("at"), str)
            for event in events
        )
    ):
        raise RunpodLocalError(
            f"instance {name} has an invalid event history",
            code="invalid_instance_record",
        )
    for event in events:
        parse_utc_timestamp(event["at"])
    pod_id = record.get("pod_id")
    if pod_id is not None and (not isinstance(pod_id, str) or not pod_id):
        raise RunpodLocalError(
            f"instance {name} has an invalid Pod ID",
            code="invalid_instance_record",
        )
    if phase in POD_OWNING_PHASES and pod_id is None:
        raise RunpodLocalError(
            f"instance {name} is {phase} but has no Pod ID",
            code="invalid_instance_record",
        )
    if phase in {
        "submitting",
        "provisioning",
        "active",
        "termination_pending",
        "rollback_required",
    }:
        submission_started_at = record.get("submission_started_at")
        if not isinstance(submission_started_at, str):
            raise RunpodLocalError(
                f"instance {name} has no submission start",
                code="invalid_instance_record",
            )
        parse_utc_timestamp(submission_started_at)
        if not isinstance(record.get("lease"), dict):
            raise RunpodLocalError(
                f"instance {name} has no submission lease",
                code="invalid_instance_record",
            )
    if phase == "active":
        lease = record.get("lease")
        if not isinstance(lease, dict):
            raise RunpodLocalError(
                f"active instance {name} has no lease",
                code="invalid_instance_record",
            )
        _positive_duration(lease.get("ttl_seconds"), label="lease TTL")
        idle_timeout = lease.get("idle_timeout_seconds")
        if idle_timeout is not None:
            _positive_duration(idle_timeout, label="idle timeout")
        parse_utc_timestamp(str(lease.get("activated_at", "")))
        parse_utc_timestamp(str(lease.get("expires_at", "")))
        parse_utc_timestamp(str(lease.get("last_activity_at", "")))
    return record


class InstanceStore:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    def load(self, name: str, *, required: bool = True) -> dict[str, Any] | None:
        validate_record_name(name)
        record = self.state.read("instances", name)
        if record is None:
            if required:
                raise RunpodLocalError(
                    f"instance does not exist locally: {name}",
                    code="instance_not_found",
                )
            return None
        return validate_instance_record(record)

    def save(self, record: dict[str, Any]) -> None:
        record = validate_instance_record(record)
        self.state.write("instances", record["name"], record)

    def list(self) -> list[dict[str, Any]]:
        return [
            validate_instance_record(record)
            for record in self.state.list("instances")
        ]

    def set_ttl(
        self,
        name: str,
        *,
        ttl_seconds: int,
        now: datetime.datetime,
    ) -> dict[str, Any]:
        _positive_duration(ttl_seconds, label="lease TTL")
        with self.state.locked("instances"):
            record = self.load(name)
            if record is None:
                raise AssertionError("required instance unexpectedly absent")
            if record["phase"] != "active":
                raise RunpodLocalError(
                    f"cannot set TTL while instance {name} is {record['phase']}",
                    code="instance_not_active",
                )
            if lease_expiry_reasons(record, now=now):
                raise RunpodLocalError(
                    f"cannot set TTL after instance {name} has expired",
                    code="lease_expired",
                )
            lease = record["lease"]
            lease["ttl_seconds"] = ttl_seconds
            lease["expires_at"] = utc_timestamp(
                now + datetime.timedelta(seconds=ttl_seconds)
            )
            append_event(
                record,
                "ttl_set",
                at=now,
                details={"ttl_seconds": ttl_seconds},
            )
            self.save(record)
            return record

    def extend_ttl(
        self,
        name: str,
        *,
        extension_seconds: int,
        now: datetime.datetime,
    ) -> dict[str, Any]:
        _positive_duration(extension_seconds, label="lease extension")
        with self.state.locked("instances"):
            record = self.load(name)
            if record is None:
                raise AssertionError("required instance unexpectedly absent")
            if record["phase"] != "active":
                raise RunpodLocalError(
                    f"cannot extend TTL while instance {name} is {record['phase']}",
                    code="instance_not_active",
                )
            if lease_expiry_reasons(record, now=now):
                raise RunpodLocalError(
                    f"cannot extend TTL after instance {name} has expired",
                    code="lease_expired",
                )
            lease = record["lease"]
            current = parse_utc_timestamp(lease["expires_at"])
            extended = current + datetime.timedelta(seconds=extension_seconds)
            if extended - now > datetime.timedelta(
                seconds=MAX_DURATION_SECONDS
            ):
                raise RunpodLocalError(
                    "extended lease would exceed the 30-day safety limit",
                    code="invalid_lease",
                )
            lease["expires_at"] = utc_timestamp(extended)
            lease["extensions_total_seconds"] = (
                lease.get("extensions_total_seconds", 0) + extension_seconds
            )
            append_event(
                record,
                "ttl_extended",
                at=now,
                details={"extension_seconds": extension_seconds},
            )
            self.save(record)
            return record

    def touch(
        self,
        name: str,
        *,
        now: datetime.datetime,
        source: str,
    ) -> dict[str, Any]:
        if (
            not source
            or len(source) > 80
            or any(ord(character) < 32 for character in source)
        ):
            raise RunpodLocalError(
                "activity source must be a short printable string",
                code="invalid_activity_source",
            )
        with self.state.locked("instances"):
            record = self.load(name)
            if record is None:
                raise AssertionError("required instance unexpectedly absent")
            if record["phase"] != "active":
                raise RunpodLocalError(
                    f"cannot record activity while instance {name} is {record['phase']}",
                    code="instance_not_active",
                )
            if lease_expiry_reasons(record, now=now):
                raise RunpodLocalError(
                    f"cannot record activity after instance {name} has expired",
                    code="lease_expired",
                )
            record["lease"]["last_activity_at"] = utc_timestamp(now)
            record["lease"]["activity_source"] = source
            append_event(
                record,
                "activity",
                at=now,
                details={"source": source},
            )
            self.save(record)
            return record
