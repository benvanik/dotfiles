"""One-shot process launch authority for retained model-session storage.

This module owns the parent-side invariants that turn retained namespace
descriptors into one direct-child launch.  The namespace executor itself is a
separate, stdlib-only source file.  Its exact bytes are copied into the
``python -c`` argument so an unchanged captured argv cannot acquire new
meaning after a user-writable source path changes.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import os
import pathlib
import signal
import stat
import subprocess
from collections.abc import Sequence
from types import TracebackType
from typing import Self

from model_session.errors import ModelSessionError


SETPRIV_BINARY = pathlib.Path("/usr/bin/setpriv")
PYTHON_BINARY = pathlib.Path("/usr/bin/python3")
BWRAP_BINARY = pathlib.Path("/usr/bin/bwrap")

CGROUP2_MAGIC = 0x63677270
_MAX_NAMESPACE_CHILD_SOURCE_BYTES = 96 * 1024
_MAX_TRUSTED_HELPER_BYTES = 4 * 1024 * 1024
_TRAMPOLINE_COMMAND = "--model-session-storage-trampoline"
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
_PACKAGE_DIRECTORY = pathlib.Path(__file__).resolve().parent
NAMESPACE_CHILD_SOURCE_PATH = (
    _PACKAGE_DIRECTORY / "storage_namespace_child.py"
)
TRUSTED_NAMESPACE_HELPERS = frozenset(
    {
        _PACKAGE_DIRECTORY / "checkpoint_worker.py",
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


_LIBC = ctypes.CDLL(None, use_errno=True)
_LIBC.fstatfs.argtypes = (ctypes.c_int, ctypes.POINTER(_StatFS))
_LIBC.fstatfs.restype = ctypes.c_int


def _fail(
    message: str,
    *,
    code: str = "storage_namespace_failed",
) -> None:
    raise ModelSessionError(message, code=code)


def descriptor_identity(descriptor: int) -> tuple[int, int, int]:
    metadata = os.fstat(descriptor)
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
    )


def encode_descriptor_binding(
    descriptor: int,
    identity: tuple[int, int, int],
) -> str:
    return ":".join(
        str(value)
        for value in (
            descriptor,
            *identity,
        )
    )


def close_if_owned(
    descriptor: int,
    identity: tuple[int, int, int],
) -> None:
    try:
        current = descriptor_identity(descriptor)
    except OSError:
        return
    if current == identity:
        try:
            os.close(descriptor)
        except OSError:
            pass


class StorageLaunch:
    """One-shot spawn authority for one identity-bound namespace launch."""

    __slots__ = (
        "_argv",
        "_bindings",
        "_closed",
        "_owned",
        "_pass_fds",
    )

    def __init__(
        self,
        *,
        argv: tuple[str, ...],
        pass_fds: tuple[int, ...],
        bindings: tuple[
            tuple[int, tuple[int, int, int]],
            ...,
        ],
        owned_descriptors: tuple[int, ...] = (),
    ) -> None:
        self._closed = True
        self._argv: tuple[str, ...] = ()
        self._pass_fds: tuple[int, ...] = ()
        self._bindings: tuple[
            tuple[int, tuple[int, int, int]], ...
        ] = ()
        self._owned: tuple[
            tuple[int, tuple[int, int, int]], ...
        ] = ()
        if (
            tuple(descriptor for descriptor, _identity in bindings)
            != pass_fds
            or len(set(pass_fds)) != len(pass_fds)
        ):
            _fail(
                "storage launch descriptor bindings are inconsistent",
                code="invalid_storage_launch",
            )
        binding_by_descriptor = dict(bindings)
        try:
            owned = tuple(
                (descriptor, binding_by_descriptor[descriptor])
                for descriptor in owned_descriptors
            )
        except KeyError as error:
            raise ModelSessionError(
                "owned storage launch descriptor lacks an identity binding",
                code="invalid_storage_launch",
            ) from error
        self._argv = argv
        self._pass_fds = pass_fds
        self._bindings = bindings
        self._owned = owned
        self._closed = False

    def _require_open(self) -> None:
        if self._closed:
            _fail(
                "storage launch is closed or was already consumed",
                code="storage_launch_closed",
            )

    def spawn(
        self,
        **popen_arguments: object,
    ) -> subprocess.Popen:
        """Atomically validate and consume this launch in one direct child."""

        self._require_open()
        argv = self._argv
        pass_fds = self._pass_fds
        bindings = self._bindings
        owned = self._owned
        self._closed = True
        self._argv = ()
        self._pass_fds = ()
        self._bindings = ()
        self._owned = ()
        try:
            forbidden = {
                "args",
                "close_fds",
                "executable",
                "pass_fds",
                "preexec_fn",
                "shell",
            }
            overridden = forbidden.intersection(popen_arguments)
            if overridden:
                _fail(
                    "storage launch cannot override fixed process authority: "
                    f"{', '.join(sorted(overridden))}",
                    code="invalid_storage_launch",
                )
            for descriptor, identity in bindings:
                try:
                    current_identity = descriptor_identity(descriptor)
                except OSError as error:
                    raise ModelSessionError(
                        "storage launch descriptor is no longer open",
                        code="invalid_storage_launch",
                    ) from error
                if current_identity != identity:
                    _fail(
                        "storage launch descriptor was replaced before spawn",
                        code="invalid_storage_launch",
                    )
            return subprocess.Popen(
                argv,
                pass_fds=pass_fds,
                close_fds=True,
                **popen_arguments,
            )
        finally:
            for descriptor, identity in reversed(owned):
                close_if_owned(descriptor, identity)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._argv = ()
        self._pass_fds = ()
        self._bindings = ()
        owned = self._owned
        self._owned = ()
        for descriptor, identity in reversed(owned):
            close_if_owned(descriptor, identity)

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


def build_namespace_launch(
    arguments: tuple[str, ...],
    *,
    command_kind: str,
    inherited: Sequence[int],
    user_namespace_descriptor: int,
    mount_namespace_descriptor: int,
    work_descriptor: int,
    history_descriptor: int,
    cgroup_descriptor: int,
    helper_descriptor: int,
    parent_death_signal_name: str,
) -> StorageLaunch:
    """Bind one immutable trampoline argv to its exact inherited authority."""

    pass_fds = tuple(inherited)
    bindings = tuple(
        (descriptor, descriptor_identity(descriptor))
        for descriptor in pass_fds
    )
    trampoline = (
        os.fspath(SETPRIV_BINARY),
        "--pdeathsig",
        parent_death_signal_name,
        os.fspath(PYTHON_BINARY),
        "-I",
        "-B",
        "-c",
        snapshot_namespace_child_source(),
        _TRAMPOLINE_COMMAND,
        command_kind,
        str(os.getpid()),
        str(getattr(signal, f"SIG{parent_death_signal_name}")),
        str(user_namespace_descriptor),
        str(mount_namespace_descriptor),
        str(work_descriptor),
        str(history_descriptor),
        str(cgroup_descriptor),
        str(helper_descriptor),
        str(len(bindings)),
        *(
            encode_descriptor_binding(descriptor, identity)
            for descriptor, identity in bindings
        ),
        "--",
        *arguments,
    )
    return StorageLaunch(
        argv=trampoline,
        pass_fds=pass_fds,
        bindings=bindings,
        owned_descriptors=(
            (helper_descriptor,)
            if helper_descriptor >= 0
            else ()
        ),
    )


def validate_open_descriptor(descriptor: int, *, label: str) -> None:
    if isinstance(descriptor, bool) or not isinstance(descriptor, int):
        _fail(
            f"{label} must be an open file descriptor",
            code="invalid_storage_launch",
        )
    try:
        os.fstat(descriptor)
    except OSError as error:
        raise ModelSessionError(
            f"{label} is not open: {error}",
            code="invalid_storage_launch",
        ) from error


def _filesystem_type(descriptor: int) -> int:
    result = _StatFS()
    if _LIBC.fstatfs(descriptor, ctypes.byref(result)) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    return result.f_type


def validate_cgroup_descriptor(descriptor: int) -> None:
    validate_open_descriptor(
        descriptor,
        label="workload cgroup.procs descriptor",
    )
    metadata = os.fstat(descriptor)
    flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
    try:
        descriptor_path = os.readlink(f"/proc/self/fd/{descriptor}")
    except OSError as error:
        raise ModelSessionError(
            f"cannot identify workload cgroup.procs descriptor: {error}",
            code="invalid_storage_launch",
        ) from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or _filesystem_type(descriptor) != CGROUP2_MAGIC
        or flags & os.O_ACCMODE not in {os.O_WRONLY, os.O_RDWR}
        or pathlib.PurePosixPath(descriptor_path).name != "cgroup.procs"
    ):
        _fail(
            "workload cgroup.procs descriptor is not a writable cgroup-v2 "
            "kernel file",
            code="invalid_storage_launch",
        )


def parent_death_signal_name(value: signal.Signals) -> str:
    if isinstance(value, bool):
        _fail(
            "parent_death_signal is not a valid signal",
            code="invalid_storage_launch",
        )
    try:
        death_signal = signal.Signals(value)
    except (TypeError, ValueError) as error:
        raise ModelSessionError(
            "parent_death_signal is not a valid signal",
            code="invalid_storage_launch",
        ) from error
    if death_signal not in _ALLOWED_PARENT_DEATH_SIGNALS:
        _fail(
            "parent_death_signal is outside the fixed terminating set",
            code="invalid_storage_launch",
        )
    return death_signal.name.removeprefix("SIG")


def _same_regular_file_snapshot(
    first: os.stat_result,
    second: os.stat_result,
) -> bool:
    return all(
        getattr(first, field) == getattr(second, field)
        for field in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
    )


def _open_trusted_source(
    source: pathlib.Path,
    *,
    label: str,
) -> tuple[int, os.stat_result]:
    try:
        expected = source.lstat()
    except OSError as error:
        raise ModelSessionError(
            f"cannot inspect {label} {source}: {error}",
            code="invalid_storage_launch",
        ) from error
    if (
        not source.is_absolute()
        or source.resolve() != source
        or not stat.S_ISREG(expected.st_mode)
        or expected.st_uid not in {0, os.getuid()}
        or expected.st_nlink != 1
        or expected.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        _fail(
            f"{label} has an unsafe identity or mode",
            code="invalid_storage_launch",
        )
    try:
        descriptor = os.open(
            source,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as error:
        raise ModelSessionError(
            f"cannot open {label} {source}: {error}",
            code="invalid_storage_launch",
        ) from error
    try:
        before = os.fstat(descriptor)
        if not _same_regular_file_snapshot(expected, before):
            _fail(
                f"{label} changed while it was opened",
                code="invalid_storage_launch",
            )
        return descriptor, before
    except BaseException:
        os.close(descriptor)
        raise


def snapshot_namespace_child_source() -> str:
    """Return one stable stdlib-only executor as an immutable argv value."""

    source = NAMESPACE_CHILD_SOURCE_PATH
    descriptor, before = _open_trusted_source(
        source,
        label="storage namespace child source",
    )
    try:
        if before.st_size > _MAX_NAMESPACE_CHILD_SOURCE_BYTES:
            _fail(
                "storage namespace child source exceeds its argv byte bound",
                code="invalid_storage_launch",
            )
        content = bytearray()
        while len(content) <= _MAX_NAMESPACE_CHILD_SOURCE_BYTES:
            chunk = os.read(
                descriptor,
                min(
                    64 * 1024,
                    _MAX_NAMESPACE_CHILD_SOURCE_BYTES + 1 - len(content),
                ),
            )
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        if (
            not _same_regular_file_snapshot(before, after)
            or len(content) != before.st_size
        ):
            _fail(
                "storage namespace child source changed while copied",
                code="invalid_storage_launch",
            )
        if len(content) > _MAX_NAMESPACE_CHILD_SOURCE_BYTES:
            _fail(
                "storage namespace child source exceeds its argv byte bound",
                code="invalid_storage_launch",
            )
        try:
            result = bytes(content).decode("utf-8")
        except UnicodeDecodeError as error:
            raise ModelSessionError(
                "storage namespace child source is not UTF-8",
                code="invalid_storage_launch",
            ) from error
        if "\x00" in result:
            _fail(
                "storage namespace child source contains a NUL byte",
                code="invalid_storage_launch",
            )
        return result
    finally:
        os.close(descriptor)


def _snapshot_trusted_helper(
    helper: pathlib.Path,
    expected: os.stat_result,
) -> int:
    source_descriptor, before = _open_trusted_source(
        helper,
        label="trusted namespace helper",
    )
    image_descriptor = -1
    try:
        if not _same_regular_file_snapshot(expected, before):
            _fail(
                "trusted namespace helper changed while it was opened",
                code="invalid_storage_launch",
            )
        if before.st_size > _MAX_TRUSTED_HELPER_BYTES:
            _fail(
                "trusted namespace helper exceeds its byte bound",
                code="invalid_storage_launch",
            )
        try:
            image_descriptor = os.memfd_create(
                "model-session-trusted-helper",
                os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
            )
        except (AttributeError, OSError) as error:
            raise ModelSessionError(
                f"cannot create sealed trusted-helper image: {error}",
                code="storage_namespace_platform_unsupported",
            ) from error
        os.fchmod(image_descriptor, 0o400)
        remaining = before.st_size
        while remaining:
            content = os.read(
                source_descriptor,
                min(remaining, 64 * 1024),
            )
            if not content:
                _fail(
                    "trusted namespace helper was truncated while copied",
                    code="invalid_storage_launch",
                )
            view = memoryview(content)
            while view:
                written = os.write(image_descriptor, view)
                if written <= 0:
                    raise OSError(
                        errno.EIO,
                        "short write to trusted-helper image",
                    )
                view = view[written:]
            remaining -= len(content)
        after = os.fstat(source_descriptor)
        if not _same_regular_file_snapshot(before, after):
            _fail(
                "trusted namespace helper changed while copied",
                code="invalid_storage_launch",
            )
        os.lseek(image_descriptor, 0, os.SEEK_SET)
        fcntl.fcntl(
            image_descriptor,
            fcntl.F_ADD_SEALS,
            _REQUIRED_HELPER_SEALS,
        )
        if (
            fcntl.fcntl(image_descriptor, fcntl.F_GET_SEALS)
            != _REQUIRED_HELPER_SEALS
        ):
            _fail(
                "trusted namespace helper image was not exactly sealed",
                code="invalid_storage_launch",
            )
        result = image_descriptor
        image_descriptor = -1
        return result
    finally:
        os.close(source_descriptor)
        if image_descriptor >= 0:
            os.close(image_descriptor)


def lock_trusted_namespace_command(
    command: Sequence[str],
    *,
    trusted_helpers: frozenset[pathlib.Path] = TRUSTED_NAMESPACE_HELPERS,
) -> tuple[tuple[str, ...], int]:
    if isinstance(command, (str, bytes)) or len(command) < 4:
        _fail(
            "trusted namespace command must name a dotfiles Python helper",
            code="invalid_storage_launch",
        )
    arguments = tuple(command)
    if any(
        not isinstance(argument, str) or "\x00" in argument
        for argument in arguments
    ):
        _fail(
            "trusted namespace command contains an invalid argument",
            code="invalid_storage_launch",
        )
    if arguments[:3] != (
        os.fspath(PYTHON_BINARY),
        "-I",
        "-B",
    ):
        _fail(
            "trusted namespace command must use exact isolated system Python",
            code="invalid_storage_launch",
        )
    helper_text = arguments[3]
    helper = pathlib.Path(helper_text)
    if (
        not helper.is_absolute()
        or os.path.normpath(helper_text) != helper_text
        or helper not in trusted_helpers
    ):
        _fail(
            "trusted namespace helper is outside the model-session "
            "infrastructure allowlist",
            code="invalid_storage_launch",
        )
    try:
        metadata = helper.lstat()
    except OSError as error:
        raise ModelSessionError(
            f"cannot inspect trusted namespace helper {helper}: {error}",
            code="invalid_storage_launch",
        ) from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.getuid()}
        or metadata.st_nlink != 1
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or helper.resolve() != helper
    ):
        _fail(
            "trusted namespace helper has an unsafe identity or mode",
            code="invalid_storage_launch",
        )
    descriptor = _snapshot_trusted_helper(helper, metadata)
    try:
        locked = (
            *arguments[:3],
            f"/proc/self/fd/{descriptor}",
            *arguments[4:],
        )
        return locked, descriptor
    except BaseException:
        os.close(descriptor)
        raise
