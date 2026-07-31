from __future__ import annotations

import errno
import io
import os
import pathlib
import signal
import socket
import tempfile
import threading
import unittest
from unittest import mock

from benchmark_lock import daemon
from benchmark_lock.configuration import (
    CONFIG_PATH,
    MAX_CONFIG_BYTES,
    canonical_policy_configuration,
)
from benchmark_lock.broker import BenchmarkBroker, wait_for_pidfd
from benchmark_lock.errors import BenchmarkLockError
from benchmark_lock.fdstore import (
    CONTROL_DESCRIPTOR_NAME,
    ActivationState,
    DescriptorRole,
    NamedDescriptor,
    ReapedTicket,
    ReapReason,
    RecoveredTicket,
    create_ticket_record,
    ticket_descriptor_name,
)
from benchmark_lock.policy import (
    AmdGpuIdentity,
    FixedHostPolicyConfig,
)
from benchmark_lock.protocol import GrantedEvent, receive_event
from benchmark_lock.scheduler import (
    Lease,
    LeaseScheduler,
    LeaseState,
    PeerIdentity,
)


LEASE_A = "a" * 32
LEASE_B = "b" * 32
LEASE_C = "c" * 32


def _identity() -> AmdGpuIdentity:
    return AmdGpuIdentity(
        bdf="0000:23:00.0",
        vendor="0x1002",
        device="0x744c",
        subsystem_vendor="0x1002",
        subsystem_device="0x0e3b",
        revision="0xc8",
        unique_id="1",
    )


def _configuration() -> FixedHostPolicyConfig:
    return FixedHostPolicyConfig((_identity(),))


def _lease(
    lease_id: str,
    *,
    sequence: int,
    state: LeaseState,
    pid: int | None = None,
) -> Lease:
    return Lease(
        lease_id=lease_id,
        sequence=sequence,
        peer=PeerIdentity(
            pid=os.getpid() if pid is None else pid,
            uid=os.getuid(),
            gid=os.getgid(),
        ),
        label=f"lease {sequence}",
        inherited_lease_id=None,
        enqueued_at=float(sequence),
        state=state,
        started_at=(float(sequence + 10) if state is LeaseState.ACTIVE else None),
    )


def _is_closed(descriptor: int) -> bool:
    try:
        os.fstat(descriptor)
    except OSError as error:
        if error.errno == errno.EBADF:
            return True
        raise
    return False


class FakeNotifier:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.ready_count = 0
        self.stopping_count = 0

    def activation_descriptors(self) -> tuple[NamedDescriptor, ...]:
        raise AssertionError("recover_activation is patched by this focused test")

    def store_descriptor(self, _name: str, _descriptor: int) -> None:
        raise AssertionError("fake broker must not persist a new ticket")

    def remove_descriptor(self, _name: str) -> None:
        raise AssertionError("fake broker must not release a ticket")

    def barrier(self) -> None:
        raise AssertionError("fake broker must not synchronize persistence")

    def ready(self) -> None:
        self.events.append("ready")
        self.ready_count += 1

    def stopping(self) -> None:
        self.events.append("stopping")
        self.stopping_count += 1


class RecordingNotifier(FakeNotifier):
    def __init__(
        self,
        events: list[str],
        activation: tuple[NamedDescriptor, ...] = (),
    ) -> None:
        super().__init__(events)
        self.activation = activation
        self.stored: list[str] = []
        self.removed: list[str] = []
        self.barrier_count = 0

    def activation_descriptors(self) -> tuple[NamedDescriptor, ...]:
        self.events.append("activation")
        return self.activation

    def store_descriptor(self, name: str, _descriptor: int) -> None:
        self.stored.append(name)

    def remove_descriptor(self, name: str) -> None:
        self.removed.append(name)

    def barrier(self) -> None:
        self.barrier_count += 1


