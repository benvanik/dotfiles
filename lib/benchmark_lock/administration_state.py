"""Crash-visible benchmark administrator state and admission fencing."""

from __future__ import annotations

import contextlib
import fcntl
import os
import pathlib
import stat
from collections.abc import Iterator

from .errors import BenchmarkLockError


INSTALL_ROOT = pathlib.Path("/usr/local/lib/benchmarkd")
STATE_DIRECTORY = pathlib.Path("/var/lib/benchmarkd")
ADMIN_LOCK_PATH = STATE_DIRECTORY / "admin.lock"
INSTALL_INTENT_PATH = INSTALL_ROOT / "install.json"
INSTALL_PUBLISH_PATH = INSTALL_ROOT / ".install-publish.json"
INSTALL_TRANSITION_PATH = INSTALL_ROOT / ".install-transition.json"
UNINSTALL_INTENT_PATH = INSTALL_ROOT / "uninstall.json"
UNINSTALL_PUBLISH_PATH = INSTALL_ROOT / ".uninstall-publish.json"
UNINSTALL_TRANSITION_PATH = INSTALL_ROOT / ".uninstall-transition.json"
MAX_ADMINISTRATION_STATE_BYTES = 64 * 1024

INSTALL_STATE_PATHS = (
    INSTALL_INTENT_PATH,
    INSTALL_PUBLISH_PATH,
    INSTALL_TRANSITION_PATH,
)
UNINSTALL_STATE_PATHS = (
    UNINSTALL_INTENT_PATH,
    UNINSTALL_PUBLISH_PATH,
    UNINSTALL_TRANSITION_PATH,
)


def _state_error(message: str) -> BenchmarkLockError:
    return BenchmarkLockError(
        message,
        code="invalid_benchmark_administration_state",
    )


def _mode(metadata: os.stat_result) -> int:
    return stat.S_IMODE(metadata.st_mode)


