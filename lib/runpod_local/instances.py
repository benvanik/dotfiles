"""Local instance receipts, launch payloads, and lease arithmetic."""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Iterator
from typing import Any

from .api import validate_provider_pod_snapshot
from .errors import RunpodLocalError
from .profile import provider_effective_environment_summary, validate_profile
from .state import StateRecordScan, StateStore, validate_record_name
from .template import (
    validate_image_digest,
    validate_private_template_contract,
)
from .timeutil import (
    MAX_DURATION_SECONDS,
    parse_utc_timestamp,
    utc_timestamp,
)

INSTANCE_SCHEMA = "runpod.instance.v4"
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
MIN_IDLE_TIMEOUT_SECONDS = 30
OPERATION_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
INSTANCE_TRANSITIONS = {
    "intent": {"submitting", "aborted"},
    "submitting": {"provisioning", "conflict", "aborted"},
    "provisioning": {
        "active",
        "termination_pending",
        "rollback_required",
        "conflict",
    },
    "active": {"termination_pending", "conflict"},
    "termination_pending": {"terminated", "conflict"},
    "rollback_required": {"rolled_back", "conflict"},
    "conflict": {"terminated"},
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


def instance_lock_scope(name: str) -> str:
    validate_record_name(name)
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]
    return f"instance-{digest}"


def validate_lease_request(
    ttl_seconds: Any, idle_timeout_seconds: Any
) -> None:
    _positive_duration(ttl_seconds, label="lease TTL")
    if idle_timeout_seconds is not None:
        _positive_duration(idle_timeout_seconds, label="idle timeout")
        if idle_timeout_seconds < MIN_IDLE_TIMEOUT_SECONDS:
            raise RunpodLocalError(
                f"idle timeout must be at least {MIN_IDLE_TIMEOUT_SECONDS} seconds",
                code="invalid_lease",
            )


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
    data_center_id: str | None,
    provider_termination_at: str,
) -> dict[str, Any]:
    profile = validate_profile(profile)
    parse_utc_timestamp(provider_termination_at)
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
        "terminateAfter": provider_termination_at,
    }
    if data_center_id is not None:
        payload["dataCenterId"] = data_center_id
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
    while len(events) > MAX_EVENTS:
        activity_index = next(
            (
                index
                for index, existing in enumerate(events)
                if existing.get("event") == "activity"
            ),
            None,
        )
        del events[0 if activity_index is None else activity_index]


