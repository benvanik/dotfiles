"""Durable owner-operation journals for generic Runpod host claims."""

from __future__ import annotations

import datetime
import hashlib
import json
from collections.abc import Callable
from typing import Any

from .claims import (
    CLAIM_ID_PATTERN,
    CLAIM_REQUEST_IDENTITY_V1,
    CLAIM_REQUEST_IDENTITY_V2,
    CLOSED_CLAIM_REASONS,
    SHA256_PATTERN,
    HostClaim,
    HostClaimRequest,
    validate_claim_owner_name,
    validate_claim_owner_operation_id,
    validate_closed_claim_document,
    validate_claim_ledger,
    validate_host_operation_id,
)
from .errors import RunpodLocalError
from .state import (
    HOST_CONTROLLER_LOCK_SCOPE,
    StateRecordScan,
    StateStore,
    validate_record_name,
)
from .timeutil import parse_utc_timestamp, utc_timestamp


CLAIM_ACQUISITION_SCHEMA_V1 = "runpod.host-claim-acquisition.v1"
CLAIM_ACQUISITION_SCHEMA = "runpod.host-claim-acquisition.v2"
CLAIM_ACQUISITION_MIGRATION_SCHEMA = (
    "runpod.host-claim-acquisition-migration.v1"
)
CLAIM_ACQUISITION_FIELDS = {
    "schema_version",
    "record_name",
    "owner_system",
    "owner_instance",
    "owner_operation_id",
    "request_sha256",
    "request_identity_schema",
    "target",
    "host",
    "claim",
    "claim_closure",
    "acquisition_closure",
    "generation",
    "created_at",
    "updated_at",
}
CLAIM_ACQUISITION_V1_FIELDS = CLAIM_ACQUISITION_FIELDS - {
    "acquisition_closure",
    "request_identity_schema",
}
CLAIM_ACQUISITION_V1_EXTENDED_FIELDS = (
    CLAIM_ACQUISITION_V1_FIELDS | {"acquisition_closure"}
)


def _acquisition_record_name(
    owner_system: str,
    owner_instance: str,
    owner_operation_id: str,
) -> str:
    encoded = json.dumps(
        [owner_system, owner_instance, owner_operation_id],
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"claimop-{digest[:55]}"


def _json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _migration_record_name(record_name: str) -> str:
    digest = hashlib.sha256(record_name.encode("ascii")).hexdigest()
    return f"hostclaim-v2-{digest[:50]}"


def _validate_v1_migration_shape(document: Any) -> dict[str, Any]:
    fields = frozenset(document) if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or document.get("schema_version")
        != CLAIM_ACQUISITION_SCHEMA_V1
        or fields
        not in {
            frozenset(CLAIM_ACQUISITION_V1_FIELDS),
            frozenset(CLAIM_ACQUISITION_V1_EXTENDED_FIELDS),
        }
    ):
        raise RunpodLocalError(
            "host claim acquisition v1 migration source has invalid fields",
            code="invalid_host_claim_acquisition",
        )
    target = document.get("target")
    if target is not None and (
        not isinstance(target, dict)
        or set(target)
        != {
            "host_name",
            "host_operation_id",
            "predecessor_operation_id",
            "profile",
        }
    ):
        raise RunpodLocalError(
            "host claim acquisition v1 migration target has invalid fields",
            code="invalid_host_claim_acquisition",
        )
    return dict(document)


