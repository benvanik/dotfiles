from __future__ import annotations

import unittest

from benchmark_lock.errors import BenchmarkLockError
from benchmark_lock.scheduler import (
    Lease,
    LeaseScheduler,
    LeaseState,
    PeerIdentity,
)


class LeaseIds:
    def __init__(self) -> None:
        self.next_value = 1

    def __call__(self) -> str:
        value = f"lease-{self.next_value}"
        self.next_value += 1
        return value


class LeaseSchedulerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scheduler = LeaseScheduler(lease_id_factory=LeaseIds())
        self.peer_a = PeerIdentity(pid=101, uid=1000, gid=1000)
        self.peer_b = PeerIdentity(pid=102, uid=1000, gid=1000)
        self.peer_c = PeerIdentity(pid=103, uid=1001, gid=1001)

    def admit(
        self,
        peer: PeerIdentity,
        *,
        inherited_lease_id: str | None = None,
        now: float = 1.0,
    ) -> Lease:
        return self.scheduler.admit(
            peer=peer,
            label=f"pid-{peer.pid}",
            inherited_lease_id=inherited_lease_id,
            now=now,
        )

    def test_fifo_survives_middle_cancellation_and_direct_handoff(self) -> None:
        first = self.admit(self.peer_a)
        canceled = self.admit(self.peer_b)
        third = self.admit(self.peer_c)

        self.assertEqual(self.scheduler.queue_position(first.lease_id), 1)
        self.assertEqual(self.scheduler.queue_position(canceled.lease_id), 2)
        self.assertEqual(self.scheduler.queue_position(third.lease_id), 3)
        self.assertEqual(self.scheduler.cancel(canceled.lease_id), canceled)
        self.assertEqual(self.scheduler.queue_position(third.lease_id), 2)

        preparing = self.scheduler.begin_preparing()
        self.assertEqual(preparing.lease_id, first.lease_id)
        active = self.scheduler.activate(first.lease_id, now=2.0)
        self.assertEqual(active.state, LeaseState.ACTIVE)
        self.assertEqual(active.started_at, 2.0)
        self.assertIsNone(self.scheduler.begin_preparing())

        self.assertEqual(
            self.scheduler.complete_active(first.lease_id),
            active,
        )
        next_preparing = self.scheduler.begin_preparing()
        self.assertEqual(next_preparing.lease_id, third.lease_id)

    def test_policy_failure_drops_only_the_fifo_head(self) -> None:
        first = self.admit(self.peer_a)
        second = self.admit(self.peer_b)

        self.scheduler.begin_preparing()
        failed = self.scheduler.fail_preparing(first.lease_id)
        self.assertEqual(failed.lease_id, first.lease_id)
        self.assertEqual(
            self.scheduler.begin_preparing().lease_id,
            second.lease_id,
        )

    def test_current_inherited_lease_rejects_nested_owner(self) -> None:
        first = self.admit(self.peer_a)
        self.scheduler.begin_preparing()
        self.scheduler.activate(first.lease_id, now=2.0)

        with self.assertRaises(BenchmarkLockError) as raised:
            self.admit(
                self.peer_b,
                inherited_lease_id=first.lease_id,
            )
        self.assertEqual(raised.exception.code, "nested_lease")

    def test_stale_or_other_uid_inherited_identity_is_not_authority(self) -> None:
        first = self.admit(self.peer_a)
        self.scheduler.begin_preparing()
        self.scheduler.activate(first.lease_id, now=2.0)

        other_uid = self.admit(
            self.peer_c,
            inherited_lease_id=first.lease_id,
        )
        stale = self.admit(
            self.peer_b,
            inherited_lease_id="already-finished",
        )
        self.assertEqual(
            self.scheduler.snapshot.queued,
            (other_uid, stale),
        )

    def test_maintenance_is_root_only_atomic_and_connection_owned(self) -> None:
        with self.assertRaises(BenchmarkLockError) as raised:
            self.scheduler.enter_maintenance(self.peer_a)
        self.assertEqual(
            raised.exception.code,
            "maintenance_not_authorized",
        )

        root = PeerIdentity(pid=1, uid=0, gid=0)
        self.scheduler.enter_maintenance(root)
        with self.assertRaises(BenchmarkLockError) as raised:
            self.admit(self.peer_a)
        self.assertEqual(raised.exception.code, "maintenance_active")
        with self.assertRaises(BenchmarkLockError):
            self.scheduler.leave_maintenance(PeerIdentity(pid=2, uid=0, gid=0))
        self.scheduler.leave_maintenance(root)
        self.admit(self.peer_a)

        with self.assertRaises(BenchmarkLockError) as raised:
            self.scheduler.enter_maintenance(root)
        self.assertEqual(raised.exception.code, "maintenance_busy")

    def test_crash_visible_fence_survives_connection_maintenance(self) -> None:
        root = PeerIdentity(pid=1, uid=0, gid=0)
        self.scheduler.set_admission_fence(True)

        with self.assertRaises(BenchmarkLockError) as raised:
            self.admit(self.peer_a)
        self.assertEqual(raised.exception.code, "maintenance_active")
        self.scheduler.enter_maintenance(root)
        self.assertTrue(self.scheduler.snapshot.admission_fenced)
        self.scheduler.leave_maintenance(root)
        self.assertTrue(self.scheduler.snapshot.admission_fenced)

        self.scheduler.set_admission_fence(False)
        admitted = self.admit(self.peer_a)
        self.assertEqual(self.scheduler.snapshot.queued, (admitted,))

    def test_recovered_fifo_head_cannot_prepare_while_fenced(self) -> None:
        queued = Lease(
            lease_id="queued",
            sequence=1,
            peer=self.peer_a,
            label="queued",
            inherited_lease_id=None,
            enqueued_at=1.0,
            state=LeaseState.QUEUED,
        )
        scheduler = LeaseScheduler()
        scheduler.restore(
            active=None,
            preparing=None,
            queued=(queued,),
            next_sequence=2,
            admission_fenced=True,
        )

        self.assertIsNone(scheduler.begin_preparing())
        scheduler.set_admission_fence(False)
        self.assertEqual(scheduler.begin_preparing().lease_id, queued.lease_id)

    def test_duplicate_lease_identity_fails_loud(self) -> None:
        scheduler = LeaseScheduler(lease_id_factory=lambda: "same")
        scheduler.admit(
            peer=self.peer_a,
            label="first",
            inherited_lease_id=None,
            now=1.0,
        )
        with self.assertRaises(BenchmarkLockError) as raised:
            scheduler.admit(
                peer=self.peer_b,
                label="second",
                inherited_lease_id=None,
                now=2.0,
            )
        self.assertEqual(
            raised.exception.code,
            "invalid_lease_identity",
        )

    def test_terminal_lease_identities_are_pruned_for_bounded_reuse(self) -> None:
        scheduler = LeaseScheduler(lease_id_factory=lambda: "same")

        canceled = scheduler.admit(
            peer=self.peer_a,
            label="canceled",
            inherited_lease_id=None,
            now=1.0,
        )
        self.assertEqual(scheduler.cancel(canceled.lease_id), canceled)

        failed = scheduler.admit(
            peer=self.peer_a,
            label="failed",
            inherited_lease_id=None,
            now=2.0,
        )
        scheduler.begin_preparing()
        self.assertEqual(scheduler.fail_preparing(failed.lease_id).lease_id, "same")

        completed = scheduler.admit(
            peer=self.peer_a,
            label="completed",
            inherited_lease_id=None,
            now=3.0,
        )
        scheduler.begin_preparing()
        scheduler.activate(completed.lease_id, now=4.0)
        self.assertEqual(
            scheduler.complete_active(completed.lease_id).lease_id,
            "same",
        )

        reused = scheduler.admit(
            peer=self.peer_a,
            label="reused",
            inherited_lease_id=None,
            now=5.0,
        )
        self.assertEqual(reused.lease_id, "same")
        self.assertEqual(scheduler.snapshot.queued, (reused,))

    def test_admission_bound_is_an_explicit_scheduler_invariant(self) -> None:
        scheduler = LeaseScheduler(
            lease_id_factory=LeaseIds(),
            maximum_tickets=2,
        )
        for peer in (self.peer_a, self.peer_b):
            scheduler.admit(
                peer=peer,
                label=f"pid-{peer.pid}",
                inherited_lease_id=None,
                now=1.0,
            )
        with self.assertRaises(BenchmarkLockError) as raised:
            scheduler.admit(
                peer=self.peer_c,
                label="third",
                inherited_lease_id=None,
                now=1.0,
            )
        self.assertEqual(raised.exception.code, "benchmark_queue_full")

    def test_restore_rejects_non_fifo_or_conflicting_state(self) -> None:
        active = Lease(
            lease_id="active",
            sequence=2,
            peer=self.peer_a,
            label="active",
            inherited_lease_id=None,
            enqueued_at=1.0,
            state=LeaseState.ACTIVE,
            started_at=2.0,
        )
        queued = Lease(
            lease_id="queued",
            sequence=1,
            peer=self.peer_b,
            label="queued",
            inherited_lease_id=None,
            enqueued_at=1.0,
            state=LeaseState.QUEUED,
        )
        scheduler = LeaseScheduler()
        with self.assertRaises(BenchmarkLockError) as raised:
            scheduler.restore(
                active=active,
                preparing=None,
                queued=(queued,),
                next_sequence=3,
            )
        self.assertEqual(
            raised.exception.code,
            "invalid_scheduler_recovery",
        )
        self.assertIsNone(scheduler.snapshot.active)
        self.assertIsNone(scheduler.snapshot.preparing)
        self.assertEqual(scheduler.snapshot.queued, ())
        scheduler.admit(
            peer=self.peer_a,
            label="recovered",
            inherited_lease_id=None,
            now=3.0,
        )


if __name__ == "__main__":
    unittest.main()