class AdministrationAdmissionFence:
    """Reflect live root administration and durable transaction state.

    The administrator's exclusive flock is process-owned, so it survives a
    broker crash but disappears when the administrator exits. Install state is
    a dynamic durable fence which clears only after a valid absent scan under
    the shared flock. Uninstall state is terminal and, once observed, never
    clears within the running broker. Unsafe state permanently fails closed.
    """

    def __init__(
        self,
        *,
        admin_lock_path: pathlib.Path = ADMIN_LOCK_PATH,
        install_root: pathlib.Path = INSTALL_ROOT,
        install_state_paths: tuple[pathlib.Path, ...] = INSTALL_STATE_PATHS,
        uninstall_state_paths: tuple[pathlib.Path, ...] = UNINSTALL_STATE_PATHS,
        root_uid: int = 0,
        root_gid: int = 0,
    ) -> None:
        self._admin_lock_path = pathlib.Path(admin_lock_path)
        self._install_root = pathlib.Path(install_root)
        self._install_state_paths = tuple(
            pathlib.Path(path) for path in install_state_paths
        )
        self._uninstall_state_paths = tuple(
            pathlib.Path(path) for path in uninstall_state_paths
        )
        if (
            not self._admin_lock_path.is_absolute()
            or not self._install_root.is_absolute()
            or any(not path.is_absolute() for path in self._install_state_paths)
            or any(not path.is_absolute() for path in self._uninstall_state_paths)
            or any(
                path.parent != self._install_root for path in self._install_state_paths
            )
            or any(
                path.parent != self._install_root
                for path in self._uninstall_state_paths
            )
            or isinstance(root_uid, bool)
            or not isinstance(root_uid, int)
            or root_uid < 0
            or isinstance(root_gid, bool)
            or not isinstance(root_gid, int)
            or root_gid < 0
        ):
            raise ValueError("benchmark administration fence parameters are invalid")
        self._root_uid = root_uid
        self._root_gid = root_gid
        self._admin_lock_descriptor = self._open_admin_lock()
        self._install_fenced = False
        self._uninstall_fenced = False
        self._fence_reason: str | None = None
        self._closed = False

    def _validate_admin_lock_metadata(
        self,
        metadata: os.stat_result,
    ) -> None:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != self._root_uid
            or metadata.st_gid != self._root_gid
            or _mode(metadata) != 0o600
            or metadata.st_nlink != 1
        ):
            raise _state_error(
                "benchmark administrator lock has unsafe ownership or metadata"
            )

    def _validate_admin_lock_parent(self) -> None:
        try:
            metadata = os.lstat(self._admin_lock_path.parent)
        except OSError as error:
            raise _state_error(
                f"cannot inspect benchmark administrator lock directory: {error}"
            ) from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != self._root_uid
            or metadata.st_gid != self._root_gid
            or _mode(metadata) & 0o022
        ):
            raise _state_error(
                "benchmark administrator lock directory has unsafe metadata"
            )

    def _open_admin_lock(self) -> int:
        self._validate_admin_lock_parent()
        flags = os.O_RDWR | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor: int | None = None
        try:
            try:
                previous_mask = os.umask(0)
                try:
                    descriptor = os.open(
                        self._admin_lock_path,
                        flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                finally:
                    os.umask(previous_mask)
                os.fchmod(descriptor, 0o600)
            except FileExistsError:
                descriptor = os.open(self._admin_lock_path, flags)
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            raise _state_error(
                f"cannot open benchmark administrator lock: {error}"
            ) from error
        try:
            opened = os.fstat(descriptor)
            self._validate_admin_lock_metadata(opened)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _validate_state_metadata(
        self,
        path: pathlib.Path,
        metadata: os.stat_result,
        *,
        description: str,
    ) -> None:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != self._root_uid
            or metadata.st_gid != self._root_gid
            or metadata.st_nlink != 1
            or _mode(metadata) & 0o022
            or metadata.st_size > MAX_ADMINISTRATION_STATE_BYTES
        ):
            raise _state_error(
                f"benchmark {description} state has unsafe metadata: {path}"
            )

    def _state_present(
        self,
        paths: tuple[pathlib.Path, ...],
        *,
        description: str,
    ) -> bool:
        try:
            install_metadata = os.lstat(self._install_root)
        except OSError as error:
            raise _state_error(
                f"cannot inspect benchmark installation root: {error}"
            ) from error
        if (
            not stat.S_ISDIR(install_metadata.st_mode)
            or install_metadata.st_uid != self._root_uid
            or install_metadata.st_gid != self._root_gid
            or _mode(install_metadata) != 0o755
        ):
            raise _state_error(
                "benchmark installation root has unsafe ownership or metadata"
            )
        present = False
        for path in paths:
            try:
                metadata = os.lstat(path)
            except FileNotFoundError:
                continue
            except OSError as error:
                raise _state_error(
                    f"cannot inspect benchmark {description} state {path}: {error}"
                ) from error
            self._validate_state_metadata(
                path,
                metadata,
                description=description,
            )
            present = True
        return present

    def _install_state_present(self) -> bool:
        return self._state_present(
            self._install_state_paths,
            description="install",
        )

    def _uninstall_state_present(self) -> bool:
        return self._state_present(
            self._uninstall_state_paths,
            description="uninstall",
        )

    def _lock_shared_nonblocking(self) -> bool:
        try:
            fcntl.flock(
                self._admin_lock_descriptor,
                fcntl.LOCK_SH | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            return False
        except OSError as error:
            raise _state_error(
                f"cannot acquire benchmark administrator observation lock: {error}"
            ) from error
        return True

    @contextlib.contextmanager
    def hold_observation(self) -> Iterator[bool]:
        """Observe the fence while excluding an administrator cutover."""

        if self._closed:
            raise _state_error("benchmark administration fence is closed")
        if not self._lock_shared_nonblocking():
            yield True
            return
        try:
            if self._fence_reason is not None:
                yield True
            else:
                try:
                    self._install_fenced = self._install_state_present()
                    if not self._uninstall_fenced:
                        self._uninstall_fenced = self._uninstall_state_present()
                except BenchmarkLockError as error:
                    self._install_fenced = True
                    self._uninstall_fenced = True
                    self._fence_reason = f"{error.code}: {error}"
                yield self._install_fenced or self._uninstall_fenced
        finally:
            fcntl.flock(self._admin_lock_descriptor, fcntl.LOCK_UN)

    def refresh(self) -> bool:
        """Return whether admissions remain fenced at this instant."""

        with self.hold_observation() as fenced:
            return fenced

    @property
    def reason(self) -> str | None:
        """Return the permanent fail-closed reason, when one was observed."""

        return self._fence_reason

    def close(self) -> None:
        """Release the observation descriptor without changing authority."""

        if self._closed:
            return
        self._closed = True
        os.close(self._admin_lock_descriptor)
