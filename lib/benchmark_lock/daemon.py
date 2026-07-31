"""Privileged benchmark broker process and crash-recovery composition."""

from __future__ import annotations

import errno
import os
import pathlib
import signal
import socket
import stat
import sys
import traceback
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from typing import Protocol

from .broker import (
    BenchmarkBroker,
    RecoveredBrokerTicket,
    wait_for_pidfd,
)
from .configuration import (
    CONFIG_PATH as POLICY_CONFIGURATION_PATH,
    MAX_CONFIG_BYTES as MAX_POLICY_CONFIGURATION_BYTES,
    parse_policy_configuration,
)
from .errors import BenchmarkLockError
from .fdstore import (
    ActivationState,
    DescriptorNotifier,
    SystemdLeasePersistence,
    SystemdNotifier,
    recover_activation,
)
from .linux import PeerCredentials
from .linux import PeerIdentity as LinuxPeerIdentity
from .policy import (
    EpochJournal,
    FixedHostPolicy,
    FixedHostPolicyConfig,
    GioPowerProfilesBackend,
    LinuxHostFilesystem,
)
from .scheduler import LeaseScheduler, LeaseState


POLICY_JOURNAL_PATH = pathlib.Path("/var/lib/benchmarkd/active-epoch.json")


class RecoverableHostPolicy(Protocol):
    """Host policy with the startup recovery operation used by the daemon."""

    def recover(self) -> None:
        """Restore any durable pre-crash policy epoch."""


class ServiceNotifier(DescriptorNotifier, Protocol):
    """Descriptor store plus the service lifecycle notifications we emit."""

    def ready(self) -> None:
        """Publish that recovery is complete and requests may be accepted."""

    def stopping(self) -> None:
        """Publish that the broker has entered its bounded shutdown path."""


class BrokerRuntime(Protocol):
    """Broker operations needed by the daemon lifecycle."""

    def run(self, *, ready: Callable[[], None]) -> None:
        """Prepare runtime descriptors, publish readiness, and serve."""

    def request_clean_stop(self) -> None:
        """Request a policy-restoring, persistence-releasing stop."""


class SystemdServiceNotifier(SystemdNotifier):
    """Production systemd fd-store and service-lifecycle notifier."""

    def ready(self) -> None:
        self._notify("READY=1")

    def stopping(self) -> None:
        self._notify("STOPPING=1")


PolicyConfigurationParser = Callable[[bytes], FixedHostPolicyConfig]
PolicyFactory = Callable[[FixedHostPolicyConfig], RecoverableHostPolicy]
BrokerFactory = Callable[..., BrokerRuntime]
OwnerSignaler = Callable[[int, int], None]
OwnerWaiter = Callable[[int, float | None], bool]
BrokerProvider = Callable[[], BrokerRuntime | None]
Reporter = Callable[[str], None]


def _configuration_error(message: str) -> BenchmarkLockError:
    return BenchmarkLockError(
        message,
        code="invalid_benchmark_policy_configuration",
    )


def _stderr_report(message: str) -> None:
    print(f"benchmarkd: {message}", file=sys.stderr, flush=True)


def _report_activation_recovery(
    activation: ActivationState,
    report: Reporter,
) -> None:
    for reaped in activation.reaped:
        descriptor_names = ", ".join(reaped.descriptor_names)
        report(
            f"reaped stored lease {reaped.lease_id} during activation "
            f"({reaped.reason.value}); removed {descriptor_names}"
        )
    for descriptor_name in activation.discarded_descriptor_names:
        report(f"discarded stale stored descriptor {descriptor_name} during activation")


def _validate_configuration_metadata(
    metadata: os.stat_result,
    *,
    owner_uid: int,
) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise _configuration_error(
            "benchmark policy configuration is not a regular file"
        )
    if metadata.st_uid != owner_uid:
        raise _configuration_error("benchmark policy configuration has the wrong owner")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise _configuration_error("benchmark policy configuration mode is not 0600")
    if metadata.st_size < 1:
        raise _configuration_error("benchmark policy configuration is empty")
    if metadata.st_size > MAX_POLICY_CONFIGURATION_BYTES:
        raise _configuration_error(
            "benchmark policy configuration exceeds its fixed size limit"
        )


