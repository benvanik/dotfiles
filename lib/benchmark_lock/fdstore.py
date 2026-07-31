"""Crash-safe benchmark ticket ownership through systemd's fd store."""

from __future__ import annotations

import ctypes
import dataclasses
import enum
import fcntl
import json
import math
import os
import re
import select
import socket
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import BenchmarkLockError
from .linux import peer_credentials
from .protocol import (
    MAX_PACKET_BYTES,
    AcquireRequest,
    canonical_json_bytes,
    encode_request,
    require_seqpacket_channel,
)
from .scheduler import MAX_TICKETS, Lease, LeaseState, PeerIdentity


CONTROL_DESCRIPTOR_NAME = "benchmarkd.control"
TICKET_RECORD_SCHEMA = "benchmarkd.ticket.v1"

# Every queued ticket owns a pidfd, a client channel, and its immutable
# admission record. The one active ticket may transiently retain that queued
# record while its active record is committed. This is the exact value for
# systemd's FileDescriptorStoreMax= setting.
QUEUED_TICKET_DESCRIPTOR_COUNT = 3
ACTIVE_RECORD_TRANSITION_HEADROOM = 1
FILE_DESCRIPTOR_STORE_MAX = (
    MAX_TICKETS * QUEUED_TICKET_DESCRIPTOR_COUNT + ACTIVE_RECORD_TRANSITION_HEADROOM
)

_BARRIER_TIMEOUT_MICROSECONDS = (1 << 64) - 1
_TICKET_DESCRIPTOR = re.compile(
    r"^ticket\.([0-9a-f]{32})\."
    r"(owner|channel|queued-record|active-record)$"
)
_REQUIRED_RECORD_SEALS = (
    fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
)
_TICKET_RECORD_FIELDS = frozenset(
    {
        "enqueued_at",
        "inherited_lease_id",
        "label",
        "lease_id",
        "peer",
        "schema",
        "sequence",
        "started_at",
        "state",
    }
)
_PEER_FIELDS = frozenset({"gid", "pid", "uid"})


class DescriptorRole(enum.StrEnum):
    """One named member of a persisted ticket closure."""

    OWNER = "owner"
    CHANNEL = "channel"
    QUEUED_RECORD = "queued-record"
    ACTIVE_RECORD = "active-record"


class ReapReason(enum.StrEnum):
    """Why an activation-time ticket closure cannot be resumed."""

    UNCOMMITTED = "uncommitted"
    OWNER_MISSING = "owner-missing"
    OWNER_EXITED = "owner-exited"
    CHANNEL_MISSING = "channel-missing"
    CHANNEL_CLOSED = "channel-closed"


@dataclass(frozen=True)
class NamedDescriptor:
    """One activation descriptor and its exact systemd name."""

    descriptor: int
    name: str


@dataclass(frozen=True)
class RecoveredTicket:
    """A complete scheduler-restorable ticket closure."""

    lease: Lease
    owner_descriptor: int
    channel_descriptor: int | None
    record_descriptor: int


@dataclass(frozen=True)
class ReapedTicket:
    """A terminal or incomplete closure removed during recovery."""

    lease_id: str
    reason: ReapReason
    descriptor_names: tuple[str, ...]


@dataclass(frozen=True)
class ActivationState:
    """Validated listener and live tickets received during activation."""

    control_descriptor: int
    tickets: tuple[RecoveredTicket, ...]
    reaped: tuple[ReapedTicket, ...]
    discarded_descriptor_names: tuple[str, ...]


class DescriptorNotifier(Protocol):
    """Minimal systemd notification boundary used by fd-store logic."""

    def activation_descriptors(self) -> tuple[NamedDescriptor, ...]:
        """Consume and return this process's named activation descriptors."""

    def store_descriptor(self, name: str, descriptor: int) -> None:
        """Submit exactly one descriptor under one unique name."""

    def remove_descriptor(self, name: str) -> None:
        """Remove every stored descriptor carrying ``name``."""

    def barrier(self) -> None:
        """Wait until the service manager consumed prior notifications."""


BarrierFunction = Callable[[int, int], int]


def _load_systemd_daemon() -> Any:
    try:
        from systemd import daemon
    except ImportError as error:
        raise BenchmarkLockError(
            "systemd-python is unavailable for benchmark fd storage",
            code="benchmark_systemd_unavailable",
        ) from error
    return daemon


def _load_notify_barrier() -> BarrierFunction:
    try:
        library = ctypes.CDLL("libsystemd.so.0", use_errno=True)
        function = library.sd_notify_barrier
    except (AttributeError, OSError) as error:
        raise BenchmarkLockError(
            "libsystemd does not provide sd_notify_barrier",
            code="benchmark_systemd_unavailable",
        ) from error
    function.argtypes = (ctypes.c_int, ctypes.c_uint64)
    function.restype = ctypes.c_int
    return function


