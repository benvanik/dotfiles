"""Deterministic live-file projection for a journaled benchmarkd install.

An install intent binds one immutable prior/target generation pair. This module
uses that closure to recognize only crash-reachable projection prefixes and to
publish through one fixed adjacent stage per live file. It never decides when a
cutover, rollback, or service operation is allowed; the administrator owns that
state machine.
"""

from __future__ import annotations

import ctypes
import dataclasses
import os
import pathlib
import stat
from collections.abc import Callable, Sequence
from typing import TypeAlias

from .errors import BenchmarkLockError


_AT_FDCWD = -100
_AT_EMPTY_PATH = 0x1000
_LIBC = ctypes.CDLL(None, use_errno=True)
_LINKAT = _LIBC.linkat
_LINKAT.argtypes = (
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_int,
)
_LINKAT.restype = ctypes.c_int

Reporter = Callable[[str], None]


def _projection_error(message: str) -> BenchmarkLockError:
    return BenchmarkLockError(
        message,
        code="benchmark_admin_install_invalid",
    )


@dataclasses.dataclass(frozen=True)
class RegularProjection:
    """One fixed regular-file projection in the install replay order."""

    description: str
    destination: pathlib.Path
    stage: pathlib.Path
    prior: bytes | None
    target: bytes
    mode: int = 0o644

    def __post_init__(self) -> None:
        if (
            not self.description
            or not self.destination.is_absolute()
            or self.stage.parent != self.destination.parent
            or self.stage == self.destination
            or not isinstance(self.target, bytes)
            or not self.target
            or (self.prior is not None and not isinstance(self.prior, bytes))
            or self.mode != 0o644
        ):
            raise ValueError("regular install projection is invalid")


@dataclasses.dataclass(frozen=True)
class SymlinkProjection:
    """One fixed symbolic-link projection in the install replay order."""

    description: str
    destination: pathlib.Path
    stage: pathlib.Path
    prior: str | None
    target: str

    def __post_init__(self) -> None:
        if (
            not self.description
            or not self.destination.is_absolute()
            or self.stage.parent != self.destination.parent
            or self.stage == self.destination
            or not self.target
            or (self.prior is not None and not self.prior)
        ):
            raise ValueError("symbolic-link install projection is invalid")


Projection: TypeAlias = RegularProjection | SymlinkProjection


def _mode(metadata: os.stat_result) -> int:
    return stat.S_IMODE(metadata.st_mode)


def _fsync_directory(path: pathlib.Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_new_regular(
    path: pathlib.Path,
    payload: bytes,
    *,
    mode: int,
    root_uid: int,
    root_gid: int,
) -> None:
    """Link one complete, fsynced unnamed inode at an exact absent path."""

    flags = os.O_WRONLY | os.O_CLOEXEC | os.O_TMPFILE
    try:
        descriptor = os.open(path.parent, flags, mode)
    except OSError as error:
        raise _projection_error(
            f"cannot create unnamed managed file for {path}: {error}"
        ) from error
    linked = False
    try:
        try:
            os.fchown(descriptor, root_uid, root_gid)
            os.fchmod(descriptor, mode)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("managed file write made no progress")
                view = view[written:]
            os.fsync(descriptor)
            result = _LINKAT(
                descriptor,
                b"",
                _AT_FDCWD,
                os.fsencode(path),
                _AT_EMPTY_PATH,
            )
            if result != 0:
                error_number = ctypes.get_errno()
                raise OSError(
                    error_number,
                    os.strerror(error_number),
                    path,
                )
            linked = True
            metadata = os.lstat(path)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != root_uid
                or metadata.st_gid != root_gid
                or _mode(metadata) != mode
                or metadata.st_nlink != 1
                or metadata.st_size != len(payload)
            ):
                raise _projection_error(
                    f"published managed file has unsafe metadata: {path}"
                )
            _fsync_directory(path.parent)
        except OSError as error:
            raise _projection_error(
                f"cannot publish managed file {path}: {error}"
            ) from error
    except BaseException:
        if linked and os.path.lexists(path):
            os.unlink(path)
            _fsync_directory(path.parent)
        raise
    finally:
        os.close(descriptor)


