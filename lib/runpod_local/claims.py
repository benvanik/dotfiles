"""Opaque, crash-safe consumer claims over generic Runpod hosts."""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from .errors import RunpodLocalError
from .state import StateRecordScan, StateStore, validate_record_name
from .timeutil import MAX_DURATION_SECONDS, parse_utc_timestamp, utc_timestamp


CLAIM_LEDGER_SCHEMA = "runpod.host-claim-ledger.v1"
CLAIM_SCHEMA = "runpod.host-claim.v1"
CLAIM_RELEASE_SCHEMA = "runpod.host-claim-release.v1"
CLAIM_REQUEST_IDENTITY_V1 = "runpod.host-claim-request.v1"
CLAIM_REQUEST_IDENTITY_V2 = "runpod.host-claim-request.v2"
CLAIM_MODES = frozenset({"shared", "gpu-exclusive", "host-exclusive"})
RETENTION_MODES = frozenset({"manual", "while-claimed"})
ENDPOINT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
OWNER_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9.-]{0,62}$")
OWNER_OPERATION_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")
CLAIM_ID_PATTERN = re.compile(r"^claim-[0-9a-f]{32}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MIN_REMOTE_PORT = 18000
MAX_REMOTE_PORT = 28999
MAX_CLAIMS_PER_HOST = 256
MAX_CLOSED_CLAIMS_PER_HOST = 4096
MAX_ENDPOINTS_PER_CLAIM = 32
CLOSED_CLAIM_REASONS = frozenset(
    {
        "acquisition-timeout",
        "expired",
        "host-operation-ended",
        "released",
    }
)
CLAIM_RELEASE_REASONS = frozenset(
    {"acquisition-timeout", "released"}
)
CLAIM_EXPIRY_QUARANTINE_REASON = "expired-claim-cleanup-unproven"
HOST_OPERATION_END_REASON = "host-operation-ended"


def _positive_duration(value: Any, *, label: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
        or value > MAX_DURATION_SECONDS
    ):
        raise RunpodLocalError(
            f"{label} must be between 1 second and 30 days",
            code="invalid_host_claim",
        )
    return value


def _nonnegative_integer(value: Any, *, label: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        raise RunpodLocalError(
            f"{label} must be a nonnegative integer",
            code="invalid_host_claim",
        )
    return value


def _positive_integer(value: Any, *, label: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
    ):
        raise RunpodLocalError(
            f"{label} must be a positive integer",
            code="invalid_host_claim",
        )
    return value


def _nonnegative_number(value: Any, *, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise RunpodLocalError(
            f"{label} must be a finite nonnegative number",
            code="invalid_host_claim",
        )
    return float(value)


def validate_claim_owner_name(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not OWNER_NAME_PATTERN.fullmatch(value):
        raise RunpodLocalError(
            f"{label} must be a lowercase opaque owner name",
            code="invalid_host_claim",
        )
    return value


def validate_host_operation_id(value: Any) -> str:
    if not isinstance(value, str):
        raise RunpodLocalError(
            "owner operation ID must be a UUID",
            code="invalid_host_claim",
        )
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise RunpodLocalError(
            "owner operation ID must be a UUID",
            code="invalid_host_claim",
        ) from error
    if str(parsed) != value:
        raise RunpodLocalError(
            "owner operation ID must use canonical lowercase UUID text",
            code="invalid_host_claim",
        )
    return value


def validate_claim_owner_operation_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not OWNER_OPERATION_PATTERN.fullmatch(value)
    ):
        raise RunpodLocalError(
            "owner operation ID must be printable opaque text",
            code="invalid_host_claim",
        )
    return value


def _profile_names(value: Any) -> tuple[str, ...]:
    if (
        not isinstance(value, (tuple, list))
        or not value
        or not all(isinstance(name, str) for name in value)
    ):
        raise RunpodLocalError(
            "at least one allowed host profile is required",
            code="invalid_host_claim",
        )
    names = tuple(value)
    for name in names:
        validate_record_name(name)
    if len(set(names)) != len(names):
        raise RunpodLocalError(
            "allowed host profiles must be unique",
            code="invalid_host_claim",
        )
    return names


def _gpu_devices(value: Any) -> tuple[int, ...]:
    if not isinstance(value, (tuple, list)):
        raise RunpodLocalError(
            "GPU devices must be an ordered integer list",
            code="invalid_host_claim",
        )
    devices = tuple(value)
    if (
        not all(
            isinstance(device, int)
            and not isinstance(device, bool)
            and 0 <= device < 64
            for device in devices
        )
        or tuple(sorted(set(devices))) != devices
    ):
        raise RunpodLocalError(
            "GPU devices must be unique ascending indices from 0 through 63",
            code="invalid_host_claim",
        )
    return devices


def _endpoint_names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise RunpodLocalError(
            "endpoint names must be an ordered list",
            code="invalid_host_claim",
        )
    names = tuple(value)
    if (
        len(names) > MAX_ENDPOINTS_PER_CLAIM
        or not all(
            isinstance(name, str) and ENDPOINT_NAME_PATTERN.fullmatch(name)
            for name in names
        )
        or tuple(sorted(set(names))) != names
    ):
        raise RunpodLocalError(
            "endpoint names must be unique sorted lowercase identifiers",
            code="invalid_host_claim",
        )
    return names


@dataclass(frozen=True)
class HostClaimRequest:
    """One consumer's opaque resource request and host-creation policy."""

    owner_system: str
    owner_instance: str
    owner_operation_id: str
    allowed_profile_names: tuple[str, ...]
    mode: Literal["shared", "gpu-exclusive", "host-exclusive"] = "shared"
    host_name: str | None = None
    create_if_missing: bool = True
    gpu_devices: tuple[int, ...] = ()
    gpu_memory_gb: float = 0.0
    cpu_count: int = 0
    ram_gb: int = 0
    ephemeral_disk_gb: int = 0
    endpoint_names: tuple[str, ...] = ()
    minimum_remaining_seconds: int = 0
    acquisition_timeout_seconds: int = 300
    acquisition_expires_at: str | None = None
    renewal_ttl_seconds: int = 120
    new_host_hard_ttl_seconds: int = 7200
    new_host_retention: Literal["manual", "while-claimed"] = "while-claimed"

    def validated(self) -> HostClaimRequest:
        validate_claim_owner_name(self.owner_system, label="owner system")
        validate_claim_owner_name(
            self.owner_instance,
            label="owner instance",
        )
        validate_claim_owner_operation_id(self.owner_operation_id)
        _profile_names(self.allowed_profile_names)
        if self.host_name is not None:
            validate_record_name(self.host_name)
        if type(self.create_if_missing) is not bool:
            raise RunpodLocalError(
                "create-if-missing must be boolean",
                code="invalid_host_claim",
            )
        if self.mode not in CLAIM_MODES:
            raise RunpodLocalError(
                f"unsupported host claim mode: {self.mode!r}",
                code="invalid_host_claim",
            )
        devices = _gpu_devices(self.gpu_devices)
        _nonnegative_number(self.gpu_memory_gb, label="GPU memory")
        _nonnegative_integer(self.cpu_count, label="CPU count")
        _nonnegative_integer(self.ram_gb, label="RAM")
        _nonnegative_integer(
            self.ephemeral_disk_gb,
            label="ephemeral disk",
        )
        _endpoint_names(self.endpoint_names)
        _nonnegative_integer(
            self.minimum_remaining_seconds,
            label="minimum remaining lifetime",
        )
        _positive_duration(
            self.acquisition_timeout_seconds,
            label="claim acquisition timeout",
        )
        if self.acquisition_expires_at is not None:
            if not isinstance(self.acquisition_expires_at, str):
                raise RunpodLocalError(
                    "claim acquisition expiration must be a UTC timestamp",
                    code="invalid_host_claim",
                )
            parse_utc_timestamp(self.acquisition_expires_at)
        _positive_duration(
            self.renewal_ttl_seconds,
            label="claim renewal TTL",
        )
        _positive_duration(
            self.new_host_hard_ttl_seconds,
            label="new-host hard TTL",
        )
        if self.new_host_retention not in RETENTION_MODES:
            raise RunpodLocalError(
                f"unsupported host retention: {self.new_host_retention!r}",
                code="invalid_host_claim",
            )
        if self.mode == "gpu-exclusive" and not devices:
            raise RunpodLocalError(
                "gpu-exclusive claims require at least one GPU device",
                code="invalid_host_claim",
            )
        if self.gpu_memory_gb > 0 and not devices:
            raise RunpodLocalError(
                "GPU-memory reservations require explicit GPU devices",
                code="invalid_host_claim",
            )
        return self

    def identity_document(self) -> dict[str, Any]:
        self.validated()
        return {
            "owner_system": self.owner_system,
            "owner_instance": self.owner_instance,
            "owner_operation_id": self.owner_operation_id,
            "allowed_profile_names": list(self.allowed_profile_names),
            "host_name": self.host_name,
            "create_if_missing": self.create_if_missing,
            "mode": self.mode,
            "resources": {
                "gpu_devices": list(self.gpu_devices),
                "gpu_memory_gb": float(self.gpu_memory_gb),
                "cpu_count": self.cpu_count,
                "ram_gb": self.ram_gb,
                "ephemeral_disk_gb": self.ephemeral_disk_gb,
            },
            "endpoint_names": list(self.endpoint_names),
            "minimum_remaining_seconds": self.minimum_remaining_seconds,
            "acquisition_timeout_seconds": self.acquisition_timeout_seconds,
            "acquisition_expires_at": self.acquisition_expires_at,
            "renewal_ttl_seconds": self.renewal_ttl_seconds,
            "new_host_hard_ttl_seconds": self.new_host_hard_ttl_seconds,
            "new_host_retention": self.new_host_retention,
        }

    def sha256(self) -> str:
        return self.sha256_for_schema(CLAIM_REQUEST_IDENTITY_V2)

    def sha256_for_schema(self, schema: str) -> str:
        document = self.identity_document()
        if schema == CLAIM_REQUEST_IDENTITY_V1:
            if (
                self.acquisition_timeout_seconds != 300
                or self.acquisition_expires_at is not None
            ):
                raise RunpodLocalError(
                    "v1 host claim operations require the original "
                    "300-second relative acquisition budget",
                    code="host_claim_operation_conflict",
                )
            document.pop("acquisition_timeout_seconds")
            document.pop("acquisition_expires_at")
        elif schema != CLAIM_REQUEST_IDENTITY_V2:
            raise RunpodLocalError(
                "unsupported host claim request identity schema",
                code="invalid_host_claim",
            )
        encoded = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def normalize_host_claim_request(value: Any) -> HostClaimRequest:
    """Accept the stable byte/count consumer protocol as a local request."""

    if isinstance(value, HostClaimRequest):
        return value.validated()
    try:
        gpu_device_count = value.gpu_device_count
        gpu_memory_bytes = value.gpu_memory_bytes
        memory_bytes = value.memory_bytes
        ephemeral_disk_bytes = value.ephemeral_disk_bytes
    except AttributeError as error:
        raise RunpodLocalError(
            "host claim request does not implement the generic facade contract",
            code="invalid_host_claim",
        ) from error
    if (
        not isinstance(gpu_device_count, int)
        or isinstance(gpu_device_count, bool)
        or not 0 <= gpu_device_count <= 64
        or not isinstance(gpu_memory_bytes, int)
        or isinstance(gpu_memory_bytes, bool)
        or gpu_memory_bytes < 0
        or not isinstance(memory_bytes, int)
        or isinstance(memory_bytes, bool)
        or memory_bytes < 0
        or not isinstance(ephemeral_disk_bytes, int)
        or isinstance(ephemeral_disk_bytes, bool)
        or ephemeral_disk_bytes < 0
    ):
        raise RunpodLocalError(
            "host claim byte/count resources are invalid",
            code="invalid_host_claim",
        )
    gib = 1024**3
    owner_operation_id = getattr(
        value,
        "owner_operation_id",
        getattr(value, "operation_id", None),
    )
    return HostClaimRequest(
        owner_system=value.owner_system,
        owner_instance=value.owner_instance,
        owner_operation_id=owner_operation_id,
        allowed_profile_names=tuple(value.allowed_profile_names),
        mode=value.mode,
        host_name=value.host_name,
        create_if_missing=value.create_if_missing,
        gpu_devices=tuple(range(gpu_device_count)),
        gpu_memory_gb=gpu_memory_bytes / gib,
        cpu_count=value.cpu_count,
        ram_gb=math.ceil(memory_bytes / gib),
        ephemeral_disk_gb=math.ceil(ephemeral_disk_bytes / gib),
        endpoint_names=tuple(sorted(value.endpoint_names)),
        minimum_remaining_seconds=value.minimum_remaining_seconds,
        acquisition_timeout_seconds=value.acquisition_timeout_seconds,
        acquisition_expires_at=getattr(
            value,
            "acquisition_expires_at",
            None,
        ),
        renewal_ttl_seconds=value.renewal_ttl_seconds,
        new_host_hard_ttl_seconds=value.new_host_hard_ttl_seconds,
        new_host_retention=value.new_host_retention,
    ).validated()


@dataclass(frozen=True)
class HostClaim:
    """Stable consumer-facing view of one admitted claim."""

    host_name: str
    operation_id: str
    provider_resource_id: str
    profile_name: str
    profile_sha256: str
    hard_expires_at: str
    claim_id: str
    generation: int
    mode: str
    remote_root: str
    endpoints: dict[str, int]
    allocation: dict[str, Any]
    renewal_deadline: str

    @classmethod
    def from_documents(
        cls,
        ledger: dict[str, Any],
        claim: dict[str, Any],
    ) -> HostClaim:
        validate_claim_ledger(ledger)
        validate_claim_document(claim)
        return cls(
            host_name=ledger["host_name"],
            operation_id=ledger["host_operation_id"],
            provider_resource_id=ledger["pod_id"],
            profile_name=ledger["profile"]["name"],
            profile_sha256=ledger["profile"]["sha256"],
            hard_expires_at=ledger["provider_termination_at"],
            claim_id=claim["claim_id"],
            generation=claim["generation"],
            mode=claim["mode"],
            remote_root=claim["remote_root"],
            endpoints=dict(claim["ports"]),
            allocation=dict(ledger["allocation"]),
            renewal_deadline=claim["renewal_deadline"],
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": CLAIM_SCHEMA,
            "host_name": self.host_name,
            "operation_id": self.operation_id,
            "provider_resource_id": self.provider_resource_id,
            "profile": {
                "name": self.profile_name,
                "sha256": self.profile_sha256,
            },
            "hard_expires_at": self.hard_expires_at,
            "claim_id": self.claim_id,
            "generation": self.generation,
            "mode": self.mode,
            "remote_root": self.remote_root,
            "endpoints": dict(self.endpoints),
            "allocation": dict(self.allocation),
            "renewal_deadline": self.renewal_deadline,
        }

    @property
    def host_operation_id(self) -> str:
        return self.operation_id

    @property
    def pod_id(self) -> str:
        return self.provider_resource_id

    @property
    def provider_termination_at(self) -> str:
        return self.hard_expires_at

    @property
    def ports(self) -> dict[str, int]:
        return dict(self.endpoints)


@dataclass(frozen=True)
class ClaimReleaseResult:
    """Result of releasing one exact claim generation."""

    host_name: str
    claim_id: str
    released_generation: int
    remaining_claim_count: int
    retention: str
    empty_since: str | None
    retire_at: str | None
    retirement_due: bool

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": CLAIM_RELEASE_SCHEMA,
            "host_name": self.host_name,
            "claim_id": self.claim_id,
            "released_generation": self.released_generation,
            "remaining_claim_count": self.remaining_claim_count,
            "retention": self.retention,
            "empty_since": self.empty_since,
            "retire_at": self.retire_at,
            "retirement_due": self.retirement_due,
        }

    @property
    def released(self) -> bool:
        return True

    @property
    def final_claim(self) -> bool:
        return self.remaining_claim_count == 0

    @property
    def retirement(self) -> str:
        if not self.final_claim:
            return "retained-by-other-claims"
        if self.retire_at is None:
            return "retained-manually"
        return "retiring-now" if self.retirement_due else "grace-started"

    @property
    def empty_deadline(self) -> str | None:
        return self.retire_at


def _claim_resources(document: dict[str, Any]) -> dict[str, Any]:
    resources = document.get("resources")
    if not isinstance(resources, dict):
        raise RunpodLocalError(
            "claim has no resource reservation",
            code="invalid_host_claim_record",
        )
    devices = _gpu_devices(resources.get("gpu_devices"))
    gpu_memory_gb = _nonnegative_number(
        resources.get("gpu_memory_gb"),
        label="GPU memory",
    )
    cpu_count = _nonnegative_integer(
        resources.get("cpu_count"),
        label="CPU count",
    )
    ram_gb = _nonnegative_integer(resources.get("ram_gb"), label="RAM")
    disk_gb = _nonnegative_integer(
        resources.get("ephemeral_disk_gb"),
        label="ephemeral disk",
    )
    return {
        "gpu_devices": list(devices),
        "gpu_memory_gb": gpu_memory_gb,
        "cpu_count": cpu_count,
        "ram_gb": ram_gb,
        "ephemeral_disk_gb": disk_gb,
    }


def validate_claim_document(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise RunpodLocalError(
            "host claim record is not an object",
            code="invalid_host_claim_record",
        )
    claim_id = document.get("claim_id")
    if not isinstance(claim_id, str) or not CLAIM_ID_PATTERN.fullmatch(
        claim_id
    ):
        raise RunpodLocalError(
            "host claim has an invalid claim ID",
            code="invalid_host_claim_record",
        )
    validate_claim_owner_name(
        document.get("owner_system"),
        label="owner system",
    )
    validate_claim_owner_name(
        document.get("owner_instance"),
        label="owner instance",
    )
    validate_claim_owner_operation_id(document.get("owner_operation_id"))
    request_sha256 = document.get("request_sha256")
    if (
        not isinstance(request_sha256, str)
        or not SHA256_PATTERN.fullmatch(request_sha256)
    ):
        raise RunpodLocalError(
            "host claim has an invalid request identity",
            code="invalid_host_claim_record",
        )
    mode = document.get("mode")
    if mode not in CLAIM_MODES:
        raise RunpodLocalError(
            "host claim has an invalid mode",
            code="invalid_host_claim_record",
        )
    resources = _claim_resources(document)
    if mode == "gpu-exclusive" and not resources["gpu_devices"]:
        raise RunpodLocalError(
            "gpu-exclusive host claim has no GPU devices",
            code="invalid_host_claim_record",
        )
    if resources["gpu_memory_gb"] > 0 and not resources["gpu_devices"]:
        raise RunpodLocalError(
            "GPU-memory claim has no GPU devices",
            code="invalid_host_claim_record",
        )
    endpoint_names = _endpoint_names(document.get("endpoint_names"))
    ports = document.get("ports")
    if (
        not isinstance(ports, dict)
        or sorted(ports) != list(endpoint_names)
        or not all(
            type(port) is int and MIN_REMOTE_PORT <= port <= MAX_REMOTE_PORT
            for port in ports.values()
        )
        or len(set(ports.values())) != len(ports)
    ):
        raise RunpodLocalError(
            "host claim has an invalid endpoint allocation",
            code="invalid_host_claim_record",
        )
    generation = document.get("generation")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation <= 0
    ):
        raise RunpodLocalError(
            "host claim has an invalid generation",
            code="invalid_host_claim_record",
        )
    remote_root = document.get("remote_root")
    if remote_root != f"/root/runpod-session/claims/{claim_id}":
        raise RunpodLocalError(
            "host claim has an invalid remote root",
            code="invalid_host_claim_record",
        )
    for field in ("acquired_at", "renewed_at", "renewal_deadline"):
        value = document.get(field)
        if not isinstance(value, str):
            raise RunpodLocalError(
                f"host claim has no {field}",
                code="invalid_host_claim_record",
            )
        parse_utc_timestamp(value)
    if parse_utc_timestamp(document["renewal_deadline"]) <= (
        parse_utc_timestamp(document["renewed_at"])
    ):
        raise RunpodLocalError(
            "host claim renewal deadline is not after renewal",
            code="invalid_host_claim_record",
        )
    normalized = dict(document)
    normalized["resources"] = resources
    normalized["endpoint_names"] = list(endpoint_names)
    normalized["ports"] = {
        name: ports[name] for name in sorted(ports)
    }
    return normalized


def validate_closed_claim_document(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise RunpodLocalError(
            "closed host claim record is not an object",
            code="invalid_host_claim_record",
        )
    claim_id = document.get("claim_id")
    if not isinstance(claim_id, str) or not CLAIM_ID_PATTERN.fullmatch(
        claim_id
    ):
        raise RunpodLocalError(
            "closed host claim has an invalid claim ID",
            code="invalid_host_claim_record",
        )
    validate_claim_owner_name(
        document.get("owner_system"),
        label="owner system",
    )
    validate_claim_owner_name(
        document.get("owner_instance"),
        label="owner instance",
    )
    validate_claim_owner_operation_id(document.get("owner_operation_id"))
    request_sha256 = document.get("request_sha256")
    if (
        not isinstance(request_sha256, str)
        or not SHA256_PATTERN.fullmatch(request_sha256)
    ):
        raise RunpodLocalError(
            "closed host claim has an invalid request identity",
            code="invalid_host_claim_record",
        )
    generation = document.get("generation")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation <= 0
    ):
        raise RunpodLocalError(
            "closed host claim has an invalid generation",
            code="invalid_host_claim_record",
        )
    if document.get("reason") not in CLOSED_CLAIM_REASONS:
        raise RunpodLocalError(
            "closed host claim has an invalid reason",
            code="invalid_host_claim_record",
        )
    closed_at = document.get("closed_at")
    if not isinstance(closed_at, str):
        raise RunpodLocalError(
            "closed host claim has no close timestamp",
            code="invalid_host_claim_record",
        )
    parse_utc_timestamp(closed_at)
    return dict(document)


def validate_claim_quarantine(
    document: Any,
    *,
    closed_claims: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Validate durable admission quarantine for one exact host operation."""

    if document is None:
        return None
    if not isinstance(document, dict) or set(document) != {
        "reason",
        "claim_ids",
        "started_at",
    }:
        raise RunpodLocalError(
            "host claim quarantine has an invalid shape",
            code="invalid_host_claim_record",
        )
    if document["reason"] != CLAIM_EXPIRY_QUARANTINE_REASON:
        raise RunpodLocalError(
            "host claim quarantine has an invalid reason",
            code="invalid_host_claim_record",
        )
    claim_ids = document["claim_ids"]
    if (
        not isinstance(claim_ids, list)
        or not claim_ids
        or len(claim_ids) > MAX_CLAIMS_PER_HOST
        or not all(
            isinstance(claim_id, str)
            and CLAIM_ID_PATTERN.fullmatch(claim_id)
            for claim_id in claim_ids
        )
        or claim_ids != sorted(set(claim_ids))
    ):
        raise RunpodLocalError(
            "host claim quarantine has invalid claim identities",
            code="invalid_host_claim_record",
        )
    started_at = document["started_at"]
    if not isinstance(started_at, str):
        raise RunpodLocalError(
            "host claim quarantine has no start timestamp",
            code="invalid_host_claim_record",
        )
    started_time = parse_utc_timestamp(started_at)
    closed_by_id = {
        claim["claim_id"]: claim for claim in closed_claims
    }
    for claim_id in claim_ids:
        closed = closed_by_id.get(claim_id)
        if closed is None or closed["reason"] != "expired":
            raise RunpodLocalError(
                "host claim quarantine does not name an expired claim",
                code="invalid_host_claim_record",
            )
        if started_time > parse_utc_timestamp(closed["closed_at"]):
            raise RunpodLocalError(
                "host claim quarantine starts after its claim closure",
                code="invalid_host_claim_record",
            )
    return {
        "reason": CLAIM_EXPIRY_QUARANTINE_REASON,
        "claim_ids": list(claim_ids),
        "started_at": started_at,
    }


def validate_host_operation_end(document: Any) -> dict[str, Any] | None:
    """Validate durable proof that one exact host operation cannot return."""

    if document is None:
        return None
    if not isinstance(document, dict) or set(document) != {
        "reason",
        "observed_at",
    }:
        raise RunpodLocalError(
            "host operation end has an invalid shape",
            code="invalid_host_claim_record",
        )
    if document["reason"] != HOST_OPERATION_END_REASON:
        raise RunpodLocalError(
            "host operation end has an invalid reason",
            code="invalid_host_claim_record",
        )
    observed_at = document["observed_at"]
    if not isinstance(observed_at, str):
        raise RunpodLocalError(
            "host operation end has no observation timestamp",
            code="invalid_host_claim_record",
        )
    parse_utc_timestamp(observed_at)
    return {
        "reason": HOST_OPERATION_END_REASON,
        "observed_at": observed_at,
    }


def validate_claim_ledger(document: Any) -> dict[str, Any]:
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != CLAIM_LEDGER_SCHEMA
    ):
        raise RunpodLocalError(
            "host claim ledger has an unsupported schema",
            code="invalid_host_claim_record",
        )
    host_name = document.get("host_name")
    if not isinstance(host_name, str):
        raise RunpodLocalError(
            "host claim ledger has no host name",
            code="invalid_host_claim_record",
        )
    validate_record_name(host_name)
    host_operation_id = document.get("host_operation_id")
    validate_host_operation_id(host_operation_id)
    pod_id = document.get("pod_id")
    if not isinstance(pod_id, str) or not pod_id or not pod_id.isprintable():
        raise RunpodLocalError(
            "host claim ledger has no Pod ID",
            code="invalid_host_claim_record",
        )
    profile = document.get("profile")
    if (
        not isinstance(profile, dict)
        or not isinstance(profile.get("name"), str)
        or not isinstance(profile.get("sha256"), str)
        or not SHA256_PATTERN.fullmatch(profile["sha256"])
    ):
        raise RunpodLocalError(
            "host claim ledger has an invalid profile identity",
            code="invalid_host_claim_record",
        )
    validate_record_name(profile["name"])
    provider_termination_at = document.get("provider_termination_at")
    if not isinstance(provider_termination_at, str):
        raise RunpodLocalError(
            "host claim ledger has no provider deadline",
            code="invalid_host_claim_record",
        )
    parse_utc_timestamp(provider_termination_at)
    retention = document.get("retention")
    if (
        not isinstance(retention, dict)
        or retention.get("mode") not in RETENTION_MODES
        or type(retention.get("empty_grace_seconds")) is not int
        or retention["empty_grace_seconds"] < 0
        or retention["empty_grace_seconds"] > MAX_DURATION_SECONDS
    ):
        raise RunpodLocalError(
            "host claim ledger has invalid retention",
            code="invalid_host_claim_record",
        )
    allocation = document.get("allocation")
    if not isinstance(allocation, dict):
        raise RunpodLocalError(
            "host claim ledger has no allocation facts",
            code="invalid_host_claim_record",
        )
    gpu_count = allocation.get("gpu_count")
    gpu_memory_gb = allocation.get("gpu_memory_gb")
    cpu_count = allocation.get("cpu_count")
    ram_gb = allocation.get("ram_gb")
    ephemeral_disk_gb = allocation.get("ephemeral_disk_gb")
    if (
        not isinstance(allocation.get("gpu_id"), str)
        or not isinstance(gpu_count, int)
        or isinstance(gpu_count, bool)
        or gpu_count <= 0
        or not isinstance(gpu_memory_gb, (int, float))
        or isinstance(gpu_memory_gb, bool)
        or not math.isfinite(gpu_memory_gb)
        or gpu_memory_gb <= 0
        or not isinstance(cpu_count, int)
        or isinstance(cpu_count, bool)
        or cpu_count <= 0
        or not isinstance(ram_gb, int)
        or isinstance(ram_gb, bool)
        or ram_gb <= 0
        or not isinstance(ephemeral_disk_gb, int)
        or isinstance(ephemeral_disk_gb, bool)
        or ephemeral_disk_gb <= 0
    ):
        raise RunpodLocalError(
            "host claim ledger has invalid allocation facts",
            code="invalid_host_claim_record",
        )
    claims = document.get("claims")
    if (
        not isinstance(claims, list)
        or len(claims) > MAX_CLAIMS_PER_HOST
    ):
        raise RunpodLocalError(
            "host claim ledger has an invalid claim collection",
            code="invalid_host_claim_record",
        )
    normalized_claims = [validate_claim_document(claim) for claim in claims]
    claim_ids = [claim["claim_id"] for claim in normalized_claims]
    if claim_ids != sorted(set(claim_ids)):
        raise RunpodLocalError(
            "host claims are not in unique canonical order",
            code="invalid_host_claim_record",
        )
    closed_claims = document.get("closed_claims")
    if (
        not isinstance(closed_claims, list)
        or len(closed_claims) > MAX_CLOSED_CLAIMS_PER_HOST
    ):
        raise RunpodLocalError(
            "host claim ledger has an invalid closed-claim collection",
            code="invalid_host_claim_record",
        )
    normalized_closed_claims = [
        validate_closed_claim_document(claim)
        for claim in closed_claims
    ]
    closed_claim_ids = [
        claim["claim_id"] for claim in normalized_closed_claims
    ]
    if closed_claim_ids != sorted(set(closed_claim_ids)):
        raise RunpodLocalError(
            "closed host claims are not in unique canonical order",
            code="invalid_host_claim_record",
        )
    if set(claim_ids).intersection(closed_claim_ids):
        raise RunpodLocalError(
            "active and closed host claim identities overlap",
            code="invalid_host_claim_record",
        )
    if "quarantine" not in document:
        raise RunpodLocalError(
            "host claim ledger has no quarantine state",
            code="invalid_host_claim_record",
        )
    normalized_quarantine = validate_claim_quarantine(
        document["quarantine"],
        closed_claims=normalized_closed_claims,
    )
    if "operation_end" not in document:
        raise RunpodLocalError(
            "host claim ledger has no operation-end state",
            code="invalid_host_claim_record",
        )
    normalized_operation_end = validate_host_operation_end(
        document["operation_end"]
    )
    if normalized_operation_end is not None and (
        normalized_claims or normalized_quarantine is not None
    ):
        raise RunpodLocalError(
            "ended host operation retains claims or quarantine",
            code="invalid_host_claim_record",
        )
    operation_identities = [
        (
            claim["owner_system"],
            claim["owner_instance"],
            claim["owner_operation_id"],
        )
        for claim in [*normalized_claims, *normalized_closed_claims]
    ]
    if len(operation_identities) != len(set(operation_identities)):
        raise RunpodLocalError(
            "host claim owner operations are not unique",
            code="invalid_host_claim_record",
        )
    provider_deadline = parse_utc_timestamp(provider_termination_at)
    gpu_memory_by_device = {
        device: 0.0 for device in range(gpu_count)
    }
    for claim in normalized_claims:
        devices = claim["resources"]["gpu_devices"]
        if any(device >= gpu_count for device in devices):
            raise RunpodLocalError(
                "host claim reserves a nonexistent GPU device",
                code="invalid_host_claim_record",
            )
        if parse_utc_timestamp(claim["renewal_deadline"]) > provider_deadline:
            raise RunpodLocalError(
                "host claim outlives the provider hard deadline",
                code="invalid_host_claim_record",
            )
        per_device_memory = (
            claim["resources"]["gpu_memory_gb"] / len(devices)
            if devices
            else 0.0
        )
        for device in devices:
            gpu_memory_by_device[device] += per_device_memory
    if any(
        reserved > gpu_memory_gb
        for reserved in gpu_memory_by_device.values()
    ):
        raise RunpodLocalError(
            "host claims exceed GPU-memory capacity",
            code="invalid_host_claim_record",
        )
    for field in ("cpu_count", "ram_gb", "ephemeral_disk_gb"):
        if sum(
            claim["resources"][field]
            for claim in normalized_claims
        ) > allocation[field]:
            raise RunpodLocalError(
                f"host claims exceed {field} capacity",
                code="invalid_host_claim_record",
            )
    for index, claim in enumerate(normalized_claims):
        for other in normalized_claims[index + 1 :]:
            if (
                claim["mode"] == "host-exclusive"
                or other["mode"] == "host-exclusive"
            ):
                raise RunpodLocalError(
                    "host-exclusive claim overlaps another claim",
                    code="invalid_host_claim_record",
                )
            shared_devices = set(
                claim["resources"]["gpu_devices"]
            ).intersection(other["resources"]["gpu_devices"])
            if shared_devices and (
                claim["mode"] == "gpu-exclusive"
                or other["mode"] == "gpu-exclusive"
            ):
                raise RunpodLocalError(
                    "GPU-exclusive claims overlap",
                    code="invalid_host_claim_record",
                )
    allocated_ports = [
        port
        for claim in normalized_claims
        for port in claim["ports"].values()
    ]
    if len(allocated_ports) != len(set(allocated_ports)):
        raise RunpodLocalError(
            "host claim endpoint ports overlap",
            code="invalid_host_claim_record",
        )
    generation = document.get("generation")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 0
    ):
        raise RunpodLocalError(
            "host claim ledger has an invalid generation",
            code="invalid_host_claim_record",
        )
    for field in ("created_at", "updated_at"):
        value = document.get(field)
        if not isinstance(value, str):
            raise RunpodLocalError(
                f"host claim ledger has no {field}",
                code="invalid_host_claim_record",
            )
        parse_utc_timestamp(value)
    empty_since = document.get("empty_since")
    retire_at = document.get("retire_at")
    empty_grace_applied_seconds = document.get(
        "empty_grace_applied_seconds"
    )
    if claims and (
        empty_since is not None
        or retire_at is not None
        or empty_grace_applied_seconds is not None
    ):
        raise RunpodLocalError(
            "nonempty host claim ledger has empty-host retirement state",
            code="invalid_host_claim_record",
        )
    if empty_since is not None:
        if not isinstance(empty_since, str):
            raise RunpodLocalError(
                "host claim ledger has invalid empty timestamp",
                code="invalid_host_claim_record",
            )
        empty_time = parse_utc_timestamp(empty_since)
        if (
            retention["mode"] != "while-claimed"
            and empty_grace_applied_seconds != 0
        ):
            raise RunpodLocalError(
                "manual host can retire only through an immediate request",
                code="invalid_host_claim_record",
            )
        if (
            not isinstance(empty_grace_applied_seconds, int)
            or isinstance(empty_grace_applied_seconds, bool)
            or empty_grace_applied_seconds < 0
            or empty_grace_applied_seconds
            > retention["empty_grace_seconds"]
        ):
            raise RunpodLocalError(
                "empty host has invalid applied grace",
                code="invalid_host_claim_record",
            )
        if not isinstance(retire_at, str):
            raise RunpodLocalError(
                "empty host has no retirement deadline",
                code="invalid_host_claim_record",
            )
        if parse_utc_timestamp(retire_at) != empty_time + datetime.timedelta(
            seconds=empty_grace_applied_seconds
        ):
            raise RunpodLocalError(
                "empty-host retirement deadline disagrees with its grace",
                code="invalid_host_claim_record",
            )
    elif retire_at is not None or empty_grace_applied_seconds is not None:
        raise RunpodLocalError(
            "host retirement deadline has no empty timestamp",
            code="invalid_host_claim_record",
        )
    if normalized_operation_end is not None and empty_since is not None:
        raise RunpodLocalError(
            "ended host operation retains retirement state",
            code="invalid_host_claim_record",
        )
    if (
        normalized_quarantine is not None
        and not claims
        and retention["mode"] == "while-claimed"
        and (
            empty_since is None
            or empty_grace_applied_seconds != 0
        )
    ):
        raise RunpodLocalError(
            "empty quarantined host is not scheduled for immediate retirement",
            code="invalid_host_claim_record",
        )
    normalized = dict(document)
    normalized["claims"] = normalized_claims
    normalized["closed_claims"] = normalized_closed_claims
    normalized["quarantine"] = normalized_quarantine
    normalized["operation_end"] = normalized_operation_end
    normalized["allocation"] = dict(allocation)
    normalized["retention"] = dict(retention)
    return normalized


def _claims_conflict(
    request: HostClaimRequest,
    existing: dict[str, Any],
) -> bool:
    existing_mode = existing["mode"]
    if request.mode == "host-exclusive" or existing_mode == "host-exclusive":
        return True
    requested_devices = set(request.gpu_devices)
    existing_devices = set(existing["resources"]["gpu_devices"])
    if not requested_devices.intersection(existing_devices):
        return False
    return (
        request.mode == "gpu-exclusive"
        or existing_mode == "gpu-exclusive"
    )


def claim_admission_reasons(
    ledger: dict[str, Any],
    request: HostClaimRequest,
    *,
    now: datetime.datetime,
) -> list[str]:
    """Return deterministic reasons one host cannot admit the request."""

    ledger = validate_claim_ledger(ledger)
    request.validated()
    utc_timestamp(now)
    reasons: list[str] = []
    if ledger["operation_end"] is not None:
        reasons.append("host operation has ended")
    if ledger["quarantine"] is not None:
        reasons.append(
            "host is quarantined because expired-claim cleanup is unproven"
        )
    if ledger["profile"]["name"] not in request.allowed_profile_names:
        reasons.append("profile is not allowed")
    remaining_seconds = int(
        (
            parse_utc_timestamp(ledger["provider_termination_at"]) - now
        ).total_seconds()
    )
    if remaining_seconds < request.minimum_remaining_seconds:
        reasons.append("provider hard lifetime is too short")
    allocation = ledger["allocation"]
    for device in request.gpu_devices:
        if device >= allocation["gpu_count"]:
            reasons.append(f"GPU device {device} does not exist")
    for existing in ledger["claims"]:
        if _claims_conflict(request, existing):
            reasons.append(
                f"claim mode conflicts with {existing['claim_id']}"
            )
    gpu_memory_by_device = {
        device: 0.0 for device in range(allocation["gpu_count"])
    }
    for existing in ledger["claims"]:
        existing_devices = existing["resources"]["gpu_devices"]
        existing_per_device = (
            existing["resources"]["gpu_memory_gb"] / len(existing_devices)
            if existing_devices
            else 0.0
        )
        for device in existing_devices:
            gpu_memory_by_device[device] += existing_per_device
    per_device_memory = allocation["gpu_memory_gb"]
    requested_per_device = (
        request.gpu_memory_gb / len(request.gpu_devices)
        if request.gpu_devices
        else 0.0
    )
    for device in request.gpu_devices:
        if device in gpu_memory_by_device and (
            gpu_memory_by_device[device] + requested_per_device
            > per_device_memory
        ):
            reasons.append(f"GPU device {device} memory is exhausted")
    totals = {
        "cpu_count": request.cpu_count,
        "ram_gb": request.ram_gb,
        "ephemeral_disk_gb": request.ephemeral_disk_gb,
    }
    for existing in ledger["claims"]:
        for field in totals:
            totals[field] += existing["resources"][field]
    for field, label in (
        ("cpu_count", "CPU"),
        ("ram_gb", "RAM"),
        ("ephemeral_disk_gb", "ephemeral disk"),
    ):
        if totals[field] > allocation[field]:
            reasons.append(f"{label} reservation is exhausted")
    if len(ledger["claims"]) >= MAX_CLAIMS_PER_HOST:
        reasons.append("host claim count limit is reached")
    return sorted(set(reasons))


class ClaimStore:
    """Private host claim ledgers; callers serialize with ``host-controller``."""

    def __init__(self, state: StateStore) -> None:
        self.state = state

    def load(
        self,
        host_name: str,
        *,
        required: bool = True,
    ) -> dict[str, Any] | None:
        validate_record_name(host_name)
        value = self.state.read("hostclaims", host_name)
        if value is None:
            if required:
                raise RunpodLocalError(
                    f"host has no claim ledger: {host_name}",
                    code="host_claim_ledger_not_found",
                )
            return None
        return validate_claim_ledger(value)

    def save(self, ledger: dict[str, Any]) -> None:
        ledger = validate_claim_ledger(ledger)
        self.state.write("hostclaims", ledger["host_name"], ledger)

    def list(self) -> list[dict[str, Any]]:
        return [
            validate_claim_ledger(value)
            for value in self.state.list("hostclaims")
        ]

    def scan(self) -> list[StateRecordScan]:
        records = []
        for scanned in self.state.scan("hostclaims"):
            if scanned.error is not None:
                records.append(scanned)
                continue
            try:
                ledger = validate_claim_ledger(scanned.value)
                if ledger["host_name"] != scanned.name:
                    raise RunpodLocalError(
                        "host claim ledger is stored under another host name",
                        code="invalid_host_claim_record",
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
                    value=ledger,
                    error=None,
                )
            )
        return records

    def initialize(
        self,
        *,
        host: dict[str, Any],
        allocation: dict[str, Any],
        retention: str,
        empty_grace_seconds: int,
        now: datetime.datetime,
    ) -> dict[str, Any]:
        if retention not in RETENTION_MODES:
            raise RunpodLocalError(
                f"unsupported host retention: {retention!r}",
                code="invalid_host_claim",
            )
        _nonnegative_integer(
            empty_grace_seconds,
            label="empty-host grace",
        )
        host_name = host.get("name")
        if not isinstance(host_name, str):
            raise RunpodLocalError(
                "host receipt has no name",
                code="invalid_host_receipt",
            )
        validate_record_name(host_name)
        operation_id = host.get("operation_id")
        validate_host_operation_id(operation_id)
        pod_id = host.get("pod_id")
        if not isinstance(pod_id, str) or not pod_id:
            raise RunpodLocalError(
                "host receipt has no Pod ID",
                code="invalid_host_receipt",
            )
        profile = host.get("profile")
        if not isinstance(profile, dict):
            raise RunpodLocalError(
                "host receipt has no profile identity",
                code="invalid_host_receipt",
            )
        existing = self.load(host_name, required=False)
        if existing is not None:
            if (
                existing["host_operation_id"] != operation_id
                or existing["pod_id"] != pod_id
            ):
                if existing["claims"]:
                    raise RunpodLocalError(
                        "host operation changed while claims remain",
                        code="host_operation_conflict",
                    )
            else:
                return existing
        timestamp = utc_timestamp(now)
        initial_grace = (
            empty_grace_seconds if retention == "while-claimed" else None
        )
        ledger = {
            "schema_version": CLAIM_LEDGER_SCHEMA,
            "host_name": host_name,
            "host_operation_id": operation_id,
            "pod_id": pod_id,
            "profile": {
                "name": profile.get("name"),
                "sha256": profile.get("sha256"),
            },
            "provider_termination_at": host.get("provider_termination_at"),
            "allocation": dict(allocation),
            "retention": {
                "mode": retention,
                "empty_grace_seconds": empty_grace_seconds,
            },
            "claims": [],
            "closed_claims": (
                list(existing["closed_claims"])
                if existing is not None
                else []
            ),
            # Quarantine belongs to one exact provider operation. A new
            # operation cannot retain remote processes or credentials from
            # the retired predecessor, while its closed-claim history remains
            # the durable acquisition-journal outbox.
            "quarantine": None,
            "operation_end": None,
            "generation": 0,
            "created_at": timestamp,
            "updated_at": timestamp,
            "empty_since": (
                timestamp if initial_grace is not None else None
            ),
            "retire_at": (
                utc_timestamp(
                    now + datetime.timedelta(seconds=initial_grace)
                )
                if initial_grace is not None
                else None
            ),
            "empty_grace_applied_seconds": initial_grace,
        }
        self.save(ledger)
        return ledger

    def find_owner_operation(
        self,
        request: HostClaimRequest,
        *,
        strict_host_names: set[str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        request.validated()
        strict_names = set(strict_host_names or ())
        for host_name in strict_names:
            validate_record_name(host_name)
        for scanned in self.scan():
            if scanned.error is not None:
                if scanned.name in strict_names:
                    raise scanned.error
                continue
            ledger = scanned.value
            if ledger is None:
                continue
            for claim in ledger["claims"]:
                if (
                    claim["owner_system"] == request.owner_system
                    and claim["owner_instance"] == request.owner_instance
                    and claim["owner_operation_id"]
                    == request.owner_operation_id
                ):
                    return ledger, claim
        return None

    def find_closed_owner_operation(
        self,
        request: HostClaimRequest,
        *,
        strict_host_names: set[str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        request.validated()
        strict_names = set(strict_host_names or ())
        for host_name in strict_names:
            validate_record_name(host_name)
        for scanned in self.scan():
            if scanned.error is not None:
                if scanned.name in strict_names:
                    raise scanned.error
                continue
            ledger = scanned.value
            if ledger is None:
                continue
            for claim in ledger["closed_claims"]:
                if (
                    claim["owner_system"] == request.owner_system
                    and claim["owner_instance"] == request.owner_instance
                    and claim["owner_operation_id"]
                    == request.owner_operation_id
                ):
                    return ledger, claim
        return None

    @staticmethod
    def _close_claim(
        ledger: dict[str, Any],
        claim: dict[str, Any],
        *,
        reason: str,
        now: datetime.datetime,
    ) -> None:
        closed = validate_closed_claim_document(
            {
                "claim_id": claim["claim_id"],
                "owner_system": claim["owner_system"],
                "owner_instance": claim["owner_instance"],
                "owner_operation_id": claim["owner_operation_id"],
                "request_sha256": claim["request_sha256"],
                "generation": claim["generation"],
                "reason": reason,
                "closed_at": utc_timestamp(now),
            }
        )
        ledger["closed_claims"].append(closed)
        ledger["closed_claims"].sort(
            key=lambda item: item["claim_id"]
        )
        if len(ledger["closed_claims"]) > MAX_CLOSED_CLAIMS_PER_HOST:
            ledger["closed_claims"] = sorted(
                ledger["closed_claims"],
                key=lambda item: (item["closed_at"], item["claim_id"]),
            )[-MAX_CLOSED_CLAIMS_PER_HOST:]
            ledger["closed_claims"].sort(
                key=lambda item: item["claim_id"]
            )

    def _expire_claims(
        self,
        ledger: dict[str, Any],
        *,
        now: datetime.datetime,
        persist: bool,
    ) -> tuple[dict[str, Any], list[str]]:
        ledger = validate_claim_ledger(ledger)
        utc_timestamp(now)
        active = []
        expired = []
        expired_deadlines: list[datetime.datetime] = []
        for claim in ledger["claims"]:
            if now >= parse_utc_timestamp(claim["renewal_deadline"]):
                expired.append(claim["claim_id"])
                expired_deadlines.append(
                    parse_utc_timestamp(claim["renewal_deadline"])
                )
                self._close_claim(
                    ledger,
                    claim,
                    reason="expired",
                    now=now,
                )
            else:
                active.append(claim)
        if not expired:
            return ledger, []
        ledger["claims"] = active
        prior_quarantine = ledger["quarantine"]
        quarantine_started_at = min(
            [
                *expired_deadlines,
                *(
                    [
                        parse_utc_timestamp(
                            prior_quarantine["started_at"]
                        )
                    ]
                    if prior_quarantine is not None
                    else []
                ),
            ]
        )
        ledger["quarantine"] = {
            "reason": CLAIM_EXPIRY_QUARANTINE_REASON,
            "claim_ids": sorted(
                {
                    *expired,
                    *(
                        prior_quarantine["claim_ids"]
                        if prior_quarantine is not None
                        else []
                    ),
                }
            ),
            "started_at": utc_timestamp(quarantine_started_at),
        }
        ledger["generation"] += 1
        ledger["updated_at"] = utc_timestamp(now)
        if not active and ledger["retention"]["mode"] == "while-claimed":
            # Expiry revokes authority but cannot prove that the opaque
            # consumer stopped its process or removed credentials. The exact
            # host operation therefore becomes retirement-due when its final
            # valid claim ends; ordinary empty grace would allow unsafe reuse.
            empty_since = max(expired_deadlines)
            ledger["empty_since"] = utc_timestamp(empty_since)
            ledger["empty_grace_applied_seconds"] = 0
            ledger["retire_at"] = utc_timestamp(empty_since)
        if persist:
            self.save(ledger)
        return ledger, expired

    def expire_claims(
        self,
        ledger: dict[str, Any],
        *,
        now: datetime.datetime,
    ) -> tuple[dict[str, Any], list[str]]:
        """Expire and durably close claims whose immutable deadline passed."""

        return self._expire_claims(ledger, now=now, persist=True)

    def close_host_operation(
        self,
        ledger: dict[str, Any],
        *,
        now: datetime.datetime,
    ) -> tuple[dict[str, Any], list[str]]:
        """Close every claim after exact proof its host operation ended."""

        ledger = validate_claim_ledger(ledger)
        utc_timestamp(now)
        if ledger["operation_end"] is not None:
            return ledger, []
        closed_claim_ids = [
            claim["claim_id"] for claim in ledger["claims"]
        ]
        for claim in ledger["claims"]:
            self._close_claim(
                ledger,
                claim,
                reason=HOST_OPERATION_END_REASON,
                now=now,
            )
        ledger["claims"] = []
        # Exact terminal/replacement evidence proves the old Pod operation
        # cannot retain processes or credentials. Preserve its permanent end
        # marker instead of carrying an unsafe-process quarantine onto a
        # replacement operation.
        ledger["quarantine"] = None
        ledger["operation_end"] = {
            "reason": HOST_OPERATION_END_REASON,
            "observed_at": utc_timestamp(now),
        }
        ledger["empty_since"] = None
        ledger["retire_at"] = None
        ledger["empty_grace_applied_seconds"] = None
        ledger["generation"] += 1
        ledger["updated_at"] = utc_timestamp(now)
        self.save(ledger)
        return ledger, closed_claim_ids

    def preview_expire_claims(
        self,
        ledger: dict[str, Any],
        *,
        now: datetime.datetime,
    ) -> tuple[dict[str, Any], list[str]]:
        """Compute expiry and retirement state without writing it."""

        return self._expire_claims(ledger, now=now, persist=False)

    def admit(
        self,
        ledger: dict[str, Any],
        request: HostClaimRequest,
        *,
        now: datetime.datetime,
        claim_id: str,
    ) -> HostClaim:
        request.validated()
        ledger = validate_claim_ledger(ledger)
        if not CLAIM_ID_PATTERN.fullmatch(claim_id):
            raise RunpodLocalError(
                "generated claim ID is invalid",
                code="invalid_host_claim",
            )
        ledger, _ = self.expire_claims(ledger, now=now)
        if ledger["operation_end"] is not None:
            raise RunpodLocalError(
                f"host {ledger['host_name']} operation has ended",
                code="host_claim_host_changed",
            )
        if ledger["quarantine"] is not None:
            raise RunpodLocalError(
                f"host {ledger['host_name']} is quarantined because cleanup "
                "for an expired claim is unproven",
                code="host_claim_quarantined",
            )
        strict_host_names = {ledger["host_name"]}
        existing_operation = self.find_owner_operation(
            request,
            strict_host_names=strict_host_names,
        )
        if existing_operation is not None:
            existing_ledger, existing_claim = existing_operation
            if existing_claim["request_sha256"] != request.sha256():
                raise RunpodLocalError(
                    "owner operation already names a different claim request",
                    code="host_claim_operation_conflict",
                )
            return HostClaim.from_documents(
                existing_ledger,
                existing_claim,
            )
        closed_operation = self.find_closed_owner_operation(
            request,
            strict_host_names=strict_host_names,
        )
        if closed_operation is not None:
            _, closed_claim = closed_operation
            if closed_claim["request_sha256"] != request.sha256():
                raise RunpodLocalError(
                    "owner operation already names a different closed claim",
                    code="host_claim_operation_conflict",
                )
            raise RunpodLocalError(
                "owner operation already completed a claim",
                code="host_claim_operation_closed",
            )
        reasons = claim_admission_reasons(ledger, request, now=now)
        if reasons:
            raise RunpodLocalError(
                f"host {ledger['host_name']} cannot admit claim: "
                + "; ".join(reasons),
                code="host_claim_not_admitted",
            )
        used_ports = {
            port
            for existing in ledger["claims"]
            for port in existing["ports"].values()
        }
        ports: dict[str, int] = {}
        candidate = MIN_REMOTE_PORT
        for endpoint_name in request.endpoint_names:
            while candidate in used_ports and candidate <= MAX_REMOTE_PORT:
                candidate += 1
            if candidate > MAX_REMOTE_PORT:
                raise RunpodLocalError(
                    "host endpoint port range is exhausted",
                    code="host_endpoint_ports_exhausted",
                )
            ports[endpoint_name] = candidate
            used_ports.add(candidate)
            candidate += 1
        acquired_at = utc_timestamp(now)
        renewal_deadline = now + datetime.timedelta(
            seconds=request.renewal_ttl_seconds
        )
        provider_deadline = parse_utc_timestamp(
            ledger["provider_termination_at"]
        )
        if renewal_deadline >= provider_deadline:
            renewal_deadline = provider_deadline
        if renewal_deadline <= now:
            raise RunpodLocalError(
                "host provider deadline has expired",
                code="host_provider_deadline_expired",
            )
        claim = {
            "claim_id": claim_id,
            "owner_system": request.owner_system,
            "owner_instance": request.owner_instance,
            "owner_operation_id": request.owner_operation_id,
            "request_sha256": request.sha256(),
            "mode": request.mode,
            "resources": {
                "gpu_devices": list(request.gpu_devices),
                "gpu_memory_gb": float(request.gpu_memory_gb),
                "cpu_count": request.cpu_count,
                "ram_gb": request.ram_gb,
                "ephemeral_disk_gb": request.ephemeral_disk_gb,
            },
            "endpoint_names": list(request.endpoint_names),
            "ports": ports,
            "remote_root": f"/root/runpod-session/claims/{claim_id}",
            "generation": 1,
            "acquired_at": acquired_at,
            "renewed_at": acquired_at,
            "renewal_deadline": utc_timestamp(renewal_deadline),
        }
        claim = validate_claim_document(claim)
        ledger["claims"].append(claim)
        ledger["claims"].sort(key=lambda item: item["claim_id"])
        ledger["generation"] += 1
        ledger["updated_at"] = acquired_at
        ledger["empty_since"] = None
        ledger["retire_at"] = None
        ledger["empty_grace_applied_seconds"] = None
        self.save(ledger)
        return HostClaim.from_documents(ledger, claim)

    def renew(
        self,
        host_name: str,
        claim_id: str,
        *,
        expected_generation: int,
        renewal_ttl_seconds: int,
        now: datetime.datetime,
    ) -> HostClaim:
        validate_record_name(host_name)
        _positive_integer(
            expected_generation,
            label="expected claim generation",
        )
        _positive_duration(
            renewal_ttl_seconds,
            label="claim renewal TTL",
        )
        ledger = self.load(host_name)
        if ledger is None:
            raise AssertionError("required claim ledger unexpectedly absent")
        ledger, expired = self.expire_claims(ledger, now=now)
        if claim_id in expired:
            raise RunpodLocalError(
                f"host claim already expired: {claim_id}",
                code="host_claim_expired",
            )
        if ledger["operation_end"] is not None:
            raise RunpodLocalError(
                f"host {host_name} operation has ended",
                code="host_claim_host_changed",
            )
        if ledger["quarantine"] is not None:
            raise RunpodLocalError(
                f"host {host_name} is quarantined and cannot renew claims",
                code="host_claim_quarantined",
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
        claim = matches[0]
        if claim["generation"] != expected_generation:
            raise RunpodLocalError(
                "host claim generation changed",
                code="host_claim_generation_changed",
            )
        provider_deadline = parse_utc_timestamp(
            ledger["provider_termination_at"]
        )
        requested_deadline = now + datetime.timedelta(
            seconds=renewal_ttl_seconds
        )
        if requested_deadline >= provider_deadline:
            requested_deadline = provider_deadline
        if requested_deadline <= now:
            raise RunpodLocalError(
                "host provider deadline has expired",
                code="host_provider_deadline_expired",
            )
        claim["generation"] += 1
        claim["renewed_at"] = utc_timestamp(now)
        claim["renewal_deadline"] = utc_timestamp(requested_deadline)
        ledger["generation"] += 1
        ledger["updated_at"] = utc_timestamp(now)
        self.save(ledger)
        return HostClaim.from_documents(ledger, claim)

    def release(
        self,
        host_name: str,
        claim_id: str,
        *,
        expected_generation: int,
        now: datetime.datetime,
        retire_now: bool,
        reason: str = "released",
    ) -> ClaimReleaseResult:
        validate_record_name(host_name)
        _positive_integer(
            expected_generation,
            label="expected claim generation",
        )
        if type(retire_now) is not bool:
            raise RunpodLocalError(
                "retire-now selector must be boolean",
                code="invalid_host_claim",
            )
        if reason not in CLAIM_RELEASE_REASONS:
            raise RunpodLocalError(
                "claim release reason is unsupported",
                code="invalid_host_claim",
            )
        ledger = self.load(host_name)
        if ledger is None:
            raise AssertionError("required claim ledger unexpectedly absent")
        ledger, expired = self.expire_claims(ledger, now=now)
        if claim_id in expired:
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
        claim = matches[0]
        if claim["generation"] != expected_generation:
            raise RunpodLocalError(
                "host claim generation changed",
                code="host_claim_generation_changed",
            )
        ledger["claims"] = [
            candidate
            for candidate in ledger["claims"]
            if candidate["claim_id"] != claim_id
        ]
        self._close_claim(
            ledger,
            claim,
            reason=reason,
            now=now,
        )
        ledger["generation"] += 1
        ledger["updated_at"] = utc_timestamp(now)
        if (
            ledger["claims"]
            or ledger["retention"]["mode"] == "manual"
        ):
            ledger["empty_since"] = None
            ledger["retire_at"] = None
            ledger["empty_grace_applied_seconds"] = None
        else:
            grace = (
                0
                if retire_now or ledger["quarantine"] is not None
                else ledger["retention"]["empty_grace_seconds"]
            )
            ledger["empty_since"] = utc_timestamp(now)
            ledger["empty_grace_applied_seconds"] = grace
            ledger["retire_at"] = utc_timestamp(
                now + datetime.timedelta(seconds=grace)
            )
        self.save(ledger)
        retirement_due = (
            not ledger["claims"]
            and ledger["retire_at"] is not None
            and now >= parse_utc_timestamp(ledger["retire_at"])
        )
        return ClaimReleaseResult(
            host_name=host_name,
            claim_id=claim_id,
            released_generation=expected_generation,
            remaining_claim_count=len(ledger["claims"]),
            retention=ledger["retention"]["mode"],
            empty_since=ledger["empty_since"],
            retire_at=ledger["retire_at"],
            retirement_due=retirement_due,
        )


def claim_id_from_uuid(value: uuid.UUID) -> str:
    return f"claim-{value.hex}"


def default_allocation_from_host(
    host: dict[str, Any],
) -> dict[str, Any]:
    """Build conservative reusable-resource capacity from a host receipt."""

    expected = host.get("expected")
    if not isinstance(expected, dict):
        raise RunpodLocalError(
            "host receipt has no expected allocation",
            code="invalid_host_receipt",
        )
    gpu_id = expected.get("gpu_id")
    profile_capacity = expected.get("claim_capacity", {})
    if not isinstance(profile_capacity, dict):
        raise RunpodLocalError(
            "host receipt has invalid claim capacity",
            code="invalid_host_receipt",
        )
    gpu_count = expected.get("gpu_count")
    container_disk_gb = expected.get("container_disk_gb")
    return {
        "gpu_id": gpu_id,
        "gpu_count": gpu_count,
        "gpu_memory_gb": expected.get("gpu_memory_gb"),
        "cpu_count": profile_capacity.get(
            "cpu_count",
            expected.get("min_vcpu_count", 1),
        ),
        "ram_gb": profile_capacity.get(
            "ram_gb",
            expected.get("min_ram_gb", 1),
        ),
        "ephemeral_disk_gb": profile_capacity.get(
            "ephemeral_disk_gb",
            container_disk_gb,
        ),
        "network_volume_id": expected.get("network_volume_id"),
        "image": expected.get("image"),
        "data_center_id": expected.get("data_center_id"),
    }


def attest_claim_ledger_receipt(
    host: dict[str, Any],
    ledger: dict[str, Any],
) -> dict[str, Any]:
    """Require a ledger to repeat one exact immutable host receipt."""

    ledger = validate_claim_ledger(ledger)
    host_name = host.get("name")
    if (
        ledger["host_name"] != host_name
        or ledger["host_operation_id"] != host.get("operation_id")
        or ledger["pod_id"] != host.get("pod_id")
    ):
        raise RunpodLocalError(
            f"host {host_name} claim ledger names another operation",
            code="host_claim_host_changed",
        )
    receipt_retention = host.get("retention")
    profile = host.get("profile")
    provider_termination_at = host.get("provider_termination_at")
    if not isinstance(receipt_retention, dict):
        raise RunpodLocalError(
            f"host {host_name} has no retention receipt",
            code="invalid_host_receipt",
        )
    if not isinstance(profile, dict):
        raise RunpodLocalError(
            f"host {host_name} has no profile receipt",
            code="invalid_host_receipt",
        )
    expected_facts = {
        "profile": dict(profile),
        "provider_termination_at": provider_termination_at,
        "allocation": default_allocation_from_host(host),
        "retention": {
            "mode": receipt_retention.get("mode"),
            "empty_grace_seconds": receipt_retention.get(
                "empty_grace_seconds"
            ),
        },
    }
    drifted = [
        field
        for field, expected in expected_facts.items()
        if ledger[field] != expected
    ]
    if drifted:
        raise RunpodLocalError(
            f"host {host_name} claim ledger differs from its immutable "
            "receipt: "
            + ", ".join(drifted),
            code="host_claim_ledger_drift",
        )
    return ledger