def read_policy_configuration(
    path: pathlib.Path,
    *,
    parser: PolicyConfigurationParser,
    owner_uid: int = 0,
) -> FixedHostPolicyConfig:
    """Read one exact root-owned policy configuration without following links."""

    candidate = pathlib.Path(path)
    if (
        not candidate.is_absolute()
        or pathlib.Path(os.path.normpath(candidate)) != candidate
    ):
        raise _configuration_error(
            "benchmark policy configuration path is not canonical"
        )
    if isinstance(owner_uid, bool) or not isinstance(owner_uid, int) or owner_uid < 0:
        raise ValueError("benchmark policy configuration owner UID is invalid")
    try:
        before = os.lstat(candidate)
    except OSError as error:
        raise _configuration_error(
            f"cannot inspect benchmark policy configuration: {error}"
        ) from error
    _validate_configuration_metadata(before, owner_uid=owner_uid)

    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(candidate, flags)
    except OSError as error:
        raise _configuration_error(
            f"cannot open benchmark policy configuration: {error}"
        ) from error
    try:
        opened = os.fstat(descriptor)
        _validate_configuration_metadata(opened, owner_uid=owner_uid)
        if opened.st_dev != before.st_dev or opened.st_ino != before.st_ino:
            raise _configuration_error(
                "benchmark policy configuration changed while it was opened"
            )
        chunks: list[bytes] = []
        remaining = opened.st_size + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        _validate_configuration_metadata(after, owner_uid=owner_uid)
        if (
            len(payload) != opened.st_size
            or after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or after.st_ctime_ns != opened.st_ctime_ns
        ):
            raise _configuration_error(
                "benchmark policy configuration changed while it was read"
            )
    except OSError as error:
        raise _configuration_error(
            f"cannot read benchmark policy configuration: {error}"
        ) from error
    finally:
        os.close(descriptor)

    try:
        configuration = parser(payload)
    except BenchmarkLockError:
        raise
    except Exception as error:
        raise _configuration_error(
            f"cannot parse benchmark policy configuration: {error}"
        ) from error
    if not isinstance(configuration, FixedHostPolicyConfig):
        raise _configuration_error(
            "benchmark policy configuration parser returned the wrong type"
        )
    return configuration


def _default_configuration_parser(payload: bytes) -> FixedHostPolicyConfig:
    """Parse policy without loading the administrator mutation surface."""

    return parse_policy_configuration(payload)


def _production_policy(
    configuration: FixedHostPolicyConfig,
) -> FixedHostPolicy:
    return FixedHostPolicy(
        configuration,
        power_profiles=GioPowerProfilesBackend(),
        filesystem=LinuxHostFilesystem(),
        journal=EpochJournal(POLICY_JOURNAL_PATH),
    )


def _signal_pidfd(descriptor: int, signal_number: int) -> None:
    if not hasattr(signal, "pidfd_send_signal"):
        raise BenchmarkLockError(
            "safe recovered-owner invalidation requires pidfd_send_signal",
            code="benchmark_platform_unsupported",
        )
    try:
        signal.pidfd_send_signal(descriptor, signal_number)
    except ProcessLookupError:
        return
    except OSError as error:
        raise BenchmarkLockError(
            f"cannot invalidate recovered benchmark owner: {error}",
            code="benchmark_owner_signal_failed",
        ) from error