def _require_descriptor_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or not name
        or len(name.encode("ascii", errors="ignore")) != len(name)
        or len(name) > 255
        or ":" in name
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in name)
    ):
        raise BenchmarkLockError(
            "benchmark fd-store descriptor name is invalid",
            code="invalid_fdstore_name",
        )


class SystemdNotifier:
    """systemd-python notifications with a libsystemd barrier."""

    def __init__(
        self,
        *,
        daemon_module: Any | None = None,
        barrier_function: BarrierFunction | None = None,
    ) -> None:
        self._daemon = (
            _load_systemd_daemon() if daemon_module is None else daemon_module
        )
        self._barrier = (
            _load_notify_barrier() if barrier_function is None else barrier_function
        )

    def activation_descriptors(self) -> tuple[NamedDescriptor, ...]:
        """Consume LISTEN_FDS/LISTEN_FDNAMES through systemd-python."""

        try:
            descriptors = self._daemon.listen_fds_with_names(unset_environment=True)
        except Exception as error:
            raise BenchmarkLockError(
                f"cannot receive benchmark activation descriptors: {error}",
                code="invalid_fdstore_activation",
            ) from error
        if not isinstance(descriptors, Mapping):
            raise BenchmarkLockError(
                "systemd returned an invalid activation descriptor map",
                code="invalid_fdstore_activation",
            )
        result: list[NamedDescriptor] = []
        for descriptor, name in descriptors.items():
            if (
                isinstance(descriptor, bool)
                or not isinstance(descriptor, int)
                or descriptor < 0
                or not isinstance(name, str)
            ):
                raise BenchmarkLockError(
                    "systemd returned an invalid named descriptor",
                    code="invalid_fdstore_activation",
                )
            result.append(NamedDescriptor(descriptor, name))
        return tuple(result)

    def _notify(
        self,
        fields: str,
        *,
        descriptor: int | None = None,
    ) -> None:
        try:
            if descriptor is None:
                delivered = self._daemon.notify(
                    fields,
                    unset_environment=False,
                )
            else:
                delivered = self._daemon.notify(
                    fields,
                    unset_environment=False,
                    fds=[descriptor],
                )
        except Exception as error:
            raise BenchmarkLockError(
                f"cannot notify systemd about benchmark descriptors: {error}",
                code="benchmark_fdstore_unavailable",
            ) from error
        if not delivered:
            raise BenchmarkLockError(
                "systemd did not accept the benchmark notification",
                code="benchmark_fdstore_unavailable",
            )

    def store_descriptor(self, name: str, descriptor: int) -> None:
        """Send one FDSTORE message containing exactly one descriptor."""

        _require_descriptor_name(name)
        _require_open_descriptor(descriptor)
        self._notify(
            f"FDSTORE=1\nFDNAME={name}",
            descriptor=descriptor,
        )

    def remove_descriptor(self, name: str) -> None:
        """Remove one uniquely named role from the service's fd store."""

        _require_descriptor_name(name)
        self._notify(f"FDSTOREREMOVE=1\nFDNAME={name}")

    def barrier(self) -> None:
        """Synchronize with systemd using the native barrier operation."""

        try:
            result = self._barrier(0, _BARRIER_TIMEOUT_MICROSECONDS)
        except Exception as error:
            raise BenchmarkLockError(
                f"cannot execute the systemd notification barrier: {error}",
                code="benchmark_fdstore_unavailable",
            ) from error
        if result <= 0:
            detail = (
                "notification socket is unavailable"
                if result == 0
                else os.strerror(-result)
            )
            raise BenchmarkLockError(
                f"systemd notification barrier failed: {detail}",
                code="benchmark_fdstore_unavailable",
            )


def ticket_descriptor_name(lease_id: str, role: DescriptorRole) -> str:
    """Return one exact immutable role name for ``lease_id``."""

    _validate_protocol_identity(lease_id, "benchmark ticket")
    return f"ticket.{lease_id}.{role.value}"


def _require_open_descriptor(descriptor: int) -> None:
    if (
        isinstance(descriptor, bool)
        or not isinstance(descriptor, int)
        or descriptor < 0
    ):
        raise BenchmarkLockError(
            "benchmark fd-store descriptor number is invalid",
            code="invalid_fdstore_descriptor",
        )
    try:
        os.fstat(descriptor)
    except OSError as error:
        raise BenchmarkLockError(
            f"benchmark fd-store descriptor is not open: {error}",
            code="invalid_fdstore_descriptor",
        ) from error


