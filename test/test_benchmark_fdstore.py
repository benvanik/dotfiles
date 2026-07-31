from __future__ import annotations

import dataclasses
import errno
import fcntl
import itertools
import json
import os
import socket
import unittest

from benchmark_lock.errors import BenchmarkLockError
from benchmark_lock.fdstore import (
    ACTIVE_RECORD_TRANSITION_HEADROOM,
    CONTROL_DESCRIPTOR_NAME,
    FILE_DESCRIPTOR_STORE_MAX,
    QUEUED_TICKET_DESCRIPTOR_COUNT,
    ActivationState,
    DescriptorRole,
    NamedDescriptor,
    ReapReason,
    SystemdLeasePersistence,
    SystemdNotifier,
    create_ticket_record,
    recover_activation,
    store_active_ticket,
    store_queued_ticket,
    ticket_descriptor_name,
)
from benchmark_lock.protocol import canonical_json_bytes
from benchmark_lock.scheduler import (
    MAX_TICKETS,
    Lease,
    LeaseState,
    PeerIdentity,
)


LEASE_A = "a" * 32
LEASE_B = "b" * 32
LEASE_C = "c" * 32
LEASE_D = "d" * 32

REQUIRED_SEALS = (
    fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
)
_LISTENER_IDENTITIES = itertools.count()


