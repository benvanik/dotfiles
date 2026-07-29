"""Admission and lifetime channel for a model-lab-owned session use.

The connected Unix stream is both the admission authority and the use lease.
Its server-side state belongs to the singleton model-lab supervisor.  After
admission the launcher keeps the stream open for the complete sandbox
lifetime; EOF means that the supervisor can no longer renew or account for
the service and the sandbox must be reaped.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import socket
import stat
import struct
from dataclasses import dataclass, field
from typing import Any, Protocol

from .errors import ModelSessionError


SESSION_USE_ADMISSION_SCHEMA = "model-lab.session-use-admit.v1"
SESSION_USE_ACCEPTED_SCHEMA = "model-lab.session-use-accepted.v1"
SESSION_USE_ERROR_SCHEMA = "model-lab.supervisor-error.v1"
MAX_SUPERVISOR_FRAME_BYTES = 16 * 1024

_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_OPAQUE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")
_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PROCESS_START_TIME_PATTERN = re.compile(r"^[0-9]{1,32}$")
_ACCEPTED_KEYS = {
    "schema",
    "profile_id",
    "service_id",
    "workload_sha256",
    "deployment_id",
    "use_lease_id",
    "supervisor_pid",
    "supervisor_start_time",
    "session_pid",
    "session_start_time",
}
_ERROR_KEYS = {"schema", "code", "message"}


class ProfileRoute(Protocol):
    profile_id: str
    service_id: str


@dataclass
class SessionUseAuthority:
    """One admitted use plus its kernel-held supervisor lifetime channel."""

    profile_id: str
    service_id: str
    workload_sha256: str
    deployment_id: str
    use_lease_id: str
    supervisor_pid: int
    supervisor_start_time: str
    session_pid: int
    session_start_time: str
    channel: socket.socket = field(repr=False, compare=False)
    _closed: bool = field(default=False, init=False, repr=False, compare=False)

    def receive_supervisor_event(self) -> bytes:
        """Block until supervisor loss or an invalid post-admission byte."""

        try:
            return self.channel.recv(1)
        except OSError as error:
            raise ModelSessionError(
                f"model-lab supervisor channel failed: {error}",
                code="model_lab_supervisor_lost",
            ) from error

    def close(self) -> None:
        """Release this process's exact use-channel reference."""

        if self._closed:
            return
        self._closed = True
        try:
            self.channel.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.channel.close()


def _fail(
    message: str,
    *,
    code: str = "invalid_model_lab_use_authority",
) -> None:
    raise ModelSessionError(message, code=code)


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("ascii")


def _strict_json_object(payload: bytes) -> dict[str, Any]:
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
        text = payload.decode("ascii")
        value = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as error:
        raise ModelSessionError(
            "model-lab supervisor response is not strict canonical JSON",
            code="invalid_model_lab_use_authority",
        ) from error
    if not isinstance(value, dict) or _canonical_json_bytes(value) != payload:
        _fail("model-lab supervisor response is not strict canonical JSON")
    return value


def _supervisor_socket_path() -> pathlib.Path:
    runtime_root = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_root:
        _fail(
            "XDG_RUNTIME_DIR is required for model-lab supervisor admission",
            code="model_lab_supervisor_unavailable",
        )
    path = pathlib.Path(runtime_root).expanduser()
    if (
        not path.is_absolute()
        or pathlib.Path(os.path.normpath(path)) != path
        or path
        in {
            pathlib.Path("/"),
            pathlib.Path("/run"),
            pathlib.Path("/tmp"),
            pathlib.Path("/var"),
        }
    ):
        _fail(
            "XDG_RUNTIME_DIR is unsafe for model-lab supervisor admission",
            code="model_lab_supervisor_unavailable",
        )
    return path / "model-lab" / "supervisor.sock"


def _read_frame(channel: socket.socket) -> bytes:
    payload = bytearray()
    while True:
        remaining = MAX_SUPERVISOR_FRAME_BYTES + 1 - len(payload)
        if remaining <= 0:
            _fail("model-lab supervisor response exceeds its frame bound")
        try:
            chunk = channel.recv(remaining)
        except OSError as error:
            raise ModelSessionError(
                f"cannot read model-lab supervisor response: {error}",
                code="invalid_model_lab_use_authority",
            ) from error
        if not chunk:
            _fail("model-lab supervisor closed before session admission")
        payload.extend(chunk)
        if len(payload) > MAX_SUPERVISOR_FRAME_BYTES:
            _fail("model-lab supervisor response exceeds its frame bound")
        newline = payload.find(b"\n")
        if newline >= 0:
            if newline != len(payload) - 1:
                _fail("model-lab supervisor sent trailing admission bytes")
            return bytes(payload)