class InstallationProjection:
    """Validate and replay one exact prior/target live projection."""

    def __init__(
        self,
        *,
        items: Sequence[Projection],
        root_uid: int = 0,
        root_gid: int = 0,
        report: Reporter = print,
    ) -> None:
        self.items = tuple(items)
        destinations = tuple(item.destination for item in self.items)
        stages = tuple(item.stage for item in self.items)
        if (
            not self.items
            or len(set(destinations)) != len(destinations)
            or len(set(stages)) != len(stages)
            or set(destinations) & set(stages)
            or isinstance(root_uid, bool)
            or not isinstance(root_uid, int)
            or root_uid < 0
            or isinstance(root_gid, bool)
            or not isinstance(root_gid, int)
            or root_gid < 0
        ):
            raise ValueError("benchmark install projection parameters are invalid")
        self.root_uid = root_uid
        self.root_gid = root_gid
        self.report = report

    @property
    def stage_paths(self) -> tuple[pathlib.Path, ...]:
        return tuple(item.stage for item in self.items)

    @staticmethod
    def _fsync_directory(path: pathlib.Path) -> None:
        _fsync_directory(path)

    def _read_regular(
        self,
        path: pathlib.Path,
        *,
        maximum: int,
        mode: int,
    ) -> bytes:
        try:
            metadata = os.lstat(path)
        except OSError as error:
            raise _projection_error(
                f"cannot inspect install projection {path}: {error}"
            ) from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != self.root_uid
            or metadata.st_gid != self.root_gid
            or _mode(metadata) != mode
            or metadata.st_nlink != 1
            or metadata.st_size > maximum
        ):
            raise _projection_error(f"install projection has unsafe metadata: {path}")
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
            try:
                payload = os.read(descriptor, maximum + 1)
                opened = os.fstat(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise _projection_error(
                f"cannot read install projection {path}: {error}"
            ) from error
        if (
            len(payload) != metadata.st_size
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_uid != metadata.st_uid
            or opened.st_gid != metadata.st_gid
            or _mode(opened) != _mode(metadata)
            or opened.st_size != metadata.st_size
            or opened.st_nlink != metadata.st_nlink
        ):
            raise _projection_error(
                f"install projection changed while being read: {path}"
            )
        return payload

    def _read_symlink(self, path: pathlib.Path) -> str:
        try:
            metadata = os.lstat(path)
            target = os.readlink(path)
        except OSError as error:
            raise _projection_error(
                f"cannot inspect install projection {path}: {error}"
            ) from error
        if (
            not stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != self.root_uid
            or metadata.st_gid != self.root_gid
        ):
            raise _projection_error(f"install projection has unsafe metadata: {path}")
        return target

    def _observed(self, item: Projection, *, stage: bool = False) -> bytes | str | None:
        path = item.stage if stage else item.destination
        if not os.path.lexists(path):
            return None
        if isinstance(item, RegularProjection):
            maximum = max(
                len(item.target),
                0 if item.prior is None else len(item.prior),
            )
            return self._read_regular(path, maximum=maximum, mode=item.mode)
        return self._read_symlink(path)

    @staticmethod
    def _value(
        item: Projection,
        *,
        target: bool,
    ) -> bytes | str | None:
        return item.target if target else item.prior

    def require_stages_absent(self) -> None:
        unexpected = tuple(
            item.stage for item in self.items if os.path.lexists(item.stage)
        )
        if unexpected:
            formatted = ", ".join(os.fspath(path) for path in unexpected)
            raise _projection_error(
                f"fixed install projection stage exists without replay: {formatted}"
            )

    def _require_exact(self, *, target: bool) -> None:
        self.require_stages_absent()
        for item in self.items:
            expected = self._value(item, target=target)
            observed = self._observed(item)
            if observed != expected:
                state = "target" if target else "prior"
                raise _projection_error(
                    f"{item.description} is not the exact {state} projection"
                )

    def require_exact_prior(self) -> None:
        """Require the untouched prepared-state projection."""

        self._require_exact(target=False)

    def require_exact_target(self) -> None:
        """Require a terminal target projection with no intermediate."""

        self._require_exact(target=True)

    def _require_prefix(self, *, target: bool) -> None:
        desired_values = tuple(self._value(item, target=target) for item in self.items)
        source_values = tuple(
            self._value(item, target=not target) for item in self.items
        )
        observed_values = tuple(self._observed(item) for item in self.items)
        staged_indices = tuple(
            index
            for index, item in enumerate(self.items)
            if os.path.lexists(item.stage)
        )
        if len(staged_indices) > 1:
            raise _projection_error(
                "more than one fixed install projection stage is visible"
            )
        if staged_indices:
            stage_index = staged_indices[0]
            staged = self._observed(self.items[stage_index], stage=True)
            if staged != desired_values[stage_index]:
                raise _projection_error(
                    "fixed install projection stage has the wrong content"
                )
        else:
            stage_index = None

        valid_boundaries: list[int] = []
        for boundary in range(len(self.items) + 1):
            if all(
                observed_values[index]
                == (desired_values[index] if index < boundary else source_values[index])
                for index in range(len(self.items))
            ):
                valid_boundaries.append(boundary)
        if not valid_boundaries:
            direction = "target" if target else "prior"
            raise _projection_error(
                f"install projection is not a deterministic {direction} prefix"
            )
        if stage_index is not None and not any(
            boundary == stage_index for boundary in valid_boundaries
        ):
            raise _projection_error(
                "fixed install projection stage is outside the replay boundary"
            )

    def require_target_prefix(self) -> None:
        self._require_prefix(target=True)

    def require_prior_prefix(self) -> None:
        self._require_prefix(target=False)

    def _publish_regular_stage(
        self,
        item: RegularProjection,
        payload: bytes,
    ) -> None:
        publish_new_regular(
            item.stage,
            payload,
            mode=item.mode,
            root_uid=self.root_uid,
            root_gid=self.root_gid,
        )

    def _publish_symlink_stage(
        self,
        item: SymlinkProjection,
        target: str,
    ) -> None:
        created = False
        try:
            os.symlink(target, item.stage)
            created = True
            os.chown(
                item.stage,
                self.root_uid,
                self.root_gid,
                follow_symlinks=False,
            )
            self._fsync_directory(item.stage.parent)
        except BaseException:
            if created and os.path.lexists(item.stage):
                os.unlink(item.stage)
                self._fsync_directory(item.stage.parent)
            raise

    def _converge(self, *, target: bool) -> None:
        self._require_prefix(target=target)
        for item in self.items:
            desired = self._value(item, target=target)
            if desired is None:
                raise AssertionError("projection convergence target is absent")
            observed = self._observed(item)
            if observed == desired:
                continue
            source = self._value(item, target=not target)
            if observed != source:
                raise _projection_error(
                    f"{item.description} changed outside install replay"
                )
            if os.path.lexists(item.stage):
                staged = self._observed(item, stage=True)
                if staged != desired:
                    raise _projection_error(
                        f"{item.description} stage changed outside install replay"
                    )
            elif isinstance(item, RegularProjection):
                if not isinstance(desired, bytes):
                    raise AssertionError("regular projection payload is not bytes")
                self._publish_regular_stage(item, desired)
            else:
                if not isinstance(desired, str):
                    raise AssertionError("symlink projection target is not text")
                self._publish_symlink_stage(item, desired)
            os.replace(item.stage, item.destination)
            self._fsync_directory(item.destination.parent)
        self._require_exact(target=target)

    def converge_target(self) -> None:
        """Replay a stopped transaction to its exact target projection."""

        self._converge(target=True)

    def converge_prior(self) -> None:
        """Replay a rollback transaction to its exact prior projection."""

        if any(item.prior is None for item in self.items):
            raise AssertionError("first-install rollback has no prior projection")
        self._converge(target=False)

    def require_removal_prefix(self) -> None:
        """Require a deterministic target-to-absent first-install rollback."""

        self.require_stages_absent()
        reversed_items = tuple(reversed(self.items))
        observed = tuple(self._observed(item) for item in reversed_items)
        valid = any(
            all(
                observed[index]
                == (None if index < boundary else reversed_items[index].target)
                for index in range(len(reversed_items))
            )
            for boundary in range(len(reversed_items) + 1)
        )
        if not valid:
            raise _projection_error(
                "first-install rollback is not a deterministic removal prefix"
            )

    def remove_target(self) -> None:
        """Replay a first-install rollback to an entirely absent projection."""

        self.require_removal_prefix()
        for item in reversed(self.items):
            observed = self._observed(item)
            if observed is None:
                continue
            if observed != item.target:
                raise _projection_error(
                    f"{item.description} changed during first-install rollback"
                )
            os.unlink(item.destination)
            self._fsync_directory(item.destination.parent)
        self.require_exact_prior()