def validate_claim_acquisition(document: Any) -> dict[str, Any]:
    """Validate one durable owner-operation idempotency journal."""

    if (
        not isinstance(document, dict)
        or document.get("schema_version") != CLAIM_ACQUISITION_SCHEMA
    ):
        raise RunpodLocalError(
            "host claim acquisition has an unsupported schema",
            code="invalid_host_claim_acquisition",
        )
    if set(document) != CLAIM_ACQUISITION_FIELDS:
        raise RunpodLocalError(
            "host claim acquisition has invalid top-level fields",
            code="invalid_host_claim_acquisition",
        )
    owner_system = validate_claim_owner_name(
        document.get("owner_system"),
        label="owner system",
    )
    owner_instance = validate_claim_owner_name(
        document.get("owner_instance"),
        label="owner instance",
    )
    owner_operation_id = validate_claim_owner_operation_id(
        document.get("owner_operation_id")
    )
    record_name = document.get("record_name")
    expected_record_name = _acquisition_record_name(
        owner_system,
        owner_instance,
        owner_operation_id,
    )
    if record_name != expected_record_name:
        raise RunpodLocalError(
            "host claim acquisition has a different record identity",
            code="invalid_host_claim_acquisition",
        )
    request_sha256 = document.get("request_sha256")
    if (
        not isinstance(request_sha256, str)
        or not SHA256_PATTERN.fullmatch(request_sha256)
    ):
        raise RunpodLocalError(
            "host claim acquisition has an invalid request identity",
            code="invalid_host_claim_acquisition",
        )
    request_identity_schema = document.get("request_identity_schema")
    if request_identity_schema not in {
        CLAIM_REQUEST_IDENTITY_V1,
        CLAIM_REQUEST_IDENTITY_V2,
    }:
        raise RunpodLocalError(
            "host claim acquisition has an invalid request identity schema",
            code="invalid_host_claim_acquisition",
        )
    generation = document.get("generation")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation <= 0
    ):
        raise RunpodLocalError(
            "host claim acquisition has an invalid generation",
            code="invalid_host_claim_acquisition",
        )
    target = document.get("target")
    if target is not None:
        if not isinstance(target, dict) or set(target) != {
            "host_name",
            "host_operation_id",
            "predecessor_operation_id",
            "profile",
            "created_for_acquisition",
        }:
            raise RunpodLocalError(
                "host claim acquisition has an invalid launch target",
                code="invalid_host_claim_acquisition",
            )
        target_host_name = target["host_name"]
        if not isinstance(target_host_name, str):
            raise RunpodLocalError(
                "host claim acquisition has an invalid target host",
                code="invalid_host_claim_acquisition",
            )
        validate_record_name(target_host_name)
        validate_host_operation_id(target["host_operation_id"])
        predecessor_operation_id = target["predecessor_operation_id"]
        if predecessor_operation_id is not None:
            validate_host_operation_id(predecessor_operation_id)
            if predecessor_operation_id == target["host_operation_id"]:
                raise RunpodLocalError(
                    "host claim acquisition target repeats its predecessor",
                    code="invalid_host_claim_acquisition",
                )
        target_profile = target["profile"]
        if (
            not isinstance(target_profile, dict)
            or set(target_profile) != {"name", "sha256"}
            or not isinstance(target_profile["name"], str)
            or not isinstance(target_profile["sha256"], str)
            or not SHA256_PATTERN.fullmatch(target_profile["sha256"])
        ):
            raise RunpodLocalError(
                "host claim acquisition has an invalid target profile",
                code="invalid_host_claim_acquisition",
            )
        validate_record_name(target_profile["name"])
        if (
            target["created_for_acquisition"] is not None
            and type(target["created_for_acquisition"]) is not bool
        ):
            raise RunpodLocalError(
                "host claim acquisition target has invalid provenance",
                code="invalid_host_claim_acquisition",
            )
    host_binding = document.get("host")
    if host_binding is not None:
        if not isinstance(host_binding, dict) or set(host_binding) != {
            "host_name",
            "host_operation_id",
            "pod_id",
        }:
            raise RunpodLocalError(
                "host claim acquisition has an invalid host binding",
                code="invalid_host_claim_acquisition",
            )
        bound_host_name = host_binding["host_name"]
        if not isinstance(bound_host_name, str):
            raise RunpodLocalError(
                "host claim acquisition has an invalid bound host",
                code="invalid_host_claim_acquisition",
            )
        validate_record_name(bound_host_name)
        validate_host_operation_id(host_binding["host_operation_id"])
        bound_pod_id = host_binding["pod_id"]
        if (
            not isinstance(bound_pod_id, str)
            or not bound_pod_id
            or not bound_pod_id.isprintable()
        ):
            raise RunpodLocalError(
                "host claim acquisition has an invalid bound Pod",
                code="invalid_host_claim_acquisition",
            )
        if (
            target is None
            or target["host_name"] != bound_host_name
            or target["host_operation_id"]
            != host_binding["host_operation_id"]
        ):
            raise RunpodLocalError(
                "host claim acquisition host differs from its launch target",
                code="invalid_host_claim_acquisition",
            )
    binding = document.get("claim")
    if binding is not None:
        if not isinstance(binding, dict) or set(binding) != {
            "host_name",
            "host_operation_id",
            "pod_id",
            "claim_id",
        }:
            raise RunpodLocalError(
                "host claim acquisition has an invalid claim binding",
                code="invalid_host_claim_acquisition",
            )
        host_name = binding["host_name"]
        if not isinstance(host_name, str):
            raise RunpodLocalError(
                "host claim acquisition has an invalid host binding",
                code="invalid_host_claim_acquisition",
            )
        validate_record_name(host_name)
        validate_host_operation_id(binding["host_operation_id"])
        pod_id = binding["pod_id"]
        if (
            not isinstance(pod_id, str)
            or not pod_id
            or not pod_id.isprintable()
        ):
            raise RunpodLocalError(
                "host claim acquisition has an invalid Pod binding",
                code="invalid_host_claim_acquisition",
            )
        claim_id = binding["claim_id"]
        if (
            not isinstance(claim_id, str)
            or not CLAIM_ID_PATTERN.fullmatch(claim_id)
        ):
            raise RunpodLocalError(
                "host claim acquisition has an invalid claim binding",
                code="invalid_host_claim_acquisition",
            )
        if (
            host_binding is None
            or binding["host_name"] != host_binding["host_name"]
            or binding["host_operation_id"]
            != host_binding["host_operation_id"]
            or binding["pod_id"] != host_binding["pod_id"]
        ):
            raise RunpodLocalError(
                "host claim acquisition claim differs from its host binding",
                code="invalid_host_claim_acquisition",
            )
    claim_closure = document.get("claim_closure")
    if claim_closure is not None:
        if (
            binding is None
            or not isinstance(claim_closure, dict)
            or set(claim_closure)
            != {"reason", "closed_at", "generation"}
            or claim_closure["reason"] not in CLOSED_CLAIM_REASONS
            or not isinstance(claim_closure["closed_at"], str)
            or not isinstance(claim_closure["generation"], int)
            or isinstance(claim_closure["generation"], bool)
            or claim_closure["generation"] <= 0
        ):
            raise RunpodLocalError(
                "host claim acquisition has an invalid claim closure",
                code="invalid_host_claim_acquisition",
            )
        parse_utc_timestamp(claim_closure["closed_at"])
    acquisition_closure = document.get("acquisition_closure")
    if acquisition_closure is not None:
        if (
            not isinstance(acquisition_closure, dict)
            or set(acquisition_closure) != {"reason", "closed_at"}
            or acquisition_closure["reason"]
            not in {"cancelled", "expired-before-admission"}
            or not isinstance(acquisition_closure["closed_at"], str)
            or binding is not None
            or claim_closure is not None
        ):
            raise RunpodLocalError(
                "host claim acquisition has an invalid pre-claim closure",
                code="invalid_host_claim_acquisition",
            )
        parse_utc_timestamp(acquisition_closure["closed_at"])
    if (
        target is not None
        and target["created_for_acquisition"] is None
        and binding is None
        and acquisition_closure is None
    ):
        raise RunpodLocalError(
            "open unbound host claim acquisition has unknown target provenance",
            code="invalid_host_claim_acquisition",
        )
    timestamps = {}
    for field in ("created_at", "updated_at"):
        timestamp = document.get(field)
        if not isinstance(timestamp, str):
            raise RunpodLocalError(
                f"host claim acquisition has no {field}",
                code="invalid_host_claim_acquisition",
            )
        timestamps[field] = parse_utc_timestamp(timestamp)
    if timestamps["updated_at"] < timestamps["created_at"]:
        raise RunpodLocalError(
            "host claim acquisition update predates its creation",
            code="invalid_host_claim_acquisition",
        )
    normalized = dict(document)
    normalized["target"] = None if target is None else {
        "host_name": target["host_name"],
        "host_operation_id": target["host_operation_id"],
        "predecessor_operation_id": target[
            "predecessor_operation_id"
        ],
        "profile": dict(target["profile"]),
        "created_for_acquisition": target[
            "created_for_acquisition"
        ],
    }
    normalized["host"] = (
        None if host_binding is None else dict(host_binding)
    )
    normalized["claim"] = None if binding is None else dict(binding)
    normalized["claim_closure"] = (
        None if claim_closure is None else dict(claim_closure)
    )
    normalized["acquisition_closure"] = (
        None
        if acquisition_closure is None
        else dict(acquisition_closure)
    )
    return normalized