def process_start_time(pid: int) -> str:
    """Return Linux `/proc` start ticks for one exact process generation."""

    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        _fail("model-lab process ID is invalid")
    path = f"/proc/{pid}/stat"
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        raise ModelSessionError(
            f"cannot open model-lab process identity {path}: {error}",
            code="invalid_model_lab_use_authority",
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _fail("model-lab process identity is not a regular proc file")
        chunks: list[bytes] = []
        remaining = 4097
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    except OSError as error:
        raise ModelSessionError(
            f"cannot read model-lab process identity {path}: {error}",
            code="invalid_model_lab_use_authority",
        ) from error
    finally:
        os.close(descriptor)
    if not payload or len(payload) > 4096:
        _fail("model-lab process identity has an unsupported size")
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as error:
        raise ModelSessionError(
            "model-lab process identity is not ASCII",
            code="invalid_model_lab_use_authority",
        ) from error
    closing = text.rfind(")")
    fields = text[closing + 1 :].split() if closing >= 0 else []
    if len(fields) < 20 or not _PROCESS_START_TIME_PATTERN.fullmatch(fields[19]):
        _fail("model-lab process identity has an unsupported format")
    return fields[19]


def _positive_pid(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _fail(f"{label} is invalid")
    return value


def _process_start(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _PROCESS_START_TIME_PATTERN.fullmatch(value):
        _fail(f"{label} is invalid")
    return value


def _identifier(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        _fail(f"{label} is invalid")
    return value


def _opaque_identifier(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _OPAQUE_IDENTIFIER_PATTERN.fullmatch(value):
        _fail(f"{label} is invalid")
    return value


def _peer_credentials(channel: socket.socket) -> tuple[int, int, int]:
    if not hasattr(socket, "SO_PEERCRED"):
        _fail(
            "model-lab supervisor admission requires Linux SO_PEERCRED",
            code="model_lab_supervisor_unavailable",
        )
    size = struct.calcsize("3i")
    try:
        value = channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size)
    except OSError as error:
        raise ModelSessionError(
            f"cannot inspect model-lab supervisor peer: {error}",
            code="invalid_model_lab_use_authority",
        ) from error
    if len(value) != size:
        _fail("model-lab supervisor peer credentials are truncated")
    return struct.unpack("3i", value)


def _open_channel(descriptor: int | None) -> tuple[socket.socket, int]:
    if (
        descriptor is None
        or isinstance(descriptor, bool)
        or not isinstance(descriptor, int)
        or descriptor < 3
    ):
        if descriptor is None:
            _fail(
                "new and resumed sessions must be launched by `model-lab pi`",
                code="model_lab_use_authority_required",
            )
        _fail("model-lab supervisor descriptor is invalid")
    try:
        metadata = os.fstat(descriptor)
    except OSError as error:
        raise ModelSessionError(
            f"cannot inspect model-lab supervisor descriptor: {error}",
            code="invalid_model_lab_use_authority",
        ) from error
    try:
        channel = socket.socket(fileno=descriptor)
    except OSError as error:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise ModelSessionError(
            f"cannot adopt model-lab supervisor descriptor: {error}",
            code="invalid_model_lab_use_authority",
        ) from error
    try:
        if (
            not stat.S_ISSOCK(metadata.st_mode)
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            or channel.family != socket.AF_UNIX
            or channel.type & socket.SOCK_STREAM != socket.SOCK_STREAM
        ):
            _fail("model-lab use authority must be an owned Unix stream")
        expected_path = os.fspath(_supervisor_socket_path())
        if channel.getpeername() != expected_path:
            _fail(
                "model-lab use authority is not connected to the canonical "
                f"supervisor socket {expected_path}"
            )
        peer_pid, peer_uid, _peer_gid = _peer_credentials(channel)
        if hasattr(os, "getuid") and peer_uid != os.getuid():
            _fail("model-lab supervisor belongs to another user")
        if peer_pid < 1:
            _fail("model-lab supervisor peer PID is invalid")
        return channel, peer_pid
    except BaseException:
        channel.close()
        raise


def _parse_accepted(
    value: dict[str, Any],
    *,
    route: ProfileRoute,
    channel: socket.socket,
    peer_pid: int,
    session_pid: int,
    session_start_time: str,
) -> SessionUseAuthority:
    if set(value) != _ACCEPTED_KEYS:
        missing = sorted(_ACCEPTED_KEYS.difference(value))
        unsupported = sorted(set(value).difference(_ACCEPTED_KEYS))
        _fail(
            "model-lab session-use acceptance fields do not match the "
            f"protocol; missing={missing!r}, unsupported={unsupported!r}"
        )
    if value["schema"] != SESSION_USE_ACCEPTED_SCHEMA:
        _fail(
            "model-lab session-use acceptance schema must be "
            f"{SESSION_USE_ACCEPTED_SCHEMA!r}"
        )
    profile_id = _identifier(value["profile_id"], label="profile_id")
    service_id = _identifier(value["service_id"], label="service_id")
    workload_sha256 = value["workload_sha256"]
    if not isinstance(workload_sha256, str) or not _SHA256_PATTERN.fullmatch(
        workload_sha256
    ):
        _fail("workload_sha256 is invalid")
    deployment_id = _opaque_identifier(
        value["deployment_id"],
        label="deployment_id",
    )
    use_lease_id = _opaque_identifier(
        value["use_lease_id"],
        label="use_lease_id",
    )
    supervisor_pid = _positive_pid(
        value["supervisor_pid"],
        label="supervisor_pid",
    )
    supervisor_start_time = _process_start(
        value["supervisor_start_time"],
        label="supervisor_start_time",
    )
    accepted_session_pid = _positive_pid(
        value["session_pid"],
        label="session_pid",
    )
    accepted_session_start_time = _process_start(
        value["session_start_time"],
        label="session_start_time",
    )
    if profile_id != route.profile_id or service_id != route.service_id:
        _fail(
            "model-lab session-use acceptance does not match the profile",
            code="model_lab_use_authority_mismatch",
        )
    if supervisor_pid != peer_pid or supervisor_start_time != process_start_time(
        peer_pid
    ):
        _fail(
            "model-lab session-use acceptance belongs to another supervisor generation",
            code="model_lab_use_authority_parent_mismatch",
        )
    if (
        accepted_session_pid != session_pid
        or accepted_session_start_time != session_start_time
    ):
        _fail(
            "model-lab session-use acceptance belongs to another launcher generation",
            code="model_lab_use_authority_parent_mismatch",
        )
    return SessionUseAuthority(
        profile_id=profile_id,
        service_id=service_id,
        workload_sha256=workload_sha256,
        deployment_id=deployment_id,
        use_lease_id=use_lease_id,
        supervisor_pid=supervisor_pid,
        supervisor_start_time=supervisor_start_time,
        session_pid=accepted_session_pid,
        session_start_time=accepted_session_start_time,
        channel=channel,
    )


def _raise_supervisor_error(value: dict[str, Any]) -> None:
    if set(value) != _ERROR_KEYS:
        _fail("model-lab supervisor error has unsupported fields")
    code = value["code"]
    message = value["message"]
    if (
        not isinstance(code, str)
        or not _ERROR_CODE_PATTERN.fullmatch(code)
        or not isinstance(message, str)
        or not message
        or len(message.encode("utf-8")) > 4096
    ):
        _fail("model-lab supervisor error is invalid")
    raise ModelSessionError(message, code=code)


def read_session_use_authority(
    descriptor: int | None,
    route: ProfileRoute,
) -> SessionUseAuthority:
    """Admit this launcher over one supervisor-owned use connection."""

    channel, peer_pid = _open_channel(descriptor)
    session_pid = os.getpid()
    session_start_time = process_start_time(session_pid)
    request = {
        "schema": SESSION_USE_ADMISSION_SCHEMA,
        "profile_id": route.profile_id,
        "service_id": route.service_id,
        "pid": session_pid,
        "start_time": session_start_time,
    }
    try:
        channel.sendall(_canonical_json_bytes(request))
        response = _strict_json_object(_read_frame(channel))
        if response.get("schema") == SESSION_USE_ERROR_SCHEMA:
            _raise_supervisor_error(response)
        return _parse_accepted(
            response,
            route=route,
            channel=channel,
            peer_pid=peer_pid,
            session_pid=session_pid,
            session_start_time=session_start_time,
        )
    except BaseException:
        channel.close()
        raise


def attest_workload(
    authority: SessionUseAuthority,
    *,
    service_id: str,
    workload_sha256: str,
) -> None:
    """Bind the admitted supervisor lease to one exact live/frozen workload."""

    if (
        authority.service_id != service_id
        or authority.workload_sha256 != workload_sha256
    ):
        _fail(
            "model-lab use authority does not match the service workload",
            code="model_lab_use_authority_workload_mismatch",
        )
