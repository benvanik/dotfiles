"""Pure FIFO ownership model for host benchmark leases."""

from __future__ import annotations

import dataclasses
import enum
import math
import secrets
from collections import deque
from collections.abc import Callable, Iterable

from .errors import BenchmarkLockError


MAX_TICKETS = 64


class LeaseState(enum.StrEnum):
    """One admitted request's scheduler-owned lifecycle."""

    QUEUED = "queued"
    PREPARING = "preparing"
    ACTIVE = "active"


@dataclasses.dataclass(frozen=True)
class PeerIdentity:
    """Kernel-attested identity for one connected client process."""

    pid: int
    uid: int
    gid: int


@dataclasses.dataclass(frozen=True)
class Lease:
    """Immutable scheduler record for one exact client process."""

    lease_id: str
    sequence: int
    peer: PeerIdentity
    label: str
    inherited_lease_id: str | None
    enqueued_at: float
    state: LeaseState
    started_at: float | None = None


@dataclasses.dataclass(frozen=True)
class SchedulerSnapshot:
    """Read-only status projection of current scheduler ownership."""

    active: Lease | None
    preparing: Lease | None
    queued: tuple[Lease, ...]
    maintenance_owner: PeerIdentity | None


LeaseIdFactory = Callable[[], str]


def _default_lease_id() -> str:
    return secrets.token_hex(16)


