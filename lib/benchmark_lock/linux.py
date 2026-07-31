"""Linux authority and process controls for benchmark admission."""

from __future__ import annotations

import ctypes
import os
import pathlib
import platform
import socket
import stat
import struct
import sys
from dataclasses import dataclass
from typing import Any

from .errors import BenchmarkLockError
from .protocol import enable_sender_credentials, require_seqpacket_channel


CONTROL_SOCKET_PATH = pathlib.Path("/run/benchmarkd/control.sock")
CONTROL_SOCKET_MODE = 0o660
ADDR_NO_RANDOMIZE = 0x0040000

_PERSONALITY_QUERY = 0xFFFFFFFF
_PEER_CREDENTIALS = struct.Struct("3i")

# SO_PEERPIDFD is a Linux UAPI macro and Python does not currently expose it.
# The value is architecture-specific: notably, parisc and sparc do not use the
# asm-generic number. Keep this list deliberately narrower than Linux rather
# than silently applying an incorrect socket option.
_SO_PEERPIDFD_BY_ARCHITECTURE = {
    "aarch64": 77,
    "riscv64": 77,
    "x86_64": 77,
}

_LIBC = ctypes.CDLL(None, use_errno=True)
_PERSONALITY: Any | None = getattr(_LIBC, "personality", None)
if _PERSONALITY is not None:
    _PERSONALITY.argtypes = (ctypes.c_ulong,)
    _PERSONALITY.restype = ctypes.c_int


@dataclass(frozen=True)
class PeerCredentials:
    """Kernel-authenticated process identity for one Unix peer."""

    pid: int
    uid: int
    gid: int

    def as_tuple(self) -> tuple[int, int, int]:
        return (self.pid, self.uid, self.gid)


@dataclass
class PeerIdentity:
    """Connected peer credentials and exact process-lifetime descriptor."""

    credentials: PeerCredentials
    pid_descriptor: int

    def close(self) -> None:
        """Close the owned pidfd exactly once."""

        descriptor = self.pid_descriptor
        if descriptor < 0:
            return
        self.pid_descriptor = -1
        os.close(descriptor)


def _fail(
    message: str,
    *,
    code: str = "invalid_benchmark_channel",
) -> None:
    raise BenchmarkLockError(message, code=code)


def peer_credentials(connection: socket.socket) -> PeerCredentials:
    """Read the connection-time PID, UID, and GID from SO_PEERCRED."""

    require_seqpacket_channel(connection)
    if not hasattr(socket, "SO_PEERCRED"):
        _fail(
            "Linux SO_PEERCRED is unavailable",
            code="benchmark_platform_unsupported",
        )
    try:
        payload = connection.getsockopt(
            socket.SOL_SOCKET,
            socket.SO_PEERCRED,
            _PEER_CREDENTIALS.size,
        )
        pid, uid, gid = _PEER_CREDENTIALS.unpack(payload)
    except (OSError, struct.error) as error:
        raise BenchmarkLockError(
            f"cannot attest benchmark peer credentials: {error}",
            code="invalid_benchmark_channel",
        ) from error
    if pid < 1 or uid < 0 or gid < 0:
        _fail("benchmark peer returned invalid credentials")
    return PeerCredentials(pid=pid, uid=uid, gid=gid)


def require_root_peer(connection: socket.socket) -> PeerCredentials:
    """Require that the connected broker was created by UID 0."""

    credentials = peer_credentials(connection)
    if credentials.uid != 0:
        _fail("benchmark broker peer is not root")
    return credentials