class FakePolicy:
    identity = "fake-policy"
    state = "idle"

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def recover(self) -> None:
        self.events.append("policy-recover")

    def enter(self) -> None:
        self.events.append("policy-enter")
        self.state = "held"

    def preflight(self) -> None:
        self.events.append("policy-preflight")

    def verify(self) -> None:
        if self.state != "held":
            raise AssertionError("policy verification requires a held policy")
        self.events.append("policy-verify")

    def leave(self) -> None:
        self.events.append("policy-leave")
        self.state = "idle"


class FakeBroker:
    def __init__(
        self,
        events: list[str],
        captured: dict[str, object],
        run_action=None,
        **arguments,
    ) -> None:
        self.events = events
        self.captured = captured
        self.run_action = run_action
        self.clean_stop_requests = 0
        self.captured.update(arguments)
        self.captured["broker"] = self
        self.events.append("broker-init")

    def run(self, *, ready) -> None:
        ready()
        self.events.append("broker-run")
        if self.run_action is not None:
            self.run_action()

    def request_clean_stop(self) -> None:
        self.clean_stop_requests += 1
        self.events.append("clean-stop")


class ActivationFixture:
    def __init__(self) -> None:
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.listener.bind(f"\0benchmark-daemon-test-{os.getpid()}-{id(self)}")
        self.listener.listen()
        self.channel_pairs: list[tuple[socket.socket, socket.socket]] = []
        self.descriptors: list[int] = []
        self.named_descriptors: tuple[NamedDescriptor, ...] = ()

    def build(
        self,
        leases: tuple[Lease, ...],
        *,
        owner_pids: tuple[int, ...] | None = None,
    ) -> ActivationState:
        if owner_pids is None:
            owner_pids = tuple(os.getpid() for _lease_value in leases)
        if len(owner_pids) != len(leases):
            raise ValueError("every daemon test lease needs one owner PID")
        control = os.dup(self.listener.fileno())
        self.descriptors.append(control)
        named_descriptors = [
            NamedDescriptor(control, CONTROL_DESCRIPTOR_NAME),
        ]
        tickets: list[RecoveredTicket] = []
        for lease, owner_pid in zip(leases, owner_pids, strict=True):
            owner = os.pidfd_open(owner_pid)
            channel: int | None = None
            if lease.state is LeaseState.QUEUED:
                broker_channel, client_channel = socket.socketpair(
                    socket.AF_UNIX,
                    socket.SOCK_SEQPACKET,
                )
                channel = os.dup(broker_channel.fileno())
                self.channel_pairs.append((broker_channel, client_channel))
                self.descriptors.append(channel)
                named_descriptors.append(
                    NamedDescriptor(
                        channel,
                        ticket_descriptor_name(
                            lease.lease_id,
                            DescriptorRole.CHANNEL,
                        ),
                    )
                )
            record = create_ticket_record(
                lease,
                state=lease.state,
            )
            self.descriptors.extend((owner, record))
            named_descriptors.extend(
                (
                    NamedDescriptor(
                        owner,
                        ticket_descriptor_name(
                            lease.lease_id,
                            DescriptorRole.OWNER,
                        ),
                    ),
                    NamedDescriptor(
                        record,
                        ticket_descriptor_name(
                            lease.lease_id,
                            (
                                DescriptorRole.ACTIVE_RECORD
                                if lease.state is LeaseState.ACTIVE
                                else DescriptorRole.QUEUED_RECORD
                            ),
                        ),
                    ),
                )
            )
            tickets.append(
                RecoveredTicket(
                    lease=lease,
                    owner_descriptor=owner,
                    channel_descriptor=channel,
                    record_descriptor=record,
                )
            )
        self.named_descriptors = tuple(named_descriptors)
        return ActivationState(
            control_descriptor=control,
            tickets=tuple(tickets),
            reaped=(),
            discarded_descriptor_names=(),
        )

    def close(self) -> None:
        for descriptor in self.descriptors:
            try:
                os.close(descriptor)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise
        for broker_channel, client_channel in self.channel_pairs:
            broker_channel.close()
            client_channel.close()
        self.listener.close()


