"""Crash-safe materialization and validation of isolated model sessions."""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import re
import secrets
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from .errors import ModelSessionError
from .pi_runtime import (
    INFERENCE_RELAY_PATH,
    INFERENCE_RELAY_ROLE,
    PI_MODELS_PATH,
    PI_MODELS_ROLE,
    SESSION_POLICY_PATH,
    SESSION_POLICY_ROLE,
    PiInstallationIdentity,
    PiRuntimeAsset,
    fingerprint_pi_installation,
    generated_pi_configuration_assets,
    parse_pi_installation_identity,
    pi_runtime_assets,
)
from .profile import (
    AGENTS_FILE_NAME,
    PROFILE_FILE_NAME,
    Profile,
    ProfileContract,
    parse_locked_profile,
    validate_state_route,
)


LOCK_SCHEMA = "model-session.lock.v1"
RUN_SCHEMA = "model-session.run.v1"
SESSION_ID_EXPRESSION = r"[0-9]{8}T[0-9]{12}Z-[0-9a-f]{16}"
SESSION_ID_PATTERN = re.compile(rf"^{SESSION_ID_EXPRESSION}$")
STAGING_NAME_PATTERN = re.compile(
    rf"^\.creating-({SESSION_ID_EXPRESSION})$"
)
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_RUN_KEYS = {
    "schema",
    "session_id",
    "profile_id",
    "project_id",
    "created_at",
    "lock_sha256",
}
_LOCK_KEYS = {
    "schema",
    "session_id",
    "created_at",
    "source_profile_root",
    "profile",
    "resources",
    "project",
    "pi_installation",
}
_PROJECT_KEYS = {"report_directory", "memory_directory"}
_RESOURCE_KEYS = {"path", "roles", "sha256", "size"}


@dataclass(frozen=True)
class LockedResource:
    relative_path: pathlib.PurePosixPath
    roles: tuple[str, ...]
    path: pathlib.Path
    sha256: str
    size: int


@dataclass(frozen=True)
class SessionRun:
    session_id: str
    created_at: str
    root: pathlib.Path
    profile: ProfileContract
    snapshot_root: pathlib.Path
    workspace: pathlib.Path
    pi_sessions: pathlib.Path
    report_directory: pathlib.Path
    memory_directory: pathlib.Path
    resources: tuple[LockedResource, ...]
    pi_installation: PiInstallationIdentity

    def resource_for_role(self, role: str) -> LockedResource | None:
        for resource in self.resources:
            if role in resource.roles:
                return resource
        return None


def _fail(message: str, *, code: str = "invalid_session_state") -> None:
    raise ModelSessionError(message, code=code)


def _directory_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        _fail(
            "model-session state requires O_NOFOLLOW and O_DIRECTORY",
            code="session_platform_unsupported",
        )
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _validate_private_directory_descriptor(
    descriptor: int,
    *,
    path: pathlib.Path,
    label: str,
) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        _fail(f"{label} is not a real directory: {path}", code="unsafe_session_state")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        _fail(
            f"{label} is not owned by the current user: {path}",
            code="unsafe_session_state",
        )
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        _fail(
            f"{label} permissions must be exactly 0700: {path}",
            code="unsafe_session_permissions",
        )


def _validate_project_directory_descriptor(
    descriptor: int,
    *,
    path: pathlib.Path,
    label: str,
) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        _fail(f"{label} is not a real directory: {path}", code="unsafe_session_state")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        _fail(
            f"{label} is not owned by the current user: {path}",
            code="unsafe_session_state",
        )
    if metadata.st_mode & stat.S_IWOTH:
        _fail(
            f"{label} is world-writable: {path}",
            code="unsafe_session_permissions",
        )


