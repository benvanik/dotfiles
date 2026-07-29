"""Descriptor-safe filesystem transaction primitives for legacy migration."""

from __future__ import annotations

import contextlib
import fcntl
import os
import pathlib
import stat

from .errors import ModelLabError


def _fail(message: str, *, code: str = "legacy_migration_failed") -> None:
    raise ModelLabError(message, code=code)


def _absolute_normalized_path(
    value: os.PathLike[str] | str,
    *,
    label: str,
) -> pathlib.Path:
    try:
        text = os.fspath(value)
    except TypeError as error:
        raise ModelLabError(
            f"{label} must be an absolute filesystem path",
            code="invalid_legacy_migration_request",
        ) from error
    if (
        not isinstance(text, str)
        or not text
        or "\x00" in text
        or text != os.path.normpath(text)
    ):
        _fail(
            f"{label} must be an absolute normalized path",
            code="invalid_legacy_migration_request",
        )
    path = pathlib.Path(text)
    if not path.is_absolute():
        _fail(
            f"{label} must be an absolute normalized path",
            code="invalid_legacy_migration_request",
        )
    return path


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_directory_no_links(
    path: pathlib.Path,
    *,
    label: str,
    code: str,
    missing_ok: bool = False,
) -> int | None:
    """Open an absolute directory without following any pathname component."""

    if not path.is_absolute() or str(path) != os.path.normpath(path):
        _fail(f"{label} is not an absolute normalized path", code=code)
    try:
        descriptor = os.open("/", _directory_flags())
    except OSError as error:
        raise ModelLabError(
            f"cannot open filesystem root while inspecting {label}: {error}",
            code=code,
        ) from error
    cursor = pathlib.Path("/")
    try:
        for component in path.parts[1:]:
            cursor /= component
            try:
                child = os.open(
                    component,
                    _directory_flags(),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if missing_ok:
                    os.close(descriptor)
                    return None
                raise ModelLabError(
                    f"cannot open {label} {path}: missing component {cursor}",
                    code=code,
                ) from None
            except OSError as error:
                raise ModelLabError(
                    f"cannot open {label} {path} without following links: {error}",
                    code=code,
                ) from error
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _entry_metadata(
    path: pathlib.Path,
    *,
    label: str,
    code: str = "unsafe_legacy_migration_destination",
) -> os.stat_result | None:
    parent = _open_directory_no_links(
        path.parent,
        label=f"{label} parent",
        code=code,
        missing_ok=True,
    )
    if parent is None:
        return None
    try:
        try:
            return os.stat(
                path.name,
                dir_fd=parent,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ModelLabError(
                f"cannot inspect {label} {path}: {error}",
                code=code,
            ) from error
    finally:
        os.close(parent)


def _validate_destination_root(path: pathlib.Path) -> None:
    if path.name != "model-lab" or path in {
        pathlib.Path("/"),
        pathlib.Path("/home"),
        pathlib.Path("/mnt"),
        pathlib.Path("/tmp"),
        pathlib.Path("/var"),
    }:
        _fail(
            "destination_root must be a dedicated directory named 'model-lab'",
            code="invalid_legacy_migration_request",
        )
    metadata = _entry_metadata(path, label="destination model-lab root")
    if metadata is None:
        return
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        _fail(
            f"destination model-lab root has an unsafe identity: {path}",
            code="unsafe_legacy_migration_destination",
        )


def _paths_overlap(first: pathlib.Path, second: pathlib.Path) -> bool:
    return (
        first == second or first.is_relative_to(second) or second.is_relative_to(first)
    )


def _ensure_directory(path: pathlib.Path, *, mode: int = 0o700) -> None:
    if not path.is_absolute() or str(path) != os.path.normpath(path):
        _fail(
            f"migration directory is not an absolute normalized path: {path}",
            code="unsafe_legacy_migration_destination",
        )
    descriptor = os.open("/", _directory_flags())
    cursor = pathlib.Path("/")
    try:
        for index, component in enumerate(path.parts[1:]):
            cursor /= component
            created = False
            try:
                child = os.open(
                    component,
                    _directory_flags(),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode, dir_fd=descriptor)
                    os.fsync(descriptor)
                    child = os.open(
                        component,
                        _directory_flags(),
                        dir_fd=descriptor,
                    )
                    created = True
                except OSError as error:
                    raise ModelLabError(
                        f"cannot create migration directory {cursor}: {error}",
                        code="unsafe_legacy_migration_destination",
                    ) from error
            except OSError as error:
                raise ModelLabError(
                    f"cannot open migration directory {cursor} without "
                    f"following links: {error}",
                    code="unsafe_legacy_migration_destination",
                ) from error
            os.close(descriptor)
            descriptor = child
            metadata = os.fstat(descriptor)
            final = index == len(path.parts[1:]) - 1
            if not stat.S_ISDIR(metadata.st_mode) or (
                (created or final)
                and hasattr(os, "getuid")
                and metadata.st_uid != os.getuid()
            ):
                _fail(
                    f"migration directory has an unsafe identity: {cursor}",
                    code="unsafe_legacy_migration_destination",
                )
            if created:
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
            if final and stat.S_IMODE(os.fstat(descriptor).st_mode) != mode:
                _fail(
                    f"migration directory has an unsafe identity: {cursor}",
                    code="unsafe_legacy_migration_destination",
                )
    finally:
        os.close(descriptor)


def _fsync_path(path: pathlib.Path) -> None:
    descriptor = _open_directory_no_links(
        path,
        label="migration durability directory",
        code="legacy_migration_durability_unknown",
    )
    if descriptor is None:
        raise AssertionError("required migration directory is absent")
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            raise ModelLabError(
                f"cannot make migration directory durable {path}: {error}",
                code="legacy_migration_durability_unknown",
            ) from error
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _migration_lock(root: pathlib.Path):
    locks = root / ".migrations"
    _ensure_directory(locks)
    path = locks / "migration.lock"
    locks_descriptor = _open_directory_no_links(
        locks,
        label="migration lock directory",
        code="unsafe_legacy_migration_destination",
    )
    if locks_descriptor is None:
        raise AssertionError("migration lock directory is absent")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(
            path.name,
            flags,
            0o600,
            dir_fd=locks_descriptor,
        )
    except OSError as error:
        os.close(locks_descriptor)
        raise ModelLabError(
            f"cannot open migration lock {path}: {error}",
            code="unsafe_legacy_migration_destination",
        ) from error
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            _fail(
                f"migration lock has an unsafe identity: {path}",
                code="unsafe_legacy_migration_destination",
            )
        os.fsync(descriptor)
        os.fsync(locks_descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        os.close(locks_descriptor)


@contextlib.contextmanager
def _state_materialization_read_lock(root: pathlib.Path):
    """Block model-session publication while retaining read-only state access."""

    metadata = _entry_metadata(
        root,
        label="model-session state root",
        code="unsafe_legacy_migration_source",
    )
    if metadata is None:
        _fail(
            "cannot establish a coherent migration cutover because the legacy "
            f"state root does not exist: {root}",
            code="legacy_migration_source_state_missing",
        )
    if not stat.S_ISDIR(metadata.st_mode):
        _fail(
            f"model-session state root is not a directory: {root}",
            code="unsafe_legacy_migration_source",
        )
    descriptor = _open_source_directory(root)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _ensure_model_session_lock(root: pathlib.Path) -> None:
    locks = root / "locks"
    _ensure_directory(locks)
    path = locks / "materialize.lock"
    locks_descriptor = _open_directory_no_links(
        locks,
        label="model-session lock directory",
        code="unsafe_legacy_migration_destination",
    )
    if locks_descriptor is None:
        raise AssertionError("model-session lock directory is absent")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(
            path.name,
            flags,
            0o600,
            dir_fd=locks_descriptor,
        )
    except BaseException:
        os.close(locks_descriptor)
        raise
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            _fail(
                f"model-session lock has an unsafe identity: {path}",
                code="unsafe_legacy_migration_destination",
            )
        os.fsync(descriptor)
        os.fsync(locks_descriptor)
    finally:
        os.close(descriptor)
        os.close(locks_descriptor)


def _validate_source_metadata(
    metadata: os.stat_result,
    *,
    path: pathlib.Path,
    symlink: bool = False,
) -> None:
    if hasattr(os, "getuid") and metadata.st_uid not in {0, os.getuid()}:
        _fail(
            f"migration source has an unexpected owner: {path}",
            code="unsafe_legacy_migration_source",
        )
    if not symlink and metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        _fail(
            f"migration source is group- or world-writable: {path}",
            code="unsafe_legacy_migration_source",
        )
    if (
        stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
    ) and metadata.st_nlink != 1:
        _fail(
            f"migration source has filesystem aliases: {path}",
            code="unsafe_legacy_migration_source",
        )


def _open_source_directory(path: pathlib.Path) -> int:
    descriptor = _open_directory_no_links(
        path,
        label="migration source directory",
        code="unsafe_legacy_migration_source",
    )
    if descriptor is None:
        raise AssertionError("required migration source directory is absent")
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        _fail(
            f"migration source is not a directory: {path}",
            code="unsafe_legacy_migration_source",
        )
    _validate_source_metadata(metadata, path=path)
    return descriptor


def _write_file(
    path: pathlib.Path,
    content: bytes,
    *,
    mode: int = 0o600,
) -> None:
    parent = _open_directory_no_links(
        path.parent,
        label="migration file parent",
        code="unsafe_legacy_migration_destination",
    )
    if parent is None:
        raise AssertionError("migration file parent is absent")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path.name, flags, mode, dir_fd=parent)
    except OSError as error:
        os.close(parent)
        raise ModelLabError(
            f"cannot create migration file {path}: {error}",
            code="legacy_migration_copy_failed",
        ) from error
    try:
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    _fail(
                        f"short write while creating {path}",
                        code="legacy_migration_copy_failed",
                    )
                view = view[written:]
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.fsync(parent)
        except OSError as error:
            raise ModelLabError(
                f"cannot make migration file durable {path}: {error}",
                code="legacy_migration_durability_unknown",
            ) from error
    finally:
        os.close(parent)


def _publish_directory(staging: pathlib.Path, destination: pathlib.Path) -> None:
    if (
        _entry_metadata(
            destination,
            label="migration publication destination",
        )
        is not None
    ):
        _fail(
            f"migration destination already exists: {destination}",
            code="legacy_migration_destination_conflict",
        )
    _ensure_directory(destination.parent)
    source_parent = _open_directory_no_links(
        staging.parent,
        label="migration staging parent",
        code="unsafe_legacy_migration_destination",
    )
    destination_parent = _open_directory_no_links(
        destination.parent,
        label="migration publication parent",
        code="unsafe_legacy_migration_destination",
    )
    if source_parent is None or destination_parent is None:
        if source_parent is not None:
            os.close(source_parent)
        if destination_parent is not None:
            os.close(destination_parent)
        raise AssertionError("migration publication parent is absent")
    try:
        try:
            os.rename(
                staging.name,
                destination.name,
                src_dir_fd=source_parent,
                dst_dir_fd=destination_parent,
            )
        except OSError as error:
            raise ModelLabError(
                f"cannot atomically publish {destination}: {error}",
                code="legacy_migration_publish_failed",
            ) from error
        finally:
            os.close(source_parent)
        try:
            os.fsync(destination_parent)
        except OSError as error:
            raise ModelLabError(
                f"cannot make migration publication durable {destination}: {error}",
                code="legacy_migration_durability_unknown",
            ) from error
    finally:
        os.close(destination_parent)