def _validate_protocol_identity(lease_id: str, label: str) -> None:
    try:
        encode_request(
            AcquireRequest(
                label=label,
                inherited_lease_id=lease_id,
            )
        )
    except BenchmarkLockError as error:
        raise BenchmarkLockError(
            "benchmark fd-store ticket identity is invalid",
            code="invalid_fdstore_ticket",
        ) from error


def _validate_lease(lease: Lease, *, state: LeaseState) -> Lease:
    if not isinstance(lease, Lease):
        raise BenchmarkLockError(
            "benchmark fd-store record is not a scheduler lease",
            code="invalid_fdstore_ticket",
        )
    _validate_protocol_identity(lease.lease_id, lease.label)
    if lease.inherited_lease_id is not None:
        _validate_protocol_identity(
            lease.inherited_lease_id,
            lease.label,
        )
    try:
        enqueued_at_is_finite = math.isfinite(lease.enqueued_at)
    except (OverflowError, TypeError):
        enqueued_at_is_finite = False
    if (
        isinstance(lease.sequence, bool)
        or not isinstance(lease.sequence, int)
        or lease.sequence < 1
        or not isinstance(lease.peer, PeerIdentity)
        or isinstance(lease.peer.pid, bool)
        or not isinstance(lease.peer.pid, int)
        or lease.peer.pid < 1
        or isinstance(lease.peer.uid, bool)
        or not isinstance(lease.peer.uid, int)
        or lease.peer.uid < 0
        or isinstance(lease.peer.gid, bool)
        or not isinstance(lease.peer.gid, int)
        or lease.peer.gid < 0
        or isinstance(lease.enqueued_at, bool)
        or not isinstance(lease.enqueued_at, (int, float))
        or not enqueued_at_is_finite
        or lease.enqueued_at < 0
    ):
        raise BenchmarkLockError(
            "benchmark fd-store ticket fields are invalid",
            code="invalid_fdstore_ticket",
        )
    if state is LeaseState.QUEUED:
        if lease.state not in {LeaseState.QUEUED, LeaseState.PREPARING}:
            raise BenchmarkLockError(
                "queued fd-store record does not describe a queued lease",
                code="invalid_fdstore_ticket",
            )
        return dataclasses.replace(
            lease,
            state=LeaseState.QUEUED,
            started_at=None,
            enqueued_at=float(lease.enqueued_at),
        )
    try:
        started_at_is_finite = math.isfinite(lease.started_at)
    except (OverflowError, TypeError):
        started_at_is_finite = False
    if (
        lease.state is not LeaseState.ACTIVE
        or isinstance(lease.started_at, bool)
        or not isinstance(lease.started_at, (int, float))
        or not started_at_is_finite
        or lease.started_at < lease.enqueued_at
    ):
        raise BenchmarkLockError(
            "active fd-store record does not describe an active lease",
            code="invalid_fdstore_ticket",
        )
    return dataclasses.replace(
        lease,
        enqueued_at=float(lease.enqueued_at),
        started_at=float(lease.started_at),
    )


def _ticket_document(lease: Lease) -> dict[str, Any]:
    return {
        "enqueued_at": lease.enqueued_at,
        "inherited_lease_id": lease.inherited_lease_id,
        "label": lease.label,
        "lease_id": lease.lease_id,
        "peer": {
            "gid": lease.peer.gid,
            "pid": lease.peer.pid,
            "uid": lease.peer.uid,
        },
        "schema": TICKET_RECORD_SCHEMA,
        "sequence": lease.sequence,
        "started_at": lease.started_at,
        "state": lease.state.value,
    }


def _strict_document(payload: bytes) -> dict[str, Any]:
    if not payload or payload[-1:] != b"\n" or len(payload) > MAX_PACKET_BYTES:
        raise BenchmarkLockError(
            "benchmark fd-store record is empty, unframed, or oversized",
            code="invalid_fdstore_record",
        )

    def unique_object(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite number {value}")

    try:
        value = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as error:
        raise BenchmarkLockError(
            "benchmark fd-store record is not strict JSON",
            code="invalid_fdstore_record",
        ) from error
    if not isinstance(value, dict) or canonical_json_bytes(value) != payload:
        raise BenchmarkLockError(
            "benchmark fd-store record is not canonical JSON",
            code="invalid_fdstore_record",
        )
    return value


def _require_integer(value: Any, *, positive: bool) -> int:
    minimum = 1 if positive else 0
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise BenchmarkLockError(
            "benchmark fd-store record integer is invalid",
            code="invalid_fdstore_record",
        )
    return value


def _require_time(value: Any, *, nullable: bool) -> float | None:
    if nullable and value is None:
        return None
    try:
        finite = math.isfinite(value)
    except (OverflowError, TypeError):
        finite = False
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not finite
        or value < 0
    ):
        raise BenchmarkLockError(
            "benchmark fd-store record time is invalid",
            code="invalid_fdstore_record",
        )
    return float(value)