class LeaseScheduler:
    """Serialize benchmark requests without owning transport or policy."""

    def __init__(
        self,
        *,
        lease_id_factory: LeaseIdFactory = _default_lease_id,
        maximum_tickets: int = MAX_TICKETS,
    ) -> None:
        if (
            isinstance(maximum_tickets, bool)
            or not isinstance(maximum_tickets, int)
            or maximum_tickets < 1
        ):
            raise ValueError("maximum benchmark tickets must be positive")
        self._lease_id_factory = lease_id_factory
        self._maximum_tickets = maximum_tickets
        self._next_sequence = 1
        self._queued: deque[Lease] = deque()
        self._preparing: Lease | None = None
        self._active: Lease | None = None
        self._maintenance_owner: PeerIdentity | None = None
        self._live_lease_ids: set[str] = set()

    @property
    def snapshot(self) -> SchedulerSnapshot:
        return SchedulerSnapshot(
            active=self._active,
            preparing=self._preparing,
            queued=tuple(self._queued),
            maintenance_owner=self._maintenance_owner,
        )

    def admit(
        self,
        *,
        peer: PeerIdentity,
        label: str,
        inherited_lease_id: str | None,
        now: float,
    ) -> Lease:
        """Append one request to the immutable FIFO admission order."""

        if self._maintenance_owner is not None:
            raise BenchmarkLockError(
                "benchmark service is in administrator maintenance",
                code="maintenance_active",
            )
        if (
            self._active is not None
            and inherited_lease_id == self._active.lease_id
            and peer.uid == self._active.peer.uid
        ):
            raise BenchmarkLockError(
                "nested benchmark-lock would wait for its own active lease",
                code="nested_lease",
            )
        admitted_count = (
            len(self._queued)
            + int(self._preparing is not None)
            + int(self._active is not None)
        )
        if admitted_count >= self._maximum_tickets:
            raise BenchmarkLockError(
                "benchmark queue reached its fixed admission bound",
                code="benchmark_queue_full",
            )
        lease_id = self._lease_id_factory()
        if (
            not isinstance(lease_id, str)
            or not lease_id
            or lease_id in self._live_lease_ids
        ):
            raise BenchmarkLockError(
                "lease ID source returned an invalid or live duplicate identity",
                code="invalid_lease_identity",
            )
        lease = Lease(
            lease_id=lease_id,
            sequence=self._next_sequence,
            peer=peer,
            label=label,
            inherited_lease_id=inherited_lease_id,
            enqueued_at=now,
            state=LeaseState.QUEUED,
        )
        self._next_sequence += 1
        self._live_lease_ids.add(lease_id)
        self._queued.append(lease)
        self._require_invariants()
        return lease

    def cancel(self, lease_id: str) -> Lease | None:
        """Remove a queued or preparing request without reordering survivors."""

        if self._preparing is not None and self._preparing.lease_id == lease_id:
            canceled = self._preparing
            self._preparing = None
            self._live_lease_ids.remove(canceled.lease_id)
            self._require_invariants()
            return canceled
        for index, lease in enumerate(self._queued):
            if lease.lease_id == lease_id:
                del self._queued[index]
                self._live_lease_ids.remove(lease.lease_id)
                self._require_invariants()
                return lease
        return None

    def begin_preparing(self) -> Lease | None:
        """Claim the live FIFO head for a policy transaction."""

        if self._active is not None or self._preparing is not None:
            return None
        if not self._queued:
            return None
        queued = self._queued.popleft()
        self._preparing = dataclasses.replace(
            queued,
            state=LeaseState.PREPARING,
        )
        self._require_invariants()
        return self._preparing

    def activate(self, lease_id: str, *, now: float) -> Lease:
        """Commit a verified policy transaction to the preparing request."""

        if self._preparing is None or self._preparing.lease_id != lease_id:
            raise BenchmarkLockError(
                "only the preparing FIFO head can become active",
                code="invalid_scheduler_transition",
            )
        if self._active is not None:
            raise BenchmarkLockError(
                "a benchmark lease is already active",
                code="invalid_scheduler_transition",
            )
        self._active = dataclasses.replace(
            self._preparing,
            state=LeaseState.ACTIVE,
            started_at=now,
        )
        self._preparing = None
        self._require_invariants()
        return self._active

    def fail_preparing(self, lease_id: str) -> Lease:
        """Drop a head whose policy transaction failed before grant."""

        if self._preparing is None or self._preparing.lease_id != lease_id:
            raise BenchmarkLockError(
                "policy failure does not name the preparing FIFO head",
                code="invalid_scheduler_transition",
            )
        failed = self._preparing
        self._preparing = None
        self._live_lease_ids.remove(failed.lease_id)
        self._require_invariants()
        return failed

    def complete_active(self, lease_id: str) -> Lease:
        """Release the one active process after its pidfd becomes readable."""

        if self._active is None or self._active.lease_id != lease_id:
            raise BenchmarkLockError(
                "process exit does not name the active benchmark lease",
                code="invalid_scheduler_transition",
            )
        completed = self._active
        self._active = None
        self._live_lease_ids.remove(completed.lease_id)
        self._require_invariants()
        return completed

    def queue_position(self, lease_id: str) -> int | None:
        """Return the current one-based FIFO position for a queued lease."""

        for position, lease in enumerate(self._queued, start=1):
            if lease.lease_id == lease_id:
                return position
        return None

    def enter_maintenance(self, peer: PeerIdentity) -> None:
        """Atomically fence new admissions when the scheduler is empty."""

        if peer.uid != 0:
            raise BenchmarkLockError(
                "benchmark maintenance requires root",
                code="maintenance_not_authorized",
            )
        if self._maintenance_owner is not None:
            raise BenchmarkLockError(
                "benchmark maintenance is already active",
                code="maintenance_active",
            )
        if self._active is not None or self._preparing is not None or self._queued:
            raise BenchmarkLockError(
                "benchmark service has an active or queued lease",
                code="maintenance_busy",
            )
        self._maintenance_owner = peer
        self._require_invariants()

    def leave_maintenance(self, peer: PeerIdentity) -> None:
        """Remove the exact administrator connection's admission fence."""

        if self._maintenance_owner != peer:
            raise BenchmarkLockError(
                "maintenance release does not match its owner",
                code="invalid_scheduler_transition",
            )
        self._maintenance_owner = None
        self._require_invariants()

    def restore(
        self,
        *,
        active: Lease | None,
        preparing: Lease | None,
        queued: Iterable[Lease],
        next_sequence: int,
    ) -> None:
        """Validate and load boot-local recovery metadata."""

        if (
            self._active is not None
            or self._preparing is not None
            or self._queued
            or self._maintenance_owner is not None
            or self._live_lease_ids
        ):
            raise BenchmarkLockError(
                "scheduler recovery requires a fresh scheduler",
                code="invalid_scheduler_recovery",
            )
        self._active = active
        self._preparing = preparing
        self._queued = deque(queued)
        self._next_sequence = next_sequence
        restored_leases = list(self._queued)
        if preparing is not None:
            restored_leases.insert(0, preparing)
        if active is not None:
            restored_leases.insert(0, active)
        self._live_lease_ids = {
            lease.lease_id
            for lease in restored_leases
            if isinstance(lease, Lease) and isinstance(lease.lease_id, str)
        }
        violation = self._invariant_violation()
        if violation is not None:
            self._active = None
            self._preparing = None
            self._queued.clear()
            self._live_lease_ids.clear()
            self._next_sequence = 1
            raise BenchmarkLockError(
                f"recovered scheduler metadata violates FIFO invariants: {violation}",
                code="invalid_scheduler_recovery",
            )
        self._require_invariants()

    def _invariant_violation(self) -> str | None:
        leases = list(self._queued)
        if self._preparing is not None:
            leases.insert(0, self._preparing)
        if self._active is not None:
            leases.insert(0, self._active)
        if any(not isinstance(lease, Lease) for lease in leases):
            return "a scheduler entry is not a lease"
        if len(leases) > self._maximum_tickets:
            return "the live ticket count exceeds its admission bound"
        if self._maintenance_owner is not None and leases:
            return "maintenance overlaps live benchmark tickets"
        if self._active is not None and self._preparing is not None:
            return "active and preparing leases overlap"

        for lease in leases:
            if not isinstance(lease.lease_id, str) or not lease.lease_id:
                return "a lease identity is empty or not text"
            if (
                isinstance(lease.sequence, bool)
                or not isinstance(lease.sequence, int)
                or lease.sequence <= 0
            ):
                return "a lease sequence is not a positive integer"
            if (
                isinstance(lease.enqueued_at, bool)
                or not isinstance(lease.enqueued_at, (int, float))
                or not math.isfinite(lease.enqueued_at)
                or lease.enqueued_at < 0
            ):
                return "a lease enqueue time is invalid"

        if (
            isinstance(self._next_sequence, bool)
            or not isinstance(self._next_sequence, int)
            or self._next_sequence <= 0
        ):
            return "the next lease sequence is invalid"
        identities = [lease.lease_id for lease in leases]
        sequences = [lease.sequence for lease in leases]
        if len(identities) != len(set(identities)):
            return "live lease identities are not unique"
        if len(sequences) != len(set(sequences)):
            return "live lease sequences are not unique"
        if self._next_sequence <= max(sequences, default=0):
            return "the next sequence does not follow every live lease"
        if not all(
            earlier.sequence < later.sequence
            for earlier, later in zip(self._queued, tuple(self._queued)[1:])
        ):
            return "queued leases are not in FIFO sequence order"
        owner = self._active if self._active is not None else self._preparing
        if owner is not None and not all(
            owner.sequence < lease.sequence for lease in self._queued
        ):
            return "the current owner does not precede every queued lease"
        if set(identities) != self._live_lease_ids:
            return "live lease identity tracking disagrees with scheduler state"
        if any(lease.state is not LeaseState.QUEUED for lease in self._queued):
            return "a queued lease has the wrong state"
        if any(lease.started_at is not None for lease in self._queued):
            return "a queued lease has a start time"
        if self._preparing is not None and (
            self._preparing.state is not LeaseState.PREPARING
            or self._preparing.started_at is not None
        ):
            return "the preparing lease has invalid state"
        if self._active is not None:
            started_at = self._active.started_at
            if (
                self._active.state is not LeaseState.ACTIVE
                or isinstance(started_at, bool)
                or not isinstance(started_at, (int, float))
                or not math.isfinite(started_at)
                or started_at < self._active.enqueued_at
            ):
                return "the active lease has invalid state or start time"
        return None

    def _require_invariants(self) -> None:
        violation = self._invariant_violation()
        if violation is not None:
            raise BenchmarkLockError(
                f"benchmark scheduler invariant failed: {violation}",
                code="invalid_scheduler_transition",
            )
        # Runtime validation above owns correctness under optimized Python.
        assert self._invariant_violation() is None
