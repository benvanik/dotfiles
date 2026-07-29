"""Strict local protocol for the model-lab control and Pi lease channel."""

from __future__ import annotations

import datetime
import json
import math
import pathlib
import re
import socket
import struct
import time
from collections.abc import Callable
from typing import Any

from .documents import canonical_json_bytes
from .errors import ModelLabError


SUPERVISOR_REQUEST_SCHEMA = "model-lab.supervisor-request.v1"
PI_PENDING_SCHEMA = "model-lab.pi-pending.v1"
SESSION_USE_ADMIT_SCHEMA = "model-lab.session-use-admit.v1"
SESSION_USE_ACCEPTED_SCHEMA = "model-lab.session-use-accepted.v1"
SUPERVISOR_RESULT_SCHEMA = "model-lab.supervisor-result.v1"
SUPERVISOR_ERROR_SCHEMA = "model-lab.supervisor-error.v1"
MAX_SUPERVISOR_MESSAGE_BYTES = 16 * 1024

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_OPAQUE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROCESS_START_TIME = re.compile(r"^[0-9]{1,32}$")
_INPUT_MODALITIES = frozenset({"image", "text"})


def supervisor_socket_path(root: pathlib.Path) -> pathlib.Path:
    return root / "supervisor.sock"


def supervisor_lock_path(root: pathlib.Path) -> pathlib.Path:
    return root / "supervisor.lock"


def process_start_time(pid: int) -> str:
    """Return Linux process start ticks for one exact PID generation."""

    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        raise ModelLabError(
            "process ID is invalid",
            code="invalid_supervisor_protocol",
        )
    path = pathlib.Path("/proc") / str(pid) / "stat"
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ModelLabError(
            f"cannot read process identity {path}: {error}",
            code="invalid_supervisor_protocol",
        ) from error
    if len(payload) > 4096:
        raise ModelLabError(
            f"process identity is too large: {path}",
            code="invalid_supervisor_protocol",
        )
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as error:
        raise ModelLabError(
            f"process identity is not ASCII: {path}",
            code="invalid_supervisor_protocol",
        ) from error
    closing = text.rfind(")")
    fields = text[closing + 1 :].split() if closing >= 0 else []
    if len(fields) < 20 or not _PROCESS_START_TIME.fullmatch(fields[19]):
        raise ModelLabError(
            f"process identity has an unsupported format: {path}",
            code="invalid_supervisor_protocol",
        )
    return fields[19]