def transition_instance(
    record: dict[str, Any],
    phase: str,
    *,
    at: datetime.datetime,
    event: str,
    details: dict[str, Any] | None = None,
) -> None:
    current = record.get("phase")
    if (
        current not in INSTANCE_TRANSITIONS
        or phase not in INSTANCE_TRANSITIONS[current]
    ):
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
    hard_expires_at: datetime.datetime,
    now: datetime.datetime,
) -> None:
    validate_lease_request(ttl_seconds, idle_timeout_seconds)
    utc_timestamp(hard_started_at)
    utc_timestamp(hard_expires_at)
    utc_timestamp(now)
    if hard_expires_at != hard_started_at + datetime.timedelta(
        seconds=ttl_seconds
    ):
        raise RunpodLocalError(
            "lease deadline does not match its hard start and requested TTL",
            code="invalid_lease",
        )
    if now < hard_started_at:
        raise RunpodLocalError(
            "lease activity time precedes its hard start",
            code="invalid_lease",
        )
    record["lease"] = {
        "activated_at": utc_timestamp(hard_started_at),
        "expires_at": utc_timestamp(hard_expires_at),
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
    if record.get("phase") == "termination_pending":
        return ["termination_retry"]
    if record.get("phase") == "rollback_required":
        return ["rollback_retry"]
    if (
        record.get("phase") == "conflict"
        and record.get("conflict_cleanup_requested_at") is not None
    ):
        return ["conflict_cleanup_retry"]
    if record.get("phase") == "intent":
        reasons = []
        provider_termination_at = record.get("provider_termination_at")
        if (
            isinstance(provider_termination_at, str)
            and now >= parse_utc_timestamp(provider_termination_at)
        ):
            reasons.append("hard_ttl")
        deadline = record.get("intent_expires_at")
        if not isinstance(deadline, str):
            raise RunpodLocalError(
                f"instance {record.get('name')} has no launch deadline",
                code="invalid_instance_record",
            )
        if now >= parse_utc_timestamp(deadline):
            reasons.append("launch_intent_timeout")
        return reasons
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
    gpu_memory_gb = expected.get("gpu_memory_gb")
    if (
        not isinstance(expected.get("gpu_id"), str)
        or not isinstance(gpu_count, int)
        or isinstance(gpu_count, bool)
        or gpu_count <= 0
        or not isinstance(gpu_memory_gb, (int, float))
        or isinstance(gpu_memory_gb, bool)
        or not math.isfinite(gpu_memory_gb)
        or gpu_memory_gb <= 0
        or not isinstance(price_cap, (int, float))
        or isinstance(price_cap, bool)
        or not math.isfinite(price_cap)
        or price_cap <= 0
    ):
        raise RunpodLocalError(
            f"instance {name} has an invalid expected placement",
            code="invalid_instance_record",
        )
    try:
        expected_image = validate_image_digest(expected.get("image"))
    except RunpodLocalError as error:
        raise RunpodLocalError(
            f"instance {name} has no immutable expected image",
            code="invalid_instance_record",
        ) from error
    expected_ports = expected.get("ports")
    if expected_ports != ["22/tcp"]:
        raise RunpodLocalError(
            f"instance {name} has an invalid expected port contract",
            code="invalid_instance_record",
        )
    expected_container_disk_gb = expected.get("container_disk_gb")
    expected_volume_in_gb = expected.get("volume_in_gb")
    expected_volume_mount_path = expected.get("volume_mount_path")
    expected_environment_names = expected.get("environment_names")
    expected_environment_sha256 = expected.get("environment_sha256")
    if (
        not isinstance(expected_container_disk_gb, int)
        or isinstance(expected_container_disk_gb, bool)
        or expected_container_disk_gb < 20
        or not isinstance(expected_volume_in_gb, int)
        or isinstance(expected_volume_in_gb, bool)
        or expected_volume_in_gb < 0
        or not isinstance(expected_volume_mount_path, str)
        or not expected_volume_mount_path.startswith("/")
    ):
        raise RunpodLocalError(
            f"instance {name} has an invalid expected storage contract",
            code="invalid_instance_record",
        )
    if (
        not isinstance(expected_environment_names, list)
        or not all(
            isinstance(name, str) and bool(name) and name.isprintable()
            for name in expected_environment_names
        )
        or expected_environment_names != sorted(set(expected_environment_names))
        or not isinstance(expected_environment_sha256, str)
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            expected_environment_sha256,
        )
        or type(expected.get("has_registry_auth")) is not bool
        or expected["has_registry_auth"] is not False
    ):
        raise RunpodLocalError(
            f"instance {name} has an invalid expected provider environment",
            code="invalid_instance_record",
        )
    template_contract = expected.get("template_contract")
    if template_contract is not None:
        try:
            template_contract = validate_private_template_contract(
                template_contract,
                require_id=True,
            )
        except RunpodLocalError as error:
            raise RunpodLocalError(
                f"instance {name} has an invalid expected template contract",
                code="invalid_instance_record",
            ) from error
        if (
            template_contract["image"] != expected_image
            or template_contract["docker_entrypoint"]
            != expected.get("docker_entrypoint")
            or template_contract["docker_start_cmd"]
            != expected.get("docker_start_cmd")
            or template_contract["ports"] != expected_ports
            or template_contract["container_disk_gb"]
            != expected_container_disk_gb
            or template_contract["volume_mount_path"]
            != expected_volume_mount_path
            or template_contract["volume_in_gb"]
            != expected_volume_in_gb
        ):
            raise RunpodLocalError(
                f"instance {name} expected host contract disagrees with its template",
                code="invalid_instance_record",
            )
    elif (
        expected.get("docker_entrypoint") is not None
        or expected.get("docker_start_cmd") is not None
    ):
        raise RunpodLocalError(
            f"instance {name} has Docker overrides without a template",
            code="invalid_instance_record",
        )
    min_vcpu_count = expected.get("min_vcpu_count")
    min_ram_gb = expected.get("min_ram_gb")
    if (
        not isinstance(min_vcpu_count, int)
        or isinstance(min_vcpu_count, bool)
        or min_vcpu_count <= 0
        or not isinstance(min_ram_gb, int)
        or isinstance(min_ram_gb, bool)
        or min_ram_gb <= 0
    ):
        raise RunpodLocalError(
            f"instance {name} has invalid generic host capacity floors",
            code="invalid_instance_record",
        )
    retention = record.get("retention")
    if (
        not isinstance(retention, dict)
        or retention.get("mode") not in {"manual", "while-claimed"}
        or not isinstance(retention.get("empty_grace_seconds"), int)
        or isinstance(retention.get("empty_grace_seconds"), bool)
        or retention["empty_grace_seconds"] < 0
        or retention["empty_grace_seconds"] > MAX_DURATION_SECONDS
    ):
        raise RunpodLocalError(
            f"instance {name} has invalid host retention policy",
            code="invalid_instance_record",
        )
    lease_request = record.get("lease_request")
    if not isinstance(lease_request, dict):
        raise RunpodLocalError(
            f"instance {name} has no lease request",
            code="invalid_instance_record",
        )
    validate_lease_request(
        lease_request.get("ttl_seconds"),
        lease_request.get("idle_timeout_seconds"),
    )
    pod_payload = record.get("pod_payload")
    if not isinstance(pod_payload, dict):
        raise RunpodLocalError(
            f"instance {name} has no Pod request payload",
            code="invalid_instance_record",
        )
    payload_environment = provider_effective_environment_summary(
        pod_payload.get("env")
    )
    if (
        payload_environment is None
        or payload_environment["environment_names"]
        != expected_environment_names
        or payload_environment["environment_sha256"]
        != expected_environment_sha256
        or "containerRegistryAuthId" in pod_payload
    ):
        raise RunpodLocalError(
            f"instance {name} Pod request has a different environment source",
            code="invalid_instance_record",
        )
    if (
        type(pod_payload.get("containerDiskInGb")) is not int
        or pod_payload["containerDiskInGb"] != expected_container_disk_gb
        or type(pod_payload.get("volumeMountPath")) is not str
        or pod_payload["volumeMountPath"] != expected_volume_mount_path
    ):
        raise RunpodLocalError(
            f"instance {name} Pod request has a different storage contract",
            code="invalid_instance_record",
        )
    if template_contract is None:
        if (
            pod_payload.get("imageName") != expected_image
            or "templateId" in pod_payload
        ):
            raise RunpodLocalError(
                f"instance {name} Pod request has a different image source",
                code="invalid_instance_record",
            )
    elif (
        pod_payload.get("templateId") != template_contract["id"]
        or "imageName" in pod_payload
    ):
        raise RunpodLocalError(
            f"instance {name} Pod request has a different template source",
            code="invalid_instance_record",
        )
    expected_network_volume_id = expected.get("network_volume_id")
    if expected_network_volume_id is not None and not isinstance(
        expected_network_volume_id,
        str,
    ):
        raise RunpodLocalError(
            f"instance {name} has an invalid expected network volume",
            code="invalid_instance_record",
        )
    if expected_network_volume_id is not None:
        if (
            pod_payload.get("networkVolumeId")
            != expected_network_volume_id
            or "volumeInGb" in pod_payload
            or expected_volume_in_gb != 0
        ):
            raise RunpodLocalError(
                f"instance {name} Pod request has a different volume source",
                code="invalid_instance_record",
            )
    elif (
        "networkVolumeId" in pod_payload
        or type(pod_payload.get("volumeInGb")) is not int
        or pod_payload["volumeInGb"] != expected_volume_in_gb
    ):
        raise RunpodLocalError(
            f"instance {name} Pod request has a different local volume",
            code="invalid_instance_record",
        )
    provider_termination_at = record.get("provider_termination_at")
    payload_termination_at = pod_payload.get("terminateAfter")
    if not isinstance(provider_termination_at, str):
        raise RunpodLocalError(
            f"instance {name} has no provider termination deadline",
            code="invalid_instance_record",
        )
    provider_deadline = parse_utc_timestamp(provider_termination_at)
    if payload_termination_at != provider_termination_at:
        raise RunpodLocalError(
            f"instance {name} Pod request has a different termination "
            "deadline",
            code="invalid_instance_record",
        )
    created_at = parse_utc_timestamp(record["created_at"])
    expected_deadline = created_at + datetime.timedelta(
        seconds=lease_request["ttl_seconds"]
    )
    if provider_deadline != expected_deadline:
        raise RunpodLocalError(
            f"instance {name} provider termination deadline does not "
            "match its intent clock and requested TTL",
            code="invalid_instance_record",
        )
    expected_data_center_id = expected.get("data_center_id")
    payload_data_center_id = pod_payload.get("dataCenterId")
    if payload_data_center_id != expected_data_center_id:
        raise RunpodLocalError(
            f"instance {name} Pod request has a different data center",
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
    conflict_pod_ids = record.get("conflict_pod_ids")
    if conflict_pod_ids is not None and (
        not isinstance(conflict_pod_ids, list)
        or len(conflict_pod_ids) < 2
        or not all(
            isinstance(conflict_pod_id, str) and conflict_pod_id
            for conflict_pod_id in conflict_pod_ids
        )
        or conflict_pod_ids != sorted(set(conflict_pod_ids))
        or phase not in {"conflict", "rolled_back", "terminated", "aborted"}
    ):
        raise RunpodLocalError(
            f"instance {name} has invalid conflicted Pod identities",
            code="invalid_instance_record",
        )
    if phase == "conflict" and conflict_pod_ids is None:
        raise RunpodLocalError(
            f"instance {name} is conflicted but has no Pod identities",
            code="invalid_instance_record",
        )
    conflict_cleanup_requested_at = record.get(
        "conflict_cleanup_requested_at"
    )
    if conflict_cleanup_requested_at is not None:
        if (
            conflict_pod_ids is None
            or phase
            not in {"conflict", "rolled_back", "terminated", "aborted"}
            or not isinstance(conflict_cleanup_requested_at, str)
        ):
            raise RunpodLocalError(
                f"instance {name} has invalid conflict cleanup intent",
                code="invalid_instance_record",
            )
        parse_utc_timestamp(conflict_cleanup_requested_at)
    conflict_review_required_at = record.get(
        "conflict_review_required_at"
    )
    if conflict_review_required_at is not None:
        if (
            conflict_pod_ids is None
            or phase
            not in {"conflict", "rolled_back", "terminated", "aborted"}
            or not isinstance(conflict_review_required_at, str)
        ):
            raise RunpodLocalError(
                f"instance {name} has invalid conflict review state",
                code="invalid_instance_record",
            )
        parse_utc_timestamp(conflict_review_required_at)
    if (
        conflict_cleanup_requested_at is not None
        and conflict_review_required_at is not None
    ):
        raise RunpodLocalError(
            f"instance {name} has conflicting cleanup authority",
            code="invalid_instance_record",
        )
    if (
        conflict_pod_ids is not None
        and conflict_cleanup_requested_at is None
        and conflict_review_required_at is None
    ):
        raise RunpodLocalError(
            f"instance {name} has no conflict cleanup disposition",
            code="invalid_instance_record",
        )
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
    provider = record.get("provider")
    if provider is not None:
        try:
            validate_provider_pod_snapshot(provider)
        except RunpodLocalError as error:
            raise RunpodLocalError(
                f"instance {name} has an invalid durable provider snapshot",
                code="invalid_instance_record",
            ) from error
        if pod_id is None:
            raise RunpodLocalError(
                f"instance {name} has provider evidence without a Pod ID",
                code="invalid_instance_record",
            )
    submission_started_at = record.get("submission_started_at")
    lease = record.get("lease")
    if submission_started_at is not None:
        if not isinstance(submission_started_at, str):
            raise RunpodLocalError(
                f"instance {name} has an invalid submission start",
                code="invalid_instance_record",
            )
        parse_utc_timestamp(submission_started_at)
    if phase == "intent" and (
        submission_started_at is not None or lease is not None
    ):
        raise RunpodLocalError(
            f"unsubmitted intent {name} has submission state",
            code="invalid_instance_record",
        )
    if phase == "aborted" and submission_started_at is None and (
        lease is not None
        or pod_id is not None
        or conflict_pod_ids is not None
    ):
        raise RunpodLocalError(
            f"unsubmitted aborted receipt {name} owns provider state",
            code="invalid_instance_record",
        )
    if (
        phase not in {"intent", "aborted"}
        and submission_started_at is None
    ):
        raise RunpodLocalError(
            f"instance {name} has no submission start",
            code="invalid_instance_record",
        )
    if submission_started_at is not None and not isinstance(lease, dict):
        raise RunpodLocalError(
            f"submitted instance {name} has no lease",
            code="invalid_instance_record",
        )
    if submission_started_at is not None:
        if not isinstance(lease, dict):
            raise RunpodLocalError(
                f"instance {name} has no submission lease",
                code="invalid_instance_record",
            )
        lease_expires_at = parse_utc_timestamp(
            str(lease.get("expires_at", ""))
        )
        activated_at = parse_utc_timestamp(
            str(lease.get("activated_at", ""))
        )
        if activated_at != parse_utc_timestamp(record["created_at"]):
            raise RunpodLocalError(
                f"instance {name} lease is not anchored to its intent clock",
                code="invalid_instance_record",
            )
        lease_ttl_seconds = lease.get("ttl_seconds")
        extensions_total_seconds = lease.get(
            "extensions_total_seconds", 0
        )
        if (
            not isinstance(lease_ttl_seconds, int)
            or isinstance(lease_ttl_seconds, bool)
            or not isinstance(extensions_total_seconds, int)
            or isinstance(extensions_total_seconds, bool)
            or extensions_total_seconds < 0
            or lease_expires_at
            != activated_at
            + datetime.timedelta(
                seconds=(
                    lease_ttl_seconds + extensions_total_seconds
                )
            )
        ):
            raise RunpodLocalError(
                f"instance {name} lease deadline does not match its local "
                "TTL history",
                code="invalid_instance_record",
            )
        if lease_expires_at > parse_utc_timestamp(
            provider_termination_at
        ):
            raise RunpodLocalError(
                f"instance {name} lease exceeds its provider termination "
                "deadline",
                code="invalid_instance_record",
            )
    if phase == "active":
        lease = record.get("lease")
        if not isinstance(lease, dict):
            raise RunpodLocalError(
                f"active instance {name} has no lease",
                code="invalid_instance_record",
            )
        validate_lease_request(
            lease.get("ttl_seconds"),
            lease.get("idle_timeout_seconds"),
        )
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

    def scan(self) -> list[StateRecordScan]:
        records = []
        for scanned in self.state.scan("instances"):
            if scanned.error is not None:
                records.append(scanned)
                continue
            try:
                record = validate_instance_record(scanned.value)
                if record["name"] != scanned.name:
                    raise RunpodLocalError(
                        "instance receipt is stored under another local name",
                        code="invalid_instance_record",
                    )
            except RunpodLocalError as error:
                records.append(
                    StateRecordScan(
                        name=scanned.name,
                        value=None,
                        error=error,
                    )
                )
                continue
            records.append(
                StateRecordScan(
                    name=scanned.name,
                    value=record,
                    error=None,
                )
            )
        return records

    def set_ttl(
        self,
        name: str,
        *,
        ttl_seconds: int,
        now: datetime.datetime,
    ) -> dict[str, Any]:
        _positive_duration(ttl_seconds, label="lease TTL")
        with self.state.locked(instance_lock_scope(name)):
            record = self.load(name)
            if record is None:
                raise AssertionError("required instance unexpectedly absent")
            if record["phase"] != "active":
                raise RunpodLocalError(
                    f"cannot set TTL while instance {name} is {record['phase']}",
                    code="instance_not_active",
                )
            provider_termination_at = record["provider_termination_at"]
            if lease_expiry_reasons(record, now=now):
                raise RunpodLocalError(
                    f"cannot set TTL after instance {name} has expired",
                    code="lease_expired",
                )
            lease = record["lease"]
            activated_at = parse_utc_timestamp(lease["activated_at"])
            expires_at = activated_at + datetime.timedelta(
                seconds=ttl_seconds
            )
            if expires_at <= now:
                raise RunpodLocalError(
                    "new hard TTL would already be expired",
                    code="invalid_lease",
                )
            provider_deadline = parse_utc_timestamp(
                provider_termination_at
            )
            if expires_at > provider_deadline:
                raise RunpodLocalError(
                    "new hard TTL would exceed the immutable provider "
                    "termination deadline",
                    code="provider_deadline_exceeded",
                )
            lease["ttl_seconds"] = ttl_seconds
            lease["expires_at"] = utc_timestamp(expires_at)
            lease.pop("extensions_total_seconds", None)
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
        with self.state.locked(instance_lock_scope(name)):
            record = self.load(name)
            if record is None:
                raise AssertionError("required instance unexpectedly absent")
            if record["phase"] != "active":
                raise RunpodLocalError(
                    f"cannot extend TTL while instance {name} is {record['phase']}",
                    code="instance_not_active",
                )
            provider_termination_at = record["provider_termination_at"]
            if lease_expiry_reasons(record, now=now):
                raise RunpodLocalError(
                    f"cannot extend TTL after instance {name} has expired",
                    code="lease_expired",
                )
            lease = record["lease"]
            current = parse_utc_timestamp(lease["expires_at"])
            extended = current + datetime.timedelta(seconds=extension_seconds)
            if extended > parse_utc_timestamp(provider_termination_at):
                raise RunpodLocalError(
                    "extended lease would exceed the immutable provider "
                    "termination deadline",
                    code="provider_deadline_exceeded",
                )
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
        expected_operation_id: str | None = None,
        expected_pod_id: str | None = None,
        record_event: bool = True,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
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
        with self.state.locked(
            instance_lock_scope(name),
            deadline=deadline,
            monotonic=monotonic,
            deadline_error_code="remote_client_timeout",
        ):
            record = self.load(name)
            if record is None:
                raise AssertionError("required instance unexpectedly absent")
            if record["phase"] != "active":
                raise RunpodLocalError(
                    f"cannot record activity while instance {name} is "
                    f"{record['phase']}",
                    code="instance_not_active",
                )
            if (
                expected_operation_id is not None
                and record["operation_id"] != expected_operation_id
            ) or (
                expected_pod_id is not None
                and record.get("pod_id") != expected_pod_id
            ):
                raise RunpodLocalError(
                    f"instance {name} no longer owns the expected Pod operation",
                    code="instance_identity_changed",
                )
            if lease_expiry_reasons(record, now=now):
                raise RunpodLocalError(
                    f"cannot record activity after instance {name} has expired",
                    code="lease_expired",
                )
            if deadline is not None and monotonic() >= deadline:
                raise RunpodLocalError(
                    "instance activity exceeded the remote-client deadline",
                    code="remote_client_timeout",
                )
            record["lease"]["last_activity_at"] = utc_timestamp(now)
            record["lease"]["activity_source"] = source
            if record_event:
                append_event(
                    record,
                    "activity",
                    at=now,
                    details={"source": source},
                )
            if deadline is not None and monotonic() >= deadline:
                raise RunpodLocalError(
                    "instance activity exceeded the remote-client deadline",
                    code="remote_client_timeout",
                )
            self.save(record)
            return record

    def check_active_lease(
        self,
        name: str,
        *,
        now: datetime.datetime,
        expected_operation_id: str,
        expected_pod_id: str,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> dict[str, Any]:
        with self.locked_active_lease(
            name,
            expected_operation_id=expected_operation_id,
            expected_pod_id=expected_pod_id,
            clock=lambda: now,
            deadline=deadline,
            monotonic=monotonic,
        ) as record:
            return record

    @contextlib.contextmanager
    def locked_active_lease(
        self,
        name: str,
        *,
        expected_operation_id: str,
        expected_pod_id: str,
        clock: Callable[[], datetime.datetime] | None = None,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> Iterator[dict[str, Any]]:
        """Hold the instance CAS boundary while a dependent receipt commits."""

        current_time = clock or (
            lambda: datetime.datetime.now(datetime.timezone.utc)
        )
        with self.state.locked(
            instance_lock_scope(name),
            deadline=deadline,
            monotonic=monotonic,
            deadline_error_code="remote_client_timeout",
        ):
            now = current_time()
            record = self.load(name)
            if record is None:
                raise AssertionError("required instance unexpectedly absent")
            if record["phase"] != "active":
                raise RunpodLocalError(
                    f"instance {name} is {record['phase']}, not active",
                    code="instance_not_active",
                )
            if (
                record["operation_id"] != expected_operation_id
                or record.get("pod_id") != expected_pod_id
            ):
                raise RunpodLocalError(
                    f"instance {name} no longer owns the expected Pod operation",
                    code="instance_identity_changed",
                )
            reasons = lease_expiry_reasons(record, now=now)
            if reasons:
                raise RunpodLocalError(
                    "instance lease has expired: " + ", ".join(reasons),
                    code="lease_expired",
                )
            if deadline is not None and monotonic() >= deadline:
                raise RunpodLocalError(
                    "instance lease check exceeded the remote-client deadline",
                    code="remote_client_timeout",
                )
            yield record
