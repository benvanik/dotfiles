"""Durable service-use leases and semantic idle transitions.

A RunPod host claim is held by one ready model service.  Individual Pi
processes take local use leases against that service.  Releasing the final use
lease starts the model-service idle interval; it does not directly release the
RunPod claim.  Only the supervisor's explicit stop transition does that.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import fcntl
import json
import os
import pathlib
import re
import secrets
from collections.abc import Iterator
from typing import Any, Callable

from .documents import canonical_json_bytes
from .errors import ModelLabError
from .paths import ensure_private_directory

DEPLOYMENT_SCHEMA = "model-lab.deployment.v1"
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_OPAQUE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PHASES = frozenset(
    {
        "preparing",
        "ready",
        "idle",
        "quiescing",
        "stopping",
        "released",
        "failed",
    }
)
_HOST_RELEASE_MODES = frozenset({"now", "empty-grace", "claim-gone"})


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def format_timestamp(value: datetime.datetime) -> str:
    return value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str, label: str) -> datetime.datetime:
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ModelLabError(
            f"{label} is not an RFC3339 timestamp",
            code="invalid_deployment_state",
        ) from error
    if parsed.tzinfo is None:
        raise ModelLabError(
            f"{label} must include a timezone",
            code="invalid_deployment_state",
        )
    return parsed.astimezone(datetime.timezone.utc)


@dataclasses.dataclass(frozen=True)
class UseLease:
    lease_id: str
    owner_pid: int
    owner_start_time: str
    acquired_at: str

    def normalized(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class Deployment:
    service_id: str
    deployment_id: str
    workload_sha256: str
    service_sha256: str
    host_name: str
    claim_id: str
    claim_generation: int
    endpoint_receipt_path: str | None
    phase: str
    created_at: str
    updated_at: str
    last_inference_at: str
    idle_deadline: str | None
    host_release_mode: str | None
    use_leases: tuple[UseLease, ...]

    def normalized(self) -> dict[str, Any]:
        return {
            "schema": DEPLOYMENT_SCHEMA,
            "service_id": self.service_id,
            "deployment_id": self.deployment_id,
            "workload_sha256": self.workload_sha256,
            "service_sha256": self.service_sha256,
            "host_name": self.host_name,
            "claim_id": self.claim_id,
            "claim_generation": self.claim_generation,
            "endpoint_receipt_path": self.endpoint_receipt_path,
            "phase": self.phase,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_inference_at": self.last_inference_at,
            "idle_deadline": self.idle_deadline,
            "host_release_mode": self.host_release_mode,
            "use_leases": [
                lease.normalized()
                for lease in sorted(self.use_leases, key=lambda item: item.lease_id)
            ],
        }


@dataclasses.dataclass(frozen=True)
class UseRelease:
    deployment: Deployment
    final_use: bool
    stop_now: bool


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ModelLabError(
            f"{label} must be a non-empty string",
            code="invalid_deployment_state",
        )
    return value


def parse_deployment(value: Any) -> Deployment:
    if not isinstance(value, dict):
        raise ModelLabError(
            "deployment state must be a JSON object",
            code="invalid_deployment_state",
        )
    fields = {
        "schema",
        "service_id",
        "deployment_id",
        "workload_sha256",
        "service_sha256",
        "host_name",
        "claim_id",
        "claim_generation",
        "endpoint_receipt_path",
        "phase",
        "created_at",
        "updated_at",
        "last_inference_at",
        "idle_deadline",
        "host_release_mode",
        "use_leases",
    }
    if set(value) != fields or value.get("schema") != DEPLOYMENT_SCHEMA:
        raise ModelLabError(
            "deployment state has unsupported fields or schema",
            code="invalid_deployment_state",
        )
    service_id = _require_string(value["service_id"], "service_id")
    if not _IDENTIFIER.fullmatch(service_id):
        raise ModelLabError(
            "deployment service_id is invalid",
            code="invalid_deployment_state",
        )
    opaque_fields = ("deployment_id", "host_name", "claim_id")
    if any(
        not _OPAQUE_IDENTIFIER.fullmatch(_require_string(value[name], name))
        for name in opaque_fields
    ):
        raise ModelLabError(
            "deployment contains an invalid opaque identifier",
            code="invalid_deployment_state",
        )
    if (
        not _SHA256.fullmatch(
            _require_string(value["workload_sha256"], "workload_sha256")
        )
        or not _SHA256.fullmatch(
            _require_string(value["service_sha256"], "service_sha256")
        )
        or isinstance(value["claim_generation"], bool)
        or not isinstance(value["claim_generation"], int)
        or value["claim_generation"] < 1
        or value["phase"] not in _PHASES
        or (
            value["endpoint_receipt_path"] is not None
            and (
                not isinstance(value["endpoint_receipt_path"], str)
                or not pathlib.Path(value["endpoint_receipt_path"]).is_absolute()
            )
        )
    ):
        raise ModelLabError(
            "deployment contains an invalid value",
            code="invalid_deployment_state",
        )
    for label in ("created_at", "updated_at", "last_inference_at"):
        parse_timestamp(_require_string(value[label], label), label)
    deadline = value["idle_deadline"]
    if deadline is not None:
        if not isinstance(deadline, str):
            raise ModelLabError(
                "idle_deadline must be null or a timestamp",
                code="invalid_deployment_state",
            )
        parse_timestamp(deadline, "idle_deadline")
    host_release_mode = value["host_release_mode"]
    if (
        host_release_mode is not None
        and host_release_mode not in _HOST_RELEASE_MODES
    ):
        raise ModelLabError(
            "host_release_mode must be null, now, empty-grace, or claim-gone",
            code="invalid_deployment_state",
        )
    raw_leases = value["use_leases"]
    if not isinstance(raw_leases, list):
        raise ModelLabError(
            "use_leases must be an array",
            code="invalid_deployment_state",
        )
    leases: list[UseLease] = []
    for raw in raw_leases:
        if not isinstance(raw, dict) or set(raw) != {
            "lease_id",
            "owner_pid",
            "owner_start_time",
            "acquired_at",
        }:
            raise ModelLabError(
                "use lease has unsupported fields",
                code="invalid_deployment_state",
            )
        lease_id = _require_string(raw["lease_id"], "use lease ID")
        if (
            not _OPAQUE_IDENTIFIER.fullmatch(lease_id)
            or isinstance(raw["owner_pid"], bool)
            or not isinstance(raw["owner_pid"], int)
            or raw["owner_pid"] < 1
        ):
            raise ModelLabError(
                "use lease contains an invalid value",
                code="invalid_deployment_state",
            )
        acquired = _require_string(raw["acquired_at"], "use lease acquired_at")
        parse_timestamp(acquired, "use lease acquired_at")
        leases.append(
            UseLease(
                lease_id=lease_id,
                owner_pid=raw["owner_pid"],
                owner_start_time=_require_string(
                    raw["owner_start_time"], "use lease owner_start_time"
                ),
                acquired_at=acquired,
            )
        )
    if len({lease.lease_id for lease in leases}) != len(leases):
        raise ModelLabError(
            "deployment contains duplicate use leases",
            code="invalid_deployment_state",
        )
    if leases and (value["phase"] != "ready" or deadline is not None):
        raise ModelLabError(
            "a deployment with active users must be ready and not idle",
            code="invalid_deployment_state",
        )
    if value["phase"] == "idle" and deadline is None:
        raise ModelLabError(
            "an idle deployment must have an idle deadline",
            code="invalid_deployment_state",
        )
    if (
        value["phase"] in {"preparing", "ready", "idle"}
        and host_release_mode is not None
    ):
        raise ModelLabError(
            "an active deployment cannot carry a host release mode",
            code="invalid_deployment_state",
        )
    if (
        value["phase"] in {"quiescing", "stopping", "failed", "released"}
        and host_release_mode is None
    ):
        raise ModelLabError(
            "a terminal deployment must carry its host release mode",
            code="invalid_deployment_state",
        )
    if (
        value["phase"] == "preparing"
        and (
            leases
            or deadline is not None
            or value["endpoint_receipt_path"] is not None
        )
    ):
        raise ModelLabError(
            "a preparing deployment cannot have users, idle state, or endpoint",
            code="invalid_deployment_state",
        )
    if (
        value["phase"] in {"ready", "idle"}
        and value["endpoint_receipt_path"] is None
    ):
        raise ModelLabError(
            "a non-preparing deployment must name its endpoint receipt",
            code="invalid_deployment_state",
        )
    return Deployment(
        service_id=service_id,
        deployment_id=value["deployment_id"],
        workload_sha256=value["workload_sha256"],
        service_sha256=value["service_sha256"],
        host_name=value["host_name"],
        claim_id=value["claim_id"],
        claim_generation=value["claim_generation"],
        endpoint_receipt_path=value["endpoint_receipt_path"],
        phase=value["phase"],
        created_at=value["created_at"],
        updated_at=value["updated_at"],
        last_inference_at=value["last_inference_at"],
        idle_deadline=deadline,
        host_release_mode=host_release_mode,
        use_leases=tuple(leases),
    )


class DeploymentStore:
    """One-file-per-service state with a service-scoped advisory lock."""

    def __init__(
        self,
        root: pathlib.Path,
        *,
        clock: Callable[[], datetime.datetime] = utc_now,
    ) -> None:
        self.root = root
        self.clock = clock

    def _deployment_path(self, service_id: str) -> pathlib.Path:
        return self.root / "deployments" / f"{service_id}.json"

    @contextlib.contextmanager
    def locked(self, service_id: str) -> Iterator[None]:
        if not _IDENTIFIER.fullmatch(service_id):
            raise ModelLabError(
                "service ID is invalid",
                code="invalid_service_id",
            )
        locks = ensure_private_directory(self.root / "locks")
        path = locks / f"{service_id}.lock"
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            metadata = os.fstat(descriptor)
            if metadata.st_mode & 0o077:
                raise ModelLabError(
                    f"deployment lock permissions are unsafe: {path}",
                    code="unsafe_deployment_state",
                )
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def load(self, service_id: str) -> Deployment | None:
        path = self._deployment_path(service_id)
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ModelLabError(
                f"cannot open deployment state {path}: {error}",
                code="unsafe_deployment_state",
            ) from error
        try:
            metadata = os.fstat(descriptor)
            if metadata.st_mode & 0o077 or metadata.st_size > 1024 * 1024:
                raise ModelLabError(
                    f"deployment state has an unsafe identity: {path}",
                    code="unsafe_deployment_state",
                )
            payload = b""
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                payload += chunk
                if len(payload) > 1024 * 1024:
                    raise ModelLabError(
                        f"deployment state exceeds its size bound: {path}",
                        code="unsafe_deployment_state",
                    )
        finally:
            os.close(descriptor)
        try:
            return parse_deployment(json.loads(payload))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ModelLabError(
                f"deployment state is not valid JSON: {path}",
                code="invalid_deployment_state",
            ) from error

    def list(self) -> tuple[Deployment, ...]:
        """Load every exact deployment document in stable service order."""

        directory = self.root / "deployments"
        try:
            entries = list(directory.iterdir())
        except FileNotFoundError:
            return ()
        deployments: list[Deployment] = []
        for path in sorted(entries, key=lambda item: item.name):
            if (
                path.name.startswith(".")
                or path.suffix != ".json"
                or not _IDENTIFIER.fullmatch(path.stem)
            ):
                raise ModelLabError(
                    f"unexpected deployment-state entry: {path}",
                    code="unsafe_deployment_state",
                )
            deployment = self.load(path.stem)
            if deployment is None:
                raise ModelLabError(
                    f"deployment state disappeared while listing: {path}",
                    code="unsafe_deployment_state",
                )
            deployments.append(deployment)
        return tuple(deployments)

    def save(self, deployment: Deployment) -> None:
        directory = ensure_private_directory(self.root / "deployments")
        path = self._deployment_path(deployment.service_id)
        temporary = directory / (
            f".{deployment.service_id}.{secrets.token_hex(12)}.tmp"
        )
        payload = canonical_json_bytes(deployment.normalized())
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            position = 0
            while position < len(payload):
                position += os.write(descriptor, payload[position:])
            os.fsync(descriptor)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory_descriptor = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)

    def publish_ready(self, deployment: Deployment) -> Deployment:
        if deployment.phase != "ready" or deployment.idle_deadline is not None:
            raise ModelLabError(
                "only a non-idle ready deployment can be published",
                code="invalid_deployment_transition",
            )
        with self.locked(deployment.service_id):
            current = self.load(deployment.service_id)
            if (
                current is not None
                and current.phase != "released"
                and current.deployment_id != deployment.deployment_id
            ):
                raise ModelLabError(
                    "another deployment already owns this service",
                    code="deployment_conflict",
                )
            self.save(deployment)
        return deployment

    def publish_preparing(self, deployment: Deployment) -> Deployment:
        if (
            deployment.phase != "preparing"
            or deployment.endpoint_receipt_path is not None
            or deployment.idle_deadline is not None
            or deployment.use_leases
        ):
            raise ModelLabError(
                "only a clean preparing deployment can be published",
                code="invalid_deployment_transition",
            )
        with self.locked(deployment.service_id):
            current = self.load(deployment.service_id)
            if (
                current is not None
                and current.phase != "released"
                and current.deployment_id != deployment.deployment_id
            ):
                raise ModelLabError(
                    "another deployment already owns this service",
                    code="deployment_conflict",
                )
            self.save(deployment)
        return deployment

    def acquire_use(
        self,
        service_id: str,
        *,
        expected_workload_sha256: str,
        owner_pid: int,
        owner_start_time: str,
    ) -> UseLease:
        now_text = format_timestamp(self.clock())
        lease = UseLease(
            lease_id=f"use-{secrets.token_hex(16)}",
            owner_pid=owner_pid,
            owner_start_time=owner_start_time,
            acquired_at=now_text,
        )
        with self.locked(service_id):
            deployment = self.load(service_id)
            if deployment is None:
                raise ModelLabError(
                    f"service {service_id} is not deployed",
                    code="service_not_ready",
                )
            if deployment.workload_sha256 != expected_workload_sha256:
                raise ModelLabError(
                    "deployed service does not match the requested workload",
                    code="service_workload_mismatch",
                )
            if deployment.phase not in {"ready", "idle"}:
                raise ModelLabError(
                    f"service {service_id} is {deployment.phase}",
                    code="service_not_ready",
                )
            updated = dataclasses.replace(
                deployment,
                phase="ready",
                updated_at=now_text,
                idle_deadline=None,
                use_leases=(*deployment.use_leases, lease),
            )
            self.save(updated)
        return lease

    def transfer_use_owner(
        self,
        service_id: str,
        lease_id: str,
        *,
        expected_owner_pid: int,
        expected_owner_start_time: str,
        owner_pid: int,
        owner_start_time: str,
    ) -> UseLease:
        """Bind a pending supervisor lease to the admitted model-session."""

        if (
            isinstance(owner_pid, bool)
            or not isinstance(owner_pid, int)
            or owner_pid < 1
            or not owner_start_time
        ):
            raise ModelLabError(
                "new use-lease owner identity is invalid",
                code="invalid_use_lease_owner",
            )
        now_text = format_timestamp(self.clock())
        with self.locked(service_id):
            deployment = self.load(service_id)
            if deployment is None:
                raise ModelLabError(
                    f"service {service_id} has no deployment",
                    code="deployment_not_found",
                )
            matches = [
                lease
                for lease in deployment.use_leases
                if lease.lease_id == lease_id
            ]
            if len(matches) != 1:
                raise ModelLabError(
                    f"use lease is not active: {lease_id}",
                    code="use_lease_not_found",
                )
            current = matches[0]
            if (
                current.owner_pid != expected_owner_pid
                or current.owner_start_time != expected_owner_start_time
            ):
                raise ModelLabError(
                    "pending use lease owner changed before admission",
                    code="use_lease_owner_mismatch",
                )
            replacement = dataclasses.replace(
                current,
                owner_pid=owner_pid,
                owner_start_time=owner_start_time,
            )
            updated = dataclasses.replace(
                deployment,
                updated_at=now_text,
                use_leases=tuple(
                    replacement if lease.lease_id == lease_id else lease
                    for lease in deployment.use_leases
                ),
            )
            self.save(updated)
            return replacement

    def renew_claim_generation(
        self,
        service_id: str,
        *,
        deployment_id: str,
        expected_generation: int,
        generation: int,
    ) -> Deployment:
        """Commit the exact generation returned by a successful host renewal."""

        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation <= expected_generation
        ):
            raise ModelLabError(
                "renewed claim generation is invalid",
                code="invalid_claim_generation",
            )
        with self.locked(service_id):
            deployment = self.load(service_id)
            if (
                deployment is None
                or deployment.deployment_id != deployment_id
                or deployment.claim_generation != expected_generation
            ):
                raise ModelLabError(
                    "deployment changed during host-claim renewal",
                    code="deployment_changed",
                )
            updated = dataclasses.replace(
                deployment,
                claim_generation=generation,
                updated_at=format_timestamp(self.clock()),
            )
            self.save(updated)
            return updated

    def reconcile_orphaned_uses(
        self,
        *,
        idle_ttl_seconds: int,
    ) -> tuple[Deployment, ...]:
        """Move pre-boot use leases to idle after their channels have died."""

        reconciled: list[Deployment] = []
        for candidate in self.list():
            if not candidate.use_leases:
                continue
            current_time = self.clock()
            with self.locked(candidate.service_id):
                deployment = self.load(candidate.service_id)
                if deployment is None or not deployment.use_leases:
                    continue
                updated = dataclasses.replace(
                    deployment,
                    phase="idle",
                    updated_at=format_timestamp(current_time),
                    idle_deadline=format_timestamp(
                        current_time
                        + datetime.timedelta(seconds=idle_ttl_seconds)
                    ),
                    use_leases=(),
                )
                self.save(updated)
                reconciled.append(updated)
        return tuple(reconciled)

    def release_use(
        self,
        service_id: str,
        lease_id: str,
        *,
        idle_ttl_seconds: int,
        now: bool = False,
    ) -> UseRelease:
        current_time = self.clock()
        now_text = format_timestamp(current_time)
        with self.locked(service_id):
            deployment = self.load(service_id)
            if deployment is None:
                raise ModelLabError(
                    f"service {service_id} has no deployment",
                    code="deployment_not_found",
                )
            remaining = tuple(
                lease for lease in deployment.use_leases if lease.lease_id != lease_id
            )
            if len(remaining) == len(deployment.use_leases):
                raise ModelLabError(
                    f"use lease is not active: {lease_id}",
                    code="use_lease_not_found",
                )
            final_use = not remaining
            phase = "ready"
            deadline = None
            stop_now = False
            if final_use:
                if now:
                    phase = "quiescing"
                    stop_now = True
                else:
                    phase = "idle"
                    deadline = format_timestamp(
                        current_time + datetime.timedelta(seconds=idle_ttl_seconds)
                    )
            updated = dataclasses.replace(
                deployment,
                phase=phase,
                updated_at=now_text,
                idle_deadline=deadline,
                host_release_mode="now" if stop_now else None,
                use_leases=remaining,
            )
            self.save(updated)
        return UseRelease(
            deployment=updated,
            final_use=final_use,
            stop_now=stop_now,
        )

    def note_inference(self, service_id: str, *, idle_ttl_seconds: int) -> Deployment:
        current_time = self.clock()
        now_text = format_timestamp(current_time)
        with self.locked(service_id):
            deployment = self.load(service_id)
            if deployment is None or deployment.phase not in {"ready", "idle"}:
                raise ModelLabError(
                    f"service {service_id} is not accepting inference",
                    code="service_not_ready",
                )
            deadline = deployment.idle_deadline
            if deployment.phase == "idle":
                deadline = format_timestamp(
                    current_time + datetime.timedelta(seconds=idle_ttl_seconds)
                )
            updated = dataclasses.replace(
                deployment,
                updated_at=now_text,
                last_inference_at=now_text,
                idle_deadline=deadline,
            )
            self.save(updated)
            return updated

    def begin_idle(
        self,
        service_id: str,
        *,
        idle_ttl_seconds: int,
        now: bool,
    ) -> Deployment:
        current_time = self.clock()
        with self.locked(service_id):
            deployment = self.load(service_id)
            if deployment is None:
                raise ModelLabError(
                    f"service {service_id} has no deployment",
                    code="deployment_not_found",
                )
            if deployment.use_leases and not now:
                raise ModelLabError(
                    f"service {service_id} has active Pi users",
                    code="service_in_use",
                )
            updated = dataclasses.replace(
                deployment,
                phase="quiescing" if now else "idle",
                updated_at=format_timestamp(current_time),
                idle_deadline=(
                    None
                    if now
                    else format_timestamp(
                        current_time + datetime.timedelta(seconds=idle_ttl_seconds)
                    )
                ),
                host_release_mode="now" if now else None,
                use_leases=() if now else deployment.use_leases,
            )
            self.save(updated)
            return updated

    def begin_idle_cleanup_if_due(
        self,
        service_id: str,
    ) -> Deployment | None:
        """Atomically recheck idle eligibility and claim cleanup ownership."""

        current_time = self.clock()
        with self.locked(service_id):
            deployment = self.load(service_id)
            if (
                deployment is None
                or deployment.phase != "idle"
                or deployment.idle_deadline is None
                or deployment.use_leases
                or parse_timestamp(
                    deployment.idle_deadline,
                    "idle_deadline",
                )
                > current_time
            ):
                return None
            quiescing = dataclasses.replace(
                deployment,
                phase="quiescing",
                updated_at=format_timestamp(current_time),
                idle_deadline=None,
                host_release_mode="empty-grace",
                use_leases=(),
            )
            self.save(quiescing)
            return quiescing

    def begin_claim_gone_cleanup(
        self,
        service_id: str,
        *,
        deployment_id: str,
        claim_id: str,
        expected_generation: int,
    ) -> Deployment:
        """Atomically revoke all local authority over a vanished host claim."""

        current_time = self.clock()
        with self.locked(service_id):
            deployment = self.load(service_id)
            if (
                deployment is None
                or deployment.deployment_id != deployment_id
                or deployment.claim_id != claim_id
                or deployment.claim_generation != expected_generation
            ):
                raise ModelLabError(
                    "deployment changed while reconciling a vanished claim",
                    code="deployment_changed",
                )
            if deployment.phase == "released":
                return deployment
            quiescing = dataclasses.replace(
                deployment,
                phase="quiescing",
                updated_at=format_timestamp(current_time),
                idle_deadline=None,
                host_release_mode="claim-gone",
                use_leases=(),
            )
            self.save(quiescing)
            return quiescing
