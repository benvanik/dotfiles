"""Self-contained child executor for model-session storage namespaces.

The parent copies this exact stdlib-only source into ``python -c``.  It must
not import the model-session package: doing so would make an already-created
launch depend on mutable package paths again.
"""

from __future__ import annotations

import array
import ctypes
import errno
import fcntl
import os
import pathlib
import signal
import socket
import stat
import struct
import sys


BWRAP_BINARY = pathlib.Path("/usr/bin/bwrap")
TMPFS_MAGIC = 0x01021994
CGROUP2_MAGIC = 0x63677270
NS_GET_NSTYPE = 0xB703

_MOUNT_NOSUID = 2
_MOUNT_NODEV = 4
_PR_SET_PDEATHSIG = 1
_PROTOCOL_MAGIC = b"model-session.storage.v1"
_PROTOCOL = struct.Struct("!24s32sQQQQq")
_STAGING_TMP_INODES = 64
_CREATOR_COMMAND = "--model-session-storage-creator"
_TRAMPOLINE_COMMAND = "--model-session-storage-trampoline"
_MAX_STORAGE_POOL_BYTES = 1 << 50
_MAX_STORAGE_POOL_INODES = 1 << 40
_REQUIRED_HELPER_SEALS = (
    fcntl.F_SEAL_SEAL
    | fcntl.F_SEAL_SHRINK
    | fcntl.F_SEAL_GROW
    | fcntl.F_SEAL_WRITE
)
_ALLOWED_PARENT_DEATH_SIGNALS = frozenset(
    {
        signal.SIGHUP,
        signal.SIGINT,
        signal.SIGQUIT,
        signal.SIGKILL,
        signal.SIGTERM,
    }
)


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


class _StoragePoolLimits:
    """Child-side reconstruction of one already-validated pool limit."""

    __slots__ = ("bytes", "inodes")

    def __init__(self, byte_count: int, inode_count: int) -> None:
        page_size = os.sysconf("SC_PAGE_SIZE")
        if (
            isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count < page_size
            or byte_count > _MAX_STORAGE_POOL_BYTES
            or byte_count % page_size
        ):
            raise ValueError("storage pool byte limit is invalid")
        if (
            isinstance(inode_count, bool)
            or not isinstance(inode_count, int)
            or inode_count < 2
            or inode_count > _MAX_STORAGE_POOL_INODES
        ):
            raise ValueError("storage pool inode limit is invalid")
        self.bytes = byte_count
        self.inodes = inode_count


_LIBC = ctypes.CDLL(None, use_errno=True)
_LIBC.mount.argtypes = (
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_ulong,
    ctypes.c_char_p,
)
_LIBC.mount.restype = ctypes.c_int
_LIBC.fstatfs.argtypes = (ctypes.c_int, ctypes.POINTER(_StatFS))
_LIBC.fstatfs.restype = ctypes.c_int
_LIBC.prctl.argtypes = (
    ctypes.c_int,
    ctypes.c_ulong,
    ctypes.c_ulong,
    ctypes.c_ulong,
    ctypes.c_ulong,
)
_LIBC.prctl.restype = ctypes.c_int


def _descriptor_identity(descriptor: int) -> tuple[int, int, int]:
    metadata = os.fstat(descriptor)
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
    )


def _parse_descriptor_binding(
    value: str,
) -> tuple[int, tuple[int, int, int]]:
    fields = value.split(":")
    if len(fields) != 4:
        raise ValueError("trampoline descriptor binding is malformed")
    try:
        numbers = tuple(int(field) for field in fields)
    except ValueError as error:
        raise ValueError(
            "trampoline descriptor binding is malformed"
        ) from error
    if (
        any(number < 0 for number in numbers)
        or value != ":".join(str(number) for number in numbers)
    ):
        raise ValueError("trampoline descriptor binding is noncanonical")
    descriptor, device, inode, file_type = numbers
    return descriptor, (device, inode, file_type)