def _close_descriptor(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError as error:
        if error.errno != errno.EBADF:
            raise


def _close_record_descriptors(activation: ActivationState) -> None:
    first_error: OSError | None = None
    for recovered in activation.tickets:
        try:
            os.close(recovered.record_descriptor)
        except OSError as error:
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise BenchmarkLockError(
            f"cannot close recovered benchmark record descriptor: {first_error}",
            code="invalid_fdstore_activation",
        ) from first_error


def _activation_descriptors(
    activation: ActivationState,
) -> set[int]:
    return {
        activation.control_descriptor,
        *(recovered.owner_descriptor for recovered in activation.tickets),
        *(recovered.record_descriptor for recovered in activation.tickets),
        *(
            recovered.channel_descriptor
            for recovered in activation.tickets
            if recovered.channel_descriptor is not None
        ),
    }


def _invalidate_recovered_active(
    activation: ActivationState,
    *,
    signal_owner: OwnerSignaler,
    wait_owner: OwnerWaiter,
) -> None:
    active = tuple(
        recovered
        for recovered in activation.tickets
        if recovered.lease.state is LeaseState.ACTIVE
    )
    if not active:
        return
    if len(active) != 1:
        raise BenchmarkLockError(
            "activation contains more than one active benchmark owner",
            code="invalid_fdstore_activation",
        )
    descriptor = active[0].owner_descriptor
    signal_owner(descriptor, signal.SIGKILL)
    if not wait_owner(descriptor, None):
        raise BenchmarkLockError(
            "recovered active benchmark owner did not exit",
            code="benchmark_owner_wait_failed",
        )


def _restore_scheduler(activation: ActivationState) -> LeaseScheduler:
    active = tuple(
        recovered.lease
        for recovered in activation.tickets
        if recovered.lease.state is LeaseState.ACTIVE
    )
    queued = tuple(
        sorted(
            (
                recovered.lease
                for recovered in activation.tickets
                if recovered.lease.state is LeaseState.QUEUED
            ),
            key=lambda lease: lease.sequence,
        )
    )
    scheduler = LeaseScheduler()
    scheduler.restore(
        active=(None if not active else active[0]),
        preparing=None,
        queued=queued,
        next_sequence=max(
            (recovered.lease.sequence for recovered in activation.tickets),
            default=0,
        )
        + 1,
    )
    return scheduler


def _adopt_broker_tickets(
    activation: ActivationState,
) -> tuple[RecoveredBrokerTicket, ...]:
    tickets: list[RecoveredBrokerTicket] = []
    try:
        for recovered in activation.tickets:
            peer = recovered.lease.peer
            connection = (
                None
                if recovered.channel_descriptor is None
                else socket.socket(fileno=recovered.channel_descriptor)
            )
            tickets.append(
                RecoveredBrokerTicket(
                    lease=recovered.lease,
                    identity=LinuxPeerIdentity(
                        credentials=PeerCredentials(
                            pid=peer.pid,
                            uid=peer.uid,
                            gid=peer.gid,
                        ),
                        pid_descriptor=recovered.owner_descriptor,
                    ),
                    connection=connection,
                )
            )
    except Exception:
        _close_broker_tickets(tuple(tickets))
        raise
    return tuple(tickets)


def _close_broker_tickets(
    tickets: tuple[RecoveredBrokerTicket, ...],
) -> None:
    for ticket in tickets:
        if ticket.connection is not None:
            ticket.connection.close()
        ticket.identity.close()


@contextmanager
def _termination_handlers(
    broker: BrokerProvider,
    lifecycle: _ServiceLifecycle,
):
    previous: dict[int, signal.Handlers] = {}

    def request_clean_stop(
        _signal_number: int,
        _frame: object,
    ) -> None:
        lifecycle.request_stop(broker())

    try:
        for signal_number in (signal.SIGTERM, signal.SIGINT):
            previous[signal_number] = signal.signal(
                signal_number,
                request_clean_stop,
            )
        yield
    finally:
        for signal_number, handler in previous.items():
            signal.signal(signal_number, handler)


class _ServiceLifecycle:
    """Order READY/STOPPING exactly once around asynchronous termination."""

    def __init__(self, notifier: ServiceNotifier) -> None:
        self._notifier = notifier
        self.ready_sent = False
        self.stopping_sent = False
        self.termination_requested = False

    def send_ready(self) -> None:
        if self.ready_sent:
            return
        if self.termination_requested:
            self.send_stopping()
            return
        self._notifier.ready()
        self.ready_sent = True
        if self.termination_requested:
            self.send_stopping()

    def send_stopping(self) -> None:
        if self.stopping_sent:
            return
        self._notifier.stopping()
        self.stopping_sent = True

    def request_stop(self, broker: BrokerRuntime | None) -> None:
        self.termination_requested = True
        if broker is not None:
            broker.request_clean_stop()
        if self.ready_sent:
            self.send_stopping()


def run_daemon(
    notifier: ServiceNotifier,
    *,
    configuration_path: pathlib.Path = POLICY_CONFIGURATION_PATH,
    configuration_parser: PolicyConfigurationParser = _default_configuration_parser,
    policy_factory: PolicyFactory = _production_policy,
    broker_factory: BrokerFactory = BenchmarkBroker,
    effective_uid: int | None = None,
    configuration_owner_uid: int = 0,
    signal_owner: OwnerSignaler = _signal_pidfd,
    wait_owner: OwnerWaiter = wait_for_pidfd,
    report: Reporter = _stderr_report,
    manage_signals: bool = True,
) -> None:
    """Recover all authority, publish readiness, and run the root broker."""

    observed_uid = os.geteuid() if effective_uid is None else effective_uid
    if observed_uid != 0:
        raise BenchmarkLockError(
            "benchmarkd must run as root",
            code="benchmark_admin_required",
        )
    listener: socket.socket | None = None
    tickets: tuple[RecoveredBrokerTicket, ...] = ()
    raw_descriptors: set[int] = set()
    lifecycle = _ServiceLifecycle(notifier)
    broker: BrokerRuntime | None = None
    handlers = (
        _termination_handlers(lambda: broker, lifecycle)
        if manage_signals
        else nullcontext()
    )
    with handlers:
        try:
            activation = recover_activation(notifier)
            raw_descriptors = _activation_descriptors(activation)
            _close_record_descriptors(activation)
            for recovered in activation.tickets:
                raw_descriptors.remove(recovered.record_descriptor)
            _report_activation_recovery(activation, report)
            _invalidate_recovered_active(
                activation,
                signal_owner=signal_owner,
                wait_owner=wait_owner,
            )
            configuration = read_policy_configuration(
                configuration_path,
                parser=configuration_parser,
                owner_uid=configuration_owner_uid,
            )
            policy = policy_factory(configuration)
            policy.recover()
            scheduler = _restore_scheduler(activation)

            listener = socket.socket(fileno=activation.control_descriptor)
            raw_descriptors.remove(activation.control_descriptor)
            tickets = _adopt_broker_tickets(activation)
            for ticket in tickets:
                raw_descriptors.remove(ticket.identity.pid_descriptor)
                if ticket.connection is not None:
                    raw_descriptors.remove(ticket.connection.fileno())
            broker = broker_factory(
                listener=listener,
                policy=policy,
                persistence=SystemdLeasePersistence(notifier),
                scheduler=scheduler,
                recovered_tickets=tickets,
                signal_owner=signal_owner,
                wait_owner=wait_owner,
                report=report,
            )
            if lifecycle.termination_requested:
                broker.request_clean_stop()
            broker.run(ready=lifecycle.send_ready)
            lifecycle.send_stopping()
        finally:
            try:
                if lifecycle.ready_sent or lifecycle.termination_requested:
                    lifecycle.send_stopping()
            finally:
                _close_broker_tickets(tickets)
                if listener is not None:
                    listener.close()
                for descriptor in raw_descriptors:
                    _close_descriptor(descriptor)


def main() -> int:
    """Installed benchmarkd entry point."""

    try:
        run_daemon(SystemdServiceNotifier())
    except BenchmarkLockError as error:
        print(
            f"benchmarkd: {error.code}: {error}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    except Exception:
        traceback.print_exc()
        return 1
    return 0
