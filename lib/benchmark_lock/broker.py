"""Single-threaded broker for exact process-lifetime benchmark leases."""

from __future__ import annotations

import dataclasses
import errno
import selectors
import signal
import socket
import threading
import time
from collections.abc import Callable, Iterable
from typing import Protocol

from .errors import BenchmarkLockError
from .linux import PeerIdentity as LinuxPeerIdentity
from .linux import attest_client_peer
from .protocol import (
    AcquireRequest,
    ActiveLease,
    ErrorEvent,
    GrantedEvent,
    MaintenanceEvent,
    MaintenanceRequest,
    QueuedEvent,
    StatusEvent,
    StatusRequest,
    WaitingEvent,
    enable_sender_credentials,
    receive_request,
    send_event,
)
from .scheduler import Lease, LeaseScheduler, PeerIdentity


REQUEST_TIMEOUT_SECONDS = 5.0
EVENT_LOOP_TICK_SECONDS = 0.25
POLICY_VERIFICATION_SECONDS = 1.0
POLICY_FAILURE_GRACE_SECONDS = 5.0
MAX_ACCEPTS_PER_TICK = 16
MAX_PENDING_CONNECTIONS = 16


class HostPolicy(Protocol):
    """Fixed host policy asserted around a contiguous run of leases.

    A failed ``enter`` either restores the exact baseline and returns to idle,
    or retains its original recovery journal and enters a faulted state.
    """

    @property
    def identity(self) -> str:
        """Return the stable policy name exposed over the protocol."""

    @property
    def state(self) -> str:
        """Return one protocol policy state."""

    def enter(self) -> None:
        """Snapshot, apply, and verify the complete host policy."""

    def preflight(self) -> None:
        """Reject foreign compute immediately before one grant."""

    def verify(self) -> None:
        """Fail if a held policy has drifted."""

    def leave(self) -> None:
        """Restore and verify the exact pre-transaction host state."""


class LeasePersistence(Protocol):
    """Crash-recovery publication boundary for admitted tickets."""

    def retain_queued(
        self,
        lease: Lease,
        *,
        channel_descriptor: int,
        owner_descriptor: int,
    ) -> None:
        """Publish one complete queued closure before acknowledgement."""

    def retain_active(self, lease: Lease) -> None:
        """Publish active intent before the grant packet."""

    def release(self, lease_id: str) -> None:
        """Remove every stored descriptor and record for one ticket."""


class NoopLeasePersistence:
    """In-memory broker persistence used by focused integration tests."""

    def retain_queued(
        self,
        _lease: Lease,
        *,
        channel_descriptor: int,
        owner_descriptor: int,
    ) -> None:
        del channel_descriptor, owner_descriptor

    def retain_active(self, _lease: Lease) -> None:
        return

    def release(self, _lease_id: str) -> None:
        return


@dataclasses.dataclass
class _Ticket:
    connection: socket.socket | None
    identity: LinuxPeerIdentity
    accepted_at: float
    lease: Lease | None = None
    last_wait_signature: tuple[object, ...] | None = None
    policy_invalidated_at: float | None = None
    term_sent_at: float | None = None
    kill_sent: bool = False
    maintenance_owner: PeerIdentity | None = None
    recovered: bool = False


@dataclasses.dataclass(frozen=True)
class RecoveredBrokerTicket:
    """One fd-store closure whose descriptor ownership passes to the broker."""

    lease: Lease
    identity: LinuxPeerIdentity
    connection: socket.socket | None


AttestPeer = Callable[[socket.socket], LinuxPeerIdentity]
Clock = Callable[[], float]
Reporter = Callable[[str], None]
OwnerSignaler = Callable[[int, int], None]
OwnerWaiter = Callable[[int, float | None], bool]


def _ignore_report(_message: str) -> None:
    return


def pidfd_has_exited(descriptor: int) -> bool:
    """Return whether one pidfd is immediately readable."""

    poller = selectors.DefaultSelector()
    try:
        poller.register(descriptor, selectors.EVENT_READ)
        return bool(poller.select(0))
    finally:
        poller.close()