def _parse_ticket_record(
    payload: bytes,
    *,
    lease_id: str,
    expected_state: LeaseState,
) -> Lease:
    value = _strict_document(payload)
    peer = value.get("peer")
    if (
        frozenset(value) != _TICKET_RECORD_FIELDS
        or value.get("schema") != TICKET_RECORD_SCHEMA
        or not isinstance(peer, dict)
        or frozenset(peer) != _PEER_FIELDS
    ):
        raise BenchmarkLockError(
            "benchmark fd-store record fields are invalid",
            code="invalid_fdstore_record",
        )
    state_value = value["state"]
    try:
        state = LeaseState(state_value)
    except (TypeError, ValueError) as error:
        raise BenchmarkLockError(
            "benchmark fd-store record state is invalid",
            code="invalid_fdstore_record",
        ) from error
    if state is not expected_state or value["lease_id"] != lease_id:
        raise BenchmarkLockError(
            "benchmark fd-store record identity or state is invalid",
            code="invalid_fdstore_record",
        )
    inherited = value["inherited_lease_id"]
    label = value["label"]
    if not isinstance(inherited, (str, type(None))) or not isinstance(label, str):
        raise BenchmarkLockError(
            "benchmark fd-store record text is invalid",
            code="invalid_fdstore_record",
        )
    started_at = _require_time(value["started_at"], nullable=True)
    lease = Lease(
        lease_id=lease_id,
        sequence=_require_integer(value["sequence"], positive=True),
        peer=PeerIdentity(
            pid=_require_integer(peer["pid"], positive=True),
            uid=_require_integer(peer["uid"], positive=False),
            gid=_require_integer(peer["gid"], positive=False),
        ),
        label=label,
        inherited_lease_id=inherited,
        enqueued_at=_require_time(
            value["enqueued_at"],
            nullable=False,
        ),
        state=state,
        started_at=started_at,
    )
    try:
        return _validate_lease(lease, state=expected_state)
    except BenchmarkLockError as error:
        raise BenchmarkLockError(
            "benchmark fd-store record semantics are invalid",
            code="invalid_fdstore_record",
        ) from error


def _memfd_name(lease_id: str, state: LeaseState) -> str:
    return f"benchmark-ticket-{lease_id}-{state.value}"


def create_ticket_record(lease: Lease, *, state: LeaseState) -> int:
    """Create a canonical, immutable memfd for one ticket state."""

    normalized = _validate_lease(lease, state=state)
    payload = canonical_json_bytes(_ticket_document(normalized))
    try:
        descriptor = os.memfd_create(
            _memfd_name(normalized.lease_id, state),
            os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
        )
    except (AttributeError, OSError) as error:
        raise BenchmarkLockError(
            f"cannot create benchmark ticket memfd: {error}",
            code="benchmark_fdstore_unavailable",
        ) from error
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written < 1:
                raise OSError("ticket memfd write made no progress")
            remaining = remaining[written:]
        fcntl.fcntl(
            descriptor,
            fcntl.F_ADD_SEALS,
            _REQUIRED_RECORD_SEALS,
        )
        _read_ticket_record(
            descriptor,
            lease_id=normalized.lease_id,
            expected_state=state,
        )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _require_record_memfd(
    descriptor: int,
    *,
    lease_id: str,
    state: LeaseState,
) -> int:
    _require_open_descriptor(descriptor)
    try:
        metadata = os.fstat(descriptor)
        seals = fcntl.fcntl(descriptor, fcntl.F_GET_SEALS)
        target = os.readlink(f"/proc/self/fd/{descriptor}")
    except OSError as error:
        raise BenchmarkLockError(
            f"cannot inspect benchmark ticket record: {error}",
            code="invalid_fdstore_record",
        ) from error
    expected_target = f"/memfd:{_memfd_name(lease_id, state)} (deleted)"
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size < 1
        or metadata.st_size > MAX_PACKET_BYTES
        or seals & _REQUIRED_RECORD_SEALS != _REQUIRED_RECORD_SEALS
        or target != expected_target
    ):
        raise BenchmarkLockError(
            "benchmark ticket record is not the expected sealed memfd",
            code="invalid_fdstore_record",
        )
    return metadata.st_size