class ClaimAcquisitionStore:
    """Permanent request bindings for claim owner-operation idempotency."""

    def __init__(
        self,
        state: StateStore,
        *,
        clock: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        self.state = state
        self.clock = clock or (
            lambda: datetime.datetime.now(datetime.timezone.utc)
        )
        self._migration_errors_by_name: dict[str, RunpodLocalError] = {}

    def _now(self) -> datetime.datetime:
        now = self.clock()
        utc_timestamp(now)
        return now

    @staticmethod
    def _v2_candidate_from_v1(
        source: dict[str, Any],
        *,
        created_for_acquisition: bool | None,
        now: datetime.datetime,
    ) -> dict[str, Any]:
        candidate = dict(source)
        candidate["schema_version"] = CLAIM_ACQUISITION_SCHEMA
        candidate["request_identity_schema"] = CLAIM_REQUEST_IDENTITY_V1
        candidate["acquisition_closure"] = source.get(
            "acquisition_closure"
        )
        target = source.get("target")
        if target is not None:
            if not isinstance(target, dict):
                raise RunpodLocalError(
                    "v1 host claim acquisition has an invalid launch target",
                    code="invalid_host_claim_acquisition",
                )
            candidate["target"] = {
                **target,
                "created_for_acquisition": created_for_acquisition,
            }
        candidate = validate_claim_acquisition(candidate)
        candidate["generation"] += 1
        previous_updated_at = parse_utc_timestamp(source.get("updated_at"))
        candidate["updated_at"] = utc_timestamp(max(previous_updated_at, now))
        return validate_claim_acquisition(candidate)

    @staticmethod
    def _v1_target_provenance(
        source: dict[str, Any],
    ) -> bool | None:
        """Recover only provenance proved by the v1 journal itself."""

        target = source.get("target")
        if target is None:
            return False
        if not isinstance(target, dict):
            raise RunpodLocalError(
                "v1 host claim acquisition has an invalid launch target",
                code="invalid_host_claim_acquisition",
            )
        if (
            source.get("claim") is not None
            or source.get("claim_closure") is not None
            or source.get("acquisition_closure") is not None
        ):
            # Once a claim or closure exists, cleanup is owned by that durable
            # binding. Historical pre-claim provenance is unknowable in v1 and
            # can never again authorize unbound destructive cleanup.
            return None
        if target.get("predecessor_operation_id") is not None:
            # Replacement operations were minted only after a definitive
            # acquisition-owned no-capacity receipt.
            return True
        raise RunpodLocalError(
            "open unbound v1 acquisition target has no durable ownership "
            f"proof: {source.get('record_name')} / "
            f"{target.get('host_name')} / "
            f"{target.get('host_operation_id')}",
            code="host_claim_acquisition_migration_required",
        )

    def _v1_exact_ledger_witness(
        self,
        source: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Recover a claim admitted just before its v1 journal binding."""

        if (
            source.get("claim") is not None
            or source.get("claim_closure") is not None
            or source.get("acquisition_closure") is not None
        ):
            return None
        target = source.get("target")
        host = source.get("host")
        if (
            not isinstance(target, dict)
            or not isinstance(host, dict)
            or set(host)
            != {"host_name", "host_operation_id", "pod_id"}
            or host["host_name"] != target.get("host_name")
            or host["host_operation_id"]
            != target.get("host_operation_id")
        ):
            return None
        ledger_value = self.state.read("hostclaims", host["host_name"])
        if ledger_value is None:
            return None
        ledger = validate_claim_ledger(ledger_value)
        if (
            ledger["host_name"] != host["host_name"]
            or ledger["host_operation_id"] != host["host_operation_id"]
            or ledger["pod_id"] != host["pod_id"]
            or ledger["profile"] != target.get("profile")
        ):
            return None
        owner_identity = (
            source.get("owner_system"),
            source.get("owner_instance"),
            source.get("owner_operation_id"),
        )
        matches = [
            (claim, None)
            for claim in ledger["claims"]
            if (
                claim["owner_system"],
                claim["owner_instance"],
                claim["owner_operation_id"],
            )
            == owner_identity
        ]
        matches.extend(
            (
                claim,
                {
                    "reason": claim["reason"],
                    "closed_at": claim["closed_at"],
                    "generation": claim["generation"],
                },
            )
            for claim in ledger["closed_claims"]
            if (
                claim["owner_system"],
                claim["owner_instance"],
                claim["owner_operation_id"],
            )
            == owner_identity
        )
        if (
            len(matches) != 1
            or matches[0][0]["request_sha256"]
            != source.get("request_sha256")
        ):
            return None
        claim, closure = matches[0]
        return {
            "host": {
                "host_name": ledger["host_name"],
                "host_operation_id": ledger["host_operation_id"],
                "pod_id": ledger["pod_id"],
            },
            "claim": {
                "host_name": ledger["host_name"],
                "host_operation_id": ledger["host_operation_id"],
                "pod_id": ledger["pod_id"],
                "claim_id": claim["claim_id"],
            },
            "claim_closure": closure,
        }

    def _migrate_v1_record(
        self,
        source: dict[str, Any],
        *,
        stored_name: str,
    ) -> None:
        source = _validate_v1_migration_shape(source)
        migration_name = _migration_record_name(stored_name)
        existing = self.state.read("migrations", migration_name)
        if existing is not None:
            if (
                existing.get("schema_version")
                != CLAIM_ACQUISITION_MIGRATION_SCHEMA
                or existing.get("record_name") != migration_name
                or existing.get("source_record_name") != stored_name
                or existing.get("source_schema_version")
                != CLAIM_ACQUISITION_SCHEMA_V1
                or existing.get("target_schema_version")
                != CLAIM_ACQUISITION_SCHEMA
                or existing.get("source") != source
                or existing.get("source_sha256") != _json_sha256(source)
            ):
                raise RunpodLocalError(
                    "host claim acquisition migration receipt differs from "
                    "the retained v1 state",
                    code="host_claim_acquisition_migration_conflict",
                )
            retained_result = existing.get("result")
            result = validate_claim_acquisition(retained_result)
            if (
                result["record_name"] != stored_name
                or existing.get("result_sha256") != _json_sha256(result)
            ):
                raise RunpodLocalError(
                    "host claim acquisition migration result drifted",
                    code="host_claim_acquisition_migration_conflict",
                )
            self.state.write("hostclaimops", stored_name, result)
            return

        now = self._now()
        ledger_witness = self._v1_exact_ledger_witness(source)
        created_for_acquisition = (
            None
            if ledger_witness is not None
            else self._v1_target_provenance(source)
        )
        migration_source = source
        if ledger_witness is not None:
            migration_source = {
                **source,
                **ledger_witness,
            }
        result = self._v2_candidate_from_v1(
            migration_source,
            created_for_acquisition=created_for_acquisition,
            now=now,
        )
        if result["record_name"] != stored_name:
            raise RunpodLocalError(
                "v1 host claim acquisition is stored under another name",
                code="invalid_host_claim_acquisition",
            )
        migration = {
            "schema_version": CLAIM_ACQUISITION_MIGRATION_SCHEMA,
            "record_name": migration_name,
            "source_record_name": stored_name,
            "source_schema_version": CLAIM_ACQUISITION_SCHEMA_V1,
            "target_schema_version": CLAIM_ACQUISITION_SCHEMA,
            "source_sha256": _json_sha256(source),
            "result_sha256": _json_sha256(result),
            "created_for_acquisition": (
                created_for_acquisition
                if source.get("target") is not None
                else None
            ),
            "migrated_at": utc_timestamp(now),
            "source": source,
            "result": result,
        }
        self.state.write("migrations", migration_name, migration)
        self.state.write("hostclaimops", stored_name, result)

    def migrate_v1_records(self) -> int:
        """Transition all provable v1 journals under the claim mutation lock."""

        migrated = 0
        self._migration_errors_by_name = {}
        with self.state.locked(HOST_CONTROLLER_LOCK_SCOPE):
            for scanned in self.state.scan("hostclaimops"):
                if scanned.error is not None:
                    self._migration_errors_by_name[scanned.name] = (
                        scanned.error
                    )
                    continue
                if scanned.value is None:
                    continue
                source = scanned.value
                if (
                    source.get("schema_version")
                    != CLAIM_ACQUISITION_SCHEMA_V1
                ):
                    try:
                        acquisition = validate_claim_acquisition(source)
                        if acquisition["record_name"] != scanned.name:
                            raise RunpodLocalError(
                                "host claim acquisition is stored under "
                                "another name",
                                code="invalid_host_claim_acquisition",
                            )
                    except RunpodLocalError as error:
                        self._migration_errors_by_name[scanned.name] = error
                    continue
                try:
                    self._migrate_v1_record(
                        source,
                        stored_name=scanned.name,
                    )
                    migrated += 1
                except RunpodLocalError as error:
                    self._migration_errors_by_name[scanned.name] = error
        return migrated

    @staticmethod
    def _name(request: HostClaimRequest) -> str:
        request.validated()
        return _acquisition_record_name(
            request.owner_system,
            request.owner_instance,
            request.owner_operation_id,
        )

    @staticmethod
    def _assert_request(
        acquisition: dict[str, Any],
        request: HostClaimRequest,
    ) -> None:
        if (
            acquisition["owner_system"] != request.owner_system
            or acquisition["owner_instance"] != request.owner_instance
            or acquisition["owner_operation_id"]
            != request.owner_operation_id
        ):
            raise RunpodLocalError(
                "host claim acquisition record identity collided",
                code="host_claim_operation_collision",
            )
        if acquisition["request_sha256"] != request.sha256_for_schema(
            acquisition["request_identity_schema"]
        ):
            raise RunpodLocalError(
                "owner operation already names a different claim request",
                code="host_claim_operation_conflict",
            )

    def load(
        self,
        request: HostClaimRequest,
        *,
        required: bool = False,
    ) -> dict[str, Any] | None:
        request.validated()
        value = self.state.read("hostclaimops", self._name(request))
        if value is None:
            if required:
                raise RunpodLocalError(
                    "owner operation has no acquisition journal",
                    code="host_claim_acquisition_not_found",
                )
            return None
        migration_error = self._migration_errors_by_name.get(
            self._name(request)
        )
        if migration_error is not None:
            raise migration_error
        acquisition = validate_claim_acquisition(value)
        self._assert_request(acquisition, request)
        return acquisition

    def begin(
        self,
        request: HostClaimRequest,
        *,
        now: datetime.datetime,
    ) -> dict[str, Any]:
        request.validated()
        timestamp = utc_timestamp(now)
        existing = self.load(request)
        if existing is not None:
            return existing
        acquisition = validate_claim_acquisition(
            {
                "schema_version": CLAIM_ACQUISITION_SCHEMA,
                "record_name": self._name(request),
                "owner_system": request.owner_system,
                "owner_instance": request.owner_instance,
                "owner_operation_id": request.owner_operation_id,
                "request_sha256": request.sha256(),
                "request_identity_schema": CLAIM_REQUEST_IDENTITY_V2,
                "target": None,
                "host": None,
                "claim": None,
                "claim_closure": None,
                "acquisition_closure": None,
                "generation": 1,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        self.state.write(
            "hostclaimops",
            acquisition["record_name"],
            acquisition,
        )
        return acquisition

    def close_unbound(
        self,
        request: HostClaimRequest,
        *,
        reason: str,
        now: datetime.datetime,
    ) -> dict[str, Any]:
        """Permanently close one owner operation before claim admission."""

        if reason not in {"cancelled", "expired-before-admission"}:
            raise RunpodLocalError(
                "unsupported pre-claim acquisition closure reason",
                code="invalid_host_claim_acquisition",
            )
        acquisition = self.load(request, required=True)
        if acquisition is None:
            raise AssertionError(
                "required claim acquisition unexpectedly absent"
            )
        closure = {
            "reason": reason,
            "closed_at": utc_timestamp(now),
        }
        existing = acquisition["acquisition_closure"]
        if existing is not None:
            if existing != closure and existing["reason"] != reason:
                raise RunpodLocalError(
                    "pre-claim acquisition closure changed",
                    code="host_claim_acquisition_drift",
                )
            return acquisition
        if acquisition["claim"] is not None:
            raise RunpodLocalError(
                "cannot close an acquisition after claim admission",
                code="host_claim_acquisition_drift",
            )
        acquisition["acquisition_closure"] = closure
        acquisition["generation"] += 1
        acquisition["updated_at"] = utc_timestamp(now)
        acquisition = validate_claim_acquisition(acquisition)
        self.state.write(
            "hostclaimops",
            acquisition["record_name"],
            acquisition,
        )
        return acquisition

    def promote_request_identity(
        self,
        request: HostClaimRequest,
        *,
        now: datetime.datetime,
    ) -> dict[str, Any]:
        """Promote one verified unbound v1 request before new admission."""

        acquisition = self.load(request, required=True)
        if acquisition is None:
            raise AssertionError(
                "required claim acquisition unexpectedly absent"
            )
        if (
            acquisition["request_identity_schema"]
            == CLAIM_REQUEST_IDENTITY_V2
        ):
            return acquisition
        if (
            acquisition["claim"] is not None
            or acquisition["claim_closure"] is not None
            or acquisition["acquisition_closure"] is not None
        ):
            raise RunpodLocalError(
                "bound or closed v1 claim acquisition cannot change request "
                "identity",
                code="host_claim_acquisition_drift",
            )
        # load() already proved the exact v1 request identity, including its
        # original 300-second relative budget.
        acquisition["request_identity_schema"] = CLAIM_REQUEST_IDENTITY_V2
        acquisition["request_sha256"] = request.sha256()
        acquisition["generation"] += 1
        acquisition["updated_at"] = utc_timestamp(now)
        acquisition = validate_claim_acquisition(acquisition)
        self.state.write(
            "hostclaimops",
            acquisition["record_name"],
            acquisition,
        )
        return acquisition

    def select_target(
        self,
        request: HostClaimRequest,
        *,
        host_name: str,
        host_operation_id: str,
        predecessor_operation_id: str | None,
        profile: dict[str, str],
        created_for_acquisition: bool,
        now: datetime.datetime,
    ) -> dict[str, Any]:
        validate_record_name(host_name)
        acquisition = self.load(request, required=True)
        if acquisition is None:
            raise AssertionError(
                "required claim acquisition unexpectedly absent"
            )
        target = {
            "host_name": host_name,
            "host_operation_id": host_operation_id,
            "predecessor_operation_id": predecessor_operation_id,
            "profile": dict(profile),
            "created_for_acquisition": created_for_acquisition,
        }
        existing = acquisition["target"]
        if existing is not None:
            if existing != target:
                raise RunpodLocalError(
                    "owner operation acquisition names another launch target",
                    code="host_claim_acquisition_drift",
                )
            return acquisition
        if acquisition["host"] is not None or acquisition["claim"] is not None:
            raise RunpodLocalError(
                "bound owner operation acquisition has no launch target",
                code="host_claim_acquisition_drift",
            )
        acquisition["target"] = target
        acquisition["generation"] += 1
        acquisition["updated_at"] = utc_timestamp(now)
        acquisition = validate_claim_acquisition(acquisition)
        self.state.write(
            "hostclaimops",
            acquisition["record_name"],
            acquisition,
        )
        return acquisition

    def clear_unsubmitted_target(
        self,
        request: HostClaimRequest,
        *,
        now: datetime.datetime,
    ) -> dict[str, Any]:
        acquisition = self.load(request, required=True)
        if acquisition is None:
            raise AssertionError(
                "required claim acquisition unexpectedly absent"
            )
        if acquisition["host"] is not None or acquisition["claim"] is not None:
            raise RunpodLocalError(
                "cannot clear a launch target after binding a host",
                code="host_claim_acquisition_drift",
            )
        if acquisition["target"] is None:
            return acquisition
        acquisition["target"] = None
        acquisition["generation"] += 1
        acquisition["updated_at"] = utc_timestamp(now)
        acquisition = validate_claim_acquisition(acquisition)
        self.state.write(
            "hostclaimops",
            acquisition["record_name"],
            acquisition,
        )
        return acquisition

    def advance_rejected_target(
        self,
        request: HostClaimRequest,
        *,
        rejected_host_operation_id: str,
        new_host_operation_id: str,
        now: datetime.datetime,
    ) -> dict[str, Any]:
        """Atomically replace one unbound, definitively rejected launch target."""

        validate_host_operation_id(rejected_host_operation_id)
        validate_host_operation_id(new_host_operation_id)
        acquisition = self.load(request, required=True)
        if acquisition is None:
            raise AssertionError(
                "required claim acquisition unexpectedly absent"
            )
        target = acquisition["target"]
        if (
            target is None
            or target["host_operation_id"]
            != rejected_host_operation_id
        ):
            raise RunpodLocalError(
                "rejected host operation differs from its acquisition target",
                code="host_claim_acquisition_drift",
            )
        if acquisition["host"] is not None or acquisition["claim"] is not None:
            raise RunpodLocalError(
                "cannot advance a rejected target after binding provider state",
                code="host_claim_acquisition_drift",
            )
        if new_host_operation_id == rejected_host_operation_id:
            raise RunpodLocalError(
                "replacement host operation repeats its rejected predecessor",
                code="invalid_host_operation_id",
            )
        acquisition["target"] = {
            "host_name": target["host_name"],
            "host_operation_id": new_host_operation_id,
            "predecessor_operation_id": rejected_host_operation_id,
            "profile": dict(target["profile"]),
            "created_for_acquisition": True,
        }
        acquisition["generation"] += 1
        acquisition["updated_at"] = utc_timestamp(now)
        acquisition = validate_claim_acquisition(acquisition)
        self.state.write(
            "hostclaimops",
            acquisition["record_name"],
            acquisition,
        )
        return acquisition

    def bind_host(
        self,
        request: HostClaimRequest,
        host: dict[str, Any],
        *,
        now: datetime.datetime,
    ) -> dict[str, Any]:
        acquisition = self.load(request, required=True)
        if acquisition is None:
            raise AssertionError(
                "required claim acquisition unexpectedly absent"
            )
        target = acquisition["target"]
        if (
            target is None
            or target["host_name"] != host.get("name")
            or target["host_operation_id"] != host.get("operation_id")
            or target["profile"] != host.get("profile")
        ):
            raise RunpodLocalError(
                "provider host differs from its acquisition launch target",
                code="host_claim_acquisition_drift",
            )
        binding = {
            "host_name": host.get("name"),
            "host_operation_id": host.get("operation_id"),
            "pod_id": host.get("pod_id"),
        }
        existing = acquisition["host"]
        if existing is not None:
            if existing != binding:
                raise RunpodLocalError(
                    "owner operation acquisition names another provider host",
                    code="host_claim_acquisition_drift",
                )
            return acquisition
        acquisition["host"] = binding
        acquisition["generation"] += 1
        acquisition["updated_at"] = utc_timestamp(now)
        acquisition = validate_claim_acquisition(acquisition)
        self.state.write(
            "hostclaimops",
            acquisition["record_name"],
            acquisition,
        )
        return acquisition

    def bind(
        self,
        request: HostClaimRequest,
        claim: HostClaim,
        *,
        now: datetime.datetime,
    ) -> dict[str, Any]:
        acquisition = self.load(request, required=True)
        if acquisition is None:
            raise AssertionError(
                "required claim acquisition unexpectedly absent"
            )
        binding = {
            "host_name": claim.host_name,
            "host_operation_id": claim.operation_id,
            "pod_id": claim.provider_resource_id,
            "claim_id": claim.claim_id,
        }
        target_identity = {
            "host_name": claim.host_name,
            "host_operation_id": claim.operation_id,
            "profile": {
                "name": claim.profile_name,
                "sha256": claim.profile_sha256,
            },
        }
        host_binding = {
            "host_name": claim.host_name,
            "host_operation_id": claim.operation_id,
            "pod_id": claim.provider_resource_id,
        }
        if acquisition["target"] is None:
            acquisition["target"] = {
                **target_identity,
                "predecessor_operation_id": None,
                "created_for_acquisition": False,
            }
        else:
            recorded_target = acquisition["target"]
            if any(
                recorded_target[field] != value
                for field, value in target_identity.items()
            ):
                raise RunpodLocalError(
                    "owner operation acquisition names another launch target",
                    code="host_claim_acquisition_drift",
                )
        if acquisition["host"] is None:
            acquisition["host"] = host_binding
        elif acquisition["host"] != host_binding:
            raise RunpodLocalError(
                "owner operation acquisition names another provider host",
                code="host_claim_acquisition_drift",
            )
        existing = acquisition["claim"]
        if existing is not None:
            if existing != binding:
                raise RunpodLocalError(
                    "owner operation acquisition names a different claim",
                    code="host_claim_acquisition_drift",
                )
            return acquisition
        acquisition["claim"] = binding
        acquisition["generation"] += 1
        acquisition["updated_at"] = utc_timestamp(now)
        acquisition = validate_claim_acquisition(acquisition)
        self.state.write(
            "hostclaimops",
            acquisition["record_name"],
            acquisition,
        )
        return acquisition

    def close_claim(
        self,
        closed_claim: dict[str, Any],
        *,
        ledger: dict[str, Any],
        now: datetime.datetime,
    ) -> dict[str, Any]:
        """Bind one ledger closure permanently to its acquisition journal."""

        closed_claim = validate_closed_claim_document(closed_claim)
        ledger = validate_claim_ledger(ledger)
        record_name = _acquisition_record_name(
            closed_claim["owner_system"],
            closed_claim["owner_instance"],
            closed_claim["owner_operation_id"],
        )
        value = self.state.read("hostclaimops", record_name)
        if value is None:
            raise RunpodLocalError(
                "closed claim has no acquisition journal",
                code="host_claim_acquisition_not_found",
            )
        acquisition = validate_claim_acquisition(value)
        closure = {
            "reason": closed_claim["reason"],
            "closed_at": closed_claim["closed_at"],
            "generation": closed_claim["generation"],
        }
        if (
            acquisition["request_sha256"]
            != closed_claim["request_sha256"]
        ):
            raise RunpodLocalError(
                "closed claim differs from its acquisition journal",
                code="host_claim_acquisition_drift",
            )
        existing_closure = acquisition["claim_closure"]
        if existing_closure is not None:
            claim_binding = acquisition["claim"]
            if (
                existing_closure != closure
                or claim_binding is None
                or claim_binding["claim_id"] != closed_claim["claim_id"]
            ):
                raise RunpodLocalError(
                    "claim closure differs from its acquisition journal",
                    code="host_claim_acquisition_drift",
                )
            return acquisition
        target_identity = {
            "host_name": ledger["host_name"],
            "host_operation_id": ledger["host_operation_id"],
            "profile": dict(ledger["profile"]),
        }
        if acquisition["target"] is None:
            acquisition["target"] = {
                **target_identity,
                "predecessor_operation_id": None,
                "created_for_acquisition": False,
            }
        elif any(
            acquisition["target"][field] != expected
            for field, expected in target_identity.items()
        ):
            raise RunpodLocalError(
                "closed claim host differs from its acquisition target",
                code="host_claim_acquisition_drift",
            )
        host_binding = {
            "host_name": ledger["host_name"],
            "host_operation_id": ledger["host_operation_id"],
            "pod_id": ledger["pod_id"],
        }
        if acquisition["host"] is None:
            acquisition["host"] = host_binding
        elif acquisition["host"] != host_binding:
            raise RunpodLocalError(
                "closed claim host differs from its acquisition journal",
                code="host_claim_acquisition_drift",
            )
        expected_claim_binding = {
            **host_binding,
            "claim_id": closed_claim["claim_id"],
        }
        claim_binding = acquisition["claim"]
        if claim_binding is None:
            acquisition["claim"] = expected_claim_binding
        elif claim_binding != expected_claim_binding:
            raise RunpodLocalError(
                "closed claim differs from its acquisition journal",
                code="host_claim_acquisition_drift",
            )
        acquisition["claim_closure"] = closure
        acquisition["generation"] += 1
        acquisition["updated_at"] = utc_timestamp(now)
        acquisition = validate_claim_acquisition(acquisition)
        self.state.write(
            "hostclaimops",
            acquisition["record_name"],
            acquisition,
        )
        return acquisition

    def reconcile_closed_claims(
        self,
        ledgers: list[dict[str, Any]],
        *,
        now: datetime.datetime,
    ) -> int:
        """Flush retained ledger closures into permanent acquisition state."""

        if not ledgers:
            return 0
        scanned_by_name = {
            scanned.name: scanned
            for scanned in self.scan()
        }
        reconciled = 0
        for value in ledgers:
            ledger = validate_claim_ledger(value)
            for closed_claim in ledger["closed_claims"]:
                record_name = _acquisition_record_name(
                    closed_claim["owner_system"],
                    closed_claim["owner_instance"],
                    closed_claim["owner_operation_id"],
                )
                scanned = scanned_by_name.get(record_name)
                if scanned is None:
                    raise RunpodLocalError(
                        "closed claim has no acquisition journal",
                        code="host_claim_acquisition_not_found",
                    )
                if scanned.error is not None:
                    raise scanned.error
                acquisition = scanned.value
                if acquisition is None:
                    raise AssertionError(
                        "valid acquisition scan unexpectedly has no value"
                    )
                closure = {
                    "reason": closed_claim["reason"],
                    "closed_at": closed_claim["closed_at"],
                    "generation": closed_claim["generation"],
                }
                existing = acquisition["claim_closure"]
                binding = acquisition["claim"]
                if existing is not None:
                    if (
                        existing != closure
                        or binding is None
                        or binding["claim_id"] != closed_claim["claim_id"]
                        or acquisition["request_sha256"]
                        != closed_claim["request_sha256"]
                    ):
                        raise RunpodLocalError(
                            "claim closure differs from its acquisition "
                            "journal",
                            code="host_claim_acquisition_drift",
                        )
                    continue
                self.close_claim(
                    closed_claim,
                    ledger=ledger,
                    now=now,
                )
                reconciled += 1
        return reconciled

    def list(self) -> list[dict[str, Any]]:
        return [
            validate_claim_acquisition(value)
            for value in self.state.list("hostclaimops")
        ]

    def scan(self) -> list[StateRecordScan]:
        records = []
        for scanned in self.state.scan("hostclaimops"):
            if scanned.error is not None:
                records.append(scanned)
                continue
            try:
                acquisition = validate_claim_acquisition(scanned.value)
                if acquisition["record_name"] != scanned.name:
                    raise RunpodLocalError(
                        "host claim acquisition is stored under another name",
                        code="invalid_host_claim_acquisition",
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
                    value=acquisition,
                    error=None,
                )
            )
        return records
