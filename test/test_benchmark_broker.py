from __future__ import annotations

import os
import pathlib
import queue
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from unittest import mock

from benchmark_lock.broker import (
    BenchmarkBroker,
    RecoveredBrokerTicket,
    _signal_pidfd,
    _Ticket,
)
from benchmark_lock.errors import BenchmarkLockError
from benchmark_lock.linux import PeerCredentials, PeerIdentity as LinuxPeerIdentity
from benchmark_lock.protocol import (
    AcquireRequest,
    ErrorEvent,
    GrantedEvent,
    MaintenanceEvent,
    MaintenanceRequest,
    QueuedEvent,
    StatusEvent,
    StatusRequest,
    WaitingEvent,
    receive_event,
    send_request,
)
from benchmark_lock.scheduler import (
    Lease,
    LeaseScheduler,
    LeaseState,
    PeerIdentity,
)


_WORKER = r"""
import socket
import sys

from benchmark_lock.protocol import (
    AcquireRequest,
    ErrorEvent,
    GrantedEvent,
    QueuedEvent,
    WaitingEvent,
    receive_event,
    send_request,
)

connection = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
connection.connect(sys.argv[1])
send_request(connection, AcquireRequest(sys.argv[2]))
while True:
    event = receive_event(connection)
    if isinstance(event, (QueuedEvent, WaitingEvent)):
        print(f"{event.__class__.__name__}:{event.position}", flush=True)
        continue
    if isinstance(event, ErrorEvent):
        print(f"error:{event.code}", flush=True)
        raise SystemExit(125)
    if isinstance(event, GrantedEvent):
        print("granted", flush=True)
        connection.close()
        sys.stdin.buffer.read(1)
        raise SystemExit(0)
    raise AssertionError(f"unexpected event: {event!r}")
"""

_EXEC_CLIENT = r"""
import os
import socket
import sys

from benchmark_lock.client import _run_acquire

connection = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
connection.connect(sys.argv[1])
status = _run_acquire(
    connection,
    command=(
        sys.executable,
        "-c",
        "import os,sys; "
        "print(f'{os.getpid()}:{os.environ[\"BENCHMARK_LOCK_LEASE_ID\"]}', "
        "flush=True); "
        "raise SystemExit(23)",
    ),
    label="exec-boundary",
    environment=os.environ,
    error=sys.stderr,
)
raise SystemExit(status)
"""

_SIGNAL_EXEC_CLIENT = r"""
import os
import socket
import sys

from benchmark_lock.client import _run_acquire

connection = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
connection.connect(sys.argv[1])
status = _run_acquire(
    connection,
    command=("/bin/cat", "/proc/self/status"),
    label="signal-boundary",
    environment=os.environ,
    error=sys.stderr,
)
raise SystemExit(status)
"""


class FakePolicy:
    identity = "test-performance"

    def __init__(self) -> None:
        self.state = "idle"
        self.entries = 0
        self.leaves = 0
        self.verifications = 0
        self.preflights = 0
        self.fail_enter = False
        self.fail_preflight = False
        self.fail_verify = False
        self.enter_callback: Callable[[], None] | None = None

    def enter(self) -> None:
        self.entries += 1
        self.state = "entering"
        if self.fail_enter:
            self.state = "idle"
            raise BenchmarkLockError(
                "fixed policy could not be applied",
                code="policy_failed",
            )
        self.state = "held"
        if self.enter_callback is not None:
            self.enter_callback()

    def verify(self) -> None:
        self.verifications += 1
        if self.fail_verify:
            self.state = "faulted"
            raise BenchmarkLockError(
                "fixed policy drifted",
                code="policy_drift",
            )

    def preflight(self) -> None:
        self.preflights += 1
        if self.fail_preflight:
            raise BenchmarkLockError(
                "foreign compute appeared before grant",
                code="benchmark_external_compute",
            )

    def leave(self) -> None:
        self.leaves += 1
        self.state = "idle"