def _close_descriptor(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError as error:
        if error.errno != errno.EBADF:
            raise


def _listening_socket() -> socket.socket:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    listener.bind(
        f"\0benchmark-fdstore-test-{os.getpid()}-{next(_LISTENER_IDENTITIES)}"
    )
    listener.listen()
    return listener


def _lease(
    lease_id: str,
    *,
    sequence: int,
    state: LeaseState = LeaseState.QUEUED,
) -> Lease:
    return Lease(
        lease_id=lease_id,
        sequence=sequence,
        peer=PeerIdentity(
            pid=os.getpid(),
            uid=os.getuid(),
            gid=os.getgid(),
        ),
        label=f"ticket {sequence}",
        inherited_lease_id=None,
        enqueued_at=float(sequence),
        state=state,
        started_at=(float(sequence + 10) if state is LeaseState.ACTIVE else None),
    )


class FakeNotifier:
    def __init__(
        self,
        activation: tuple[NamedDescriptor, ...] = (),
        *,
        fail_store_number: int | None = None,
        fail_barrier_number: int | None = None,
    ) -> None:
        self.activation = activation
        self.fail_store_number = fail_store_number
        self.fail_barrier_number = fail_barrier_number
        self.store_attempts = 0
        self.barrier_attempts = 0
        self.stored: list[tuple[str, int]] = []
        self.removed: list[str] = []
        self.operations: list[tuple[str, str | None]] = []
        self.record_snapshots: dict[str, tuple[str, int, bytes]] = {}

    def activation_descriptors(self) -> tuple[NamedDescriptor, ...]:
        return self.activation

    def store_descriptor(self, name: str, descriptor: int) -> None:
        self.store_attempts += 1
        self.operations.append(("store", name))
        if self.store_attempts == self.fail_store_number:
            raise RuntimeError("injected store failure")
        self.stored.append((name, descriptor))
        if name.endswith("-record"):
            metadata = os.fstat(descriptor)
            self.record_snapshots[name] = (
                os.readlink(f"/proc/self/fd/{descriptor}"),
                fcntl.fcntl(descriptor, fcntl.F_GET_SEALS),
                os.pread(descriptor, metadata.st_size, 0),
            )

    def remove_descriptor(self, name: str) -> None:
        self.operations.append(("remove", name))
        self.removed.append(name)

    def barrier(self) -> None:
        self.barrier_attempts += 1
        self.operations.append(("barrier", None))
        if self.barrier_attempts == self.fail_barrier_number:
            raise RuntimeError("injected barrier failure")


class FakeSystemdDaemon:
    def __init__(self, descriptors: dict[int, str]) -> None:
        self.descriptors = descriptors
        self.listen_arguments: list[bool] = []
        self.notifications: list[tuple[str, bool, tuple[int, ...] | None]] = []
        self.deliver = True

    def listen_fds_with_names(
        self,
        unset_environment: bool,
    ) -> dict[int, str]:
        self.listen_arguments.append(unset_environment)
        return self.descriptors

    def notify(
        self,
        fields: str,
        *,
        unset_environment: bool,
        fds: list[int] | None = None,
    ) -> bool:
        self.notifications.append(
            (
                fields,
                unset_environment,
                None if fds is None else tuple(fds),
            )
        )
        return self.deliver


class BenchmarkFdStoreTest(unittest.TestCase):
    def test_capacity_is_derived_from_the_shared_scheduler_bound(
        self,
    ) -> None:
        self.assertEqual(QUEUED_TICKET_DESCRIPTOR_COUNT, 3)
        self.assertEqual(ACTIVE_RECORD_TRANSITION_HEADROOM, 1)
        self.assertEqual(
            FILE_DESCRIPTOR_STORE_MAX,
            MAX_TICKETS * 3 + 1,
        )

    def test_systemd_wrapper_uses_names_one_fd_messages_and_barrier(
        self,
    ) -> None:
        read_descriptor, write_descriptor = os.pipe()
        barrier_calls: list[tuple[int, int]] = []
        daemon = FakeSystemdDaemon(
            {
                9: CONTROL_DESCRIPTOR_NAME,
                11: f"ticket.{LEASE_A}.owner",
            }
        )

        def barrier(unset_environment: int, timeout: int) -> int:
            barrier_calls.append((unset_environment, timeout))
            return 1

        try:
            notifier = SystemdNotifier(
                daemon_module=daemon,
                barrier_function=barrier,
            )
            self.assertEqual(
                notifier.activation_descriptors(),
                (
                    NamedDescriptor(9, CONTROL_DESCRIPTOR_NAME),
                    NamedDescriptor(11, f"ticket.{LEASE_A}.owner"),
                ),
            )
            notifier.store_descriptor(
                f"ticket.{LEASE_A}.owner",
                read_descriptor,
            )
            notifier.remove_descriptor(f"ticket.{LEASE_A}.owner")
            notifier.barrier()
        finally:
            os.close(read_descriptor)
            os.close(write_descriptor)

        self.assertEqual(daemon.listen_arguments, [True])
        self.assertEqual(
            daemon.notifications,
            [
                (
                    f"FDSTORE=1\nFDNAME=ticket.{LEASE_A}.owner",
                    False,
                    (read_descriptor,),
                ),
                (
                    f"FDSTOREREMOVE=1\nFDNAME=ticket.{LEASE_A}.owner",
                    False,
                    None,
                ),
            ],
        )
        self.assertEqual(
            barrier_calls,
            [(0, (1 << 64) - 1)],
        )

    def test_systemd_wrapper_fails_when_notification_or_barrier_is_lost(
        self,
    ) -> None:
        read_descriptor, write_descriptor = os.pipe()
        daemon = FakeSystemdDaemon({})
        daemon.deliver = False
        notifier = SystemdNotifier(
            daemon_module=daemon,
            barrier_function=lambda _unset, _timeout: -errno.ETIMEDOUT,
        )
        try:
            with self.assertRaises(BenchmarkLockError) as caught:
                notifier.store_descriptor("ticket", read_descriptor)
            self.assertEqual(
                caught.exception.code,
                "benchmark_fdstore_unavailable",
            )
            daemon.deliver = True
            with self.assertRaises(BenchmarkLockError) as caught:
                notifier.barrier()
            self.assertEqual(
                caught.exception.code,
                "benchmark_fdstore_unavailable",
            )
        finally:
            os.close(read_descriptor)
            os.close(write_descriptor)

    def test_queued_store_commits_a_sealed_canonical_record_last(
        self,
    ) -> None:
        owner = os.pidfd_open(os.getpid())
        broker, client = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET,
        )
        notifier = FakeNotifier()
        lease = _lease(LEASE_A, sequence=7)
        try:
            names = store_queued_ticket(
                notifier,
                lease=lease,
                owner_descriptor=owner,
                channel_descriptor=broker.fileno(),
            )
        finally:
            os.close(owner)
            broker.close()
            client.close()

        expected_names = (
            f"ticket.{LEASE_A}.owner",
            f"ticket.{LEASE_A}.channel",
            f"ticket.{LEASE_A}.queued-record",
        )
        self.assertEqual(names, expected_names)
        self.assertEqual(
            tuple(name for name, _descriptor in notifier.stored),
            expected_names,
        )
        self.assertEqual(notifier.barrier_attempts, 1)
        target, seals, payload = notifier.record_snapshots[expected_names[-1]]
        self.assertEqual(
            target,
            f"/memfd:benchmark-ticket-{LEASE_A}-queued (deleted)",
        )
        self.assertEqual(seals & REQUIRED_SEALS, REQUIRED_SEALS)
        document = json.loads(payload)
        self.assertEqual(payload, canonical_json_bytes(document))
        self.assertEqual(document["state"], "queued")
        self.assertEqual(document["lease_id"], LEASE_A)
        self.assertEqual(document["sequence"], 7)

    def test_queued_store_binds_pidfd_and_channel_to_recorded_peer(
        self,
    ) -> None:
        owner = os.pidfd_open(os.getpid())
        broker, client = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET,
        )
        lease = _lease(LEASE_A, sequence=1)
        wrong_pid = dataclasses.replace(
            lease,
            peer=dataclasses.replace(lease.peer, pid=lease.peer.pid + 1),
        )
        wrong_uid = dataclasses.replace(
            lease,
            peer=dataclasses.replace(lease.peer, uid=lease.peer.uid + 1),
        )
        try:
            with self.assertRaises(BenchmarkLockError) as pid_error:
                store_queued_ticket(
                    FakeNotifier(),
                    lease=wrong_pid,
                    owner_descriptor=owner,
                    channel_descriptor=broker.fileno(),
                )
            self.assertEqual(
                pid_error.exception.code,
                "invalid_fdstore_activation",
            )
            with self.assertRaises(BenchmarkLockError) as channel_error:
                store_queued_ticket(
                    FakeNotifier(),
                    lease=wrong_uid,
                    owner_descriptor=owner,
                    channel_descriptor=broker.fileno(),
                )
            self.assertEqual(
                channel_error.exception.code,
                "invalid_fdstore_activation",
            )
        finally:
            os.close(owner)
            broker.close()
            client.close()

    def test_preparing_store_recovers_conservatively_as_queued(
        self,
    ) -> None:
        preparing = _lease(
            LEASE_A,
            sequence=3,
            state=LeaseState.PREPARING,
        )
        descriptor = create_ticket_record(
            preparing,
            state=LeaseState.QUEUED,
        )
        try:
            payload = os.pread(
                descriptor,
                os.fstat(descriptor).st_size,
                0,
            )
        finally:
            os.close(descriptor)
        document = json.loads(payload)
        self.assertEqual(document["state"], "queued")
        self.assertIsNone(document["started_at"])

    def test_recovery_is_order_independent_and_active_supersedes_queued(
        self,
    ) -> None:
        listener = _listening_socket()
        queued_broker, queued_client = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET,
        )
        descriptors: list[int] = []
        try:
            control = os.dup(listener.fileno())
            active_owner = os.pidfd_open(os.getpid())
            queued_owner = os.pidfd_open(os.getpid())
            queued_channel = os.dup(queued_broker.fileno())
            active_lease = _lease(
                LEASE_A,
                sequence=1,
                state=LeaseState.ACTIVE,
            )
            active_queued_record = create_ticket_record(
                _lease(LEASE_A, sequence=1),
                state=LeaseState.QUEUED,
            )
            active_record = create_ticket_record(
                active_lease,
                state=LeaseState.ACTIVE,
            )
            queued_lease = _lease(LEASE_B, sequence=2)
            queued_record = create_ticket_record(
                queued_lease,
                state=LeaseState.QUEUED,
            )
            descriptors.extend(
                (
                    control,
                    active_owner,
                    queued_owner,
                    queued_channel,
                    active_queued_record,
                    active_record,
                    queued_record,
                )
            )
            notifier = FakeNotifier(
                (
                    NamedDescriptor(
                        active_record,
                        f"ticket.{LEASE_A}.active-record",
                    ),
                    NamedDescriptor(
                        queued_channel,
                        f"ticket.{LEASE_B}.channel",
                    ),
                    NamedDescriptor(control, CONTROL_DESCRIPTOR_NAME),
                    NamedDescriptor(
                        active_queued_record,
                        f"ticket.{LEASE_A}.queued-record",
                    ),
                    NamedDescriptor(
                        queued_record,
                        f"ticket.{LEASE_B}.queued-record",
                    ),
                    NamedDescriptor(
                        active_owner,
                        f"ticket.{LEASE_A}.owner",
                    ),
                    NamedDescriptor(
                        queued_owner,
                        f"ticket.{LEASE_B}.owner",
                    ),
                )
            )
            state = recover_activation(notifier)

            self.assertIsInstance(state, ActivationState)
            self.assertEqual(
                tuple(ticket.lease for ticket in state.tickets),
                (active_lease, queued_lease),
            )
            self.assertIsNone(state.tickets[0].channel_descriptor)
            self.assertEqual(
                state.tickets[1].channel_descriptor,
                queued_channel,
            )
            self.assertEqual(state.reaped, ())
            self.assertEqual(
                state.discarded_descriptor_names,
                (f"ticket.{LEASE_A}.queued-record",),
            )
            self.assertEqual(
                notifier.removed,
                [f"ticket.{LEASE_A}.queued-record"],
            )
        finally:
            for descriptor in descriptors:
                _close_descriptor(descriptor)
            listener.close()
            queued_broker.close()
            queued_client.close()

    def test_active_recovery_discards_a_closed_optional_channel(
        self,
    ) -> None:
        listener = _listening_socket()
        broker, client = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET,
        )
        descriptors: list[int] = []
        try:
            control = os.dup(listener.fileno())
            owner = os.pidfd_open(os.getpid())
            channel = os.dup(broker.fileno())
            active = _lease(
                LEASE_A,
                sequence=1,
                state=LeaseState.ACTIVE,
            )
            record = create_ticket_record(
                active,
                state=LeaseState.ACTIVE,
            )
            descriptors.extend((control, owner, channel, record))
            client.close()
            notifier = FakeNotifier(
                (
                    NamedDescriptor(
                        channel,
                        f"ticket.{LEASE_A}.channel",
                    ),
                    NamedDescriptor(
                        record,
                        f"ticket.{LEASE_A}.active-record",
                    ),
                    NamedDescriptor(control, CONTROL_DESCRIPTOR_NAME),
                    NamedDescriptor(
                        owner,
                        f"ticket.{LEASE_A}.owner",
                    ),
                )
            )
            state = recover_activation(notifier)
            self.assertEqual(len(state.tickets), 1)
            self.assertEqual(state.tickets[0].lease, active)
            self.assertIsNone(state.tickets[0].channel_descriptor)
            self.assertEqual(
                state.discarded_descriptor_names,
                (f"ticket.{LEASE_A}.channel",),
            )
        finally:
            for descriptor in descriptors:
                _close_descriptor(descriptor)
            listener.close()
            broker.close()
            client.close()

    def test_recovery_rejects_owner_or_channel_from_another_peer(
        self,
    ) -> None:
        for peer_field in ("pid", "uid"):
            with self.subTest(peer_field=peer_field):
                listener = _listening_socket()
                broker, client = socket.socketpair(
                    socket.AF_UNIX,
                    socket.SOCK_SEQPACKET,
                )
                descriptors: list[int] = []
                try:
                    control = os.dup(listener.fileno())
                    owner = os.pidfd_open(os.getpid())
                    channel = os.dup(broker.fileno())
                    lease = _lease(LEASE_A, sequence=1)
                    peer_value = getattr(lease.peer, peer_field)
                    lease = dataclasses.replace(
                        lease,
                        peer=dataclasses.replace(
                            lease.peer,
                            **{peer_field: peer_value + 1},
                        ),
                    )
                    record = create_ticket_record(
                        lease,
                        state=LeaseState.QUEUED,
                    )
                    descriptors.extend((control, owner, channel, record))
                    notifier = FakeNotifier(
                        (
                            NamedDescriptor(control, CONTROL_DESCRIPTOR_NAME),
                            NamedDescriptor(
                                owner,
                                f"ticket.{LEASE_A}.owner",
                            ),
                            NamedDescriptor(
                                channel,
                                f"ticket.{LEASE_A}.channel",
                            ),
                            NamedDescriptor(
                                record,
                                f"ticket.{LEASE_A}.queued-record",
                            ),
                        )
                    )

                    with self.assertRaises(BenchmarkLockError) as caught:
                        recover_activation(notifier)

                    self.assertEqual(
                        caught.exception.code,
                        "invalid_fdstore_activation",
                    )
                finally:
                    for descriptor in descriptors:
                        _close_descriptor(descriptor)
                    listener.close()
                    broker.close()
                    client.close()

    def test_recovery_classifies_incomplete_and_exited_closures(
        self,
    ) -> None:
        listener = _listening_socket()
        channel_pairs = [
            socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            for _index in range(3)
        ]
        descriptors: list[int] = []
        child_pid = os.fork()
        if child_pid == 0:
            os._exit(0)
        dead_owner = os.pidfd_open(child_pid)
        os.waitpid(child_pid, 0)
        try:
            control = os.dup(listener.fileno())
            uncommitted_owner = os.pidfd_open(os.getpid())
            uncommitted_channel = os.dup(channel_pairs[0][0].fileno())
            owner_missing_channel = os.dup(channel_pairs[1][0].fileno())
            owner_missing_record = create_ticket_record(
                _lease(LEASE_B, sequence=2),
                state=LeaseState.QUEUED,
            )
            channel_missing_owner = os.pidfd_open(os.getpid())
            channel_missing_record = create_ticket_record(
                _lease(LEASE_C, sequence=3),
                state=LeaseState.QUEUED,
            )
            dead_channel = os.dup(channel_pairs[2][0].fileno())
            dead_record = create_ticket_record(
                _lease(LEASE_D, sequence=4),
                state=LeaseState.QUEUED,
            )
            descriptors.extend(
                (
                    control,
                    uncommitted_owner,
                    uncommitted_channel,
                    owner_missing_channel,
                    owner_missing_record,
                    channel_missing_owner,
                    channel_missing_record,
                    dead_owner,
                    dead_channel,
                    dead_record,
                )
            )
            notifier = FakeNotifier(
                (
                    NamedDescriptor(
                        dead_record,
                        f"ticket.{LEASE_D}.queued-record",
                    ),
                    NamedDescriptor(
                        channel_missing_record,
                        f"ticket.{LEASE_C}.queued-record",
                    ),
                    NamedDescriptor(
                        uncommitted_channel,
                        f"ticket.{LEASE_A}.channel",
                    ),
                    NamedDescriptor(
                        owner_missing_record,
                        f"ticket.{LEASE_B}.queued-record",
                    ),
                    NamedDescriptor(control, CONTROL_DESCRIPTOR_NAME),
                    NamedDescriptor(
                        dead_owner,
                        f"ticket.{LEASE_D}.owner",
                    ),
                    NamedDescriptor(
                        uncommitted_owner,
                        f"ticket.{LEASE_A}.owner",
                    ),
                    NamedDescriptor(
                        owner_missing_channel,
                        f"ticket.{LEASE_B}.channel",
                    ),
                    NamedDescriptor(
                        channel_missing_owner,
                        f"ticket.{LEASE_C}.owner",
                    ),
                    NamedDescriptor(
                        dead_channel,
                        f"ticket.{LEASE_D}.channel",
                    ),
                )
            )
            state = recover_activation(notifier)
            self.assertEqual(state.tickets, ())
            self.assertEqual(
                tuple((ticket.lease_id, ticket.reason) for ticket in state.reaped),
                (
                    (LEASE_A, ReapReason.UNCOMMITTED),
                    (LEASE_B, ReapReason.OWNER_MISSING),
                    (LEASE_C, ReapReason.CHANNEL_MISSING),
                    (LEASE_D, ReapReason.OWNER_EXITED),
                ),
            )
            self.assertEqual(notifier.barrier_attempts, 4)
        finally:
            for descriptor in descriptors:
                _close_descriptor(descriptor)
            listener.close()
            for broker, client in channel_pairs:
                broker.close()
                client.close()

    def test_recovery_rejects_unknown_names_duplicate_roles_and_types(
        self,
    ) -> None:
        listener = _listening_socket()
        read_descriptor, write_descriptor = os.pipe()
        control = os.dup(listener.fileno())
        try:
            with self.assertRaises(BenchmarkLockError) as caught:
                recover_activation(
                    FakeNotifier(
                        (
                            NamedDescriptor(
                                control,
                                CONTROL_DESCRIPTOR_NAME,
                            ),
                            NamedDescriptor(
                                read_descriptor,
                                "unknown",
                            ),
                        )
                    )
                )
            self.assertEqual(
                caught.exception.code,
                "invalid_fdstore_activation",
            )
        finally:
            os.close(control)
            os.close(read_descriptor)
            os.close(write_descriptor)
            listener.close()

        listener = _listening_socket()
        control = os.dup(listener.fileno())
        first_owner = os.pidfd_open(os.getpid())
        second_owner = os.pidfd_open(os.getpid())
        try:
            with self.assertRaises(BenchmarkLockError) as caught:
                recover_activation(
                    FakeNotifier(
                        (
                            NamedDescriptor(
                                second_owner,
                                f"ticket.{LEASE_A}.owner",
                            ),
                            NamedDescriptor(
                                control,
                                CONTROL_DESCRIPTOR_NAME,
                            ),
                            NamedDescriptor(
                                first_owner,
                                f"ticket.{LEASE_A}.owner",
                            ),
                        )
                    )
                )
            self.assertEqual(
                caught.exception.code,
                "invalid_fdstore_activation",
            )
        finally:
            os.close(control)
            os.close(first_owner)
            os.close(second_owner)
            listener.close()

        listener = _listening_socket()
        control = os.dup(listener.fileno())
        broker, client = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET,
        )
        try:
            with self.assertRaises(BenchmarkLockError) as caught:
                recover_activation(
                    FakeNotifier(
                        (
                            NamedDescriptor(
                                control,
                                CONTROL_DESCRIPTOR_NAME,
                            ),
                            NamedDescriptor(
                                broker.fileno(),
                                f"ticket.{LEASE_A}.owner",
                            ),
                        )
                    )
                )
            self.assertEqual(
                caught.exception.code,
                "invalid_fdstore_activation",
            )
        finally:
            os.close(control)
            broker.close()
            client.close()
            listener.close()

    def test_ticket_memfd_is_write_sealed(self) -> None:
        descriptor = create_ticket_record(
            _lease(LEASE_A, sequence=1),
            state=LeaseState.QUEUED,
        )
        try:
            self.assertEqual(
                fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & REQUIRED_SEALS,
                REQUIRED_SEALS,
            )
            with self.assertRaises(OSError) as caught:
                os.pwrite(descriptor, b"x", 0)
            self.assertEqual(caught.exception.errno, errno.EPERM)
        finally:
            os.close(descriptor)

    def test_partial_queued_store_is_removed_before_error_returns(
        self,
    ) -> None:
        owner = os.pidfd_open(os.getpid())
        broker, client = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET,
        )
        notifier = FakeNotifier(fail_store_number=3)
        try:
            with self.assertRaises(BenchmarkLockError) as caught:
                store_queued_ticket(
                    notifier,
                    lease=_lease(LEASE_A, sequence=1),
                    owner_descriptor=owner,
                    channel_descriptor=broker.fileno(),
                )
            self.assertEqual(
                caught.exception.code,
                "benchmark_fdstore_unavailable",
            )
        finally:
            os.close(owner)
            broker.close()
            client.close()
        self.assertEqual(
            notifier.removed,
            [
                f"ticket.{LEASE_A}.owner",
                f"ticket.{LEASE_A}.channel",
            ],
        )
        self.assertEqual(notifier.barrier_attempts, 1)

    def test_committed_record_is_not_rolled_back_after_ambiguous_barrier(
        self,
    ) -> None:
        owner = os.pidfd_open(os.getpid())
        broker, client = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET,
        )
        notifier = FakeNotifier(fail_barrier_number=1)
        try:
            with self.assertRaises(BenchmarkLockError) as caught:
                store_queued_ticket(
                    notifier,
                    lease=_lease(LEASE_A, sequence=1),
                    owner_descriptor=owner,
                    channel_descriptor=broker.fileno(),
                )
            self.assertEqual(
                caught.exception.code,
                "benchmark_fdstore_unavailable",
            )
        finally:
            os.close(owner)
            broker.close()
            client.close()
        self.assertEqual(len(notifier.stored), 3)
        self.assertEqual(notifier.removed, [])

    def test_active_record_commits_before_queued_record_is_removed(
        self,
    ) -> None:
        notifier = FakeNotifier()
        active = _lease(
            LEASE_A,
            sequence=1,
            state=LeaseState.ACTIVE,
        )
        active_name = store_active_ticket(notifier, lease=active)
        self.assertEqual(
            active_name,
            f"ticket.{LEASE_A}.active-record",
        )
        self.assertEqual(
            notifier.operations,
            [
                ("store", f"ticket.{LEASE_A}.active-record"),
                ("barrier", None),
                ("remove", f"ticket.{LEASE_A}.queued-record"),
                ("barrier", None),
            ],
        )

    def test_persistence_release_survives_a_committed_active_failure(
        self,
    ) -> None:
        owner = os.pidfd_open(os.getpid())
        broker, client = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET,
        )
        notifier = FakeNotifier(fail_barrier_number=3)
        persistence = SystemdLeasePersistence(notifier)
        try:
            persistence.retain_queued(
                _lease(LEASE_A, sequence=1),
                owner_descriptor=owner,
                channel_descriptor=broker.fileno(),
            )
            with self.assertRaises(BenchmarkLockError) as caught:
                persistence.retain_active(
                    _lease(
                        LEASE_A,
                        sequence=1,
                        state=LeaseState.ACTIVE,
                    )
                )
            self.assertEqual(
                caught.exception.code,
                "benchmark_fdstore_unavailable",
            )

            # No adapter-side transition state may prevent terminal cleanup:
            # the broker can always remove every possible closure member.
            persistence.release(LEASE_A)
        finally:
            os.close(owner)
            broker.close()
            client.close()

        active_store = (
            "store",
            f"ticket.{LEASE_A}.active-record",
        )
        queued_remove = (
            "remove",
            f"ticket.{LEASE_A}.queued-record",
        )
        self.assertLess(
            notifier.operations.index(active_store),
            notifier.operations.index(queued_remove),
        )
        self.assertEqual(
            notifier.removed[-4:],
            [
                f"ticket.{LEASE_A}.owner",
                f"ticket.{LEASE_A}.channel",
                f"ticket.{LEASE_A}.queued-record",
                f"ticket.{LEASE_A}.active-record",
            ],
        )
        self.assertEqual(notifier.barrier_attempts, 4)

    def test_ticket_names_use_the_wire_protocol_identity(self) -> None:
        self.assertEqual(
            ticket_descriptor_name(LEASE_A, DescriptorRole.OWNER),
            f"ticket.{LEASE_A}.owner",
        )
        with self.assertRaises(BenchmarkLockError) as caught:
            ticket_descriptor_name("not-a-wire-id", DescriptorRole.OWNER)
        self.assertEqual(
            caught.exception.code,
            "invalid_fdstore_ticket",
        )


if __name__ == "__main__":
    unittest.main()