def _signal_pidfd(descriptor: int, signal_number: int) -> None:
    if not hasattr(signal, "pidfd_send_signal"):
        raise BenchmarkLockError(
            "safe benchmark invalidation requires pidfd_send_signal",
            code="benchmark_platform_unsupported",
        )
    try:
        signal.pidfd_send_signal(descriptor, signal_number)
    except ProcessLookupError:
        return
    except OSError as error:
        raise BenchmarkLockError(
            f"cannot signal invalid benchmark owner: {error}",
            code="benchmark_owner_signal_failed",
        ) from error


def wait_for_pidfd(descriptor: int, timeout_seconds: float | None) -> bool:
    """Wait for exact process exit, returning false only for a finite timeout."""

    poller = selectors.DefaultSelector()
    try:
        poller.register(descriptor, selectors.EVENT_READ)
        events = poller.select(timeout_seconds)
    except OSError as error:
        raise BenchmarkLockError(
            f"cannot wait for benchmark owner exit: {error}",
            code="benchmark_owner_wait_failed",
        ) from error
    finally:
        poller.close()
    return bool(events)


class BenchmarkBroker:
    """Own one FIFO scheduler and its kernel process-lifetime descriptors."""

    def __init__(
        self,
        *,
        listener: socket.socket,
        policy: HostPolicy,
        persistence: LeasePersistence | None = None,
        scheduler: LeaseScheduler | None = None,
        recovered_tickets: Iterable[RecoveredBrokerTicket] = (),
        attest_peer: AttestPeer = attest_client_peer,
        signal_owner: OwnerSignaler = _signal_pidfd,
        wait_owner: OwnerWaiter = wait_for_pidfd,
        monotonic: Clock = time.monotonic,
        report: Reporter = _ignore_report,
    ) -> None:
        self.listener = listener
        self.policy = policy
        self.persistence = (
            NoopLeasePersistence() if persistence is None else persistence
        )
        self.scheduler = LeaseScheduler() if scheduler is None else scheduler
        self.attest_peer = attest_peer
        self.signal_owner = signal_owner
        self.wait_owner = wait_owner
        self.monotonic = monotonic
        self.report = report
        self.selector = selectors.DefaultSelector()
        self.stop_event = threading.Event()
        self._accepted: dict[int, _Ticket] = {}
        self._tickets: dict[str, _Ticket] = {}
        self._last_policy_verification = self.monotonic()
        self._closed = False
        self._clean_stop_requested = False
        self._grant_boundary = threading.Lock()
        self._adopt_recovered_tickets(tuple(recovered_tickets))

    def run(self, *, ready: Callable[[], None] | None = None) -> None:
        """Prepare every descriptor, publish readiness, and serve until stopped."""

        try:
            # Credentials must be enabled before accept so a request sent
            # immediately after connect already carries SCM_CREDENTIALS.
            enable_sender_credentials(self.listener)
            self.listener.setblocking(False)
            self.selector.register(
                self.listener,
                selectors.EVENT_READ,
                ("listener", None),
            )
            self._register_recovered_tickets()
            self._invalidate_recovered_active()
            if ready is not None:
                ready()
            while not self.stop_event.is_set():
                for key, _events in self.selector.select(EVENT_LOOP_TICK_SECONDS):
                    kind, ticket = key.data
                    if kind == "listener":
                        self._accept_ready()
                    elif kind == "channel":
                        self._channel_ready(ticket)
                    elif kind == "owner":
                        self._owner_ready(ticket)
                    else:
                        raise AssertionError(f"unknown selector source {kind!r}")
                self._expire_unadmitted_connections()
                self._drive_policy_and_grants()
                self._notify_waiters()
                self._verify_held_policy()
        finally:
            self._close_runtime(clean=self._clean_stop_requested)

    def request_stop(self) -> None:
        """Request a recovery-preserving event-loop stop for tests/restart."""

        with self._grant_boundary:
            self.stop_event.set()

    def request_clean_stop(self) -> None:
        """Request a complete terminal shutdown at the next bounded tick."""

        with self._grant_boundary:
            self._clean_stop_requested = True
            self.stop_event.set()

    def _adopt_recovered_tickets(
        self,
        recovered_tickets: tuple[RecoveredBrokerTicket, ...],
    ) -> None:
        snapshot = self.scheduler.snapshot
        expected = {
            lease.lease_id: lease
            for lease in (
                (() if snapshot.active is None else (snapshot.active,))
                + (() if snapshot.preparing is None else (snapshot.preparing,))
                + snapshot.queued
            )
        }
        provided = {recovered.lease.lease_id for recovered in recovered_tickets}
        if len(provided) != len(recovered_tickets) or provided != set(expected):
            raise BenchmarkLockError(
                "recovered broker tickets do not match scheduler state",
                code="invalid_broker_recovery",
            )
        if snapshot.preparing is not None:
            raise BenchmarkLockError(
                "fd-store recovery cannot retain a preparing scheduler state",
                code="invalid_broker_recovery",
            )
        for recovered in recovered_tickets:
            lease = expected[recovered.lease.lease_id]
            credentials = recovered.identity.credentials
            if (
                recovered.lease != lease
                or credentials.pid != lease.peer.pid
                or credentials.uid != lease.peer.uid
                or credentials.gid != lease.peer.gid
            ):
                raise BenchmarkLockError(
                    "recovered broker authority disagrees with scheduler state",
                    code="invalid_broker_recovery",
                )
            self._tickets[lease.lease_id] = _Ticket(
                connection=recovered.connection,
                identity=recovered.identity,
                accepted_at=self.monotonic(),
                lease=lease,
                recovered=True,
            )

    def _register_recovered_tickets(self) -> None:
        for ticket in self._tickets.values():
            if not ticket.recovered:
                continue
            if ticket.connection is not None:
                ticket.connection.setblocking(False)
                self.selector.register(
                    ticket.connection,
                    selectors.EVENT_READ,
                    ("channel", ticket),
                )
            self.selector.register(
                ticket.identity.pid_descriptor,
                selectors.EVENT_READ,
                ("owner", ticket),
            )

    def _invalidate_recovered_active(self) -> None:
        active = self.scheduler.snapshot.active
        if active is None:
            return
        ticket = self._tickets[active.lease_id]
        self._send_error_to_ticket(
            ticket,
            BenchmarkLockError(
                "benchmark owner was invalidated by broker restart",
                code="benchmark_broker_restarted",
            ),
        )
        self._invalidate_active(ticket)
        self._close_ticket_channel(ticket)

    def _accept_ready(self) -> None:
        accepted_count = 0
        while accepted_count < MAX_ACCEPTS_PER_TICK:
            try:
                connection, _address = self.listener.accept()
            except BlockingIOError:
                return
            except OSError as error:
                if error.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                    return
                raise
            accepted_count += 1
            if len(self._accepted) >= MAX_PENDING_CONNECTIONS:
                connection.close()
                continue
            connection.setblocking(False)
            identity: LinuxPeerIdentity | None = None
            try:
                identity = self.attest_peer(connection)
                ticket = _Ticket(
                    connection=connection,
                    identity=identity,
                    accepted_at=self.monotonic(),
                )
                self._accepted[connection.fileno()] = ticket
                self.selector.register(
                    connection,
                    selectors.EVENT_READ,
                    ("channel", ticket),
                )
                self.selector.register(
                    identity.pid_descriptor,
                    selectors.EVENT_READ,
                    ("owner", ticket),
                )
            except BenchmarkLockError as error:
                if identity is not None:
                    identity.close()
                connection.close()
                self.report(
                    f"rejected unattestable benchmark client: {error.code}: {error}"
                )
            except Exception:
                if identity is not None:
                    identity.close()
                connection.close()
                raise

    def _channel_ready(self, ticket: _Ticket) -> None:
        if ticket.connection is None:
            return
        if ticket.maintenance_owner is not None:
            self._cleanup_ticket(ticket)
            return
        if ticket.lease is None:
            self._receive_initial_request(ticket)
            return
        if (
            self.scheduler.snapshot.active is not None
            and self.scheduler.snapshot.active.lease_id == ticket.lease.lease_id
        ):
            self._close_ticket_channel(ticket)
            return
        self._cancel_waiter(ticket)

    def _receive_initial_request(self, ticket: _Ticket) -> None:
        connection = ticket.connection
        if connection is None:
            return
        try:
            request = receive_request(
                connection,
                expected_credentials=ticket.identity.credentials.as_tuple(),
            )
            if isinstance(request, StatusRequest):
                self._send_status(connection)
                self._discard_unadmitted(ticket)
                return
            if isinstance(request, MaintenanceRequest):
                maintenance_owner = PeerIdentity(
                    pid=ticket.identity.credentials.pid,
                    uid=ticket.identity.credentials.uid,
                    gid=ticket.identity.credentials.gid,
                )
                self.scheduler.enter_maintenance(maintenance_owner)
                ticket.maintenance_owner = maintenance_owner
                send_event(connection, MaintenanceEvent())
                return
            if not isinstance(request, AcquireRequest):
                raise AssertionError("protocol returned an unknown request")
            credentials = ticket.identity.credentials
            lease = self.scheduler.admit(
                peer=PeerIdentity(
                    pid=credentials.pid,
                    uid=credentials.uid,
                    gid=credentials.gid,
                ),
                label=request.label,
                inherited_lease_id=request.inherited_lease_id,
                now=self.monotonic(),
            )
            ticket.lease = lease
            self._accepted.pop(connection.fileno(), None)
            self._tickets[lease.lease_id] = ticket
            self.persistence.retain_queued(
                lease,
                channel_descriptor=connection.fileno(),
                owner_descriptor=ticket.identity.pid_descriptor,
            )
            self._send_wait_event(ticket, initial=True)
        except BenchmarkLockError as error:
            self._send_error(connection, error)
            if ticket.lease is not None:
                self.scheduler.cancel(ticket.lease.lease_id)
                self._release_persistence(ticket.lease.lease_id)
            self._cleanup_ticket(ticket)

    def _send_status(self, connection: socket.socket) -> None:
        snapshot = self.scheduler.snapshot
        send_event(
            connection,
            StatusEvent(
                active=self._active_document(snapshot.active),
                queue_depth=len(snapshot.queued),
                policy_state=(
                    "maintenance"
                    if snapshot.maintenance_owner is not None
                    else self.policy.state
                ),
            ),
        )

    def _owner_ready(self, ticket: _Ticket) -> None:
        if ticket.identity.pid_descriptor < 0:
            return
        if ticket.maintenance_owner is not None:
            self._cleanup_ticket(ticket)
            return
        lease = ticket.lease
        if lease is None:
            self._discard_unadmitted(ticket)
            return
        snapshot = self.scheduler.snapshot
        if snapshot.active is not None and (snapshot.active.lease_id == lease.lease_id):
            self.scheduler.complete_active(lease.lease_id)
            self._release_persistence(lease.lease_id)
            self._cleanup_ticket(ticket)
            if ticket.policy_invalidated_at is not None and self.policy.state != "idle":
                self.policy.leave()
            self._drive_policy_and_grants()
            return
        self._cancel_waiter(ticket)

    def _cancel_waiter(self, ticket: _Ticket) -> None:
        lease = ticket.lease
        if lease is not None:
            self.scheduler.cancel(lease.lease_id)
            self._release_persistence(lease.lease_id)
        self._cleanup_ticket(ticket)
        self._notify_waiters()

    def _drive_policy_and_grants(self) -> None:
        if self.stop_event.is_set():
            return
        while self.scheduler.snapshot.active is None:
            preparing = self.scheduler.snapshot.preparing
            if preparing is None:
                preparing = self.scheduler.begin_preparing()
            if preparing is None:
                if self.policy.state != "idle":
                    self.policy.leave()
                return
            ticket = self._tickets.get(preparing.lease_id)
            if ticket is None or pidfd_has_exited(ticket.identity.pid_descriptor):
                self.scheduler.fail_preparing(preparing.lease_id)
                self._release_persistence(preparing.lease_id)
                if ticket is not None:
                    self._cleanup_ticket(ticket)
                continue
            try:
                if self.policy.state != "held":
                    self.policy.enter()
                self.policy.verify()
                self.policy.preflight()
                if pidfd_has_exited(ticket.identity.pid_descriptor):
                    self.scheduler.fail_preparing(preparing.lease_id)
                    self._release_persistence(preparing.lease_id)
                    self._cleanup_ticket(ticket)
                    if self.policy.state != "idle":
                        self.policy.leave()
                    continue
                if not self._activate_and_grant(ticket, preparing):
                    return
                self._notify_waiters()
                return
            except BenchmarkLockError as error:
                snapshot = self.scheduler.snapshot
                if (
                    snapshot.preparing is not None
                    and snapshot.preparing.lease_id == preparing.lease_id
                ):
                    self.scheduler.fail_preparing(preparing.lease_id)
                    self._send_error_to_ticket(ticket, error)
                    self._release_persistence(preparing.lease_id)
                    self._cleanup_ticket(ticket)
                    if self.policy.state != "idle":
                        self.policy.leave()
                    continue
                if (
                    snapshot.active is not None
                    and snapshot.active.lease_id == preparing.lease_id
                ):
                    self.report(
                        "grant failed for active benchmark lease "
                        f"{preparing.lease_id}: {error}"
                    )
                    self._invalidate_active(ticket)
                    return
                raise

    def _activate_and_grant(self, ticket: _Ticket, preparing: Lease) -> bool:
        """Linearize a grant before or after every asynchronous stop request."""

        if not hasattr(signal, "pthread_sigmask"):
            raise BenchmarkLockError(
                "atomic benchmark grant shutdown requires pthread_sigmask",
                code="benchmark_platform_unsupported",
            )
        try:
            prior_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK,
                {signal.SIGINT, signal.SIGTERM},
            )
        except OSError as error:
            raise BenchmarkLockError(
                f"cannot block stop signals around benchmark grant: {error}",
                code="benchmark_grant_failed",
            ) from error
        try:
            with self._grant_boundary:
                if self.stop_event.is_set():
                    return False
                active = self.scheduler.activate(
                    preparing.lease_id,
                    now=self.monotonic(),
                )
                ticket.lease = active
                self.persistence.retain_active(active)
                self._send_grant(ticket, active)
                return True
        finally:
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)
            except OSError as error:
                raise BenchmarkLockError(
                    f"cannot restore stop signals after benchmark grant: {error}",
                    code="benchmark_grant_failed",
                ) from error

    def _send_grant(self, ticket: _Ticket, lease: Lease) -> None:
        if ticket.connection is None:
            raise BenchmarkLockError(
                "benchmark client disconnected before grant",
                code="benchmark_channel_closed",
            )
        send_event(
            ticket.connection,
            GrantedEvent(
                lease_id=lease.lease_id,
                policy=self.policy.identity,
            ),
        )

    def _notify_waiters(self) -> None:
        for lease in self.scheduler.snapshot.queued:
            ticket = self._tickets.get(lease.lease_id)
            if ticket is not None:
                self._send_wait_event(ticket, initial=False)

    def _send_wait_event(self, ticket: _Ticket, *, initial: bool) -> None:
        lease = ticket.lease
        if lease is None or ticket.connection is None:
            return
        position = self.scheduler.queue_position(lease.lease_id)
        if position is None:
            return
        active = self._active_document(self.scheduler.snapshot.active)
        signature = (
            position,
            None if active is None else active.lease_id,
            None if active is None else active.elapsed_seconds // 60,
        )
        if not initial and signature == ticket.last_wait_signature:
            return
        try:
            send_event(
                ticket.connection,
                (
                    QueuedEvent(lease.lease_id, position, active)
                    if initial
                    else WaitingEvent(lease.lease_id, position, active)
                ),
            )
            ticket.last_wait_signature = signature
        except BenchmarkLockError:
            self._cancel_waiter(ticket)

    def _active_document(self, lease: Lease | None) -> ActiveLease | None:
        if lease is None:
            return None
        if lease.started_at is None:
            raise AssertionError("active lease has no start time")
        elapsed = max(0, int(self.monotonic() - lease.started_at))
        return ActiveLease(
            lease_id=lease.lease_id,
            pid=lease.peer.pid,
            uid=lease.peer.uid,
            label=lease.label,
            elapsed_seconds=elapsed,
        )

    def _verify_held_policy(self) -> None:
        active = self.scheduler.snapshot.active
        if active is None:
            return
        now = self.monotonic()
        if now - self._last_policy_verification < (POLICY_VERIFICATION_SECONDS):
            return
        self._last_policy_verification = now
        ticket = self._tickets[active.lease_id]
        if ticket.policy_invalidated_at is not None:
            if ticket.term_sent_at is None:
                if self._signal_invalid_owner(ticket, signal.SIGTERM):
                    ticket.term_sent_at = now
            escalation_start = (
                ticket.policy_invalidated_at
                if ticket.term_sent_at is None
                else ticket.term_sent_at
            )
            if (
                not ticket.kill_sent
                and now - escalation_start >= POLICY_FAILURE_GRACE_SECONDS
            ):
                if self._signal_invalid_owner(ticket, signal.SIGKILL):
                    ticket.kill_sent = True
            return
        try:
            self.policy.verify()
        except BenchmarkLockError as error:
            self.report(
                "benchmark policy drift invalidated active lease "
                f"{active.lease_id}: {error}"
            )
            self._invalidate_active(ticket)

    def _invalidate_active(self, ticket: _Ticket) -> None:
        if ticket.policy_invalidated_at is None:
            ticket.policy_invalidated_at = self.monotonic()
        if ticket.term_sent_at is not None:
            return
        if self._signal_invalid_owner(ticket, signal.SIGTERM):
            ticket.term_sent_at = self.monotonic()

    def _signal_invalid_owner(
        self,
        ticket: _Ticket,
        signal_number: int,
    ) -> bool:
        try:
            self.signal_owner(ticket.identity.pid_descriptor, signal_number)
        except BenchmarkLockError as error:
            action = "terminate" if signal_number == signal.SIGTERM else "kill"
            self.report(
                f"cannot {action} invalid benchmark owner "
                f"{ticket.lease.lease_id if ticket.lease is not None else 'unknown'}: "
                f"{error}"
            )
            return False
        return True

    def _send_error_to_ticket(
        self,
        ticket: _Ticket,
        error: BenchmarkLockError,
    ) -> None:
        if ticket.connection is not None:
            self._send_error(ticket.connection, error)

    @staticmethod
    def _send_error(
        connection: socket.socket,
        error: BenchmarkLockError,
    ) -> None:
        try:
            send_event(
                connection,
                ErrorEvent(code=error.code, message=str(error)),
            )
        except BenchmarkLockError:
            return

    def _expire_unadmitted_connections(self) -> None:
        now = self.monotonic()
        for ticket in tuple(self._accepted.values()):
            if ticket.maintenance_owner is not None:
                continue
            if now - ticket.accepted_at >= REQUEST_TIMEOUT_SECONDS:
                self._send_error_to_ticket(
                    ticket,
                    BenchmarkLockError(
                        "benchmark request did not arrive in time",
                        code="benchmark_request_timeout",
                    ),
                )
                self._discard_unadmitted(ticket)

    def _discard_unadmitted(self, ticket: _Ticket) -> None:
        if ticket.connection is not None:
            self._accepted.pop(ticket.connection.fileno(), None)
        self._cleanup_ticket(ticket)

    def _close_ticket_channel(self, ticket: _Ticket) -> None:
        connection = ticket.connection
        if connection is None:
            return
        self._unregister(connection)
        connection.close()
        ticket.connection = None

    def _cleanup_ticket(self, ticket: _Ticket) -> None:
        maintenance_owner = ticket.maintenance_owner
        if maintenance_owner is not None:
            self.scheduler.leave_maintenance(maintenance_owner)
            ticket.maintenance_owner = None
        lease = ticket.lease
        if lease is not None:
            self._tickets.pop(lease.lease_id, None)
        if ticket.connection is not None:
            self._accepted.pop(ticket.connection.fileno(), None)
        self._close_ticket_channel(ticket)
        descriptor = ticket.identity.pid_descriptor
        if descriptor >= 0:
            self._unregister(descriptor)
        ticket.identity.close()

    def _release_persistence(self, lease_id: str) -> None:
        try:
            self.persistence.release(lease_id)
        except BenchmarkLockError as error:
            self.report(f"cannot release stored benchmark lease {lease_id}: {error}")
            raise

    def _unregister(self, file_object: object) -> None:
        try:
            self.selector.unregister(file_object)
        except (KeyError, ValueError):
            return

    def _close_runtime(self, *, clean: bool) -> None:
        if self._closed:
            return
        self._closed = True
        failures: list[tuple[str, Exception]] = []
        try:
            if clean:
                self._clean_shutdown(failures)
            else:
                active = self.scheduler.snapshot.active
                active_ticket = (
                    None if active is None else self._tickets.get(active.lease_id)
                )
                if active_ticket is not None:
                    self._invalidate_active(active_ticket)
        except Exception as error:
            self._record_shutdown_failure(
                failures,
                "shutdown orchestration",
                error,
            )
        finally:
            self._close_runtime_descriptors(failures)
        if failures:
            self._raise_shutdown_failures(failures, clean=clean)

    def _clean_shutdown(
        self,
        failures: list[tuple[str, Exception]],
    ) -> None:
        """Attempt every terminal obligation without letting one hide another."""

        snapshot = self.scheduler.snapshot
        waiting = (
            () if snapshot.preparing is None else (snapshot.preparing,)
        ) + snapshot.queued
        for lease in waiting:
            ticket = self._tickets.get(lease.lease_id)
            if ticket is not None:
                self._send_error_to_ticket(
                    ticket,
                    BenchmarkLockError(
                        "benchmark service is stopping",
                        code="benchmark_broker_stopping",
                    ),
                )
            try:
                canceled = self.scheduler.cancel(lease.lease_id)
                if canceled is None:
                    raise BenchmarkLockError(
                        "clean shutdown could not cancel a live waiter",
                        code="invalid_scheduler_transition",
                    )
            except Exception as error:
                self._record_shutdown_failure(
                    failures,
                    f"cancel waiter {lease.lease_id}",
                    error,
                )
            try:
                self._release_persistence(lease.lease_id)
            except Exception as error:
                self._record_shutdown_failure(
                    failures,
                    f"release waiter {lease.lease_id}",
                    error,
                )

        active = self.scheduler.snapshot.active
        owner_exited = active is None
        if active is not None:
            ticket = self._tickets.get(active.lease_id)
            if ticket is None:
                self._record_shutdown_failure(
                    failures,
                    f"locate active owner {active.lease_id}",
                    BenchmarkLockError(
                        "clean shutdown has no exact active-owner descriptor",
                        code="invalid_scheduler_transition",
                    ),
                )
                owner_exited = False
            else:
                owner_exited = self._terminate_active_for_clean_shutdown(
                    ticket,
                    failures,
                )
            if owner_exited:
                try:
                    self.scheduler.complete_active(active.lease_id)
                except Exception as error:
                    self._record_shutdown_failure(
                        failures,
                        f"complete active lease {active.lease_id}",
                        error,
                    )
                try:
                    self._release_persistence(active.lease_id)
                except Exception as error:
                    self._record_shutdown_failure(
                        failures,
                        f"release active lease {active.lease_id}",
                        error,
                    )

        if self.policy.state != "idle":
            if owner_exited:
                try:
                    self.policy.leave()
                except Exception as error:
                    self._record_shutdown_failure(
                        failures,
                        "restore host policy",
                        error,
                    )
            else:
                self._record_shutdown_failure(
                    failures,
                    "restore host policy",
                    BenchmarkLockError(
                        "host policy remains held because exact owner exit "
                        "was not confirmed",
                        code="benchmark_owner_wait_failed",
                    ),
                )

    def _terminate_active_for_clean_shutdown(
        self,
        ticket: _Ticket,
        failures: list[tuple[str, Exception]],
    ) -> bool:
        descriptor = ticket.identity.pid_descriptor
        lease_id = "unknown" if ticket.lease is None else ticket.lease.lease_id
        prior_term_sent_at = ticket.term_sent_at
        if prior_term_sent_at is None:
            try:
                self.signal_owner(descriptor, signal.SIGTERM)
                ticket.term_sent_at = self.monotonic()
                if ticket.policy_invalidated_at is None:
                    ticket.policy_invalidated_at = ticket.term_sent_at
            except Exception as error:
                self._record_shutdown_failure(
                    failures,
                    f"terminate active owner {lease_id}",
                    error,
                )
        grace_seconds = POLICY_FAILURE_GRACE_SECONDS
        if prior_term_sent_at is not None:
            grace_seconds = max(
                0.0,
                POLICY_FAILURE_GRACE_SECONDS - (self.monotonic() - prior_term_sent_at),
            )
        try:
            if self.wait_owner(descriptor, grace_seconds):
                return True
        except Exception as error:
            self._record_shutdown_failure(
                failures,
                f"wait for active owner {lease_id}",
                error,
            )
        try:
            self.signal_owner(descriptor, signal.SIGKILL)
            ticket.kill_sent = True
        except Exception as error:
            self._record_shutdown_failure(
                failures,
                f"kill active owner {lease_id}",
                error,
            )
            try:
                return self.wait_owner(descriptor, 0.0)
            except Exception as wait_error:
                self._record_shutdown_failure(
                    failures,
                    f"confirm active owner exit {lease_id}",
                    wait_error,
                )
                return False
        try:
            if self.wait_owner(descriptor, None):
                return True
            error = BenchmarkLockError(
                "indefinite owner wait returned before process exit",
                code="benchmark_owner_wait_failed",
            )
        except Exception as wait_error:
            error = wait_error
        self._record_shutdown_failure(
            failures,
            f"wait for killed active owner {lease_id}",
            error,
        )
        return False

    def _close_runtime_descriptors(
        self,
        failures: list[tuple[str, Exception]],
    ) -> None:
        tickets = tuple(self._tickets.values()) + tuple(self._accepted.values())
        for ticket in tickets:
            try:
                self._cleanup_ticket(ticket)
            except Exception as error:
                self._record_shutdown_failure(
                    failures,
                    "close benchmark ticket descriptors",
                    error,
                )
                try:
                    self._close_ticket_channel(ticket)
                    descriptor = ticket.identity.pid_descriptor
                    if descriptor >= 0:
                        self._unregister(descriptor)
                    ticket.identity.close()
                except Exception as close_error:
                    self._record_shutdown_failure(
                        failures,
                        "force-close benchmark ticket descriptors",
                        close_error,
                    )
        self._tickets.clear()
        self._accepted.clear()
        try:
            self._unregister(self.listener)
        except Exception as error:
            self._record_shutdown_failure(
                failures,
                "unregister benchmark listener",
                error,
            )
        try:
            self.selector.close()
        except Exception as error:
            self._record_shutdown_failure(
                failures,
                "close benchmark selector",
                error,
            )

    def _record_shutdown_failure(
        self,
        failures: list[tuple[str, Exception]],
        operation: str,
        error: Exception,
    ) -> None:
        failures.append((operation, error))
        code = getattr(error, "code", error.__class__.__name__)
        try:
            self.report(f"{operation} failed: {code}: {error}")
        except Exception as report_error:
            failures.append((f"report {operation} failure", report_error))

    @staticmethod
    def _raise_shutdown_failures(
        failures: list[tuple[str, Exception]],
        *,
        clean: bool,
    ) -> None:
        details = "; ".join(
            f"{operation}: {getattr(error, 'code', error.__class__.__name__)}: {error}"
            for operation, error in failures
        )
        error = BenchmarkLockError(
            (
                "benchmark clean shutdown was incomplete: "
                if clean
                else "benchmark runtime cleanup was incomplete: "
            )
            + details,
            code=(
                "benchmark_clean_shutdown_failed"
                if clean
                else "benchmark_runtime_cleanup_failed"
            ),
        )
        causes = [failure for _operation, failure in failures]
        error.failures = tuple(failures)
        raise error from causes[0]