def _validate_descriptor_binding(
    descriptor: int,
    expected: tuple[int, int, int],
) -> None:
    try:
        current = _descriptor_identity(descriptor)
    except OSError as error:
        raise ValueError(
            "trampoline descriptor binding is no longer open"
        ) from error
    if current != expected:
        raise ValueError("trampoline descriptor identity was replaced")


def _namespace_type(descriptor: int, *, label: str) -> int:
    try:
        return fcntl.ioctl(descriptor, NS_GET_NSTYPE)
    except OSError as error:
        raise ValueError(
            f"{label} is not a Linux namespace descriptor: {error}"
        ) from error


def _filesystem_type(descriptor: int) -> int:
    result = _StatFS()
    if _LIBC.fstatfs(descriptor, ctypes.byref(result)) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    return result.f_type


def _validate_cgroup_descriptor(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
    descriptor_path = os.readlink(f"/proc/self/fd/{descriptor}")
    if (
        not stat.S_ISREG(metadata.st_mode)
        or _filesystem_type(descriptor) != CGROUP2_MAGIC
        or flags & os.O_ACCMODE not in {os.O_WRONLY, os.O_RDWR}
        or pathlib.PurePosixPath(descriptor_path).name != "cgroup.procs"
    ):
        raise ValueError(
            "workload cgroup.procs descriptor is not a writable "
            "cgroup-v2 kernel file"
        )


def _validate_locked_helper_command(
    command: tuple[str, ...],
    helper_descriptor: int,
) -> None:
    if (
        len(command) < 4
        or command[:3] != ("/usr/bin/python3", "-I", "-B")
        or command[3] != f"/proc/self/fd/{helper_descriptor}"
    ):
        raise ValueError("locked trusted Python helper command is malformed")
    metadata = os.fstat(helper_descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.getuid()}
        or stat.S_IMODE(metadata.st_mode) != 0o400
        or fcntl.fcntl(helper_descriptor, fcntl.F_GET_SEALS)
        != _REQUIRED_HELPER_SEALS
    ):
        raise ValueError("locked trusted Python helper identity is unsafe")


def _validate_pool_descriptor(
    descriptor: int,
    limits: _StoragePoolLimits,
    *,
    label: str,
) -> os.stat_result:
    metadata = os.fstat(descriptor)
    capacity = os.fstatvfs(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} is not a directory")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValueError(f"{label} mode is not 0700")
    if metadata.st_uid != os.getuid() or metadata.st_gid != os.getgid():
        raise ValueError(f"{label} ownership does not map to the caller")
    if _filesystem_type(descriptor) != TMPFS_MAGIC:
        raise ValueError(f"{label} is not tmpfs")
    if capacity.f_blocks * capacity.f_frsize != limits.bytes:
        raise ValueError(f"{label} byte capacity is incorrect")
    if capacity.f_files != limits.inodes:
        raise ValueError(f"{label} inode capacity is incorrect")
    required_flags = os.ST_NOSUID | os.ST_NODEV
    if capacity.f_flag & required_flags != required_flags:
        raise ValueError(f"{label} is missing nosuid or nodev")
    return metadata


def _validate_creator_descriptors(
    descriptors: tuple[int, int, int, int],
    *,
    work_limits: _StoragePoolLimits,
    history_limits: _StoragePoolLimits,
) -> None:
    work, history, user_namespace, mount_namespace = descriptors
    work_metadata = _validate_pool_descriptor(
        work,
        work_limits,
        label="creator work tmpfs root",
    )
    history_metadata = _validate_pool_descriptor(
        history,
        history_limits,
        label="creator history tmpfs root",
    )
    if work_metadata.st_dev == history_metadata.st_dev:
        raise ValueError(
            "creator work and history roots share one filesystem"
        )
    if (
        _namespace_type(
            user_namespace,
            label="creator user namespace",
        )
        != os.CLONE_NEWUSER
        or _namespace_type(
            mount_namespace,
            label="creator mount namespace",
        )
        != os.CLONE_NEWNS
    ):
        raise ValueError(
            "creator returned incorrect namespace descriptor types"
        )


