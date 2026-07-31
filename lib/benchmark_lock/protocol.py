"""Strict packet protocol shared by the benchmark client and broker.

The v1 packet shapes are a frozen rolling-upgrade ABI: checkout clients and
administration must interoperate with the independently installed prior broker
generation. A later wire version requires a staged dual-version migration; v1
encoders and parsers never change in place.
"""

from __future__ import annotations

import array
import json
import os
import re
import socket
import struct
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from .errors import BenchmarkLockError


REQUEST_SCHEMA = "benchmarkd.request.v1"
EVENT_SCHEMA = "benchmarkd.event.v1"
MAX_PACKET_BYTES = 4096

_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:+/@=-]{0,127}$")
_LEASE_ID = re.compile(r"^[0-9a-f]{32}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_POLICY = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_MESSAGE = re.compile(r"^[\x20-\x7e]{1,512}$")
_POLICY_STATES = frozenset(
    {
        "entering",
        "faulted",
        "held",
        "idle",
        "leaving",
        "maintenance",
        "recovery_required",
        "restoring",
    }
)
_CREDENTIALS = struct.Struct("3i")
_MAX_UNEXPECTED_DESCRIPTORS = 16


@dataclass(frozen=True)
class AcquireRequest:
    """Request one FIFO lease without exposing the benchmark command."""

    label: str
    inherited_lease_id: str | None = None


@dataclass(frozen=True)
class StatusRequest:
    """Request a point-in-time broker status document."""


@dataclass(frozen=True)
class MaintenanceRequest:
    """Fence admissions for the lifetime of one root-owned channel."""


Request: TypeAlias = AcquireRequest | StatusRequest | MaintenanceRequest


@dataclass(frozen=True)
class ActiveLease:
    """Safe holder metadata visible to cooperating benchmark users."""

    lease_id: str
    pid: int
    uid: int
    label: str
    elapsed_seconds: int


@dataclass(frozen=True)
class QueuedEvent:
    """Initial FIFO admission and current holder observation."""

    lease_id: str
    position: int
    active: ActiveLease | None


@dataclass(frozen=True)
class WaitingEvent:
    """Updated FIFO position or holder observation."""

    lease_id: str
    position: int
    active: ActiveLease | None


@dataclass(frozen=True)
class GrantedEvent:
    """Authority to replace the client process with its benchmark command."""

    lease_id: str
    policy: str
    aslr: Literal["process"] = "process"


@dataclass(frozen=True)
class ErrorEvent:
    """Stable rejection or broker failure safe to show to an operator."""

    code: str
    message: str


@dataclass(frozen=True)
class StatusEvent:
    """Bounded broker state; command arguments are deliberately absent."""

    active: ActiveLease | None
    queue_depth: int
    policy_state: str


@dataclass(frozen=True)
class MaintenanceEvent:
    """Acknowledge that the root caller exclusively fenced admissions."""


Event: TypeAlias = (
    QueuedEvent
    | WaitingEvent
    | GrantedEvent
    | ErrorEvent
    | StatusEvent
    | MaintenanceEvent
)


def _fail(
    message: str,
    *,
    code: str = "invalid_benchmark_protocol",
) -> None:
    raise BenchmarkLockError(message, code=code)


def require_seqpacket_channel(connection: socket.socket) -> None:
    """Require one AF_UNIX/SOCK_SEQPACKET channel."""

    expected_type = getattr(socket, "SOCK_SEQPACKET", None)
    if expected_type is None:
        _fail(
            "benchmark channels require Unix sequenced-packet sockets",
            code="benchmark_platform_unsupported",
        )
    try:
        family = connection.family
        actual_type = connection.getsockopt(
            socket.SOL_SOCKET,
            socket.SO_TYPE,
        )
    except (AttributeError, OSError) as error:
        raise BenchmarkLockError(
            f"cannot inspect benchmark channel: {error}",
            code="invalid_benchmark_channel",
        ) from error
    if family != socket.AF_UNIX or actual_type != expected_type:
        _fail(
            "benchmark channel is not AF_UNIX/SOCK_SEQPACKET",
            code="invalid_benchmark_channel",
        )


def canonical_json_bytes(value: dict[str, Any]) -> bytes:
    """Encode one canonical ASCII JSON packet with its framing newline."""

    if not isinstance(value, dict):
        _fail("benchmark packet root is not an object")
    try:
        payload = (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            + "\n"
        ).encode("ascii")
    except (RecursionError, TypeError, ValueError) as error:
        raise BenchmarkLockError(
            "benchmark packet cannot be encoded as canonical JSON",
            code="invalid_benchmark_protocol",
        ) from error
    if len(payload) > MAX_PACKET_BYTES:
        _fail(f"benchmark packet exceeds its {MAX_PACKET_BYTES}-byte bound")
    return payload


def _strict_json_object(payload: bytes) -> dict[str, Any]:
    if not payload or payload[-1:] != b"\n" or len(payload) > MAX_PACKET_BYTES:
        _fail(
            "benchmark packet is empty, unframed, or exceeds its "
            f"{MAX_PACKET_BYTES}-byte bound"
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
            "benchmark packet is not strict canonical JSON",
            code="invalid_benchmark_protocol",
        ) from error
    if not isinstance(value, dict) or canonical_json_bytes(value) != payload:
        _fail("benchmark packet is not strict canonical JSON")
    return value


def _require_exact_keys(
    value: dict[str, Any],
    expected: frozenset[str],
    *,
    label: str,
) -> None:
    if frozenset(value) != expected:
        _fail(f"{label} has unsupported or missing fields")


def _require_label(value: Any) -> str:
    if not isinstance(value, str) or not _LABEL.fullmatch(value):
        _fail("benchmark label is invalid")
    return value


def _require_lease_id(value: Any, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not _LEASE_ID.fullmatch(value):
        _fail("benchmark lease ID is invalid")
    return value


def _require_nonnegative_integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(f"{label} is invalid")
    return value


def _require_positive_integer(value: Any, *, label: str) -> int:
    result = _require_nonnegative_integer(value, label=label)
    if result == 0:
        _fail(f"{label} is invalid")
    return result


def _require_error_code(value: Any) -> str:
    if not isinstance(value, str) or not _ERROR_CODE.fullmatch(value):
        _fail("benchmark error code is invalid")
    return value


def _require_message(value: Any) -> str:
    if not isinstance(value, str) or not _MESSAGE.fullmatch(value):
        _fail("benchmark error message is invalid")
    return value


def _require_policy(value: Any) -> str:
    if not isinstance(value, str) or not _POLICY.fullmatch(value):
        _fail("benchmark policy identity is invalid")
    return value


def _active_lease_document(value: ActiveLease) -> dict[str, Any]:
    return {
        "elapsed_seconds": value.elapsed_seconds,
        "label": value.label,
        "lease_id": value.lease_id,
        "pid": value.pid,
        "uid": value.uid,
    }


def _parse_active_lease(value: Any) -> ActiveLease | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        _fail("active benchmark lease is invalid")
    _require_exact_keys(
        value,
        frozenset(
            {
                "elapsed_seconds",
                "label",
                "lease_id",
                "pid",
                "uid",
            }
        ),
        label="active benchmark lease",
    )
    lease_id = _require_lease_id(value["lease_id"])
    if lease_id is None:
        raise AssertionError("non-null lease validator returned null")
    return ActiveLease(
        lease_id=lease_id,
        pid=_require_positive_integer(value["pid"], label="holder PID"),
        uid=_require_nonnegative_integer(value["uid"], label="holder UID"),
        label=_require_label(value["label"]),
        elapsed_seconds=_require_nonnegative_integer(
            value["elapsed_seconds"],
            label="holder elapsed time",
        ),
    )


def request_document(value: Request) -> dict[str, Any]:
    """Return the canonical semantic document for one typed request."""

    if isinstance(value, AcquireRequest):
        return {
            "inherited_lease_id": value.inherited_lease_id,
            "label": value.label,
            "operation": "acquire",
            "schema": REQUEST_SCHEMA,
        }
    if isinstance(value, StatusRequest):
        return {
            "operation": "status",
            "schema": REQUEST_SCHEMA,
        }
    if isinstance(value, MaintenanceRequest):
        return {
            "operation": "maintenance",
            "schema": REQUEST_SCHEMA,
        }
    _fail("benchmark request has an unsupported type")


def event_document(value: Event) -> dict[str, Any]:
    """Return the canonical semantic document for one typed event."""

    if isinstance(value, (QueuedEvent, WaitingEvent)):
        return {
            "active": (
                None if value.active is None else _active_lease_document(value.active)
            ),
            "lease_id": value.lease_id,
            "position": value.position,
            "schema": EVENT_SCHEMA,
            "type": ("queued" if isinstance(value, QueuedEvent) else "waiting"),
        }
    if isinstance(value, GrantedEvent):
        return {
            "aslr": value.aslr,
            "lease_id": value.lease_id,
            "policy": value.policy,
            "schema": EVENT_SCHEMA,
            "type": "granted",
        }
    if isinstance(value, ErrorEvent):
        return {
            "code": value.code,
            "message": value.message,
            "schema": EVENT_SCHEMA,
            "type": "error",
        }
    if isinstance(value, StatusEvent):
        return {
            "active": (
                None if value.active is None else _active_lease_document(value.active)
            ),
            "policy_state": value.policy_state,
            "queue_depth": value.queue_depth,
            "schema": EVENT_SCHEMA,
            "type": "status",
        }
    if isinstance(value, MaintenanceEvent):
        return {
            "schema": EVENT_SCHEMA,
            "type": "maintenance",
        }
    _fail("benchmark event has an unsupported type")


def encode_request(value: Request) -> bytes:
    """Validate and encode one request packet."""

    document = request_document(value)
    payload = canonical_json_bytes(document)
    parse_request(payload)
    return payload


def encode_event(value: Event) -> bytes:
    """Validate and encode one event packet."""

    document = event_document(value)
    payload = canonical_json_bytes(document)
    parse_event(payload)
    return payload


def parse_request(payload: bytes) -> Request:
    """Parse one exact request schema."""

    value = _strict_json_object(payload)
    if value.get("schema") != REQUEST_SCHEMA:
        _fail("benchmark request schema is unsupported")
    operation = value.get("operation")
    if operation == "acquire":
        _require_exact_keys(
            value,
            frozenset(
                {
                    "inherited_lease_id",
                    "label",
                    "operation",
                    "schema",
                }
            ),
            label="benchmark acquire request",
        )
        inherited = _require_lease_id(
            value["inherited_lease_id"],
            nullable=True,
        )
        return AcquireRequest(
            label=_require_label(value["label"]),
            inherited_lease_id=inherited,
        )
    if operation == "status":
        _require_exact_keys(
            value,
            frozenset({"operation", "schema"}),
            label="benchmark status request",
        )
        return StatusRequest()
    if operation == "maintenance":
        _require_exact_keys(
            value,
            frozenset({"operation", "schema"}),
            label="benchmark maintenance request",
        )
        return MaintenanceRequest()
    _fail("benchmark request operation is unsupported")


def parse_event(payload: bytes) -> Event:
    """Parse one exact broker event schema."""

    value = _strict_json_object(payload)
    if value.get("schema") != EVENT_SCHEMA:
        _fail("benchmark event schema is unsupported")
    event_type = value.get("type")
    if event_type in {"queued", "waiting"}:
        _require_exact_keys(
            value,
            frozenset(
                {
                    "active",
                    "lease_id",
                    "position",
                    "schema",
                    "type",
                }
            ),
            label=f"benchmark {event_type} event",
        )
        lease_id = _require_lease_id(value["lease_id"])
        if lease_id is None:
            raise AssertionError("non-null lease validator returned null")
        arguments = {
            "lease_id": lease_id,
            "position": _require_positive_integer(
                value["position"],
                label="queue position",
            ),
            "active": _parse_active_lease(value["active"]),
        }
        return (
            QueuedEvent(**arguments)
            if event_type == "queued"
            else WaitingEvent(**arguments)
        )
    if event_type == "granted":
        _require_exact_keys(
            value,
            frozenset(
                {
                    "aslr",
                    "lease_id",
                    "policy",
                    "schema",
                    "type",
                }
            ),
            label="benchmark grant event",
        )
        lease_id = _require_lease_id(value["lease_id"])
        if lease_id is None:
            raise AssertionError("non-null lease validator returned null")
        if value["aslr"] != "process":
            _fail("benchmark ASLR policy is unsupported")
        return GrantedEvent(
            lease_id=lease_id,
            policy=_require_policy(value["policy"]),
        )
    if event_type == "error":
        _require_exact_keys(
            value,
            frozenset({"code", "message", "schema", "type"}),
            label="benchmark error event",
        )
        return ErrorEvent(
            code=_require_error_code(value["code"]),
            message=_require_message(value["message"]),
        )
    if event_type == "status":
        _require_exact_keys(
            value,
            frozenset(
                {
                    "active",
                    "policy_state",
                    "queue_depth",
                    "schema",
                    "type",
                }
            ),
            label="benchmark status event",
        )
        policy_state = value["policy_state"]
        if not isinstance(policy_state, str) or policy_state not in _POLICY_STATES:
            _fail("benchmark policy state is invalid")
        return StatusEvent(
            active=_parse_active_lease(value["active"]),
            queue_depth=_require_nonnegative_integer(
                value["queue_depth"],
                label="queue depth",
            ),
            policy_state=policy_state,
        )
    if event_type == "maintenance":
        _require_exact_keys(
            value,
            frozenset({"schema", "type"}),
            label="benchmark maintenance event",
        )
        return MaintenanceEvent()
    _fail("benchmark event type is unsupported")


def enable_sender_credentials(connection: socket.socket) -> None:
    """Enable one kernel credential record on every received request."""

    require_seqpacket_channel(connection)
    if not hasattr(socket, "SO_PASSCRED") or not hasattr(
        socket,
        "SCM_CREDENTIALS",
    ):
        _fail(
            "kernel sender credentials are unavailable",
            code="benchmark_platform_unsupported",
        )
    try:
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    except OSError as error:
        raise BenchmarkLockError(
            f"cannot enable benchmark sender credentials: {error}",
            code="invalid_benchmark_channel",
        ) from error


def _close_received_rights(
    ancillary: list[tuple[int, int, bytes]],
) -> None:
    item_size = array.array("i").itemsize
    for level, kind, payload in ancillary:
        if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
            continue
        complete_length = len(payload) - len(payload) % item_size
        descriptors = array.array("i")
        descriptors.frombytes(payload[:complete_length])
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _receive_payload(
    connection: socket.socket,
    *,
    expected_credentials: tuple[int, int, int] | None,
) -> bytes:
    require_seqpacket_channel(connection)
    ancillary_size = socket.CMSG_SPACE(_CREDENTIALS.size) + socket.CMSG_SPACE(
        array.array("i").itemsize * _MAX_UNEXPECTED_DESCRIPTORS
    )
    try:
        payload, ancillary, flags, _address = connection.recvmsg(
            MAX_PACKET_BYTES + 1,
            ancillary_size,
            getattr(socket, "MSG_CMSG_CLOEXEC", 0),
        )
    except OSError as error:
        raise BenchmarkLockError(
            f"cannot receive benchmark packet: {error}",
            code="benchmark_channel_closed",
        ) from error
    try:
        if flags & (getattr(socket, "MSG_TRUNC", 0) | getattr(socket, "MSG_CTRUNC", 0)):
            _fail("benchmark packet or authority record was truncated")
        if not payload:
            _fail(
                "benchmark channel closed before a packet arrived",
                code="benchmark_channel_closed",
            )
        credential_records: list[tuple[int, int, int]] = []
        unexpected = False
        for level, kind, value in ancillary:
            if level == socket.SOL_SOCKET and kind == getattr(
                socket, "SCM_CREDENTIALS", -1
            ):
                if len(value) != _CREDENTIALS.size:
                    _fail("benchmark sender credentials are malformed")
                credential_records.append(_CREDENTIALS.unpack(value))
            else:
                unexpected = True
        if unexpected:
            _fail("benchmark packet carried unsupported ancillary data")
        if expected_credentials is None:
            if credential_records:
                _fail("benchmark packet carried unexpected credentials")
        elif credential_records != [expected_credentials]:
            _fail(
                "benchmark packet sender does not match its connected peer",
                code="invalid_benchmark_channel",
            )
        return payload
    finally:
        _close_received_rights(ancillary)


def _send_payload(connection: socket.socket, payload: bytes) -> None:
    require_seqpacket_channel(connection)
    try:
        written = connection.send(payload)
    except OSError as error:
        raise BenchmarkLockError(
            f"cannot send benchmark packet: {error}",
            code="benchmark_channel_closed",
        ) from error
    if written != len(payload):
        _fail(
            "benchmark channel partially sent an atomic packet",
            code="benchmark_channel_closed",
        )


def send_request(connection: socket.socket, value: Request) -> None:
    """Send one complete request packet."""

    _send_payload(connection, encode_request(value))


def receive_request(
    connection: socket.socket,
    *,
    expected_credentials: tuple[int, int, int],
) -> Request:
    """Receive a request written by the exact connected peer."""

    return parse_request(
        _receive_payload(
            connection,
            expected_credentials=expected_credentials,
        )
    )


def send_event(connection: socket.socket, value: Event) -> None:
    """Send one complete broker event packet."""

    _send_payload(connection, encode_event(value))


def receive_event(connection: socket.socket) -> Event:
    """Receive one broker event packet."""

    return parse_event(_receive_payload(connection, expected_credentials=None))
