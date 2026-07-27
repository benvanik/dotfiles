"""Hard-bounded anonymous storage for one model-session process.

An unprivileged one-shot creator owns a private user and mount namespace,
mounts two independent tmpfs filesystems, and returns their roots plus both
namespace identities through one fixed ``SCM_RIGHTS`` protocol.  The caller
retains those descriptors after the creator exits; no host-writable tree is
needed to keep either filesystem alive.

Bubblewrap must begin inside the mount namespace that owns these tmpfs
mounts.  ``StorageNamespace.wrap_bwrap_argv`` therefore prefixes an unchanged
bubblewrap argv with a direct-child trampoline.  The trampoline enters the
retained user namespace and then its mount namespace before replacing itself
with bubblewrap.
"""

from __future__ import annotations

import array
import ctypes
import fcntl
import os
import pathlib
import secrets
import select
import signal
import socket
import stat
import struct
import subprocess
from collections.abc import Sequence
from types import TracebackType
from typing import Self

import model_session.storage_launch as _storage_launch
from model_session.errors import ModelSessionError
from model_session.storage_launch import (
    BWRAP_BINARY,
    PYTHON_BINARY,
    SETPRIV_BINARY,
    StorageLaunch,
)
from model_session.storage_limits import StoragePoolLimits


UNSHARE_BINARY = pathlib.Path("/usr/bin/unshare")

TMPFS_MAGIC = 0x01021994
NS_GET_USERNS = 0xB701
NS_GET_PARENT = 0xB702
NS_GET_NSTYPE = 0xB703

_PROTOCOL_MAGIC = b"model-session.storage.v1"
_PROTOCOL = struct.Struct("!24s32sQQQQq")
_PROTOCOL_DESCRIPTOR_COUNT = 4
_MAX_ANCILLARY_DESCRIPTOR_COUNT = _PROTOCOL_DESCRIPTOR_COUNT + 1
_MAX_HELPER_ERROR_BYTES = 64 * 1024
_CREATOR_COMMAND = "--model-session-storage-creator"
_OWNER_TOKEN = object()
_PEER_CREDENTIALS = struct.Struct("=iii")
TRUSTED_NAMESPACE_HELPERS = _storage_launch.TRUSTED_NAMESPACE_HELPERS


class _StatFS(ctypes.Structure):
    _fields_ = (
        ("f_type", ctypes.c_long),
        ("f_bsize", ctypes.c_long),
        ("f_blocks", ctypes.c_ulong),
        ("f_bfree", ctypes.c_ulong),
        ("f_bavail", ctypes.c_ulong),
        ("f_files", ctypes.c_ulong),
        ("f_ffree", ctypes.c_ulong),
        ("f_fsid", ctypes.c_int * 2),
        ("f_namelen", ctypes.c_long),
        ("f_frsize", ctypes.c_long),
        ("f_flags", ctypes.c_long),
        ("f_spare", ctypes.c_long * 4),
    )


_LIBC = ctypes.CDLL(None, use_errno=True)
_LIBC.fstatfs.argtypes = (ctypes.c_int, ctypes.POINTER(_StatFS))
_LIBC.fstatfs.restype = ctypes.c_int


def _fail(
    message: str,
    *,
    code: str = "storage_namespace_failed",
) -> None:
    raise ModelSessionError(message, code=code)


def _descriptor_identity(descriptor: int) -> tuple[int, int, int]:
    return _storage_launch.descriptor_identity(descriptor)


def _close_if_owned(
    descriptor: int,
    identity: tuple[int, int, int],
) -> None:
    _storage_launch.close_if_owned(descriptor, identity)