def _read_ticket_record(
    descriptor: int,
    *,
    lease_id: str,
    expected_state: LeaseState,
) -> Lease:
    size = _require_record_memfd(
        descriptor,
        lease_id=lease_id,
        state=expected_state,
    )
    try:
        payload = os.pread(descriptor, size + 1, 0)
    except OSError as error:
        raise BenchmarkLockError(
            f"cannot read benchmark ticket record: {error}",
            code="invalid_fdstore_record",
        ) from error
    if len(payload) != size:
        raise BenchmarkLockError(
            "benchmark ticket record changed while it was read",
            code="invalid_fdstore_record",
        )
    return _parse_ticket_record(
        payload,
        lease_id=lease_id,
        expected_state=expected_state,
    )


def _inspect_socket(descriptor: int, *, listening: bool) -> None:
    _require_open_descriptor(descriptor)
    try:
        inspector = socket.socket(fileno=descriptor)
    except OSError as error:
        raise BenchmarkLockError(
            f"cannot inspect benchmark socket descriptor: {error}",
            code="invalid_fdstore_activation",
        ) from error
    try:
        require_seqpacket_channel(inspector)
        is_listening = bool(
            inspector.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
        )
        if is_listening != listening:
            raise BenchmarkLockError(
                "benchmark activation socket has the wrong listening state",
                code="invalid_fdstore_activation",
            )
    finally:
        inspector.detach()


def _inspect_pidfd(descriptor: int) -> None:
    _require_open_descriptor(descriptor)
    try:
        target = os.readlink(f"/proc/self/fd/{descriptor}")
    except OSError as error:
        raise BenchmarkLockError(
            f"cannot inspect benchmark owner pidfd: {error}",
            code="invalid_fdstore_activation",
        ) from error
    if target != "anon_inode:[pidfd]":
        raise BenchmarkLockError(
            "benchmark owner descriptor is not a pidfd",
            code="invalid_fdstore_activation",
        )


def _pidfd_process_id(descriptor: int) -> int | None:
    """Return the exact live PID named by one already validated pidfd."""

    path = f"/proc/self/fdinfo/{descriptor}"
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        info_descriptor = os.open(path, flags)
        try:
            payload = os.read(info_descriptor, 4097)
        finally:
            os.close(info_descriptor)
    except OSError as error:
        raise BenchmarkLockError(
            f"cannot inspect benchmark owner pidfd identity: {error}",
            code="invalid_fdstore_activation",
        ) from error
    if len(payload) > 4096:
        raise BenchmarkLockError(
            "benchmark owner pidfd metadata exceeds its fixed bound",
            code="invalid_fdstore_activation",
        )
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as error:
        raise BenchmarkLockError(
            "benchmark owner pidfd metadata is not ASCII",
            code="invalid_fdstore_activation",
        ) from error
    pid_fields = [
        line.removeprefix("Pid:\t")
        for line in text.splitlines()
        if line.startswith("Pid:\t")
    ]
    if len(pid_fields) != 1:
        raise BenchmarkLockError(
            "benchmark owner pidfd metadata has no unique process identity",
            code="invalid_fdstore_activation",
        )
    value = pid_fields[0]
    if value == "-1":
        return None
    if not value.isdecimal() or value.startswith("0"):
        raise BenchmarkLockError(
            "benchmark owner pidfd process identity is not canonical",
            code="invalid_fdstore_activation",
        )
    process_id = int(value)
    if process_id < 1:
        raise BenchmarkLockError(
            "benchmark owner pidfd process identity is invalid",
            code="invalid_fdstore_activation",
        )
    return process_id


def _require_ticket_authority(
    lease: Lease,
    *,
    owner_descriptor: int,
    channel_descriptor: int | None,
) -> None:
    """Bind persisted role descriptors to the record's attested peer."""

    process_id = _pidfd_process_id(owner_descriptor)
    if process_id is None or process_id != lease.peer.pid:
        raise BenchmarkLockError(
            "benchmark owner pidfd does not match its ticket record",
            code="invalid_fdstore_activation",
        )
    if channel_descriptor is None:
        return
    try:
        channel = socket.socket(fileno=channel_descriptor)
        try:
            credentials = peer_credentials(channel)
        finally:
            channel.detach()
    except BenchmarkLockError as error:
        raise BenchmarkLockError(
            f"cannot attest persisted benchmark channel: {error}",
            code="invalid_fdstore_activation",
        ) from error
    if credentials.as_tuple() != (
        lease.peer.pid,
        lease.peer.uid,
        lease.peer.gid,
    ):
        raise BenchmarkLockError(
            "benchmark client channel does not match its ticket record",
            code="invalid_fdstore_activation",
        )