class FakePersistence:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.fail_release = False

    def retain_queued(
        self,
        lease: Lease,
        *,
        channel_descriptor: int,
        owner_descriptor: int,
    ) -> None:
        self.assert_open(channel_descriptor)
        self.assert_open(owner_descriptor)
        self.events.append(("queued", lease.lease_id))

    def retain_active(self, lease: Lease) -> None:
        self.events.append(("active", lease.lease_id))

    def release(self, lease_id: str) -> None:
        self.events.append(("release", lease_id))
        if self.fail_release:
            raise BenchmarkLockError(
                "injected persistence release failure",
                code="injected_release_failure",
            )

    @staticmethod
    def assert_open(descriptor: int) -> None:
        os.fstat(descriptor)


class BrokerFixture:
    def __init__(self, **broker_arguments) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.socket_path = self.root / "control.sock"
        self.listener = socket.socket(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET,
        )
        self.listener.bind(str(self.socket_path))
        self.listener.listen(16)
        self.policy = FakePolicy()
        self.reports: list[str] = []
        arguments = {
            "listener": self.listener,
            "policy": self.policy,
            "signal_owner": lambda _descriptor, _signal_number: None,
            "report": self.reports.append,
        }
        arguments.update(broker_arguments)
        self.broker = BenchmarkBroker(**arguments)
        self.failure: BaseException | None = None
        self.thread = threading.Thread(target=self._run_broker)
        self.thread.start()
        self.closed = False

    def _run_broker(self) -> None:
        try:
            self.broker.run()
        except BaseException as error:
            self.failure = error

    def take_failure(self) -> BaseException | None:
        failure = self.failure
        self.failure = None
        return failure

    def connect(self) -> socket.socket:
        connection = socket.socket(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET,
        )
        connection.connect(str(self.socket_path))
        return connection

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.broker.request_stop()
        self.thread.join(3)
        if self.thread.is_alive():
            raise AssertionError("benchmark broker did not stop")
        self.listener.close()
        self.temporary.cleanup()
        if self.failure is not None:
            raise AssertionError("benchmark broker failed") from self.failure


class LeaseWorker:
    def __init__(self, socket_path: pathlib.Path, label: str) -> None:
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = os.fspath(
            pathlib.Path(__file__).resolve().parents[1] / "lib"
        )
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _WORKER,
                os.fspath(socket_path),
                label,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=environment,
        )
        self.lines: queue.Queue[str] = queue.Queue()
        self.reader = threading.Thread(target=self._read_lines)
        self.reader.start()

    def _read_lines(self) -> None:
        if self.process.stdout is None:
            return
        for line in self.process.stdout:
            self.lines.put(line.rstrip("\n"))

    def next_line(self) -> str:
        try:
            return self.lines.get(timeout=3)
        except queue.Empty:
            stderr = ""
            if self.process.poll() is not None and self.process.stderr is not None:
                stderr = self.process.stderr.read()
            raise AssertionError(
                "lease worker produced no readiness line; "
                f"status={self.process.poll()} stderr={stderr!r}"
            ) from None

    def close_input(self) -> None:
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()

    def cleanup(self) -> None:
        self.close_input()
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        self.reader.join()
        for stream in (
            self.process.stdin,
            self.process.stdout,
            self.process.stderr,
        ):
            if stream is not None and not stream.closed:
                stream.close()


class BenchmarkBrokerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = BrokerFixture()
        self.addCleanup(self.fixture.close)

    def wait_until(self, predicate, *, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("condition did not become true")
            time.sleep(0.01)

    def acquire(self, connection: socket.socket, label: str):
        send_request(connection, AcquireRequest(label))
        queued = receive_event(connection)
        self.assertIsInstance(queued, QueuedEvent)
        return queued

    def test_status_is_bounded_and_does_not_join_the_queue(self) -> None:
        connection = self.fixture.connect()
        send_request(connection, StatusRequest())
        self.assertEqual(
            receive_event(connection),
            StatusEvent(active=None, queue_depth=0, policy_state="idle"),
        )
        self.wait_until(lambda: not self.fixture.broker.scheduler.snapshot.queued)
        connection.close()

    def test_socket_hup_after_grant_does_not_release_live_process(self) -> None:
        active_connection = self.fixture.connect()
        queued = self.acquire(active_connection, "active")
        grant = receive_event(active_connection)
        self.assertIsInstance(grant, GrantedEvent)
        self.assertEqual(grant.lease_id, queued.lease_id)
        active_connection.close()

        waiter = self.fixture.connect()
        waiting = self.acquire(waiter, "waiter")
        self.assertEqual(waiting.position, 1)
        waiter.settimeout(0.2)
        with self.assertRaises(BenchmarkLockError) as raised:
            receive_event(waiter)
        self.assertEqual(
            raised.exception.code,
            "benchmark_channel_closed",
        )
        self.assertIsNotNone(self.fixture.broker.scheduler.snapshot.active)
        waiter.close()

    def test_waiter_cancellation_removes_only_that_fifo_entry(self) -> None:
        holder = self.fixture.connect()
        self.acquire(holder, "holder")
        receive_event(holder)

        second = self.fixture.connect()
        third = self.fixture.connect()
        self.assertEqual(self.acquire(second, "second").position, 1)
        self.assertEqual(self.acquire(third, "third").position, 2)
        second.close()

        update = receive_event(third)
        self.assertIsInstance(update, WaitingEvent)
        self.assertEqual(update.position, 1)
        self.wait_until(lambda: len(self.fixture.broker.scheduler.snapshot.queued) == 1)
        third.close()
        holder.close()

    def test_policy_failure_rejects_without_a_grant(self) -> None:
        self.fixture.policy.fail_enter = True
        connection = self.fixture.connect()
        send_request(connection, AcquireRequest("will-fail"))
        self.assertIsInstance(receive_event(connection), QueuedEvent)
        error = receive_event(connection)
        self.assertEqual(
            error,
            ErrorEvent(
                code="policy_failed",
                message="fixed policy could not be applied",
            ),
        )
        self.wait_until(
            lambda: (
                self.fixture.broker.scheduler.snapshot.active is None
                and not self.fixture.broker.scheduler.snapshot.queued
            )
        )
        self.assertEqual(self.fixture.policy.state, "idle")
        connection.close()

    def test_failed_grant_preflight_restores_policy_before_retry(self) -> None:
        self.fixture.policy.fail_preflight = True
        connection = self.fixture.connect()
        send_request(connection, AcquireRequest("preflight-failure"))
        self.assertIsInstance(receive_event(connection), QueuedEvent)
        self.assertEqual(
            receive_event(connection),
            ErrorEvent(
                code="benchmark_external_compute",
                message="foreign compute appeared before grant",
            ),
        )
        self.wait_until(lambda: self.fixture.policy.state == "idle")
        self.assertEqual(self.fixture.policy.entries, 1)
        self.assertEqual(self.fixture.policy.leaves, 1)
        connection.close()

    def test_clean_stop_during_policy_enter_never_grants_preparing_owner(
        self,
    ) -> None:
        persistence = FakePersistence()
        fixture = BrokerFixture(persistence=persistence)
        self.addCleanup(fixture.close)
        fixture.policy.enter_callback = fixture.broker.request_clean_stop
        connection = fixture.connect()
        self.addCleanup(connection.close)

        queued = self.acquire(connection, "stop-during-enter")
        self.assertEqual(
            receive_event(connection),
            ErrorEvent(
                code="benchmark_broker_stopping",
                message="benchmark service is stopping",
            ),
        )
        fixture.thread.join(3)

        self.assertFalse(fixture.thread.is_alive())
        self.assertIsNone(fixture.take_failure())
        self.assertIsNone(fixture.broker.scheduler.snapshot.active)
        self.assertIsNone(fixture.broker.scheduler.snapshot.preparing)
        self.assertEqual(fixture.policy.state, "idle")
        self.assertEqual(
            persistence.events,
            [
                ("queued", queued.lease_id),
                ("release", queued.lease_id),
            ],
        )

    def test_client_attestation_failure_does_not_stop_the_broker(self) -> None:
        failures = 0

        def attest(connection: socket.socket) -> LinuxPeerIdentity:
            nonlocal failures
            failures += 1
            if failures == 1:
                raise BenchmarkLockError(
                    "injected attestation failure",
                    code="invalid_benchmark_channel",
                )
            from benchmark_lock.linux import attest_client_peer

            return attest_client_peer(connection)

        fixture = BrokerFixture(attest_peer=attest)
        self.addCleanup(fixture.close)
        rejected = fixture.connect()
        rejected.settimeout(1)
        self.assertEqual(rejected.recv(1), b"")
        rejected.close()

        status = fixture.connect()
        send_request(status, StatusRequest())
        self.assertEqual(
            receive_event(status),
            StatusEvent(active=None, queue_depth=0, policy_state="idle"),
        )
        status.close()
        self.assertTrue(
            any("injected attestation failure" in line for line in fixture.reports)
        )

    def test_ready_callback_runs_after_listener_preparation(self) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        listener.bind(f"\0benchmark-ready-test-{os.getpid()}-{id(self)}")
        listener.listen()
        broker = BenchmarkBroker(
            listener=listener,
            policy=FakePolicy(),
        )
        callback_count = 0

        def ready() -> None:
            nonlocal callback_count
            callback_count += 1
            registered = {
                key.fd: key.data for key in broker.selector.get_map().values()
            }
            self.assertEqual(
                registered[listener.fileno()],
                ("listener", None),
            )
            self.assertFalse(listener.getblocking())
            broker.request_stop()

        try:
            broker.run(ready=ready)
        finally:
            listener.close()

        self.assertEqual(callback_count, 1)
        self.assertTrue(broker._closed)

    def test_unadmitted_connections_are_bounded(self) -> None:
        connections = [self.fixture.connect() for _index in range(17)]
        for connection in connections:
            self.addCleanup(connection.close)
        self.wait_until(lambda: len(self.fixture.broker._accepted) == 16)
        connections[-1].settimeout(1)
        self.assertEqual(connections[-1].recv(1), b"")

    def test_root_maintenance_is_bound_to_the_request_channel(self) -> None:
        admission_fenced = False

        def refresh_admission_fence() -> bool:
            return admission_fenced

        self.fixture.broker.admission_fence = refresh_admission_fence
        server, client = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET,
        )
        identity = LinuxPeerIdentity(
            credentials=PeerCredentials(pid=123, uid=0, gid=0),
            pid_descriptor=os.pidfd_open(os.getpid()),
        )
        ticket = _Ticket(
            connection=server,
            identity=identity,
            accepted_at=time.monotonic(),
        )
        self.fixture.broker._accepted[server.fileno()] = ticket
        with mock.patch(
            "benchmark_lock.broker.receive_request",
            return_value=MaintenanceRequest(),
        ):
            self.fixture.broker._receive_initial_request(ticket)
        self.assertEqual(receive_event(client), MaintenanceEvent())
        self.assertEqual(
            self.fixture.broker.scheduler.snapshot.maintenance_owner,
            PeerIdentity(pid=123, uid=0, gid=0),
        )
        admission_fenced = True
        client.close()
        self.fixture.broker._channel_ready(ticket)
        self.assertIsNone(self.fixture.broker.scheduler.snapshot.maintenance_owner)
        self.assertTrue(self.fixture.broker.scheduler.snapshot.admission_fenced)

        acquire = self.fixture.connect()
        self.addCleanup(acquire.close)
        send_request(acquire, AcquireRequest("fenced-after-maintenance"))
        self.assertEqual(
            receive_event(acquire),
            ErrorEvent(
                code="maintenance_active",
                message="benchmark service is in administrator maintenance",
            ),
        )

    def test_recovered_queued_ticket_is_adopted_and_granted(self) -> None:
        server, client = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET,
        )
        lease = Lease(
            lease_id="a" * 32,
            sequence=7,
            peer=PeerIdentity(
                pid=os.getpid(),
                uid=os.getuid(),
                gid=os.getgid(),
            ),
            label="recovered-queue",
            inherited_lease_id=None,
            enqueued_at=1.0,
            state=LeaseState.QUEUED,
        )
        scheduler = LeaseScheduler()
        scheduler.restore(
            active=None,
            preparing=None,
            queued=(lease,),
            next_sequence=8,
        )
        recovered = RecoveredBrokerTicket(
            lease=lease,
            identity=LinuxPeerIdentity(
                credentials=PeerCredentials(
                    pid=os.getpid(),
                    uid=os.getuid(),
                    gid=os.getgid(),
                ),
                pid_descriptor=os.pidfd_open(os.getpid()),
            ),
            connection=server,
        )
        fixture = BrokerFixture(
            scheduler=scheduler,
            recovered_tickets=(recovered,),
        )
        self.addCleanup(fixture.close)
        self.addCleanup(client.close)

        self.assertEqual(
            receive_event(client),
            GrantedEvent(lease.lease_id, fixture.policy.identity),
        )
        self.assertEqual(fixture.policy.entries, 1)
        self.assertEqual(fixture.policy.preflights, 1)

    def test_recovered_fifo_cannot_grant_until_restart_fence_clears(self) -> None:
        server, client = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET,
        )
        lease = Lease(
            lease_id="c" * 32,
            sequence=3,
            peer=PeerIdentity(
                pid=os.getpid(),
                uid=os.getuid(),
                gid=os.getgid(),
            ),
            label="fenced-recovered-queue",
            inherited_lease_id=None,
            enqueued_at=1.0,
            state=LeaseState.QUEUED,
        )
        scheduler = LeaseScheduler()
        scheduler.restore(
            active=None,
            preparing=None,
            queued=(lease,),
            next_sequence=4,
            admission_fenced=True,
        )
        recovered = RecoveredBrokerTicket(
            lease=lease,
            identity=LinuxPeerIdentity(
                credentials=PeerCredentials(
                    pid=os.getpid(),
                    uid=os.getuid(),
                    gid=os.getgid(),
                ),
                pid_descriptor=os.pidfd_open(os.getpid()),
            ),
            connection=server,
        )
        fence_active = True

        def refresh_fence() -> bool:
            return fence_active

        fixture = BrokerFixture(
            scheduler=scheduler,
            recovered_tickets=(recovered,),
            admission_fence=refresh_fence,
        )
        self.addCleanup(fixture.close)
        self.addCleanup(client.close)

        self.assertEqual(
            receive_event(client),
            WaitingEvent(
                lease_id=lease.lease_id,
                position=1,
                active=None,
            ),
        )
        self.assertEqual(fixture.policy.entries, 0)
        self.assertEqual(
            fixture.broker.scheduler.snapshot.queued,
            (lease,),
        )

        fence_active = False
        self.assertEqual(
            receive_event(client),
            GrantedEvent(lease.lease_id, fixture.policy.identity),
        )
        self.assertEqual(fixture.policy.entries, 1)

    def test_recovered_active_ticket_is_invalidated_not_recertified(self) -> None:
        server, client = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET,
        )
        lease = Lease(
            lease_id="b" * 32,
            sequence=4,
            peer=PeerIdentity(
                pid=os.getpid(),
                uid=os.getuid(),
                gid=os.getgid(),
            ),
            label="recovered-active",
            inherited_lease_id=None,
            enqueued_at=1.0,
            state=LeaseState.ACTIVE,
            started_at=2.0,
        )
        scheduler = LeaseScheduler()
        scheduler.restore(
            active=lease,
            preparing=None,
            queued=(),
            next_sequence=5,
        )
        signals: list[tuple[int, int]] = []
        recovered = RecoveredBrokerTicket(
            lease=lease,
            identity=LinuxPeerIdentity(
                credentials=PeerCredentials(
                    pid=os.getpid(),
                    uid=os.getuid(),
                    gid=os.getgid(),
                ),
                pid_descriptor=os.pidfd_open(os.getpid()),
            ),
            connection=server,
        )
        fixture = BrokerFixture(
            scheduler=scheduler,
            recovered_tickets=(recovered,),
            signal_owner=lambda descriptor, signal_number: signals.append(
                (descriptor, signal_number)
            ),
        )
        self.addCleanup(fixture.close)
        self.addCleanup(client.close)

        self.assertEqual(
            receive_event(client),
            ErrorEvent(
                code="benchmark_broker_restarted",
                message="benchmark owner was invalidated by broker restart",
            ),
        )
        self.wait_until(lambda: bool(signals))
        self.assertEqual(signals[0][1], signal.SIGTERM)
        self.assertEqual(fixture.policy.entries, 0)

    def test_failed_term_is_retried_without_losing_invalidation_state(self) -> None:
        attempts: list[int] = []

        def signal_owner(_descriptor: int, signal_number: int) -> None:
            attempts.append(signal_number)
            if len(attempts) == 1:
                raise BenchmarkLockError(
                    "injected signal failure",
                    code="benchmark_owner_signal_failed",
                )

        fixture = BrokerFixture(signal_owner=signal_owner)
        self.addCleanup(fixture.close)
        connection = fixture.connect()
        self.addCleanup(connection.close)
        self.acquire(connection, "drift")
        receive_event(connection)
        fixture.policy.fail_verify = True

        self.wait_until(
            lambda: attempts.count(signal.SIGTERM) >= 2,
            timeout=3.5,
        )
        active = fixture.broker.scheduler.snapshot.active
        self.assertIsNotNone(active)
        ticket = fixture.broker._tickets[active.lease_id]
        self.assertIsNotNone(ticket.policy_invalidated_at)
        self.assertIsNotNone(ticket.term_sent_at)
        self.assertTrue(
            any("injected signal failure" in line for line in fixture.reports)
        )

    def test_real_client_exec_preserves_pid_and_exact_exit_status(
        self,
    ) -> None:
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = os.fspath(
            pathlib.Path(__file__).resolve().parents[1] / "lib"
        )
        global_aslr_before = pathlib.Path(
            "/proc/sys/kernel/randomize_va_space"
        ).read_text(encoding="ascii")
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _EXEC_CLIENT,
                os.fspath(self.fixture.socket_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        stdout, stderr = process.communicate(timeout=5)

        self.assertEqual(process.returncode, 23)
        child_pid, lease_id = stdout.strip().split(":")
        self.assertEqual(int(child_pid), process.pid)
        self.assertRegex(lease_id, r"^[0-9a-f]{32}$")
        self.assertEqual(stderr, "")
        self.assertEqual(
            pathlib.Path("/proc/sys/kernel/randomize_va_space").read_text(
                encoding="ascii"
            ),
            global_aslr_before,
        )
        self.wait_until(lambda: self.fixture.policy.state == "idle")

    def test_real_client_exec_restores_python_ignored_signals(self) -> None:
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = os.fspath(
            pathlib.Path(__file__).resolve().parents[1] / "lib"
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _SIGNAL_EXEC_CLIENT,
                os.fspath(self.fixture.socket_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        stdout, stderr = process.communicate(timeout=5)

        self.assertEqual(process.returncode, 0)
        self.assertEqual(stderr, "")
        ignored_line = next(
            line for line in stdout.splitlines() if line.startswith("SigIgn:")
        )
        ignored_mask = int(ignored_line.split()[1], 16)
        for signal_name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
            if hasattr(signal, signal_name):
                signal_number = getattr(signal, signal_name)
                self.assertEqual(
                    ignored_mask & (1 << (signal_number - 1)),
                    0,
                    signal_name,
                )
        self.wait_until(lambda: self.fixture.policy.state == "idle")

    def test_real_pidfds_drive_fifo_after_cancellation_and_sigkill(
        self,
    ) -> None:
        holder = LeaseWorker(self.fixture.socket_path, "holder")
        self.addCleanup(holder.cleanup)
        self.assertEqual(holder.next_line(), "QueuedEvent:1")
        self.assertEqual(holder.next_line(), "granted")

        canceled = LeaseWorker(self.fixture.socket_path, "canceled")
        self.addCleanup(canceled.cleanup)
        self.assertEqual(canceled.next_line(), "QueuedEvent:1")

        survivor = LeaseWorker(self.fixture.socket_path, "survivor")
        self.addCleanup(survivor.cleanup)
        self.assertEqual(survivor.next_line(), "QueuedEvent:2")

        canceled.process.terminate()
        canceled.process.wait()
        self.assertEqual(survivor.next_line(), "WaitingEvent:1")

        holder.process.kill()
        holder.process.wait()
        self.assertEqual(survivor.next_line(), "granted")
        self.assertEqual(self.fixture.policy.entries, 1)
        self.assertEqual(self.fixture.policy.preflights, 2)

        survivor.close_input()
        self.assertEqual(survivor.process.wait(), 0)
        self.wait_until(lambda: self.fixture.policy.state == "idle")
        self.assertEqual(self.fixture.policy.leaves, 1)

    def test_persistence_transition_and_release_are_exactly_once(self) -> None:
        persistence = FakePersistence()
        fixture = BrokerFixture(persistence=persistence)
        self.addCleanup(fixture.close)
        worker = LeaseWorker(fixture.socket_path, "persistent")
        self.addCleanup(worker.cleanup)

        self.assertEqual(worker.next_line(), "QueuedEvent:1")
        self.assertEqual(worker.next_line(), "granted")
        active = fixture.broker.scheduler.snapshot.active
        self.assertIsNotNone(active)
        ticket = fixture.broker._tickets[active.lease_id]
        worker.close_input()
        self.assertEqual(worker.process.wait(), 0)
        self.wait_until(
            lambda: (
                fixture.broker.scheduler.snapshot.active is None
                and ticket.identity.pid_descriptor == -1
            )
        )

        self.assertEqual(
            persistence.events,
            [
                ("queued", active.lease_id),
                ("active", active.lease_id),
                ("release", active.lease_id),
            ],
        )
        fixture.broker._owner_ready(ticket)
        self.assertEqual(
            [event for event in persistence.events if event[0] == "release"],
            [("release", active.lease_id)],
        )

    def test_clean_stop_terminates_and_reaps_an_active_owner(self) -> None:
        persistence = FakePersistence()
        fixture = BrokerFixture(
            persistence=persistence,
            signal_owner=_signal_pidfd,
        )
        self.addCleanup(fixture.close)
        worker = LeaseWorker(fixture.socket_path, "shutdown")
        self.addCleanup(worker.cleanup)
        self.assertEqual(worker.next_line(), "QueuedEvent:1")
        self.assertEqual(worker.next_line(), "granted")
        active = fixture.broker.scheduler.snapshot.active
        self.assertIsNotNone(active)

        fixture.broker.request_clean_stop()
        fixture.thread.join(3)
        self.assertFalse(fixture.thread.is_alive())
        self.assertEqual(worker.process.wait(), -signal.SIGTERM)
        self.assertEqual(fixture.policy.state, "idle")
        self.assertEqual(
            [event for event in persistence.events if event[0] == "release"],
            [("release", active.lease_id)],
        )

    def test_clean_stop_restores_policy_after_all_persistence_releases_fail(
        self,
    ) -> None:
        persistence = FakePersistence()
        persistence.fail_release = True
        fixture = BrokerFixture(
            persistence=persistence,
            signal_owner=_signal_pidfd,
        )
        self.addCleanup(fixture.close)
        holder = LeaseWorker(fixture.socket_path, "shutdown-holder")
        self.addCleanup(holder.cleanup)
        self.assertEqual(holder.next_line(), "QueuedEvent:1")
        self.assertEqual(holder.next_line(), "granted")
        active = fixture.broker.scheduler.snapshot.active
        self.assertIsNotNone(active)

        waiter = LeaseWorker(fixture.socket_path, "shutdown-waiter")
        self.addCleanup(waiter.cleanup)
        self.assertEqual(waiter.next_line(), "QueuedEvent:1")
        queued = fixture.broker.scheduler.snapshot.queued
        self.assertEqual(len(queued), 1)

        fixture.broker.request_clean_stop()
        fixture.thread.join(3)

        self.assertFalse(fixture.thread.is_alive())
        failure = fixture.take_failure()
        self.assertIsInstance(failure, BenchmarkLockError)
        self.assertEqual(failure.code, "benchmark_clean_shutdown_failed")
        self.assertIn("release waiter", str(failure))
        self.assertIn("release active lease", str(failure))
        self.assertEqual(holder.process.wait(), -signal.SIGTERM)
        self.assertEqual(waiter.process.wait(), 125)
        self.assertEqual(fixture.policy.state, "idle")
        self.assertEqual(fixture.policy.leaves, 1)
        self.assertIsNone(fixture.broker.scheduler.snapshot.active)
        self.assertFalse(fixture.broker.scheduler.snapshot.queued)
        self.assertEqual(
            persistence.events,
            [
                ("queued", active.lease_id),
                ("active", active.lease_id),
                ("queued", queued[0].lease_id),
                ("release", queued[0].lease_id),
                ("release", active.lease_id),
            ],
        )


if __name__ == "__main__":
    unittest.main()