class BenchmarkDaemonConfigurationTest(unittest.TestCase):
    def setUp(self) -> None:
        descriptor, raw_path = tempfile.mkstemp(prefix="benchmarkd-config-")
        os.close(descriptor)
        self.path = pathlib.Path(raw_path)
        self.path.write_bytes(b'{"schema":"benchmarkd.config.v1"}\n')
        self.path.chmod(0o600)

    def tearDown(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def test_secure_bounded_configuration_reaches_shared_parser(self) -> None:
        payloads: list[bytes] = []

        def parse(payload: bytes) -> FixedHostPolicyConfig:
            payloads.append(payload)
            return _configuration()

        result = daemon.read_policy_configuration(
            self.path,
            parser=parse,
            owner_uid=os.getuid(),
        )

        self.assertEqual(result, _configuration())
        self.assertEqual(payloads, [self.path.read_bytes()])

    def test_production_reader_uses_shared_schema_without_admin_import(self) -> None:
        configuration = _configuration()
        self.path.write_bytes(canonical_policy_configuration(configuration))

        with mock.patch.dict("sys.modules", {"benchmark_lock.admin": None}):
            result = daemon.read_policy_configuration(
                self.path,
                parser=daemon._default_configuration_parser,
                owner_uid=os.getuid(),
            )

        self.assertEqual(result, configuration)
        self.assertEqual(daemon.POLICY_CONFIGURATION_PATH, CONFIG_PATH)
        self.assertEqual(
            daemon.MAX_POLICY_CONFIGURATION_BYTES,
            MAX_CONFIG_BYTES,
        )

    def test_configuration_rejects_mode_owner_symlink_and_size(self) -> None:
        parse = mock.Mock(return_value=_configuration())

        self.path.chmod(0o640)
        with self.assertRaises(BenchmarkLockError):
            daemon.read_policy_configuration(
                self.path,
                parser=parse,
                owner_uid=os.getuid(),
            )
        self.path.chmod(0o600)

        with self.assertRaises(BenchmarkLockError):
            daemon.read_policy_configuration(
                self.path,
                parser=parse,
                owner_uid=os.getuid() + 1,
            )

        link = self.path.with_name(self.path.name + "-link")
        link.symlink_to(self.path)
        try:
            with self.assertRaises(BenchmarkLockError):
                daemon.read_policy_configuration(
                    link,
                    parser=parse,
                    owner_uid=os.getuid(),
                )
        finally:
            link.unlink()

        self.path.write_bytes(b"x" * (daemon.MAX_POLICY_CONFIGURATION_BYTES + 1))
        with self.assertRaises(BenchmarkLockError):
            daemon.read_policy_configuration(
                self.path,
                parser=parse,
                owner_uid=os.getuid(),
            )

        parse.assert_not_called()

    def test_configuration_parser_must_return_policy_type(self) -> None:
        with self.assertRaises(BenchmarkLockError) as caught:
            daemon.read_policy_configuration(
                self.path,
                parser=lambda _payload: object(),  # type: ignore[return-value]
                owner_uid=os.getuid(),
            )
        self.assertEqual(
            caught.exception.code,
            "invalid_benchmark_policy_configuration",
        )


class BenchmarkDaemonRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        descriptor, raw_path = tempfile.mkstemp(prefix="benchmarkd-config-")
        os.close(descriptor)
        self.config_path = pathlib.Path(raw_path)
        self.config_path.write_bytes(b"configuration\n")
        self.config_path.chmod(0o600)
        self.activation_fixture = ActivationFixture()

    def tearDown(self) -> None:
        self.activation_fixture.close()
        try:
            self.config_path.unlink()
        except FileNotFoundError:
            pass

    def _run(
        self,
        activation: ActivationState,
        *,
        events: list[str],
        captured: dict[str, object],
        manage_signals: bool = False,
        run_action=None,
        startup_action=None,
    ) -> FakeNotifier:
        notifier = FakeNotifier(events)

        def broker_factory(**arguments) -> FakeBroker:
            return FakeBroker(
                events,
                captured,
                run_action=run_action,
                **arguments,
            )

        def signal_owner(descriptor: int, signal_number: int) -> None:
            self.assertEqual(
                descriptor,
                next(
                    ticket.owner_descriptor
                    for ticket in activation.tickets
                    if ticket.lease.state is LeaseState.ACTIVE
                ),
            )
            self.assertEqual(signal_number, signal.SIGKILL)
            events.append("active-kill")

        def wait_owner(descriptor: int, timeout: float | None) -> bool:
            self.assertEqual(
                descriptor,
                next(
                    ticket.owner_descriptor
                    for ticket in activation.tickets
                    if ticket.lease.state is LeaseState.ACTIVE
                ),
            )
            self.assertIsNone(timeout)
            events.append("active-exit")
            return True

        def recover(observed) -> ActivationState:
            if observed is not notifier:
                self.fail("wrong notifier")
            events.append("activation")
            if startup_action is not None:
                startup_action()
            return activation

        with mock.patch.object(daemon, "recover_activation", side_effect=recover):
            daemon.run_daemon(
                notifier,
                configuration_path=self.config_path,
                configuration_parser=lambda _payload: _configuration(),
                policy_factory=lambda _configuration_value: FakePolicy(events),
                broker_factory=broker_factory,
                effective_uid=0,
                configuration_owner_uid=os.getuid(),
                signal_owner=signal_owner,
                wait_owner=wait_owner,
                report=lambda message: events.append(f"report:{message}"),
                manage_signals=manage_signals,
            )
        return notifier

    def test_active_is_killed_before_policy_recovery_then_adopted(self) -> None:
        events: list[str] = []
        captured: dict[str, object] = {}
        active = _lease(
            LEASE_A,
            sequence=4,
            state=LeaseState.ACTIVE,
        )
        queued_late = _lease(
            LEASE_C,
            sequence=9,
            state=LeaseState.QUEUED,
        )
        queued_early = _lease(
            LEASE_B,
            sequence=7,
            state=LeaseState.QUEUED,
        )
        activation = self.activation_fixture.build((active, queued_late, queued_early))
        records = tuple(ticket.record_descriptor for ticket in activation.tickets)

        notifier = self._run(
            activation,
            events=events,
            captured=captured,
        )

        self.assertEqual(
            events,
            [
                "activation",
                "active-kill",
                "active-exit",
                "policy-recover",
                "broker-init",
                "ready",
                "broker-run",
                "stopping",
            ],
        )
        scheduler = captured["scheduler"]
        self.assertIsInstance(scheduler, LeaseScheduler)
        snapshot = scheduler.snapshot
        self.assertEqual(snapshot.active, active)
        self.assertEqual(snapshot.queued, (queued_early, queued_late))
        recovered = captured["recovered_tickets"]
        self.assertEqual(
            tuple(ticket.lease for ticket in recovered),
            (active, queued_late, queued_early),
        )
        self.assertEqual(notifier.ready_count, 1)
        self.assertEqual(notifier.stopping_count, 1)
        self.assertTrue(all(_is_closed(descriptor) for descriptor in records))
        self.assertTrue(
            all(
                _is_closed(descriptor)
                for descriptor in self.activation_fixture.descriptors
            )
        )

    def test_queue_only_recovery_does_not_signal_an_owner(self) -> None:
        events: list[str] = []
        captured: dict[str, object] = {}
        activation = self.activation_fixture.build(
            (
                _lease(LEASE_A, sequence=1, state=LeaseState.QUEUED),
                _lease(LEASE_B, sequence=2, state=LeaseState.QUEUED),
            )
        )

        with mock.patch.object(
            daemon,
            "_invalidate_recovered_active",
            wraps=daemon._invalidate_recovered_active,
        ) as invalidation:
            self._run(
                activation,
                events=events,
                captured=captured,
            )
        invalidation.assert_called_once()
        self.assertNotIn("active-kill", events)
        self.assertNotIn("active-exit", events)
        self.assertLess(events.index("policy-recover"), events.index("ready"))

    def test_signal_requests_clean_stop_and_stopping_is_exactly_once(self) -> None:
        events: list[str] = []
        captured: dict[str, object] = {}
        activation = self.activation_fixture.build(())
        installed: dict[int, object] = {}

        def install(signal_number: int, handler):
            prior = installed.get(signal_number, signal.SIG_DFL)
            installed[signal_number] = handler
            return prior

        def run_action() -> None:
            handler = installed[signal.SIGTERM]
            self.assertTrue(callable(handler))
            handler(signal.SIGTERM, None)

        with mock.patch.object(signal, "signal", side_effect=install):
            notifier = self._run(
                activation,
                events=events,
                captured=captured,
                manage_signals=True,
                run_action=run_action,
            )

        broker = captured["broker"]
        self.assertIsInstance(broker, FakeBroker)
        self.assertEqual(broker.clean_stop_requests, 1)
        self.assertEqual(notifier.stopping_count, 1)
        self.assertLess(events.index("ready"), events.index("clean-stop"))
        self.assertLess(events.index("clean-stop"), events.index("stopping"))

    def test_startup_signal_finishes_recovery_without_publishing_ready(self) -> None:
        events: list[str] = []
        captured: dict[str, object] = {}
        activation = self.activation_fixture.build(
            (_lease(LEASE_A, sequence=1, state=LeaseState.ACTIVE),)
        )
        installed: dict[int, object] = {}

        def install(signal_number: int, handler):
            prior = installed.get(signal_number, signal.SIG_DFL)
            installed[signal_number] = handler
            return prior

        def request_stop_during_activation() -> None:
            handler = installed[signal.SIGTERM]
            self.assertTrue(callable(handler))
            handler(signal.SIGTERM, None)

        with mock.patch.object(signal, "signal", side_effect=install):
            notifier = self._run(
                activation,
                events=events,
                captured=captured,
                manage_signals=True,
                startup_action=request_stop_during_activation,
            )

        broker = captured["broker"]
        self.assertIsInstance(broker, FakeBroker)
        self.assertEqual(broker.clean_stop_requests, 1)
        self.assertEqual(notifier.ready_count, 0)
        self.assertEqual(notifier.stopping_count, 1)
        self.assertEqual(
            events,
            [
                "activation",
                "active-kill",
                "active-exit",
                "policy-recover",
                "broker-init",
                "clean-stop",
                "stopping",
                "broker-run",
            ],
        )

    def test_activation_cleanup_and_broker_diagnostics_are_reported(self) -> None:
        events: list[str] = []
        captured: dict[str, object] = {}
        base = self.activation_fixture.build(())
        activation = ActivationState(
            control_descriptor=base.control_descriptor,
            tickets=(),
            reaped=(
                ReapedTicket(
                    lease_id=LEASE_A,
                    reason=ReapReason.OWNER_EXITED,
                    descriptor_names=(
                        f"ticket.{LEASE_A}.owner",
                        f"ticket.{LEASE_A}.queued-record",
                    ),
                ),
            ),
            discarded_descriptor_names=(f"ticket.{LEASE_B}.queued-record",),
        )

        self._run(
            activation,
            events=events,
            captured=captured,
        )

        self.assertIn(
            "report:reaped stored lease "
            f"{LEASE_A} during activation (owner-exited); removed "
            f"ticket.{LEASE_A}.owner, ticket.{LEASE_A}.queued-record",
            events,
        )
        self.assertIn(
            "report:discarded stale stored descriptor "
            f"ticket.{LEASE_B}.queued-record during activation",
            events,
        )
        report = captured["report"]
        self.assertTrue(callable(report))
        report("injected broker diagnostic")
        self.assertEqual(events[-1], "report:injected broker diagnostic")

    def test_real_broker_reaps_restarted_active_before_granting_fifo_head(
        self,
    ) -> None:
        events: list[str] = []
        captured: dict[str, object] = {}
        child_pid = os.fork()
        if child_pid == 0:
            signal.pause()
            os._exit(70)
        active = _lease(
            LEASE_A,
            sequence=1,
            state=LeaseState.ACTIVE,
            pid=child_pid,
        )
        queued = _lease(
            LEASE_B,
            sequence=2,
            state=LeaseState.QUEUED,
        )
        try:
            activation = self.activation_fixture.build(
                (active, queued),
                owner_pids=(child_pid, os.getpid()),
            )
            active_descriptor = activation.tickets[0].owner_descriptor
            queued_descriptor = activation.tickets[1].owner_descriptor
            queued_client = self.activation_fixture.channel_pairs[0][1]
            notifier = RecordingNotifier(
                events,
                self.activation_fixture.named_descriptors,
            )
            watcher: threading.Thread | None = None

            def broker_factory(**arguments) -> BenchmarkBroker:
                nonlocal watcher
                broker = BenchmarkBroker(**arguments)
                captured["broker"] = broker

                def watch_grant() -> None:
                    event = receive_event(queued_client)
                    captured["grant"] = event
                    broker.request_clean_stop()

                watcher = threading.Thread(target=watch_grant)
                watcher.start()
                return broker

            def signal_owner(descriptor: int, signal_number: int) -> None:
                if descriptor == active_descriptor:
                    events.append(
                        "startup-active-kill"
                        if signal_number == signal.SIGKILL
                        else "broker-active-invalidate"
                    )
                    daemon._signal_pidfd(descriptor, signal_number)
                    return
                self.assertEqual(descriptor, queued_descriptor)
                self.assertEqual(signal_number, signal.SIGTERM)

            def wait_owner(descriptor: int, timeout: float | None) -> bool:
                if descriptor == active_descriptor:
                    return wait_for_pidfd(descriptor, timeout)
                self.assertEqual(descriptor, queued_descriptor)
                self.assertEqual(timeout, 5.0)
                return True

            daemon.run_daemon(
                notifier,
                configuration_path=self.config_path,
                configuration_parser=lambda _payload: _configuration(),
                policy_factory=lambda _config: FakePolicy(events),
                broker_factory=broker_factory,
                effective_uid=0,
                configuration_owner_uid=os.getuid(),
                signal_owner=signal_owner,
                wait_owner=wait_owner,
                report=lambda message: events.append(f"report:{message}"),
                manage_signals=False,
            )
            if watcher is None:
                self.fail("real broker factory did not start its grant watcher")
            watcher.join()

            grant = captured["grant"]
            self.assertIsInstance(grant, GrantedEvent)
            self.assertEqual(grant.lease_id, LEASE_B)
            self.assertEqual(grant.policy, "fake-policy")
            self.assertLess(events.index("policy-recover"), events.index("ready"))
            self.assertLess(events.index("ready"), events.index("policy-enter"))
            self.assertEqual(notifier.ready_count, 1)
            self.assertEqual(notifier.stopping_count, 1)
            self.assertLess(
                events.index("broker-active-invalidate"),
                events.index("ready"),
            )
            self.assertIn(f"ticket.{LEASE_A}.active-record", notifier.removed)
            self.assertIn(f"ticket.{LEASE_B}.active-record", notifier.removed)
        finally:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(child_pid, 0)
            except ChildProcessError:
                pass

    def test_failure_before_adoption_never_publishes_ready_and_closes_fds(
        self,
    ) -> None:
        events: list[str] = []
        activation = self.activation_fixture.build(
            (_lease(LEASE_A, sequence=1, state=LeaseState.QUEUED),)
        )
        notifier = FakeNotifier(events)
        with (
            mock.patch.object(
                daemon,
                "recover_activation",
                return_value=activation,
            ),
            self.assertRaisesRegex(RuntimeError, "recovery failed"),
        ):
            daemon.run_daemon(
                notifier,
                configuration_path=self.config_path,
                configuration_parser=lambda _payload: _configuration(),
                policy_factory=lambda _config: mock.Mock(
                    recover=mock.Mock(side_effect=RuntimeError("recovery failed"))
                ),
                effective_uid=0,
                configuration_owner_uid=os.getuid(),
                manage_signals=False,
            )

        self.assertEqual(notifier.ready_count, 0)
        self.assertTrue(
            all(
                _is_closed(descriptor)
                for descriptor in self.activation_fixture.descriptors
            )
        )

    def test_invalid_configuration_cannot_leave_restarted_active_running(
        self,
    ) -> None:
        events: list[str] = []
        active = _lease(
            LEASE_A,
            sequence=1,
            state=LeaseState.ACTIVE,
        )
        activation = self.activation_fixture.build((active,))
        active_descriptor = activation.tickets[0].owner_descriptor
        notifier = FakeNotifier(events)
        self.config_path.chmod(0o644)

        with (
            mock.patch.object(
                daemon,
                "recover_activation",
                return_value=activation,
            ),
            self.assertRaises(BenchmarkLockError),
        ):
            daemon.run_daemon(
                notifier,
                configuration_path=self.config_path,
                configuration_parser=lambda _payload: _configuration(),
                policy_factory=lambda _config: self.fail(
                    "invalid configuration reached the policy factory"
                ),
                effective_uid=0,
                configuration_owner_uid=os.getuid(),
                signal_owner=lambda descriptor, signal_number: events.append(
                    (
                        "kill"
                        if (
                            descriptor == active_descriptor
                            and signal_number == signal.SIGKILL
                        )
                        else self.fail("wrong recovered-owner signal")
                    )
                ),
                wait_owner=lambda descriptor, timeout: (
                    events.append(
                        (
                            "exit"
                            if descriptor == active_descriptor and timeout is None
                            else self.fail("wrong recovered-owner wait")
                        )
                    )
                    or True
                ),
                manage_signals=False,
            )

        self.assertEqual(events, ["kill", "exit"])
        self.assertEqual(notifier.ready_count, 0)
        self.assertTrue(
            all(
                _is_closed(descriptor)
                for descriptor in self.activation_fixture.descriptors
            )
        )

    def test_non_root_start_is_rejected_before_configuration_or_activation(
        self,
    ) -> None:
        events: list[str] = []
        notifier = FakeNotifier(events)
        parser = mock.Mock(return_value=_configuration())
        with self.assertRaises(BenchmarkLockError) as caught:
            daemon.run_daemon(
                notifier,
                configuration_path=self.config_path,
                configuration_parser=parser,
                effective_uid=1,
                manage_signals=False,
            )
        self.assertEqual(caught.exception.code, "benchmark_admin_required")
        parser.assert_not_called()
        self.assertEqual(events, [])

    def test_entry_point_reports_infrastructure_failure_and_is_nonzero(
        self,
    ) -> None:
        failure = BenchmarkLockError(
            "injected startup failure",
            code="injected_failure",
        )
        error_output = io.StringIO()
        with (
            mock.patch.object(daemon, "SystemdServiceNotifier"),
            mock.patch.object(daemon, "run_daemon", side_effect=failure),
            mock.patch("sys.stderr", error_output),
        ):
            status = daemon.main()

        self.assertEqual(status, 1)
        self.assertEqual(
            error_output.getvalue(),
            "benchmarkd: injected_failure: injected startup failure\n",
        )