def open_peer_pidfd(
    connection: socket.socket,
    *,
    architecture: str | None = None,
) -> int:
    """Acquire the connected peer's race-free lifetime descriptor."""

    require_seqpacket_channel(connection)
    if sys.platform != "linux":
        _fail(
            "benchmark peer pidfds require Linux",
            code="benchmark_platform_unsupported",
        )
    machine = platform.machine() if architecture is None else architecture
    option = _SO_PEERPIDFD_BY_ARCHITECTURE.get(machine)
    if option is None:
        _fail(
            f"SO_PEERPIDFD is not mapped for Linux architecture {machine!r}",
            code="benchmark_platform_unsupported",
        )
    try:
        payload = connection.getsockopt(
            socket.SOL_SOCKET,
            option,
            struct.calcsize("i"),
        )
        descriptor = struct.unpack("i", payload)[0]
    except (OSError, struct.error) as error:
        raise BenchmarkLockError(
            f"cannot acquire benchmark peer pidfd: {error}",
            code="benchmark_peer_pidfd_unavailable",
        ) from error
    if descriptor < 0:
        _fail(
            "kernel returned an invalid benchmark peer pidfd",
            code="benchmark_peer_pidfd_unavailable",
        )
    try:
        os.set_inheritable(descriptor, False)
        os.fstat(descriptor)
    except OSError as error:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise BenchmarkLockError(
            f"cannot retain benchmark peer pidfd: {error}",
            code="benchmark_peer_pidfd_unavailable",
        ) from error
    return descriptor


def attest_client_peer(connection: socket.socket) -> PeerIdentity:
    """Prepare per-packet credentials and retain the client's exact lifetime."""

    enable_sender_credentials(connection)
    credentials = peer_credentials(connection)
    descriptor = open_peer_pidfd(connection)
    return PeerIdentity(
        credentials=credentials,
        pid_descriptor=descriptor,
    )


def validate_root_socket_path(
    path: pathlib.Path = CONTROL_SOCKET_PATH,
    *,
    expected_group_id: int,
) -> None:
    """Validate a root-owned broker socket and its non-writable parent."""

    if (
        isinstance(expected_group_id, bool)
        or not isinstance(expected_group_id, int)
        or expected_group_id < 0
    ):
        raise ValueError("expected benchmark group ID must be non-negative")
    candidate = pathlib.Path(path)
    if (
        not candidate.is_absolute()
        or pathlib.Path(os.path.normpath(candidate)) != candidate
    ):
        _fail("benchmark broker socket path is not canonical")
    try:
        parent_metadata = os.lstat(candidate.parent)
        socket_metadata = os.lstat(candidate)
    except OSError as error:
        raise BenchmarkLockError(
            f"cannot inspect benchmark broker socket: {error}",
            code="benchmark_broker_unavailable",
        ) from error
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_ISLNK(parent_metadata.st_mode)
        or parent_metadata.st_uid != 0
        or stat.S_IMODE(parent_metadata.st_mode) & 0o022
    ):
        _fail("benchmark broker runtime directory has an unsafe identity")
    if (
        not stat.S_ISSOCK(socket_metadata.st_mode)
        or stat.S_ISLNK(socket_metadata.st_mode)
        or socket_metadata.st_uid != 0
        or socket_metadata.st_gid != expected_group_id
        or stat.S_IMODE(socket_metadata.st_mode) != CONTROL_SOCKET_MODE
    ):
        _fail("benchmark broker socket has an unsafe identity")


def disable_aslr_for_exec() -> int:
    """Set ADDR_NO_RANDOMIZE on this process and return its prior personality."""

    if sys.platform != "linux" or _PERSONALITY is None:
        _fail(
            "process-scoped ASLR control requires Linux personality()",
            code="benchmark_platform_unsupported",
        )
    ctypes.set_errno(0)
    current = _PERSONALITY(_PERSONALITY_QUERY)
    if current == -1:
        error_number = ctypes.get_errno()
        raise BenchmarkLockError(
            f"cannot read process personality: {os.strerror(error_number)}",
            code="benchmark_aslr_control_failed",
        )
    if current & ADDR_NO_RANDOMIZE:
        return current
    ctypes.set_errno(0)
    if _PERSONALITY(current | ADDR_NO_RANDOMIZE) == -1:
        error_number = ctypes.get_errno()
        raise BenchmarkLockError(
            f"cannot disable ASLR for benchmark process: {os.strerror(error_number)}",
            code="benchmark_aslr_control_failed",
        )
    ctypes.set_errno(0)
    verified = _PERSONALITY(_PERSONALITY_QUERY)
    if verified == -1:
        error_number = ctypes.get_errno()
        raise BenchmarkLockError(
            f"cannot verify benchmark process personality: {os.strerror(error_number)}",
            code="benchmark_aslr_control_failed",
        )
    if not verified & ADDR_NO_RANDOMIZE:
        _fail(
            "kernel did not retain process-scoped ASLR control",
            code="benchmark_aslr_control_failed",
        )
    return current