def _mount_tmpfs(
    path: str,
    limits: _StoragePoolLimits,
    *,
    source: str,
) -> int:
    options = (
        f"size={limits.bytes},nr_inodes={limits.inodes},mode=0700"
    )
    result = _LIBC.mount(
        source.encode("ascii"),
        os.fsencode(path),
        b"tmpfs",
        _MOUNT_NOSUID | _MOUNT_NODEV,
        options.encode("ascii"),
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            f"cannot mount {source} at {path}: "
            f"{os.strerror(error_number)}",
        )
    return os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )


def _prepare_private_mountpoints(
    work_path: str,
    history_path: str,
) -> None:
    work = pathlib.Path(work_path)
    history = pathlib.Path(history_path)
    root = work.parent
    if (
        history.parent != root
        or root.parent != pathlib.Path("/tmp")
        or not root.name.startswith("model-session-storage.")
    ):
        raise ValueError("creator mountpoints are outside private /tmp scratch")
    page_size = os.sysconf("SC_PAGE_SIZE")
    staging_options = (
        f"size={page_size * 16},nr_inodes={_STAGING_TMP_INODES},mode=1777"
    )
    if (
        _LIBC.mount(
            b"model-session-staging",
            b"/tmp",
            b"tmpfs",
            _MOUNT_NOSUID | _MOUNT_NODEV,
            staging_options.encode("ascii"),
        )
        != 0
    ):
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            f"cannot mount private storage staging /tmp: "
            f"{os.strerror(error_number)}",
        )
    root.mkdir(mode=0o700)
    work.mkdir(mode=0o700)
    history.mkdir(mode=0o700)


def _send_creator_packet(
    sock: socket.socket,
    payload: bytes,
    descriptors: tuple[int, int, int, int],
) -> None:
    rights = array.array("i", descriptors)
    written = sock.sendmsg(
        [payload],
        [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights.tobytes())],
    )
    if written != len(payload):
        raise RuntimeError("creator protocol packet was partially sent")


def _arm_parent_death(
    expected_parent_pid: int,
    death_signal: signal.Signals,
) -> None:
    if (
        expected_parent_pid <= 1
        or death_signal not in _ALLOWED_PARENT_DEATH_SIGNALS
    ):
        raise ValueError("parent-death contract is invalid")
    if (
        _LIBC.prctl(
            _PR_SET_PDEATHSIG,
            int(death_signal),
            0,
            0,
            0,
        )
        != 0
    ):
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            f"cannot arm parent-death signal: "
            f"{os.strerror(error_number)}",
        )
    if os.getppid() != expected_parent_pid:
        os.kill(os.getpid(), signal.SIGKILL)
        raise RuntimeError("process survived its expected parent")


def _parse_creator_arguments(arguments: list[str]) -> tuple[
    int,
    int,
    str,
    str,
    _StoragePoolLimits,
    _StoragePoolLimits,
    bytes,
]:
    if len(arguments) != 9:
        raise ValueError("creator received the wrong argument count")
    socket_descriptor = int(arguments[0])
    parent_pid = int(arguments[1])
    if parent_pid <= 1:
        raise ValueError("creator parent PID is invalid")
    work_path = arguments[2]
    history_path = arguments[3]
    if (
        not pathlib.Path(work_path).is_absolute()
        or os.path.normpath(work_path) != work_path
        or not pathlib.Path(history_path).is_absolute()
        or os.path.normpath(history_path) != history_path
    ):
        raise ValueError("creator mount paths are not normalized absolute paths")
    work_limits = _StoragePoolLimits(
        int(arguments[4]),
        int(arguments[5]),
    )
    history_limits = _StoragePoolLimits(
        int(arguments[6]),
        int(arguments[7]),
    )
    nonce = bytes.fromhex(arguments[8])
    if len(nonce) != 32 or arguments[8] != nonce.hex():
        raise ValueError("creator nonce is malformed")
    return (
        socket_descriptor,
        parent_pid,
        work_path,
        history_path,
        work_limits,
        history_limits,
        nonce,
    )