def _open_absolute_directory(
    path: pathlib.Path,
    *,
    label: str,
    create_missing: bool = False,
) -> int:
    """Open an absolute directory without following any path component."""

    if not path.is_absolute() or os.path.normpath(str(path)) != str(path):
        _fail(f"{label} is not an absolute normalized path: {path}")
    flags = _directory_flags()
    descriptor = os.open("/", flags)
    try:
        for component in path.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create_missing:
                    raise
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                else:
                    os.fsync(descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
                _validate_private_directory_descriptor(
                    child,
                    path=path,
                    label=label,
                )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except ModelSessionError:
        os.close(descriptor)
        raise
    except OSError as error:
        os.close(descriptor)
        raise ModelSessionError(
            f"cannot open {label} {path} without following links: {error}",
            code="unsafe_session_state",
        ) from error


def _child_name(value: str, *, label: str) -> str:
    path = pathlib.PurePosixPath(value)
    if (
        not value
        or "\x00" in value
        or path.is_absolute()
        or len(path.parts) != 1
        or path.name != value
        or value in {".", ".."}
    ):
        _fail(f"{label} is not a single filesystem component")
    return value


def _open_private_child_directory(
    parent_descriptor: int,
    name: str,
    *,
    path: pathlib.Path,
    label: str,
) -> int:
    name = _child_name(name, label=label)
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    except OSError as error:
        raise ModelSessionError(
            f"cannot open {label} {path} without following links: {error}",
            code="unsafe_session_state",
        ) from error
    try:
        _validate_private_directory_descriptor(
            descriptor,
            path=path,
            label=label,
        )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _ensure_private_child_directory(
    parent_descriptor: int,
    name: str,
    *,
    path: pathlib.Path,
    label: str,
) -> int:
    name = _child_name(name, label=label)
    try:
        return _open_private_child_directory(
            parent_descriptor,
            name,
            path=path,
            label=label,
        )
    except ModelSessionError as error:
        if not isinstance(error.__cause__, FileNotFoundError):
            raise
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
    except FileExistsError:
        pass
    except OSError as error:
        raise ModelSessionError(
            f"cannot create {label} {path}: {error}",
            code="unsafe_session_state",
        ) from error
    else:
        os.fsync(parent_descriptor)
    return _open_private_child_directory(
        parent_descriptor,
        name,
        path=path,
        label=label,
    )


def _create_private_child_directory(
    parent_descriptor: int,
    name: str,
    *,
    path: pathlib.Path,
    label: str,
) -> int:
    name = _child_name(name, label=label)
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
    except OSError as error:
        raise ModelSessionError(
            f"cannot create {label} {path}: {error}",
            code=(
                "session_id_collision"
                if isinstance(error, FileExistsError)
                else "session_materialization_failed"
            ),
        ) from error
    descriptor = _open_private_child_directory(
        parent_descriptor,
        name,
        path=path,
        label=label,
    )
    os.fsync(parent_descriptor)
    return descriptor


def _entry_exists_at(parent_descriptor: int, name: str) -> bool:
    name = _child_name(name, label="session state entry")
    try:
        os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ModelSessionError(
            f"cannot inspect session state entry {name}: {error}",
            code="unsafe_session_state",
        ) from error
    return True


def _write_exclusive_at(
    parent_descriptor: int,
    name: str,
    content: bytes,
    *,
    path: pathlib.Path,
) -> None:
    name = _child_name(name, label="session file name")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=parent_descriptor)
    except OSError as error:
        raise ModelSessionError(
            f"cannot create session file {path}: {error}",
            code="session_materialization_failed",
        ) from error
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                _fail(
                    f"short write while creating session file {path}",
                    code="session_materialization_failed",
                )
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_private_file_at(
    parent_descriptor: int,
    name: str,
    *,
    path: pathlib.Path,
    label: str,
    maximum_bytes: int = 8 * 1024 * 1024,
) -> bytes:
    name = _child_name(name, label=label)
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as error:
        raise ModelSessionError(
            f"cannot open {label} {path}: {error}",
            code="unsafe_session_state",
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _fail(f"{label} is not a regular file: {path}", code="unsafe_session_state")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            _fail(
                f"{label} is not owned by the current user: {path}",
                code="unsafe_session_state",
            )
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            _fail(
                f"{label} permissions must be exactly 0600: {path}",
                code="unsafe_session_permissions",
            )
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > maximum_bytes:
            _fail(f"{label} is unexpectedly large", code="unsafe_session_state")
        return content
    finally:
        os.close(descriptor)


def _load_json_at(
    parent_descriptor: int,
    name: str,
    *,
    path: pathlib.Path,
    label: str,
) -> tuple[dict[str, Any], bytes]:
    content = _read_private_file_at(
        parent_descriptor,
        name,
        path=path,
        label=label,
    )
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ModelSessionError(
            f"{label} is not valid JSON: {path}",
            code="invalid_session_state",
        ) from error
    if not isinstance(value, dict):
        _fail(f"{label} must contain a JSON object")
    return value, content


def _require_keys(value: dict[str, Any], expected: set[str], *, label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = expected - actual
        unexpected = actual - expected
        details = []
        if missing:
            details.append(f"missing {', '.join(sorted(missing))}")
        if unexpected:
            details.append(f"unexpected {', '.join(sorted(unexpected))}")
        _fail(f"{label} has invalid fields ({'; '.join(details)})")


def _new_session_id() -> str:
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )
    return f"{timestamp}-{secrets.token_hex(8)}"


def _created_at() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _validate_created_at(value: Any) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        _fail("session created_at is not a UTC timestamp")
    try:
        parsed = datetime.datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise ModelSessionError(
            "session created_at is not a valid timestamp",
            code="invalid_session_state",
        ) from error
    if parsed.tzinfo != datetime.timezone.utc:
        _fail("session created_at is not UTC")
    return value


def _validate_session_id(value: Any) -> str:
    if not isinstance(value, str) or not SESSION_ID_PATTERN.fullmatch(value):
        _fail("invalid internally generated session ID")
    return value


@contextlib.contextmanager
def _materialization_lock(
    state_descriptor: int,
    state_root: pathlib.Path,
) -> Iterator[None]:
    lock_directory_path = state_root / "locks"
    lock_directory_descriptor = _ensure_private_child_directory(
        state_descriptor,
        "locks",
        path=lock_directory_path,
        label="session lock directory",
    )
    lock_path = lock_directory_path / "materialize.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(
            "materialize.lock",
            flags,
            0o600,
            dir_fd=lock_directory_descriptor,
        )
    except OSError as error:
        os.close(lock_directory_descriptor)
        raise ModelSessionError(
            f"cannot open session materialization lock {lock_path}: {error}",
            code="session_lock_failed",
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _fail("session materialization lock is not a regular file")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            _fail("session materialization lock has an unexpected owner")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            _fail(
                "session materialization lock permissions must be exactly 0600",
                code="unsafe_session_permissions",
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        os.close(lock_directory_descriptor)


def _make_project_session_directories(
    project_descriptor: int,
    project_root: pathlib.Path,
    session_id: str,
) -> tuple[pathlib.Path, pathlib.Path, int, int]:
    reports_root = project_root / "reports"
    reports_descriptor = _ensure_private_child_directory(
        project_descriptor,
        "reports",
        path=reports_root,
        label="project reports directory",
    )
    memory_root = project_root / "memory"
    try:
        memory_descriptor = _ensure_private_child_directory(
            project_descriptor,
            "memory",
            path=memory_root,
            label="project memory directory",
        )
    except BaseException:
        os.close(reports_descriptor)
        raise
    report_directory = reports_root / session_id
    memory_directory = memory_root / session_id
    report_created = False
    memory_created = False
    try:
        report_session_descriptor = _create_private_child_directory(
            reports_descriptor,
            session_id,
            path=report_directory,
            label="session report directory",
        )
        os.close(report_session_descriptor)
        report_created = True
        memory_session_descriptor = _create_private_child_directory(
            memory_descriptor,
            session_id,
            path=memory_directory,
            label="session memory directory",
        )
        os.close(memory_session_descriptor)
        memory_created = True
    except Exception as error:
        cleanup_failures = []
        for parent_descriptor, path, created in (
            (memory_descriptor, memory_directory, memory_created),
            (reports_descriptor, report_directory, report_created),
        ):
            if not created:
                continue
            try:
                os.rmdir(session_id, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
            except OSError as cleanup_error:
                cleanup_failures.append(f"{path}: {cleanup_error}")
        if cleanup_failures:
            os.close(memory_descriptor)
            os.close(reports_descriptor)
            raise ModelSessionError(
                "failed to roll back project session directories: "
                + "; ".join(cleanup_failures),
                code="session_cleanup_failed",
            ) from error
        os.close(memory_descriptor)
        os.close(reports_descriptor)
        if isinstance(error, ModelSessionError):
            raise
        raise ModelSessionError(
            f"cannot create project state for generated session {session_id}: {error}",
            code="session_materialization_failed",
        ) from error
    return (
        report_directory,
        memory_directory,
        reports_descriptor,
        memory_descriptor,
    )


def _remove_empty_project_directories(
    reports_descriptor: int | None,
    memory_descriptor: int | None,
    report_directory: pathlib.Path | None,
    memory_directory: pathlib.Path | None,
    session_id: str,
) -> None:
    failures = []
    for parent_descriptor, path in (
        (memory_descriptor, memory_directory),
        (reports_descriptor, report_directory),
    ):
        if parent_descriptor is None or path is None:
            continue
        try:
            os.rmdir(session_id, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        except FileNotFoundError:
            continue
        except OSError as error:
            failures.append(f"{path}: {error}")
    if failures:
        _fail(
            "failed to roll back incomplete project session directories: "
            + "; ".join(failures),
            code="session_cleanup_failed",
        )


def _open_optional_private_child_directory(
    parent_descriptor: int,
    name: str,
    *,
    path: pathlib.Path,
    label: str,
) -> int | None:
    try:
        return _open_private_child_directory(
            parent_descriptor,
            name,
            path=path,
            label=label,
        )
    except ModelSessionError as error:
        if isinstance(error.__cause__, FileNotFoundError):
            return None
        raise


def _recover_project_directory(
    project_descriptor: int,
    project_root: pathlib.Path,
    root_name: str,
    session_id: str,
) -> None:
    root_path = project_root / root_name
    root_descriptor = _open_optional_private_child_directory(
        project_descriptor,
        root_name,
        path=root_path,
        label=f"project {root_name} directory",
    )
    if root_descriptor is None:
        return
    try:
        session_path = root_path / session_id
        session_descriptor = _open_optional_private_child_directory(
            root_descriptor,
            session_id,
            path=session_path,
            label="incomplete project session directory",
        )
        if session_descriptor is None:
            return
        try:
            if os.listdir(session_descriptor):
                _fail(
                    "incomplete project session directory is not empty; "
                    f"refusing automatic recovery: {session_path}",
                    code="session_recovery_required",
                )
        finally:
            os.close(session_descriptor)
        try:
            os.rmdir(session_id, dir_fd=root_descriptor)
            os.fsync(root_descriptor)
        except OSError as error:
            raise ModelSessionError(
                "cannot remove empty incomplete project session directory "
                f"{session_path}: {error}",
                code="session_recovery_required",
            ) from error
    finally:
        os.close(root_descriptor)


def _remove_private_tree_at(
    parent_descriptor: int,
    name: str,
    *,
    path: pathlib.Path,
) -> None:
    descriptor = _open_private_child_directory(
        parent_descriptor,
        name,
        path=path,
        label="session staging directory",
    )
    try:
        for entry_name in os.listdir(descriptor):
            entry_path = path / entry_name
            try:
                metadata = os.stat(
                    entry_name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise ModelSessionError(
                    f"cannot inspect generated staging entry {entry_path}: {error}",
                    code="session_cleanup_failed",
                ) from error
            if stat.S_ISDIR(metadata.st_mode):
                _remove_private_tree_at(
                    descriptor,
                    entry_name,
                    path=entry_path,
                )
                continue
            if not stat.S_ISREG(metadata.st_mode):
                _fail(
                    f"generated staging contains a non-regular entry: {entry_path}",
                    code="session_cleanup_failed",
                )
            try:
                os.unlink(entry_name, dir_fd=descriptor)
            except OSError as error:
                raise ModelSessionError(
                    f"cannot remove generated staging file {entry_path}: {error}",
                    code="session_cleanup_failed",
                ) from error
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.rmdir(name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    except OSError as error:
        raise ModelSessionError(
            f"cannot remove generated staging directory {path}: {error}",
            code="session_cleanup_failed",
        ) from error


def _recover_incomplete_materializations(
    profile_sessions_descriptor: int,
    profile_sessions: pathlib.Path,
    project_descriptor: int,
    project_root: pathlib.Path,
) -> None:
    for name in sorted(os.listdir(profile_sessions_descriptor)):
        if not name.startswith(".creating-"):
            continue
        path = profile_sessions / name
        match = STAGING_NAME_PATTERN.fullmatch(name)
        if match is None:
            _fail(
                f"malformed session staging path requires inspection: {path}",
                code="unsafe_session_state",
            )
        staging_descriptor = _open_private_child_directory(
            profile_sessions_descriptor,
            name,
            path=path,
            label="incomplete session staging",
        )
        try:
            entries = os.listdir(staging_descriptor)
        finally:
            os.close(staging_descriptor)
        if entries:
            _fail(
                "incomplete session staging contains partial state; refusing "
                f"automatic deletion: {path}",
                code="session_recovery_required",
            )
        session_id = match.group(1)
        _recover_project_directory(
            project_descriptor,
            project_root,
            "memory",
            session_id,
        )
        _recover_project_directory(
            project_descriptor,
            project_root,
            "reports",
            session_id,
        )
        try:
            os.rmdir(name, dir_fd=profile_sessions_descriptor)
            os.fsync(profile_sessions_descriptor)
        except OSError as error:
            raise ModelSessionError(
                f"cannot remove empty incomplete staging directory {path}: {error}",
                code="session_recovery_required",
            ) from error


def _snapshot_file_entries(
    profile: Profile,
    runtime_assets: tuple[PiRuntimeAsset, ...],
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    contents = {f"profile/{PROFILE_FILE_NAME}": profile.document}
    roles = {f"profile/{PROFILE_FILE_NAME}": ("profile",)}
    for resource in profile.resources:
        relative = f"profile/{resource.relative_path.as_posix()}"
        contents[relative] = resource.content
        roles[relative] = resource.roles
    for asset in runtime_assets:
        relative = asset.relative_path.as_posix()
        if relative in contents:
            _fail(
                f"runtime snapshot path collides with profile state: {relative}",
                code="invalid_runtime_assets",
            )
        contents[relative] = asset.content
        roles[relative] = asset.roles
    entries = []
    for relative in sorted(contents):
        content = contents[relative]
        entries.append(
            {
                "path": relative,
                "roles": list(roles[relative]),
                "sha256": _sha256(content),
                "size": len(content),
            }
        )
    return entries, contents


def _open_relative_private_directory(
    root_descriptor: int,
    root: pathlib.Path,
    relative: pathlib.PurePosixPath,
    *,
    create_missing: bool,
) -> int:
    descriptor = os.dup(root_descriptor)
    current_path = root
    try:
        for component in relative.parts:
            current_path /= component
            if create_missing:
                child = _ensure_private_child_directory(
                    descriptor,
                    component,
                    path=current_path,
                    label="session snapshot directory",
                )
            else:
                child = _open_private_child_directory(
                    descriptor,
                    component,
                    path=current_path,
                    label="session snapshot directory",
                )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _write_relative_exclusive(
    root_descriptor: int,
    root: pathlib.Path,
    relative: pathlib.PurePosixPath,
    content: bytes,
) -> None:
    parent_descriptor = _open_relative_private_directory(
        root_descriptor,
        root,
        relative.parent,
        create_missing=False,
    )
    try:
        _write_exclusive_at(
            parent_descriptor,
            relative.name,
            content,
            path=root.joinpath(*relative.parts),
        )
    finally:
        os.close(parent_descriptor)


def _read_relative_private_file(
    root_descriptor: int,
    root: pathlib.Path,
    relative: pathlib.PurePosixPath,
    *,
    label: str,
) -> bytes:
    parent_descriptor = _open_relative_private_directory(
        root_descriptor,
        root,
        relative.parent,
        create_missing=False,
    )
    try:
        return _read_private_file_at(
            parent_descriptor,
            relative.name,
            path=root.joinpath(*relative.parts),
            label=label,
        )
    finally:
        os.close(parent_descriptor)


def _make_directory_tree(
    root_descriptor: int,
    root: pathlib.Path,
    relative_paths: set[str],
) -> None:
    directories: set[pathlib.PurePosixPath] = set()
    for relative in relative_paths:
        parent = pathlib.PurePosixPath(relative).parent
        while parent != pathlib.PurePosixPath("."):
            directories.add(parent)
            parent = parent.parent
    for relative in sorted(
        directories,
        key=lambda path: (len(path.parts), path.as_posix()),
    ):
        descriptor = _open_relative_private_directory(
            root_descriptor,
            root,
            relative,
            create_missing=True,
        )
        os.close(descriptor)


def _fsync_directory_tree(descriptor: int, path: pathlib.Path) -> None:
    for name in os.listdir(descriptor):
        try:
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as error:
            raise ModelSessionError(
                f"cannot inspect session tree entry {path / name}: {error}",
                code="session_materialization_failed",
            ) from error
        if stat.S_ISDIR(metadata.st_mode):
            child = _open_private_child_directory(
                descriptor,
                name,
                path=path / name,
                label="session directory",
            )
            try:
                _fsync_directory_tree(child, path / name)
            finally:
                os.close(child)
        elif not stat.S_ISREG(metadata.st_mode):
            _fail(
                f"session tree contains unsupported entry: {path / name}",
                code="session_materialization_failed",
            )
    os.fsync(descriptor)


def _materialize_staging(
    staging_descriptor: int,
    staging: pathlib.Path,
    profile: Profile,
    runtime_assets: tuple[PiRuntimeAsset, ...],
    pi_installation: PiInstallationIdentity,
    *,
    session_id: str,
    created_at: str,
    report_directory: pathlib.Path,
    memory_directory: pathlib.Path,
) -> None:
    snapshot_root = staging / "snapshot"
    workspace = staging / "workspace"
    pi_root = staging / "pi"
    with contextlib.ExitStack() as descriptors:
        snapshot_descriptor = _create_private_child_directory(
            staging_descriptor,
            "snapshot",
            path=snapshot_root,
            label="snapshot root",
        )
        descriptors.callback(os.close, snapshot_descriptor)
        workspace_descriptor = _create_private_child_directory(
            staging_descriptor,
            "workspace",
            path=workspace,
            label="session workspace",
        )
        descriptors.callback(os.close, workspace_descriptor)
        pi_descriptor = _create_private_child_directory(
            staging_descriptor,
            "pi",
            path=pi_root,
            label="private Pi root",
        )
        descriptors.callback(os.close, pi_descriptor)
        workspace_pi_descriptor = _create_private_child_directory(
            workspace_descriptor,
            ".pi",
            path=workspace / ".pi",
            label="masked workspace .pi mountpoint",
        )
        os.close(workspace_pi_descriptor)
        sessions_descriptor = _create_private_child_directory(
            pi_descriptor,
            "sessions",
            path=pi_root / "sessions",
            label="private Pi sessions directory",
        )
        os.close(sessions_descriptor)

        resource_entries, contents = _snapshot_file_entries(
            profile,
            runtime_assets,
        )
        _make_directory_tree(
            snapshot_descriptor,
            snapshot_root,
            set(contents),
        )
        for relative, content in sorted(contents.items()):
            _write_relative_exclusive(
                snapshot_descriptor,
                snapshot_root,
                pathlib.PurePosixPath(relative),
                content,
            )

        agents = profile.resource_for_role("agents")
        if agents is None:
            _fail("validated profile has no AGENTS.md resource")
        _write_exclusive_at(
            workspace_descriptor,
            AGENTS_FILE_NAME,
            agents.content,
            path=workspace / AGENTS_FILE_NAME,
        )

        manifest = {
            "schema": LOCK_SCHEMA,
            "session_id": session_id,
            "created_at": created_at,
            "source_profile_root": str(profile.contract.profile_root),
            "profile": profile.contract.as_dict(),
            "resources": resource_entries,
            "pi_installation": pi_installation.as_dict(),
            "project": {
                "report_directory": str(report_directory),
                "memory_directory": str(memory_directory),
            },
        }
        manifest_bytes = _json_bytes(manifest)
        _write_exclusive_at(
            snapshot_descriptor,
            "lock.json",
            manifest_bytes,
            path=snapshot_root / "lock.json",
        )
        receipt = {
            "schema": RUN_SCHEMA,
            "session_id": session_id,
            "profile_id": profile.contract.profile_id,
            "project_id": profile.contract.project_id,
            "created_at": created_at,
            "lock_sha256": _sha256(manifest_bytes),
        }
        _write_exclusive_at(
            staging_descriptor,
            "run.json",
            _json_bytes(receipt),
            path=staging / "run.json",
        )

        _fsync_directory_tree(staging_descriptor, staging)


def materialize_new_run(profile: Profile) -> SessionRun:
    """Create one new run; no caller-controlled ID and no implicit resume."""

    runtime_assets = pi_runtime_assets(profile.contract)
    pi_installation = fingerprint_pi_installation(profile.contract)
    state_root = profile.contract.state_root
    sessions_root = state_root / "sessions"
    profile_sessions = sessions_root / profile.contract.profile_id
    project_root = profile.contract.project_root

    with contextlib.ExitStack() as descriptors:
        state_descriptor = _open_absolute_directory(
            state_root,
            label="model-session state_root",
            create_missing=True,
        )
        descriptors.callback(os.close, state_descriptor)
        _validate_private_directory_descriptor(
            state_descriptor,
            path=state_root,
            label="model-session state_root",
        )
        sessions_descriptor = _ensure_private_child_directory(
            state_descriptor,
            "sessions",
            path=sessions_root,
            label="model-session sessions directory",
        )
        descriptors.callback(os.close, sessions_descriptor)
        profile_sessions_descriptor = _ensure_private_child_directory(
            sessions_descriptor,
            profile.contract.profile_id,
            path=profile_sessions,
            label="profile sessions directory",
        )
        descriptors.callback(os.close, profile_sessions_descriptor)
        project_descriptor = _open_absolute_directory(
            project_root,
            label="project_root",
        )
        descriptors.callback(os.close, project_descriptor)
        _validate_project_directory_descriptor(
            project_descriptor,
            path=project_root,
            label="project_root",
        )

        with _materialization_lock(state_descriptor, state_root):
            _recover_incomplete_materializations(
                profile_sessions_descriptor,
                profile_sessions,
                project_descriptor,
                project_root,
            )
            for _ in range(16):
                session_id = _new_session_id()
                final = profile_sessions / session_id
                staging_name = f".creating-{session_id}"
                staging = profile_sessions / staging_name
                if _entry_exists_at(
                    profile_sessions_descriptor,
                    session_id,
                ) or _entry_exists_at(
                    profile_sessions_descriptor,
                    staging_name,
                ):
                    continue
                report_directory: pathlib.Path | None = None
                memory_directory: pathlib.Path | None = None
                reports_descriptor: int | None = None
                memory_descriptor: int | None = None
                staging_created = False
                published = False
                try:
                    staging_descriptor = _create_private_child_directory(
                        profile_sessions_descriptor,
                        staging_name,
                        path=staging,
                        label="session staging directory",
                    )
                    descriptors.callback(os.close, staging_descriptor)
                    staging_created = True
                    (
                        report_directory,
                        memory_directory,
                        reports_descriptor,
                        memory_descriptor,
                    ) = _make_project_session_directories(
                        project_descriptor,
                        project_root,
                        session_id,
                    )
                    descriptors.callback(os.close, reports_descriptor)
                    descriptors.callback(os.close, memory_descriptor)
                    created_at = _created_at()
                    _materialize_staging(
                        staging_descriptor,
                        staging,
                        profile,
                        runtime_assets,
                        pi_installation,
                        session_id=session_id,
                        created_at=created_at,
                        report_directory=report_directory,
                        memory_directory=memory_directory,
                    )
                    if _entry_exists_at(
                        profile_sessions_descriptor,
                        session_id,
                    ):
                        _fail(
                            f"generated session already exists: {final}",
                            code="session_id_collision",
                        )
                    try:
                        os.rename(
                            staging_name,
                            session_id,
                            src_dir_fd=profile_sessions_descriptor,
                            dst_dir_fd=profile_sessions_descriptor,
                        )
                    except OSError as error:
                        raise ModelSessionError(
                            f"cannot publish generated session {final}: {error}",
                            code="session_materialization_failed",
                        ) from error
                    published = True
                    try:
                        os.fsync(profile_sessions_descriptor)
                    except OSError as error:
                        raise ModelSessionError(
                            "session was published but its directory entry "
                            "durability is unknown; inspect "
                            f"{final}: {error}",
                            code="published_session_durability_unknown",
                        ) from error
                    try:
                        return load_run(profile, session_id)
                    except Exception as error:
                        raise ModelSessionError(
                            "session was durably published but its persisted state "
                            f"could not be validated; inspect {final}: {error}",
                            code="published_session_requires_recovery",
                        ) from error
                except ModelSessionError as error:
                    if not published:
                        try:
                            if staging_created:
                                _remove_private_tree_at(
                                    profile_sessions_descriptor,
                                    staging_name,
                                    path=staging,
                                )
                            _remove_empty_project_directories(
                                reports_descriptor,
                                memory_descriptor,
                                report_directory,
                                memory_directory,
                                session_id,
                            )
                        except ModelSessionError as cleanup_error:
                            raise cleanup_error from error
                    if error.code == "session_id_collision":
                        continue
                    raise
                except Exception as error:
                    if not published:
                        try:
                            if staging_created:
                                _remove_private_tree_at(
                                    profile_sessions_descriptor,
                                    staging_name,
                                    path=staging,
                                )
                            _remove_empty_project_directories(
                                reports_descriptor,
                                memory_descriptor,
                                report_directory,
                                memory_directory,
                                session_id,
                            )
                        except ModelSessionError as cleanup_error:
                            raise cleanup_error from error
                    raise ModelSessionError(
                        f"cannot materialize session: {error}",
                        code="session_materialization_failed",
                    ) from error
            _fail(
                "could not allocate a collision-free internal session ID",
                code="session_id_exhausted",
            )


def _parse_resource_entries(
    value: Any,
    *,
    snapshot_descriptor: int,
    snapshot_root: pathlib.Path,
) -> tuple[LockedResource, ...]:
    if not isinstance(value, list) or not value:
        _fail("lock resources must be a nonempty array")
    resources = []
    seen_paths: set[str] = set()
    seen_roles: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict):
            _fail("lock resource entry must be an object")
        _require_keys(entry, _RESOURCE_KEYS, label="lock resource")
        relative = entry["path"]
        roles = entry["roles"]
        digest = entry["sha256"]
        size = entry["size"]
        if (
            not isinstance(relative, str)
            or "\\" in relative
        ):
            _fail("lock resource path is invalid")
        pure_path = pathlib.PurePosixPath(relative)
        if (
            pure_path.is_absolute()
            or pure_path.as_posix() != relative
            or any(part in {"", ".", ".."} for part in pure_path.parts)
            or len(pure_path.parts) < 2
            or pure_path.parts[0] not in {"profile", "pi", "runtime"}
            or relative in seen_paths
        ):
            _fail("lock resource path is duplicate or non-normalized")
        if (
            not isinstance(roles, list)
            or not roles
            or any(not isinstance(role, str) or not role for role in roles)
            or len(set(roles)) != len(roles)
        ):
            _fail("lock resource roles are invalid")
        for role in roles:
            if role in seen_roles:
                _fail(f"lock resource role appears more than once: {role}")
            seen_roles.add(role)
        if not isinstance(digest, str) or not HASH_PATTERN.fullmatch(digest):
            _fail("lock resource hash is invalid")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            _fail("lock resource size is invalid")
        path = snapshot_root.joinpath(*pure_path.parts)
        content = _read_relative_private_file(
            snapshot_descriptor,
            snapshot_root,
            pure_path,
            label=f"locked resource {relative}",
        )
        if len(content) != size or _sha256(content) != digest:
            _fail(
                f"locked resource hash or size mismatch: {relative}",
                code="immutable_snapshot_changed",
            )
        seen_paths.add(relative)
        resources.append(
            LockedResource(
                relative_path=pure_path,
                roles=tuple(roles),
                path=path,
                sha256=digest,
                size=size,
            )
        )
    if "profile" not in seen_roles or "agents" not in seen_roles:
        _fail("lock resources do not identify profile.toml and AGENTS.md")
    return tuple(resources)


def _validate_snapshot_tree(
    snapshot_descriptor: int,
    snapshot_root: pathlib.Path,
    resources: tuple[LockedResource, ...],
) -> None:
    expected_files = {
        resource.relative_path.as_posix() for resource in resources
    }
    expected_files.add("lock.json")
    expected_directories = {""}
    for relative in expected_files:
        parent = pathlib.PurePosixPath(relative).parent
        while parent != pathlib.PurePosixPath("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent

    actual_files: set[str] = set()
    actual_directories = {""}

    def walk(
        descriptor: int,
        relative_root: pathlib.PurePosixPath,
    ) -> None:
        for name in os.listdir(descriptor):
            relative = (
                pathlib.PurePosixPath(name)
                if relative_root == pathlib.PurePosixPath(".")
                else relative_root / name
            )
            path = snapshot_root.joinpath(*relative.parts)
            try:
                metadata = os.stat(
                    name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise ModelSessionError(
                    f"cannot inspect snapshot entry {path}: {error}",
                    code="unsafe_session_state",
                ) from error
            if stat.S_ISDIR(metadata.st_mode):
                child = _open_private_child_directory(
                    descriptor,
                    name,
                    path=path,
                    label="snapshot directory",
                )
                actual_directories.add(relative.as_posix())
                try:
                    walk(child, relative)
                finally:
                    os.close(child)
                continue
            _read_private_file_at(
                descriptor,
                name,
                path=path,
                label="snapshot file",
            )
            actual_files.add(relative.as_posix())

    walk(snapshot_descriptor, pathlib.PurePosixPath("."))
    if actual_files != expected_files or actual_directories != expected_directories:
        _fail(
            "immutable snapshot contains missing or unexpected paths",
            code="immutable_snapshot_changed",
        )


def _validate_resource_roles(
    profile: ProfileContract,
    resources: tuple[LockedResource, ...],
) -> None:
    expected: dict[str, set[str]] = {
        f"profile/{PROFILE_FILE_NAME}": {"profile"},
        f"profile/{AGENTS_FILE_NAME}": {"agents"},
    }
    if profile.pi.system_prompt_file is not None:
        path = f"profile/{profile.pi.system_prompt_file.as_posix()}"
        expected.setdefault(path, set()).add("system_prompt")
    if profile.pi.append_system_prompt_file is not None:
        path = f"profile/{profile.pi.append_system_prompt_file.as_posix()}"
        expected.setdefault(path, set()).add("append_system_prompt")
    expected.update(
        {
            PI_MODELS_PATH.as_posix(): {PI_MODELS_ROLE},
            INFERENCE_RELAY_PATH.as_posix(): {INFERENCE_RELAY_ROLE},
            SESSION_POLICY_PATH.as_posix(): {SESSION_POLICY_ROLE},
        }
    )
    actual = {
        resource.relative_path.as_posix(): set(resource.roles)
        for resource in resources
    }
    if actual != expected:
        _fail(
            "locked resource paths and roles do not match profile.toml",
            code="immutable_snapshot_changed",
        )


def _validate_generated_pi_configuration(
    profile: ProfileContract,
    resources: tuple[LockedResource, ...],
    *,
    snapshot_descriptor: int,
    snapshot_root: pathlib.Path,
) -> None:
    resources_by_path = {
        resource.relative_path: resource for resource in resources
    }
    for expected in generated_pi_configuration_assets(profile):
        resource = resources_by_path.get(expected.relative_path)
        if resource is None:
            _fail(
                "locked Pi configuration is incomplete",
                code="immutable_snapshot_changed",
            )
        content = _read_relative_private_file(
            snapshot_descriptor,
            snapshot_root,
            resource.relative_path,
            label=(
                "generated locked Pi configuration "
                f"{resource.relative_path.as_posix()}"
            ),
        )
        if (
            content != expected.content
            or resource.sha256 != expected.sha256
            or resource.size != expected.size
        ):
            _fail(
                "locked Pi configuration does not match the locked profile: "
                f"{resource.relative_path.as_posix()}",
                code="immutable_snapshot_changed",
            )


def _validate_workspace_file(
    workspace_descriptor: int,
    workspace: pathlib.Path,
) -> None:
    _read_private_file_at(
        workspace_descriptor,
        AGENTS_FILE_NAME,
        path=workspace / AGENTS_FILE_NAME,
        label="workspace AGENTS.md",
    )


def _validate_masked_workspace_pi(
    workspace_descriptor: int,
    workspace: pathlib.Path,
) -> None:
    path = workspace / ".pi"
    descriptor = _open_private_child_directory(
        workspace_descriptor,
        ".pi",
        path=path,
        label="masked workspace .pi mountpoint",
    )
    try:
        entries = os.listdir(descriptor)
    except OSError as error:
        os.close(descriptor)
        raise ModelSessionError(
            f"cannot inspect masked workspace .pi mountpoint {path}: {error}",
            code="unsafe_session_state",
        ) from error
    os.close(descriptor)
    if entries:
        _fail(
            "workspace .pi mountpoint must remain empty so project-local Pi "
            "configuration cannot become resume-time authority",
            code="unsafe_session_state",
        )


def _load_run(
    state_root: pathlib.Path,
    profile_id: str,
    session_id: str,
) -> SessionRun:
    _validate_session_id(session_id)
    sessions_root = state_root / "sessions"
    profile_sessions = sessions_root / profile_id
    root = profile_sessions / session_id
    snapshot_root = root / "snapshot"
    workspace = root / "workspace"
    pi_root = root / "pi"
    pi_sessions = pi_root / "sessions"

    with contextlib.ExitStack() as descriptors:
        state_descriptor = _open_absolute_directory(
            state_root,
            label="model-session state_root",
        )
        descriptors.callback(os.close, state_descriptor)
        _validate_private_directory_descriptor(
            state_descriptor,
            path=state_root,
            label="model-session state_root",
        )
        sessions_descriptor = _open_private_child_directory(
            state_descriptor,
            "sessions",
            path=sessions_root,
            label="model-session sessions directory",
        )
        descriptors.callback(os.close, sessions_descriptor)
        profile_sessions_descriptor = _open_private_child_directory(
            sessions_descriptor,
            profile_id,
            path=profile_sessions,
            label="profile sessions directory",
        )
        descriptors.callback(os.close, profile_sessions_descriptor)
        root_descriptor = _open_private_child_directory(
            profile_sessions_descriptor,
            session_id,
            path=root,
            label="session root",
        )
        descriptors.callback(os.close, root_descriptor)

        receipt, _ = _load_json_at(
            root_descriptor,
            "run.json",
            path=root / "run.json",
            label="session receipt",
        )
        _require_keys(receipt, _RUN_KEYS, label="session receipt")
        if receipt["schema"] != RUN_SCHEMA:
            _fail(f"session receipt schema must be {RUN_SCHEMA!r}")
        if receipt["session_id"] != session_id:
            _fail("session receipt ID does not match selected session")
        if receipt["profile_id"] != profile_id:
            _fail("session receipt belongs to another profile")
        if not isinstance(receipt["project_id"], str):
            _fail("session receipt project_id is invalid")
        created_at = _validate_created_at(receipt["created_at"])
        expected_lock_hash = receipt["lock_sha256"]
        if not isinstance(expected_lock_hash, str) or not HASH_PATTERN.fullmatch(
            expected_lock_hash
        ):
            _fail("session receipt lock hash is invalid")

        snapshot_descriptor = _open_private_child_directory(
            root_descriptor,
            "snapshot",
            path=snapshot_root,
            label="snapshot root",
        )
        descriptors.callback(os.close, snapshot_descriptor)
        workspace_descriptor = _open_private_child_directory(
            root_descriptor,
            "workspace",
            path=workspace,
            label="session workspace",
        )
        descriptors.callback(os.close, workspace_descriptor)
        pi_descriptor = _open_private_child_directory(
            root_descriptor,
            "pi",
            path=pi_root,
            label="private Pi root",
        )
        descriptors.callback(os.close, pi_descriptor)
        pi_sessions_descriptor = _open_private_child_directory(
            pi_descriptor,
            "sessions",
            path=pi_sessions,
            label="private Pi sessions",
        )
        descriptors.callback(os.close, pi_sessions_descriptor)
        _validate_workspace_file(workspace_descriptor, workspace)
        _validate_masked_workspace_pi(workspace_descriptor, workspace)

        manifest, manifest_bytes = _load_json_at(
            snapshot_descriptor,
            "lock.json",
            path=snapshot_root / "lock.json",
            label="session lock manifest",
        )
        if _sha256(manifest_bytes) != expected_lock_hash:
            _fail(
                "session lock manifest does not match its receipt",
                code="immutable_snapshot_changed",
            )
        _require_keys(manifest, _LOCK_KEYS, label="session lock manifest")
        if manifest["schema"] != LOCK_SCHEMA:
            _fail(f"session lock schema must be {LOCK_SCHEMA!r}")
        if manifest["session_id"] != session_id:
            _fail("session lock ID does not match selected session")
        if manifest["created_at"] != created_at:
            _fail("session lock timestamp does not match its receipt")
        if not isinstance(manifest["source_profile_root"], str):
            _fail("session lock source_profile_root is invalid")

        resources = _parse_resource_entries(
            manifest["resources"],
            snapshot_descriptor=snapshot_descriptor,
            snapshot_root=snapshot_root,
        )
        _validate_snapshot_tree(
            snapshot_descriptor,
            snapshot_root,
            resources,
        )
        profile_resource = next(
            resource for resource in resources if "profile" in resource.roles
        )
        profile_document = _read_relative_private_file(
            snapshot_descriptor,
            snapshot_root,
            profile_resource.relative_path,
            label="locked profile document",
        )
        locked_profile = parse_locked_profile(
            profile_document,
            source_profile_root=manifest["source_profile_root"],
        )
        if locked_profile.as_dict() != manifest["profile"]:
            _fail(
                "locked profile contract does not match profile.toml",
                code="immutable_snapshot_changed",
            )
        _validate_resource_roles(locked_profile, resources)
        _validate_generated_pi_configuration(
            locked_profile,
            resources,
            snapshot_descriptor=snapshot_descriptor,
            snapshot_root=snapshot_root,
        )
        if locked_profile.profile_id != profile_id:
            _fail("locked profile ID does not match selected profile")
        if locked_profile.state_root != state_root:
            _fail("locked profile state_root does not match selected state")
        if receipt["project_id"] != locked_profile.project_id:
            _fail("session receipt project ID does not match locked profile")
        pi_installation = parse_pi_installation_identity(
            manifest["pi_installation"]
        )
        actual_pi_installation = fingerprint_pi_installation(locked_profile)
        if actual_pi_installation != pi_installation:
            _fail(
                "Pi installation content differs from the locked session "
                f"identity: expected {pi_installation.sha256}, got "
                f"{actual_pi_installation.sha256}",
                code="pi_installation_changed",
            )

        project_value = manifest["project"]
        if not isinstance(project_value, dict):
            _fail("session lock project binding must be an object")
        _require_keys(
            project_value,
            _PROJECT_KEYS,
            label="session lock project binding",
        )
        report_directory = (
            locked_profile.project_root / "reports" / session_id
        )
        memory_directory = (
            locked_profile.project_root / "memory" / session_id
        )
        if project_value != {
            "report_directory": str(report_directory),
            "memory_directory": str(memory_directory),
        }:
            _fail("session lock project paths are not canonical")

        project_descriptor = _open_absolute_directory(
            locked_profile.project_root,
            label="project_root",
        )
        descriptors.callback(os.close, project_descriptor)
        _validate_project_directory_descriptor(
            project_descriptor,
            path=locked_profile.project_root,
            label="project_root",
        )
        for root_name, directory, label in (
            ("reports", report_directory, "session report directory"),
            ("memory", memory_directory, "session memory directory"),
        ):
            project_state_descriptor = _open_private_child_directory(
                project_descriptor,
                root_name,
                path=locked_profile.project_root / root_name,
                label=f"project {root_name} directory",
            )
            descriptors.callback(os.close, project_state_descriptor)
            session_project_descriptor = _open_private_child_directory(
                project_state_descriptor,
                session_id,
                path=directory,
                label=label,
            )
            descriptors.callback(os.close, session_project_descriptor)

        return SessionRun(
            session_id=session_id,
            created_at=created_at,
            root=root,
            profile=locked_profile,
            snapshot_root=snapshot_root,
            workspace=workspace,
            pi_sessions=pi_sessions,
            report_directory=report_directory,
            memory_directory=memory_directory,
            resources=resources,
            pi_installation=pi_installation,
        )


def load_run(profile: Profile, session_id: str) -> SessionRun:
    """Load one run while using a current profile only as a state route."""

    return _load_run(
        profile.contract.state_root,
        profile.contract.profile_id,
        session_id,
    )


def load_run_from_state(
    state_root: str | pathlib.Path,
    profile_id: str,
    session_id: str,
) -> SessionRun:
    """Load a run without consulting its canonical profile directory."""

    validated_root, validated_profile_id = validate_state_route(
        state_root,
        profile_id,
    )
    return _load_run(
        validated_root,
        validated_profile_id,
        session_id,
    )
