"""Strict local protocol for the model-lab control and Pi lease channel."""

from __future__ import annotations

import json
import pathlib
import re
import socket
import struct
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


def send_document(connection: socket.socket, value: dict[str, Any]) -> None:
    payload = canonical_json_bytes(value)
    if len(payload) > MAX_SUPERVISOR_MESSAGE_BYTES:
        raise ModelLabError(
            "supervisor message exceeds its 16384-byte bound",
            code="invalid_supervisor_protocol",
        )
    try:
        connection.sendall(payload)
    except OSError as error:
        raise ModelLabError(
            f"cannot send supervisor message: {error}",
            code="supervisor_channel_closed",
        ) from error


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
) -> tuple[dict[str, Any], tuple[int, int, int] | None]:
    payload = bytearray()
    observed_credentials: tuple[int, int, int] | None = None
    credential_size = struct.calcsize("3i")
    ancillary_size = socket.CMSG_SPACE(credential_size)
    while len(payload) <= MAX_SUPERVISOR_MESSAGE_BYTES:
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


def receive_document(connection: socket.socket) -> dict[str, Any]:
    """Read exactly one canonical newline-terminated JSON object."""

    value, _ = _receive_framed_document(connection, credentials=False)
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
) -> tuple[dict[str, Any], tuple[int, int, int]]:
    """Read one document plus kernel-authenticated sender PID, UID, and GID."""

    value, credentials = _receive_framed_document(
        connection,
        credentials=True,
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