def require_timestamp(value: Any, *, label: str) -> str:
    """Require one canonical UTC RFC3339 timestamp."""

    if not isinstance(value, str):
        raise ModelLabError(
            f"{label} is not text",
            code="invalid_supervisor_protocol",
        )
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ModelLabError(
            f"{label} is not an RFC3339 timestamp",
            code="invalid_supervisor_protocol",
        ) from error
    if parsed.tzinfo is None:
        raise ModelLabError(
            f"{label} has no timezone",
            code="invalid_supervisor_protocol",
        )
    normalized = (
        parsed.astimezone(datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    if normalized != value:
        raise ModelLabError(
            f"{label} is not canonical UTC",
            code="invalid_supervisor_protocol",
        )
    return value


def require_monotonic_deadline(value: Any, *, label: str) -> float:
    """Require one finite absolute CLOCK_MONOTONIC deadline."""

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ModelLabError(
            f"{label} is not a finite non-negative number",
            code="invalid_supervisor_protocol",
        )
    return float(value)


def peer_credentials(connection: socket.socket) -> tuple[int, int, int]:
    """Return ``(pid, uid, gid)`` from Linux ``SO_PEERCRED``."""

    if connection.family != socket.AF_UNIX or connection.type & 0xF != socket.SOCK_STREAM:
        raise ModelLabError(
            "supervisor channel is not an AF_UNIX stream",
            code="invalid_supervisor_channel",
        )
    try:
        payload = connection.getsockopt(
            socket.SOL_SOCKET,
            socket.SO_PEERCRED,
            struct.calcsize("3i"),
        )
        pid, uid, gid = struct.unpack("3i", payload)
    except (AttributeError, OSError, struct.error) as error:
        raise ModelLabError(
            f"cannot attest supervisor channel peer: {error}",
            code="invalid_supervisor_channel",
        ) from error
    if pid < 1 or uid < 0 or gid < 0:
        raise ModelLabError(
            "supervisor channel returned invalid peer credentials",
            code="invalid_supervisor_channel",
        )
    return pid, uid, gid


def _apply_io_deadline(
    connection: socket.socket,
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
    deadline_error_code: str,
) -> None:
    if deadline is None:
        return
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise ModelLabError(
            "supervisor channel exceeded its absolute deadline",
            code=deadline_error_code,
        )
    connection.settimeout(remaining)


def send_document(
    connection: socket.socket,
    value: dict[str, Any],
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    deadline_error_code: str = "supervisor_channel_closed",
) -> None:
    payload = canonical_json_bytes(value)
    if len(payload) > MAX_SUPERVISOR_MESSAGE_BYTES:
        raise ModelLabError(
            "supervisor message exceeds its 16384-byte bound",
            code="invalid_supervisor_protocol",
        )
    view = memoryview(payload)
    while view:
        _apply_io_deadline(
            connection,
            deadline=deadline,
            monotonic=monotonic,
            deadline_error_code=deadline_error_code,
        )
        try:
            sent = connection.send(view)
        except OSError as error:
            if deadline is not None and monotonic() >= deadline:
                raise ModelLabError(
                    "supervisor channel exceeded its absolute deadline",
                    code=deadline_error_code,
                ) from error
            raise ModelLabError(
                f"cannot send supervisor message: {error}",
                code="supervisor_channel_closed",
            ) from error
        if sent <= 0:
            raise ModelLabError(
                "supervisor channel closed while sending a message",
                code="supervisor_channel_closed",
            )
        view = view[sent:]


def _parse_document(payload: bytes) -> dict[str, Any]:
    """Parse one already-framed canonical supervisor document."""

    if not payload or payload[-1:] != b"\n" or len(payload) > MAX_SUPERVISOR_MESSAGE_BYTES:
        raise ModelLabError(
            "supervisor message is empty or exceeds its 16384-byte bound",
            code="invalid_supervisor_protocol",
        )

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = item
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite number: {value}")

    try:
        value = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise ModelLabError(
            "supervisor message is not strict canonical JSON",
            code="invalid_supervisor_protocol",
        ) from error
    if not isinstance(value, dict) or canonical_json_bytes(value) != payload:
        raise ModelLabError(
            "supervisor message is not strict canonical JSON",
            code="invalid_supervisor_protocol",
        )
    return value


def _receive_framed_document(
    connection: socket.socket,
    *,
    credentials: bool,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    deadline_error_code: str = "supervisor_channel_closed",
) -> tuple[dict[str, Any], tuple[int, int, int] | None]:
    payload = bytearray()
    observed_credentials: tuple[int, int, int] | None = None
    credential_size = struct.calcsize("3i")
    ancillary_size = socket.CMSG_SPACE(credential_size)
    while len(payload) <= MAX_SUPERVISOR_MESSAGE_BYTES:
        _apply_io_deadline(
            connection,
            deadline=deadline,
            monotonic=monotonic,
            deadline_error_code=deadline_error_code,
        )
        try:
            if credentials:
                chunk, ancillary, flags, _ = connection.recvmsg(
                    1,
                    ancillary_size,
                )
                if flags & getattr(socket, "MSG_CTRUNC", 0):
                    raise ModelLabError(
                        "session credentials were truncated",
                        code="invalid_supervisor_channel",
                    )
                for level, kind, value in ancillary:
                    if (
                        level != socket.SOL_SOCKET
                        or kind != socket.SCM_CREDENTIALS
                    ):
                        continue
                    if len(value) < credential_size:
                        raise ModelLabError(
                            "session credentials were malformed",
                            code="invalid_supervisor_channel",
                        )
                    current = struct.unpack("3i", value[:credential_size])
                    if observed_credentials not in {None, current}:
                        raise ModelLabError(
                            "session sender changed within one admission frame",
                            code="invalid_supervisor_channel",
                        )
                    observed_credentials = current
            else:
                chunk = connection.recv(1)
        except ModelLabError:
            raise
        except (AttributeError, OSError, struct.error) as error:
            if deadline is not None and monotonic() >= deadline:
                raise ModelLabError(
                    "supervisor channel exceeded its absolute deadline",
                    code=deadline_error_code,
                ) from error
            raise ModelLabError(
                f"cannot read supervisor message: {error}",
                code="supervisor_channel_closed",
            ) from error
        if not chunk:
            raise ModelLabError(
                "supervisor channel closed before a complete message",
                code="supervisor_channel_closed",
            )
        payload.extend(chunk)
        if chunk == b"\n":
            break
    value = _parse_document(bytes(payload))
    if credentials and observed_credentials is None:
        raise ModelLabError(
            "session admission carried no kernel sender credentials",
            code="invalid_supervisor_channel",
        )
    return value, observed_credentials


def receive_document(
    connection: socket.socket,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    deadline_error_code: str = "supervisor_channel_closed",
) -> dict[str, Any]:
    """Read exactly one canonical newline-terminated JSON object."""

    value, _ = _receive_framed_document(
        connection,
        credentials=False,
        deadline=deadline,
        monotonic=monotonic,
        deadline_error_code=deadline_error_code,
    )
    return value


def enable_sender_credentials(connection: socket.socket) -> None:
    """Require Linux to attach the actual writer identity to future reads."""

    if not hasattr(socket, "SO_PASSCRED") or not hasattr(
        socket,
        "SCM_CREDENTIALS",
    ):
        raise ModelLabError(
            "kernel sender credentials are unavailable",
            code="invalid_supervisor_channel",
        )
    try:
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    except OSError as error:
        raise ModelLabError(
            f"cannot enable session sender credentials: {error}",
            code="invalid_supervisor_channel",
        ) from error


def receive_document_with_credentials(
    connection: socket.socket,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    deadline_error_code: str = "supervisor_channel_closed",
) -> tuple[dict[str, Any], tuple[int, int, int]]:
    """Read one document plus kernel-authenticated sender PID, UID, and GID."""

    value, credentials = _receive_framed_document(
        connection,
        credentials=True,
        deadline=deadline,
        monotonic=monotonic,
        deadline_error_code=deadline_error_code,
    )
    if credentials is None:
        raise AssertionError("credential receive returned no credentials")
    return value, credentials


def require_identifier(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ModelLabError(
            f"{label} is invalid",
            code="invalid_supervisor_protocol",
        )
    return value


def require_opaque_identifier(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _OPAQUE_IDENTIFIER.fullmatch(value):
        raise ModelLabError(
            f"{label} is invalid",
            code="invalid_supervisor_protocol",
        )
    return value


def require_nullable_opaque_identifier(
    value: Any,
    *,
    label: str,
) -> str | None:
    if value is None:
        return None
    return require_opaque_identifier(value, label=label)


def require_canonical_input_modalities(
    value: Any,
    *,
    label: str,
) -> tuple[str, ...]:
    """Require the exact canonical service modalities carried by one RPC."""

    if not isinstance(value, (list, tuple)):
        raise ModelLabError(
            f"{label} is invalid",
            code="invalid_supervisor_protocol",
        )
    modalities = tuple(value)
    if (
        not modalities
        or any(
            not isinstance(modality, str)
            or modality not in _INPUT_MODALITIES
            for modality in modalities
        )
        or "text" not in modalities
        or len(set(modalities)) != len(modalities)
        or modalities != tuple(sorted(modalities))
    ):
        raise ModelLabError(
            f"{label} is invalid",
            code="invalid_supervisor_protocol",
        )
    return modalities


def require_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ModelLabError(
            f"{label} is invalid",
            code="invalid_supervisor_protocol",
        )
    return value


def require_process_identity(pid: Any, start_time: Any) -> tuple[int, str]:
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid < 1
        or not isinstance(start_time, str)
        or not _PROCESS_START_TIME.fullmatch(start_time)
    ):
        raise ModelLabError(
            "session process identity is invalid",
            code="invalid_supervisor_protocol",
        )
    return pid, start_time


def require_exact_fields(
    value: dict[str, Any],
    *,
    schema: str,
    fields: frozenset[str],
) -> None:
    expected = fields | {"schema"}
    if set(value) != expected or value.get("schema") != schema:
        raise ModelLabError(
            f"supervisor message must have schema {schema!r} and exact fields",
            code="invalid_supervisor_protocol",
        )