def _pidfd_exited(descriptor: int) -> bool:
    poller = select.poll()
    poller.register(
        descriptor,
        select.POLLIN | select.POLLERR | select.POLLHUP,
    )
    events = poller.poll(0)
    if not events:
        return False
    _event_descriptor, event_mask = events[0]
    if event_mask & select.POLLNVAL:
        raise BenchmarkLockError(
            "benchmark owner pidfd became invalid during recovery",
            code="invalid_fdstore_activation",
        )
    return bool(event_mask & (select.POLLIN | select.POLLERR | select.POLLHUP))


def _channel_closed(descriptor: int) -> bool:
    poller = select.poll()
    poller.register(descriptor, select.POLLERR | select.POLLHUP)
    events = poller.poll(0)
    if not events:
        return False
    _event_descriptor, event_mask = events[0]
    if event_mask & select.POLLNVAL:
        raise BenchmarkLockError(
            "benchmark client channel became invalid during recovery",
            code="invalid_fdstore_activation",
        )
    return bool(event_mask & (select.POLLERR | select.POLLHUP))


def _immutable_ticket_identity(lease: Lease) -> tuple[Any, ...]:
    return (
        lease.lease_id,
        lease.sequence,
        lease.peer,
        lease.label,
        lease.inherited_lease_id,
        lease.enqueued_at,
    )


def _descriptor_sort_key(role: DescriptorRole) -> int:
    order = {
        DescriptorRole.OWNER: 0,
        DescriptorRole.CHANNEL: 1,
        DescriptorRole.QUEUED_RECORD: 2,
        DescriptorRole.ACTIVE_RECORD: 3,
    }
    return order[role]


def _remove_and_close(
    notifier: DescriptorNotifier,
    descriptors: list[tuple[DescriptorRole, NamedDescriptor]],
) -> tuple[str, ...]:
    names = tuple(
        named.name
        for _role, named in sorted(
            descriptors,
            key=lambda item: _descriptor_sort_key(item[0]),
        )
    )
    try:
        for name in names:
            notifier.remove_descriptor(name)
        notifier.barrier()
    finally:
        for _role, named in descriptors:
            try:
                os.close(named.descriptor)
            except OSError:
                pass
    return names