class StorageNamespace:
    """Owned roots and namespace identities for two anonymous tmpfs pools."""

    __slots__ = (
        "_closed",
        "_history_descriptor",
        "_history_limits",
        "_mount_namespace_descriptor",
        "_user_namespace_descriptor",
        "_work_descriptor",
        "_work_limits",
        "_owned_identities",
    )

    def __init__(
        self,
        token: object,
        *,
        work_descriptor: int,
        history_descriptor: int,
        user_namespace_descriptor: int,
        mount_namespace_descriptor: int,
        work_limits: StoragePoolLimits,
        history_limits: StoragePoolLimits,
    ) -> None:
        self._closed = True
        self._owned_identities: dict[
            int,
            tuple[int, int, int],
        ] = {}
        if token is not _OWNER_TOKEN:
            _fail(
                "storage namespaces must be created by "
                "create_storage_namespace",
                code="invalid_storage_namespace",
            )
        self._work_descriptor = work_descriptor
        self._history_descriptor = history_descriptor
        self._user_namespace_descriptor = user_namespace_descriptor
        self._mount_namespace_descriptor = mount_namespace_descriptor
        self._work_limits = work_limits
        self._history_limits = history_limits
        self._owned_identities = {
            descriptor: _descriptor_identity(descriptor)
            for descriptor in (
                work_descriptor,
                history_descriptor,
                user_namespace_descriptor,
                mount_namespace_descriptor,
            )
        }
        self._closed = False

    def _require_open(self) -> None:
        if self._closed:
            _fail(
                "storage namespace is closed",
                code="storage_namespace_closed",
            )
        for descriptor, identity in self._owned_identities.items():
            try:
                current_identity = _descriptor_identity(descriptor)
            except OSError as error:
                raise ModelSessionError(
                    "storage namespace authority descriptor is no longer open",
                    code="invalid_storage_namespace",
                ) from error
            if current_identity != identity:
                _fail(
                    "storage namespace authority descriptor was replaced",
                    code="invalid_storage_namespace",
                )

    @property
    def work_descriptor(self) -> int:
        self._require_open()
        return self._work_descriptor

    @property
    def history_descriptor(self) -> int:
        self._require_open()
        return self._history_descriptor

    @property
    def user_namespace_descriptor(self) -> int:
        self._require_open()
        return self._user_namespace_descriptor

    @property
    def mount_namespace_descriptor(self) -> int:
        self._require_open()
        return self._mount_namespace_descriptor

    @property
    def work_limits(self) -> StoragePoolLimits:
        return self._work_limits

    @property
    def history_limits(self) -> StoragePoolLimits:
        return self._history_limits

    def wrap_bwrap_argv(
        self,
        bwrap_argv: Sequence[str],
        *,
        pass_fds: Sequence[int] = (),
        workload_cgroup_procs_fd: int | None = None,
    ) -> StorageLaunch:
        """Wrap an unchanged bwrap argv with the trusted namespace trampoline."""

        self._require_open()
        if isinstance(bwrap_argv, (str, bytes)) or not bwrap_argv:
            _fail(
                "bubblewrap argv must be a nonempty argument sequence",
                code="invalid_storage_launch",
            )
        arguments = tuple(bwrap_argv)
        if (
            arguments[0] != os.fspath(BWRAP_BINARY)
            or any(
                not isinstance(argument, str) or "\x00" in argument
                for argument in arguments
            )
        ):
            _fail(
                f"storage trampoline requires exact {BWRAP_BINARY} argv",
                code="invalid_storage_launch",
            )

        inherited: list[int] = []
        for descriptor in (
            *pass_fds,
            self._work_descriptor,
            self._history_descriptor,
            self._user_namespace_descriptor,
            self._mount_namespace_descriptor,
        ):
            _validate_open_descriptor(
                descriptor,
                label="storage launch descriptor",
            )
            if descriptor not in inherited:
                inherited.append(descriptor)

        if workload_cgroup_procs_fd is None:
            cgroup_descriptor = -1
        else:
            _validate_cgroup_descriptor(workload_cgroup_procs_fd)
            cgroup_descriptor = workload_cgroup_procs_fd
            if cgroup_descriptor not in inherited:
                inherited.append(cgroup_descriptor)

        return self._wrap_namespace_exec(
            arguments,
            command_kind="bwrap",
            inherited=inherited,
            cgroup_descriptor=cgroup_descriptor,
            helper_descriptor=-1,
            parent_death_signal_name="KILL",
        )

    def wrap_namespace_command(
        self,
        command: Sequence[str],
        *,
        pass_fds: Sequence[int] = (),
        workload_cgroup_procs_fd: int | None = None,
        parent_death_signal: signal.Signals = signal.SIGKILL,
    ) -> StorageLaunch:
        """Run one exact dotfiles Python helper with U1 ownership authority."""

        self._require_open()
        death_signal_name = _parent_death_signal_name(parent_death_signal)
        arguments, helper_descriptor = _lock_trusted_namespace_command(command)
        try:
            inherited: list[int] = []
            for descriptor in (
                *pass_fds,
                self._work_descriptor,
                self._history_descriptor,
                self._user_namespace_descriptor,
                self._mount_namespace_descriptor,
            ):
                _validate_open_descriptor(
                    descriptor,
                    label="trusted namespace command descriptor",
                )
                if descriptor not in inherited:
                    inherited.append(descriptor)
            if workload_cgroup_procs_fd is None:
                cgroup_descriptor = -1
            else:
                _validate_cgroup_descriptor(workload_cgroup_procs_fd)
                cgroup_descriptor = workload_cgroup_procs_fd
                if cgroup_descriptor not in inherited:
                    inherited.append(cgroup_descriptor)
            inherited.append(helper_descriptor)
            launch = self._wrap_namespace_exec(
                arguments,
                command_kind="trusted-python",
                inherited=inherited,
                cgroup_descriptor=cgroup_descriptor,
                helper_descriptor=helper_descriptor,
                parent_death_signal_name=death_signal_name,
            )
        except BaseException:
            os.close(helper_descriptor)
            raise
        return launch

    def _wrap_namespace_exec(
        self,
        arguments: tuple[str, ...],
        *,
        command_kind: str,
        inherited: list[int],
        cgroup_descriptor: int,
        helper_descriptor: int,
        parent_death_signal_name: str,
    ) -> StorageLaunch:
        return _storage_launch.build_namespace_launch(
            arguments,
            command_kind=command_kind,
            inherited=inherited,
            user_namespace_descriptor=self._user_namespace_descriptor,
            mount_namespace_descriptor=self._mount_namespace_descriptor,
            work_descriptor=self._work_descriptor,
            history_descriptor=self._history_descriptor,
            cgroup_descriptor=cgroup_descriptor,
            helper_descriptor=helper_descriptor,
            parent_death_signal_name=parent_death_signal_name,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        descriptors = (
            self._mount_namespace_descriptor,
            self._user_namespace_descriptor,
            self._history_descriptor,
            self._work_descriptor,
        )
        for descriptor in descriptors:
            _close_if_owned(
                descriptor,
                self._owned_identities[descriptor],
            )
        self._owned_identities = {}

    def __enter__(self) -> Self:
        self._require_open()
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()


def _validate_open_descriptor(descriptor: int, *, label: str) -> None:
    _storage_launch.validate_open_descriptor(descriptor, label=label)


def _validate_cgroup_descriptor(descriptor: int) -> None:
    _storage_launch.validate_cgroup_descriptor(descriptor)


def _parent_death_signal_name(value: signal.Signals) -> str:
    return _storage_launch.parent_death_signal_name(value)


def _lock_trusted_namespace_command(
    command: Sequence[str],
) -> tuple[tuple[str, ...], int]:
    return _storage_launch.lock_trusted_namespace_command(
        command,
        trusted_helpers=TRUSTED_NAMESPACE_HELPERS,
    )


def _namespace_type(descriptor: int, *, label: str) -> int:
    try:
        return fcntl.ioctl(descriptor, NS_GET_NSTYPE)
    except OSError as error:
        raise ModelSessionError(
            f"{label} is not a Linux namespace descriptor: {error}",
            code="invalid_storage_namespace",
        ) from error


def _same_file(first: int, second: int) -> bool:
    first_metadata = os.fstat(first)
    second_metadata = os.fstat(second)
    return (
        first_metadata.st_dev == second_metadata.st_dev
        and first_metadata.st_ino == second_metadata.st_ino
    )


def _ioctl_namespace_descriptor(
    descriptor: int,
    operation: int,
    *,
    label: str,
) -> int:
    try:
        result = fcntl.ioctl(descriptor, operation)
    except OSError as error:
        raise ModelSessionError(
            f"cannot inspect {label}: {error}",
            code="invalid_storage_namespace",
        ) from error
    try:
        fcntl.fcntl(result, fcntl.F_SETFD, fcntl.FD_CLOEXEC)
        return result
    except BaseException:
        os.close(result)
        raise


def _validate_namespace_descriptors(
    user_descriptor: int,
    mount_descriptor: int,
) -> None:
    if _namespace_type(user_descriptor, label="storage user namespace") != (
        os.CLONE_NEWUSER
    ):
        _fail(
            "storage user namespace descriptor has the wrong namespace type",
            code="invalid_storage_namespace",
        )
    if _namespace_type(mount_descriptor, label="storage mount namespace") != (
        os.CLONE_NEWNS
    ):
        _fail(
            "storage mount namespace descriptor has the wrong namespace type",
            code="invalid_storage_namespace",
        )

    current_user = -1
    current_mount = -1
    parent_user = -1
    mount_owner = -1
    try:
        current_user = os.open(
            "/proc/self/ns/user",
            os.O_RDONLY | os.O_CLOEXEC,
        )
        current_mount = os.open(
            "/proc/self/ns/mnt",
            os.O_RDONLY | os.O_CLOEXEC,
        )
        if _same_file(user_descriptor, current_user):
            _fail(
                "storage creator did not enter a new user namespace",
                code="invalid_storage_namespace",
            )
        if _same_file(mount_descriptor, current_mount):
            _fail(
                "storage creator did not enter a new mount namespace",
                code="invalid_storage_namespace",
            )
        parent_user = _ioctl_namespace_descriptor(
            user_descriptor,
            NS_GET_PARENT,
            label="storage user namespace parent",
        )
        if not _same_file(parent_user, current_user):
            _fail(
                "storage user namespace is not a direct child of the caller",
                code="invalid_storage_namespace",
            )
        mount_owner = _ioctl_namespace_descriptor(
            mount_descriptor,
            NS_GET_USERNS,
            label="storage mount namespace owner",
        )
        if not _same_file(mount_owner, user_descriptor):
            _fail(
                "storage mount namespace is not owned by its user namespace",
                code="invalid_storage_namespace",
            )
    finally:
        for descriptor in (
            mount_owner,
            parent_user,
            current_mount,
            current_user,
        ):
            if descriptor >= 0:
                os.close(descriptor)


def _filesystem_type(descriptor: int) -> int:
    result = _StatFS()
    if _LIBC.fstatfs(descriptor, ctypes.byref(result)) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    return result.f_type


def _validate_pool_descriptor(
    descriptor: int,
    limits: StoragePoolLimits,
    *,
    label: str,
) -> os.stat_result:
    try:
        metadata = os.fstat(descriptor)
        filesystem_type = _filesystem_type(descriptor)
        capacity = os.fstatvfs(descriptor)
    except OSError as error:
        raise ModelSessionError(
            f"cannot inspect {label}: {error}",
            code="invalid_storage_namespace",
        ) from error
    if not stat.S_ISDIR(metadata.st_mode):
        _fail(
            f"{label} is not a directory",
            code="invalid_storage_namespace",
        )
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        _fail(
            f"{label} mode is not 0700",
            code="invalid_storage_namespace",
        )
    if metadata.st_uid != os.getuid() or metadata.st_gid != os.getgid():
        _fail(
            f"{label} ownership does not map to the caller",
            code="invalid_storage_namespace",
        )
    if filesystem_type != TMPFS_MAGIC:
        _fail(
            f"{label} is not tmpfs",
            code="invalid_storage_namespace",
        )
    capacity_bytes = capacity.f_blocks * capacity.f_frsize
    if capacity_bytes != limits.bytes:
        _fail(
            f"{label} capacity is {capacity_bytes}, expected {limits.bytes}",
            code="invalid_storage_namespace",
        )
    if capacity.f_files != limits.inodes:
        _fail(
            f"{label} inode capacity is {capacity.f_files}, "
            f"expected {limits.inodes}",
            code="invalid_storage_namespace",
        )
    required_flags = os.ST_NOSUID | os.ST_NODEV
    if capacity.f_flag & required_flags != required_flags:
        _fail(
            f"{label} is missing nosuid or nodev",
            code="invalid_storage_namespace",
        )
    return metadata


def _validate_received_descriptors(
    descriptors: tuple[int, int, int, int],
    *,
    work_limits: StoragePoolLimits,
    history_limits: StoragePoolLimits,
) -> None:
    work, history, user_namespace, mount_namespace = descriptors
    work_metadata = _validate_pool_descriptor(
        work,
        work_limits,
        label="work tmpfs root",
    )
    history_metadata = _validate_pool_descriptor(
        history,
        history_limits,
        label="history tmpfs root",
    )
    if work_metadata.st_dev == history_metadata.st_dev:
        _fail(
            "work and history roots share one filesystem",
            code="invalid_storage_namespace",
        )
    _validate_namespace_descriptors(user_namespace, mount_namespace)


def _decode_creator_ancillary(
    ancillary: list[tuple[int, int, bytes]],
) -> tuple[list[int], tuple[int, int, int]]:
    item_size = array.array("i").itemsize
    descriptors: list[int] = []
    rights_records = 0
    credential_records = 0
    credentials: tuple[int, int, int] | None = None
    try:
        for level, kind, value in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                rights_records += 1
                if len(value) % item_size:
                    _fail(
                        "storage creator sent malformed descriptor rights",
                        code="invalid_storage_protocol",
                    )
                rights = array.array("i")
                rights.frombytes(value)
                descriptors.extend(rights)
            elif (
                level == socket.SOL_SOCKET
                and kind == socket.SCM_CREDENTIALS
            ):
                credential_records += 1
                if len(value) != _PEER_CREDENTIALS.size:
                    _fail(
                        "storage creator sent malformed peer credentials",
                        code="invalid_storage_protocol",
                    )
                credentials = _PEER_CREDENTIALS.unpack(value)
            else:
                _fail(
                    "storage creator sent an unexpected ancillary record",
                    code="invalid_storage_protocol",
                )
        if rights_records != 1 or credential_records != 1:
            _fail(
                "storage creator did not send one rights and one credential "
                "record",
                code="invalid_storage_protocol",
            )
        if credentials is None:
            raise AssertionError("credential record count lost its value")
        return descriptors, credentials
    except BaseException:
        for descriptor in descriptors:
            os.close(descriptor)
        raise


def _receive_packet(
    sock: socket.socket,
) -> tuple[bytes, list[int], tuple[int, int, int] | None] | None:
    item_size = array.array("i").itemsize
    try:
        payload, ancillary, flags, _address = sock.recvmsg(
            _PROTOCOL.size + 1,
            (
                socket.CMSG_SPACE(
                    item_size * _MAX_ANCILLARY_DESCRIPTOR_COUNT
                )
                + socket.CMSG_SPACE(_PEER_CREDENTIALS.size)
            ),
            socket.MSG_CMSG_CLOEXEC,
        )
    except BlockingIOError:
        return None
    if payload == b"" and not ancillary:
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            _fail(
                "storage creator EOF was truncated",
                code="invalid_storage_protocol",
            )
        return payload, [], None
    descriptors, credentials = _decode_creator_ancillary(ancillary)
    if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
        for descriptor in descriptors:
            os.close(descriptor)
        _fail(
            "storage creator protocol was truncated",
            code="invalid_storage_protocol",
        )
    return payload, descriptors, credentials


def _read_available(
    descriptor: int,
    destination: bytearray,
) -> bool:
    """Drain one nonblocking pipe; return true only after EOF."""

    while True:
        try:
            value = os.read(descriptor, 8192)
        except BlockingIOError:
            return False
        except InterruptedError:
            continue
        if not value:
            return True
        remaining = _MAX_HELPER_ERROR_BYTES - len(destination)
        if remaining > 0:
            destination.extend(value[:remaining])


def _run_creator(
    command: tuple[str, ...],
    *,
    child_socket: socket.socket,
    parent_socket: socket.socket,
) -> tuple[int, bytes, tuple[int, int, int, int]]:
    if not hasattr(os, "pidfd_open"):
        _fail(
            "storage namespace creation requires Linux pidfds",
            code="storage_namespace_platform_unsupported",
        )
    try:
        process = subprocess.Popen(
            command,
            pass_fds=(child_socket.fileno(),),
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env={
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin",
            },
        )
    except OSError as error:
        raise ModelSessionError(
            f"cannot start storage namespace creator: {error}",
            code="storage_namespace_unavailable",
        ) from error
    packet: tuple[bytes, list[int]] | None = None
    pid_descriptor = -1
    stderr_stream = process.stderr
    try:
        child_socket.close()
        if stderr_stream is None:
            _fail(
                "storage namespace creator omitted its error pipe",
                code="storage_namespace_unavailable",
            )
        stderr_descriptor = stderr_stream.fileno()
        os.set_blocking(stderr_descriptor, False)
        parent_socket.setblocking(False)
        try:
            pid_descriptor = os.pidfd_open(process.pid, 0)
        except OSError as error:
            raise ModelSessionError(
                f"cannot retain storage creator process identity: {error}",
                code="storage_namespace_unavailable",
            ) from error
        poller = select.poll()
        poller.register(
            parent_socket.fileno(),
            select.POLLIN | select.POLLHUP | select.POLLERR,
        )
        poller.register(
            stderr_descriptor,
            select.POLLIN | select.POLLHUP | select.POLLERR,
        )
        poller.register(
            pid_descriptor,
            select.POLLIN | select.POLLHUP | select.POLLERR,
        )

        socket_finished = False
        stderr_finished = False
        process_finished = False
        stderr = bytearray()
        while not (socket_finished and stderr_finished and process_finished):
            try:
                events = poller.poll()
            except InterruptedError:
                continue
            for descriptor, _events in events:
                if descriptor == parent_socket.fileno():
                    received = _receive_packet(parent_socket)
                    if received is None:
                        continue
                    (
                        payload,
                        received_descriptors,
                        peer_credentials,
                    ) = received
                    if (
                        payload == b""
                        and not received_descriptors
                        and peer_credentials is None
                    ):
                        poller.unregister(parent_socket.fileno())
                        socket_finished = True
                    elif peer_credentials != (
                        process.pid,
                        os.getuid(),
                        os.getgid(),
                    ):
                        for received_descriptor in received_descriptors:
                            os.close(received_descriptor)
                        _fail(
                            "storage creator packet peer is not the direct "
                            "setpriv/unshare child",
                            code="invalid_storage_protocol",
                        )
                    elif packet is not None:
                        for received_descriptor in received_descriptors:
                            os.close(received_descriptor)
                        _fail(
                            "storage creator sent more than one protocol packet",
                            code="invalid_storage_protocol",
                        )
                    else:
                        packet = (payload, received_descriptors)
                elif descriptor == stderr_descriptor:
                    if _read_available(stderr_descriptor, stderr):
                        poller.unregister(stderr_descriptor)
                        stderr_finished = True
                elif descriptor == pid_descriptor:
                    poller.unregister(pid_descriptor)
                    process.wait()
                    process_finished = True
        if packet is None:
            rendered = bytes(stderr).decode("utf-8", errors="replace").strip()
            suffix = f": {rendered}" if rendered else ""
            _fail(
                "storage namespace creator exited without a protocol packet"
                f"{suffix}",
                code="storage_namespace_creator_failed",
            )
        if process.returncode != 0:
            rendered = bytes(stderr).decode("utf-8", errors="replace").strip()
            suffix = f": {rendered}" if rendered else ""
            _fail(
                f"storage namespace creator exited {process.returncode}"
                f"{suffix}",
                code="storage_namespace_creator_failed",
            )
        if stderr:
            rendered = bytes(stderr).decode("utf-8", errors="replace").strip()
            _fail(
                "storage namespace creator wrote unexpected diagnostics: "
                f"{rendered}",
                code="storage_namespace_creator_failed",
            )
        payload, received_descriptors = packet
        if len(received_descriptors) != _PROTOCOL_DESCRIPTOR_COUNT:
            _fail(
                "storage creator sent the wrong descriptor count",
                code="invalid_storage_protocol",
            )
        for descriptor in received_descriptors:
            if not (
                fcntl.fcntl(descriptor, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
            ):
                _fail(
                    "storage creator returned a non-CLOEXEC descriptor",
                    code="invalid_storage_protocol",
                )
        return process.pid, payload, (
            received_descriptors[0],
            received_descriptors[1],
            received_descriptors[2],
            received_descriptors[3],
        )
    except BaseException:
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        while True:
            try:
                process.wait()
                break
            except InterruptedError:
                continue
        if packet is not None:
            for descriptor in packet[1]:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        raise
    finally:
        if pid_descriptor >= 0:
            os.close(pid_descriptor)
        if stderr_stream is not None:
            stderr_stream.close()


def create_storage_namespace(
    *,
    work_bytes: int,
    work_inodes: int,
    history_bytes: int,
    history_inodes: int,
) -> StorageNamespace:
    """Create two anonymous hard-bounded tmpfs roots and retain their U1."""

    work_limits = StoragePoolLimits(work_bytes, work_inodes)
    history_limits = StoragePoolLimits(history_bytes, history_inodes)
    if (
        not hasattr(os, "setns")
        or not hasattr(socket, "SOCK_SEQPACKET")
        or not hasattr(socket, "SO_PASSCRED")
        or not hasattr(socket, "SCM_CREDENTIALS")
    ):
        _fail(
            "storage namespaces require Linux setns, SOCK_SEQPACKET, and "
            "SCM_CREDENTIALS",
            code="storage_namespace_platform_unsupported",
        )

    nonce = secrets.token_bytes(32)
    root = pathlib.Path("/tmp") / (
        f"model-session-storage.{nonce.hex()}"
    )
    work_path = root / "work"
    history_path = root / "history"
    parent_socket: socket.socket | None = None
    child_socket: socket.socket | None = None
    descriptors: tuple[int, int, int, int] | None = None
    primary_error: BaseException | None = None
    try:
        parent_socket, child_socket = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET,
        )
        parent_socket.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_PASSCRED,
            1,
        )
        command = (
            os.fspath(SETPRIV_BINARY),
            "--pdeathsig",
            "KILL",
            os.fspath(UNSHARE_BINARY),
            "--user",
            "--map-root-user",
            "--mount",
            "--propagation",
            "private",
            os.fspath(PYTHON_BINARY),
            "-I",
            "-B",
            "-c",
            _storage_launch.snapshot_namespace_child_source(),
            _CREATOR_COMMAND,
            str(child_socket.fileno()),
            str(os.getpid()),
            os.fspath(work_path),
            os.fspath(history_path),
            str(work_limits.bytes),
            str(work_limits.inodes),
            str(history_limits.bytes),
            str(history_limits.inodes),
            nonce.hex(),
        )
        expected_creator_pid, payload, descriptors = _run_creator(
            command,
            child_socket=child_socket,
            parent_socket=parent_socket,
        )
        if len(payload) != _PROTOCOL.size:
            _fail(
                "storage creator sent a malformed protocol payload",
                code="invalid_storage_protocol",
            )
        (
            magic,
            received_nonce,
            received_work_bytes,
            received_work_inodes,
            received_history_bytes,
            received_history_inodes,
            creator_pid,
        ) = _PROTOCOL.unpack(payload)
        expected_values = (
            magic == _PROTOCOL_MAGIC,
            received_nonce == nonce,
            received_work_bytes == work_limits.bytes,
            received_work_inodes == work_limits.inodes,
            received_history_bytes == history_limits.bytes,
            received_history_inodes == history_limits.inodes,
        )
        if not all(expected_values):
            _fail(
                "storage creator protocol binding does not match the request",
                code="invalid_storage_protocol",
            )
        # setpriv and unshare must preserve one process identity through the
        # creator exec chain.  A hidden fork would weaken parent-death control.
        # _run_creator waits for that exact peer before returning.
        if creator_pid != expected_creator_pid or creator_pid <= 1:
            _fail(
                "storage creator protocol process identity does not match "
                "its direct setpriv/unshare exec chain",
                code="invalid_storage_protocol",
            )
        _validate_received_descriptors(
            descriptors,
            work_limits=work_limits,
            history_limits=history_limits,
        )
    except BaseException as error:
        primary_error = error
    finally:
        if parent_socket is not None:
            parent_socket.close()
        if child_socket is not None:
            try:
                child_socket.close()
            except OSError:
                pass
    if primary_error is not None:
        if descriptors is not None:
            for descriptor in descriptors:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        raise primary_error
    if descriptors is None:
        _fail(
            "storage creator returned no descriptors",
            code="invalid_storage_protocol",
        )
    try:
        return StorageNamespace(
            _OWNER_TOKEN,
            work_descriptor=descriptors[0],
            history_descriptor=descriptors[1],
            user_namespace_descriptor=descriptors[2],
            mount_namespace_descriptor=descriptors[3],
            work_limits=work_limits,
            history_limits=history_limits,
        )
    except BaseException:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