def _creator_main(arguments: list[str]) -> int:
    (
        socket_descriptor,
        parent_pid,
        work_path,
        history_path,
        work_limits,
        history_limits,
        nonce,
    ) = _parse_creator_arguments(arguments)
    _arm_parent_death(parent_pid, signal.SIGKILL)
    sock = socket.socket(fileno=socket_descriptor)
    descriptors: list[int] = []
    try:
        _prepare_private_mountpoints(work_path, history_path)
        work_descriptor = _mount_tmpfs(
            work_path,
            work_limits,
            source="model-session-work",
        )
        descriptors.append(work_descriptor)
        history_descriptor = _mount_tmpfs(
            history_path,
            history_limits,
            source="model-session-history",
        )
        descriptors.append(history_descriptor)
        user_descriptor = os.open(
            "/proc/self/ns/user",
            os.O_RDONLY | os.O_CLOEXEC,
        )
        descriptors.append(user_descriptor)
        mount_descriptor = os.open(
            "/proc/self/ns/mnt",
            os.O_RDONLY | os.O_CLOEXEC,
        )
        descriptors.append(mount_descriptor)
        _validate_creator_descriptors(
            (
                work_descriptor,
                history_descriptor,
                user_descriptor,
                mount_descriptor,
            ),
            work_limits=work_limits,
            history_limits=history_limits,
        )
        payload = _PROTOCOL.pack(
            _PROTOCOL_MAGIC,
            nonce,
            work_limits.bytes,
            work_limits.inodes,
            history_limits.bytes,
            history_limits.inodes,
            os.getpid(),
        )
        _send_creator_packet(
            sock,
            payload,
            (
                work_descriptor,
                history_descriptor,
                user_descriptor,
                mount_descriptor,
            ),
        )
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        sock.close()
    return 0


def _parse_descriptor(text: str, *, label: str) -> int:
    try:
        descriptor = int(text)
    except ValueError as error:
        raise ValueError(f"{label} is not a descriptor") from error
    if descriptor < 0:
        raise ValueError(f"{label} is not an open descriptor")
    os.fstat(descriptor)
    return descriptor