def recover_activation(notifier: DescriptorNotifier) -> ActivationState:
    """Validate and classify fd-store closures without relying on order."""

    named_descriptors = notifier.activation_descriptors()
    if len(named_descriptors) > FILE_DESCRIPTOR_STORE_MAX + 1:
        raise BenchmarkLockError(
            "benchmark activation exceeds its descriptor-store bound",
            code="invalid_fdstore_activation",
        )
    control: NamedDescriptor | None = None
    grouped: dict[str, dict[DescriptorRole, NamedDescriptor]] = {}
    for named in named_descriptors:
        _require_open_descriptor(named.descriptor)
        try:
            os.set_inheritable(named.descriptor, False)
        except OSError as error:
            raise BenchmarkLockError(
                f"cannot secure benchmark activation descriptor: {error}",
                code="invalid_fdstore_activation",
            ) from error
        if named.name == CONTROL_DESCRIPTOR_NAME:
            if control is not None:
                raise BenchmarkLockError(
                    "benchmark activation has duplicate control sockets",
                    code="invalid_fdstore_activation",
                )
            control = named
            continue
        match = _TICKET_DESCRIPTOR.fullmatch(named.name)
        if match is None:
            raise BenchmarkLockError(
                f"benchmark activation has unknown descriptor {named.name!r}",
                code="invalid_fdstore_activation",
            )
        lease_id, role_value = match.groups()
        role = DescriptorRole(role_value)
        roles = grouped.setdefault(lease_id, {})
        if role in roles:
            raise BenchmarkLockError(
                "benchmark activation has a duplicate ticket role",
                code="invalid_fdstore_activation",
            )
        roles[role] = named
    if control is None:
        raise BenchmarkLockError(
            "benchmark activation is missing its control socket",
            code="invalid_fdstore_activation",
        )
    if len(grouped) > MAX_TICKETS:
        raise BenchmarkLockError(
            "benchmark activation exceeds its ticket bound",
            code="invalid_fdstore_activation",
        )
    _inspect_socket(control.descriptor, listening=True)

    tickets: list[RecoveredTicket] = []
    reaped: list[ReapedTicket] = []
    discarded_names: list[str] = []
    for lease_id in sorted(grouped):
        roles = grouped[lease_id]
        owner = roles.get(DescriptorRole.OWNER)
        channel = roles.get(DescriptorRole.CHANNEL)
        queued_record = roles.get(DescriptorRole.QUEUED_RECORD)
        active_record = roles.get(DescriptorRole.ACTIVE_RECORD)

        if owner is not None:
            _inspect_pidfd(owner.descriptor)
        if channel is not None:
            _inspect_socket(channel.descriptor, listening=False)
        queued_lease = (
            None
            if queued_record is None
            else _read_ticket_record(
                queued_record.descriptor,
                lease_id=lease_id,
                expected_state=LeaseState.QUEUED,
            )
        )
        active_lease = (
            None
            if active_record is None
            else _read_ticket_record(
                active_record.descriptor,
                lease_id=lease_id,
                expected_state=LeaseState.ACTIVE,
            )
        )
        if (
            queued_lease is not None
            and active_lease is not None
            and _immutable_ticket_identity(queued_lease)
            != _immutable_ticket_identity(active_lease)
        ):
            raise BenchmarkLockError(
                "queued and active records disagree about ticket identity",
                code="invalid_fdstore_record",
            )

        channel_is_closed = channel is not None and _channel_closed(channel.descriptor)
        reason: ReapReason | None = None
        if queued_record is None and active_record is None:
            reason = ReapReason.UNCOMMITTED
        elif owner is None:
            reason = ReapReason.OWNER_MISSING
        elif _pidfd_exited(owner.descriptor):
            reason = ReapReason.OWNER_EXITED
        elif active_record is None and channel is None:
            reason = ReapReason.CHANNEL_MISSING
        elif active_record is None and channel_is_closed:
            reason = ReapReason.CHANNEL_CLOSED

        if reason is not None:
            members = list(roles.items())
            names = _remove_and_close(notifier, members)
            reaped.append(
                ReapedTicket(
                    lease_id=lease_id,
                    reason=reason,
                    descriptor_names=names,
                )
            )
            continue

        selected_lease = active_lease if active_lease is not None else queued_lease
        if selected_lease is None:
            raise AssertionError("live ticket has no committed lease")
        if owner is None:
            raise AssertionError("live ticket has no owner descriptor")
        _require_ticket_authority(
            selected_lease,
            owner_descriptor=owner.descriptor,
            channel_descriptor=(None if channel is None else channel.descriptor),
        )

        if active_record is not None and channel_is_closed:
            if channel is None:
                raise AssertionError("closed channel has no descriptor")
            discarded_names.extend(
                _remove_and_close(
                    notifier,
                    [(DescriptorRole.CHANNEL, channel)],
                )
            )
            channel = None

        selected_record = active_record if active_record is not None else queued_record
        if selected_record is None or selected_lease is None:
            raise AssertionError("live ticket has no committed record")

        if active_record is not None and queued_record is not None:
            discarded_names.extend(
                _remove_and_close(
                    notifier,
                    [
                        (
                            DescriptorRole.QUEUED_RECORD,
                            queued_record,
                        )
                    ],
                )
            )
        tickets.append(
            RecoveredTicket(
                lease=selected_lease,
                owner_descriptor=owner.descriptor,
                channel_descriptor=(None if channel is None else channel.descriptor),
                record_descriptor=selected_record.descriptor,
            )
        )

    tickets.sort(key=lambda ticket: ticket.lease.sequence)
    sequences = [ticket.lease.sequence for ticket in tickets]
    lease_ids = [ticket.lease.lease_id for ticket in tickets]
    active_count = sum(ticket.lease.state is LeaseState.ACTIVE for ticket in tickets)
    if (
        len(sequences) != len(set(sequences))
        or len(lease_ids) != len(set(lease_ids))
        or active_count > 1
    ):
        raise BenchmarkLockError(
            "benchmark activation has conflicting scheduler identities",
            code="invalid_fdstore_activation",
        )
    return ActivationState(
        control_descriptor=control.descriptor,
        tickets=tuple(tickets),
        reaped=tuple(reaped),
        discarded_descriptor_names=tuple(discarded_names),
    )


def _store_one(
    notifier: DescriptorNotifier,
    *,
    lease_id: str,
    role: DescriptorRole,
    descriptor: int,
) -> str:
    name = ticket_descriptor_name(lease_id, role)
    notifier.store_descriptor(name, descriptor)
    return name


def _remove_names(
    notifier: DescriptorNotifier,
    names: list[str],
) -> None:
    for name in names:
        notifier.remove_descriptor(name)
    notifier.barrier()