def _trampoline_main(arguments: list[str]) -> int:
    if len(arguments) < 12:
        raise ValueError("trampoline received too few arguments")
    command_kind = arguments[0]
    try:
        expected_parent_pid = int(arguments[1])
        death_signal = signal.Signals(int(arguments[2]))
    except ValueError as error:
        raise ValueError(
            "trampoline parent-death contract is malformed"
        ) from error
    _arm_parent_death(expected_parent_pid, death_signal)
    try:
        binding_count = int(arguments[9])
    except ValueError as error:
        raise ValueError(
            "trampoline descriptor binding count is malformed"
        ) from error
    if (
        binding_count <= 0
        or binding_count > 4096
        or arguments[9] != str(binding_count)
    ):
        raise ValueError("trampoline descriptor binding count is invalid")
    boundary = 10 + binding_count
    if len(arguments) <= boundary + 1 or arguments[boundary] != "--":
        raise ValueError("trampoline received a malformed argument boundary")
    bindings = tuple(
        _parse_descriptor_binding(value)
        for value in arguments[10:boundary]
    )
    bound_descriptors = {
        descriptor
        for descriptor, _identity in bindings
    }
    if len(bound_descriptors) != len(bindings):
        raise ValueError("trampoline descriptor binding is duplicated")
    for descriptor, identity in bindings:
        _validate_descriptor_binding(descriptor, identity)

    user_descriptor = _parse_descriptor(
        arguments[3],
        label="user namespace",
    )
    mount_descriptor = _parse_descriptor(
        arguments[4],
        label="mount namespace",
    )
    work_descriptor = _parse_descriptor(arguments[5], label="work root")
    history_descriptor = _parse_descriptor(arguments[6], label="history root")
    cgroup_text = arguments[7]
    helper_text = arguments[8]
    command = tuple(arguments[boundary + 1 :])
    if command_kind == "bwrap":
        if not command or command[0] != os.fspath(BWRAP_BINARY):
            raise ValueError("trampoline requires the exact bubblewrap binary")
        if helper_text != "-1":
            raise ValueError("bubblewrap trampoline retained a helper fd")
        helper_descriptor = -1
    elif command_kind == "trusted-python":
        helper_descriptor = _parse_descriptor(
            helper_text,
            label="trusted helper",
        )
        _validate_locked_helper_command(command, helper_descriptor)
    else:
        raise ValueError("trampoline command kind is invalid")
    required_descriptors = {
        user_descriptor,
        mount_descriptor,
        work_descriptor,
        history_descriptor,
    }
    if helper_descriptor >= 0:
        required_descriptors.add(helper_descriptor)
    if not required_descriptors.issubset(bound_descriptors):
        raise ValueError(
            "trampoline authority descriptor lacks an identity binding"
        )
    if (
        _namespace_type(user_descriptor, label="storage user namespace")
        != os.CLONE_NEWUSER
        or _namespace_type(
            mount_descriptor,
            label="storage mount namespace",
        )
        != os.CLONE_NEWNS
    ):
        raise ValueError("trampoline namespace descriptor types are invalid")
    work_metadata = os.fstat(work_descriptor)
    history_metadata = os.fstat(history_descriptor)
    if (
        not stat.S_ISDIR(work_metadata.st_mode)
        or not stat.S_ISDIR(history_metadata.st_mode)
        or _filesystem_type(work_descriptor) != TMPFS_MAGIC
        or _filesystem_type(history_descriptor) != TMPFS_MAGIC
        or work_metadata.st_dev == history_metadata.st_dev
    ):
        raise ValueError("trampoline storage root descriptors are invalid")

    if cgroup_text == "-1":
        cgroup_descriptor = -1
    else:
        cgroup_descriptor = _parse_descriptor(
            cgroup_text,
            label="workload cgroup.procs",
        )
        _validate_cgroup_descriptor(cgroup_descriptor)
        if cgroup_descriptor not in bound_descriptors:
            raise ValueError(
                "trampoline cgroup descriptor lacks an identity binding"
            )
    os.setns(user_descriptor, os.CLONE_NEWUSER)
    os.setns(mount_descriptor, os.CLONE_NEWNS)
    if cgroup_descriptor >= 0:
        value = f"{os.getpid()}\n".encode("ascii")
        if os.write(cgroup_descriptor, value) != len(value):
            raise OSError(errno.EIO, "short write to workload cgroup.procs")
    for descriptor in (
        cgroup_descriptor,
        mount_descriptor,
        user_descriptor,
    ):
        if descriptor >= 0:
            os.close(descriptor)
    os.execve(
        command[0],
        command,
        dict(os.environ),
    )
    raise RuntimeError("namespace command exec returned")


def _bounded_script_error(error: BaseException) -> None:
    rendered = (
        f"model-session storage helper failed: "
        f"{type(error).__name__}: {error}\n"
    ).encode("utf-8", errors="replace")
    try:
        os.write(2, rendered[:4096])
    except OSError:
        pass


def _script_main(arguments: list[str]) -> int:
    if not arguments:
        raise ValueError("storage helper mode is missing")
    if arguments[0] == _CREATOR_COMMAND:
        return _creator_main(arguments[1:])
    if arguments[0] == _TRAMPOLINE_COMMAND:
        return _trampoline_main(arguments[1:])
    raise ValueError("unknown storage helper mode")


if __name__ == "__main__":
    try:
        raise SystemExit(_script_main(sys.argv[1:]))
    except SystemExit:
        raise
    except BaseException as _error:
        _bounded_script_error(_error)
        raise SystemExit(70)