def store_queued_ticket(
    notifier: DescriptorNotifier,
    *,
    lease: Lease,
    owner_descriptor: int,
    channel_descriptor: int,
) -> tuple[str, str, str]:
    """Commit one queued closure with the immutable record stored last."""

    normalized = _validate_lease(lease, state=LeaseState.QUEUED)
    _inspect_pidfd(owner_descriptor)
    _inspect_socket(channel_descriptor, listening=False)
    _require_ticket_authority(
        normalized,
        owner_descriptor=owner_descriptor,
        channel_descriptor=channel_descriptor,
    )
    if _pidfd_exited(owner_descriptor) or _channel_closed(channel_descriptor):
        raise BenchmarkLockError(
            "cannot persist a terminal benchmark ticket",
            code="invalid_fdstore_ticket",
        )
    record_descriptor = create_ticket_record(
        normalized,
        state=LeaseState.QUEUED,
    )
    stored_names: list[str] = []
    record_stored = False
    try:
        stored_names.append(
            _store_one(
                notifier,
                lease_id=normalized.lease_id,
                role=DescriptorRole.OWNER,
                descriptor=owner_descriptor,
            )
        )
        stored_names.append(
            _store_one(
                notifier,
                lease_id=normalized.lease_id,
                role=DescriptorRole.CHANNEL,
                descriptor=channel_descriptor,
            )
        )
        stored_names.append(
            _store_one(
                notifier,
                lease_id=normalized.lease_id,
                role=DescriptorRole.QUEUED_RECORD,
                descriptor=record_descriptor,
            )
        )
        record_stored = True
        notifier.barrier()
    except Exception as error:
        if not record_stored and stored_names:
            try:
                _remove_names(notifier, stored_names)
            except Exception as rollback_error:
                raise BenchmarkLockError(
                    "queued ticket storage failed and its partial "
                    f"closure could not be removed: {rollback_error}",
                    code="benchmark_fdstore_rollback_failed",
                ) from rollback_error
        if isinstance(error, BenchmarkLockError):
            raise
        raise BenchmarkLockError(
            f"cannot store queued benchmark ticket: {error}",
            code="benchmark_fdstore_unavailable",
        ) from error
    finally:
        os.close(record_descriptor)
    return (stored_names[0], stored_names[1], stored_names[2])


def store_active_ticket(
    notifier: DescriptorNotifier,
    *,
    lease: Lease,
) -> str:
    """Commit the active record, then retire the superseded queued record."""

    normalized = _validate_lease(lease, state=LeaseState.ACTIVE)
    record_descriptor = create_ticket_record(
        normalized,
        state=LeaseState.ACTIVE,
    )
    active_name = ticket_descriptor_name(
        normalized.lease_id,
        DescriptorRole.ACTIVE_RECORD,
    )
    try:
        notifier.store_descriptor(active_name, record_descriptor)
        notifier.barrier()
        notifier.remove_descriptor(
            ticket_descriptor_name(
                normalized.lease_id,
                DescriptorRole.QUEUED_RECORD,
            )
        )
        notifier.barrier()
    except Exception as error:
        if isinstance(error, BenchmarkLockError):
            raise
        raise BenchmarkLockError(
            f"cannot store active benchmark ticket: {error}",
            code="benchmark_fdstore_unavailable",
        ) from error
    finally:
        os.close(record_descriptor)
    return active_name


def remove_ticket(
    notifier: DescriptorNotifier,
    *,
    lease_id: str,
) -> None:
    """Remove every possible role for one terminal ticket."""

    names = [
        ticket_descriptor_name(lease_id, role)
        for role in (
            DescriptorRole.OWNER,
            DescriptorRole.CHANNEL,
            DescriptorRole.QUEUED_RECORD,
            DescriptorRole.ACTIVE_RECORD,
        )
    ]
    _remove_names(notifier, names)


class SystemdLeasePersistence:
    """Broker persistence adapter over one systemd descriptor notifier."""

    def __init__(
        self,
        notifier: DescriptorNotifier | None = None,
    ) -> None:
        self.notifier = SystemdNotifier() if notifier is None else notifier

    def retain_queued(
        self,
        lease: Lease,
        *,
        channel_descriptor: int,
        owner_descriptor: int,
    ) -> None:
        """Publish a complete queued closure before acknowledgement."""

        store_queued_ticket(
            self.notifier,
            lease=lease,
            owner_descriptor=owner_descriptor,
            channel_descriptor=channel_descriptor,
        )

    def retain_active(self, lease: Lease) -> None:
        """Commit active intent before the broker sends its grant."""

        store_active_ticket(self.notifier, lease=lease)

    def release(self, lease_id: str) -> None:
        """Remove all possible roles regardless of transition history."""

        remove_ticket(self.notifier, lease_id=lease_id)
