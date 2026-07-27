"""Immutable loading and validation of isolated model-session runs."""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from .errors import ModelSessionError
from .lease import RunSource
from .ownership import owner_has_private_primary_group
from .pi_runtime import (
    INFERENCE_RELAY_PATH,
    INFERENCE_RELAY_ROLE,
    PI_MODELS_PATH,
    PI_MODELS_ROLE,
    SESSION_POLICY_PATH,
    SESSION_POLICY_ROLE,
    PiInstallationIdentity,
    fingerprint_pi_installation_for_root_descriptor,
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
    lock_sha256: str
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
    if metadata.st_mode & stat.S_IWOTH or (
        metadata.st_mode & stat.S_IWGRP
        and not owner_has_private_primary_group(metadata)
    ):
        _fail(
            f"{label} is writable by another principal: {path}",
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


def _open_optional_absolute_directory(
    path: pathlib.Path,
    *,
    label: str,
) -> int | None:
    try:
        return _open_absolute_directory(path, label=label)
    except ModelSessionError as error:
        if isinstance(error.__cause__, FileNotFoundError):
            return None
        raise


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


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _open_private_regular_file_at(
    parent_descriptor: int,
    name: str,
    *,
    path: pathlib.Path,
    label: str,
) -> int:
    name = _child_name(name, label=label)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
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
        _validate_private_regular_file_descriptor(
            descriptor,
            path=path,
            label=label,
        )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _validate_private_regular_file_descriptor(
    descriptor: int,
    *,
    path: pathlib.Path,
    label: str,
) -> None:
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
    if metadata.st_nlink != 1:
        _fail(
            f"{label} must not have filesystem aliases: {path}",
            code="unsafe_session_state",
        )


def _read_regular_file_descriptor(
    descriptor: int,
    *,
    label: str,
    maximum_bytes: int,
) -> bytes:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError as error:
        raise ModelSessionError(
            f"cannot seek {label}: {error}",
            code="unsafe_session_state",
        ) from error
    chunks: list[bytes] = []
    remaining = maximum_bytes + 1
    while remaining:
        try:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
        except OSError as error:
            raise ModelSessionError(
                f"cannot read {label}: {error}",
                code="unsafe_session_state",
            ) from error
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    content = b"".join(chunks)
    if len(content) > maximum_bytes:
        _fail(f"{label} is unexpectedly large", code="unsafe_session_state")
    return content


def _read_private_file_at(
    parent_descriptor: int,
    name: str,
    *,
    path: pathlib.Path,
    label: str,
    maximum_bytes: int = 8 * 1024 * 1024,
) -> bytes:
    descriptor = _open_private_regular_file_at(
        parent_descriptor,
        name,
        path=path,
        label=label,
    )
    try:
        return _read_regular_file_descriptor(
            descriptor,
            label=label,
            maximum_bytes=maximum_bytes,
        )
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


def _load_json_descriptor(
    descriptor: int,
    *,
    path: pathlib.Path,
    label: str,
) -> tuple[dict[str, Any], bytes]:
    _validate_private_regular_file_descriptor(
        descriptor,
        path=path,
        label=label,
    )
    content = _read_regular_file_descriptor(
        descriptor,
        label=label,
        maximum_bytes=8 * 1024 * 1024,
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


def _open_materialization_lock_file(
    lock_directory_descriptor: int,
    lock_path: pathlib.Path,
    *,
    create: bool,
) -> int | None:
    flags = (
        (os.O_RDWR if create else os.O_RDONLY)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    if create:
        flags |= os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(
            "materialize.lock",
            flags,
            0o600,
            dir_fd=lock_directory_descriptor,
        )
    except FileNotFoundError:
        if not create:
            return None
        raise
    except OSError as error:
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
        if metadata.st_nlink != 1:
            _fail(
                "session materialization lock must not have filesystem aliases",
                code="unsafe_session_state",
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextlib.contextmanager
def _shared_materialization_lock(
    state_descriptor: int,
    state_root: pathlib.Path,
) -> Iterator[bool]:
    """Coordinate read-only enumeration on the stable state-root inode."""

    lock_directory_path = state_root / "locks"
    lock_directory_descriptor: int | None = None
    descriptor: int | None = None
    state_locked = False
    try:
        fcntl.flock(state_descriptor, fcntl.LOCK_SH)
        state_locked = True
        lock_directory_descriptor = _open_optional_private_child_directory(
            state_descriptor,
            "locks",
            path=lock_directory_path,
            label="session lock directory",
        )
        if lock_directory_descriptor is None:
            yield False
            return
        descriptor = _open_materialization_lock_file(
            lock_directory_descriptor,
            lock_directory_path / "materialize.lock",
            create=False,
        )
        if descriptor is None:
            yield False
            return
        yield True
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if lock_directory_descriptor is not None:
            os.close(lock_directory_descriptor)
        if state_locked:
            fcntl.flock(state_descriptor, fcntl.LOCK_UN)


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


def _open_relative_private_directory(
    root_descriptor: int,
    root: pathlib.Path,
    relative: pathlib.PurePosixPath,
) -> int:
    descriptor = os.dup(root_descriptor)
    current_path = root
    try:
        for component in relative.parts:
            current_path /= component
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


def _parse_resource_entries(
    value: Any,
    *,
    snapshot_descriptor: int,
    snapshot_root: pathlib.Path,
) -> tuple[
    tuple[LockedResource, ...],
    dict[pathlib.PurePosixPath, bytes],
]:
    if not isinstance(value, list) or not value:
        _fail("lock resources must be a nonempty array")
    resources = []
    resource_contents: dict[pathlib.PurePosixPath, bytes] = {}
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
        resource_contents[pure_path] = content
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
    return tuple(resources), resource_contents


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
    resource_contents: dict[pathlib.PurePosixPath, bytes],
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
        content = resource_contents[resource.relative_path]
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


def _sealed_resource_file(
    relative_path: pathlib.PurePosixPath,
    content: bytes,
) -> int:
    """Copy one validated snapshot resource into an immutable memory file."""

    required_constants = (
        "F_ADD_SEALS",
        "F_GET_SEALS",
        "F_SEAL_GROW",
        "F_SEAL_SEAL",
        "F_SEAL_SHRINK",
        "F_SEAL_WRITE",
    )
    if (
        not hasattr(os, "memfd_create")
        or not hasattr(os, "MFD_ALLOW_SEALING")
        or any(not hasattr(fcntl, name) for name in required_constants)
    ):
        _fail(
            "sealed snapshot resources require Linux memfd seals",
            code="session_platform_unsupported",
        )
    flags = getattr(os, "MFD_CLOEXEC", 0) | os.MFD_ALLOW_SEALING
    try:
        descriptor = os.memfd_create("model-session-resource", flags)
    except OSError as error:
        raise ModelSessionError(
            "cannot allocate a sealed snapshot resource for "
            f"{relative_path.as_posix()}: {error}",
            code="session_platform_unsupported",
        ) from error
    try:
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                _fail(
                    "cannot populate sealed snapshot resource "
                    f"{relative_path.as_posix()}",
                    code="unsafe_session_state",
                )
            offset += written
        os.fchmod(descriptor, 0o400)
        os.lseek(descriptor, 0, os.SEEK_SET)
        seals = (
            fcntl.F_SEAL_WRITE
            | fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_SHRINK
            | fcntl.F_SEAL_SEAL
        )
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, seals)
        if fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & seals != seals:
            _fail(
                "kernel did not seal snapshot resource "
                f"{relative_path.as_posix()}",
                code="session_platform_unsupported",
            )
        os.set_inheritable(descriptor, False)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


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
    *,
    preopened_root_descriptor: int | None = None,
    preopened_receipt_descriptor: int | None = None,
    retained_sources: dict[RunSource, int] | None = None,
    retained_resources: dict[pathlib.PurePosixPath, int] | None = None,
    validate_pi_installation: bool = True,
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
        if (preopened_root_descriptor is None) != (
            preopened_receipt_descriptor is None
        ):
            _fail(
                "preopened run root and receipt must be supplied together",
                code="invalid_session_state",
            )
        if preopened_root_descriptor is None:
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
            receipt_descriptor = None
        else:
            root_descriptor = os.dup(preopened_root_descriptor)
            descriptors.callback(os.close, root_descriptor)
            receipt_descriptor = os.dup(preopened_receipt_descriptor)
            descriptors.callback(os.close, receipt_descriptor)
            _validate_private_directory_descriptor(
                root_descriptor,
                path=root,
                label="session root",
            )
        if preopened_root_descriptor is None:
            descriptors.callback(os.close, root_descriptor)

        receipt, _ = (
            _load_json_at(
                root_descriptor,
                "run.json",
                path=root / "run.json",
                label="session receipt",
            )
            if receipt_descriptor is None
            else _load_json_descriptor(
                receipt_descriptor,
                path=root / "run.json",
                label="session receipt",
            )
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

        resources, resource_contents = _parse_resource_entries(
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
        profile_document = resource_contents[profile_resource.relative_path]
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
            resource_contents=resource_contents,
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
        installation_root = locked_profile.pi.installation_root
        pi_installation_descriptor: int | None = None
        if (
            validate_pi_installation
            or retained_sources is not None
            or retained_resources is not None
        ):
            pi_installation_descriptor = _open_absolute_directory(
                installation_root,
                label="Pi installation root",
            )
            descriptors.callback(os.close, pi_installation_descriptor)
            actual_pi_installation = (
                fingerprint_pi_installation_for_root_descriptor(
                    locked_profile,
                    pi_installation_descriptor,
                )
            )
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
        project_session_descriptors: dict[str, int] = {}
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
            project_session_descriptors[root_name] = session_project_descriptor

        if retained_sources is not None:
            if retained_sources:
                _fail(
                    "retained source output must be empty",
                    code="invalid_session_state",
                )
            if pi_installation_descriptor is None:
                _fail(
                    "retaining a run requires the Pi installation descriptor",
                    code="invalid_session_state",
                )
            exact_sources = {
                RunSource.WORKSPACE: workspace_descriptor,
                RunSource.PI_SESSIONS: pi_sessions_descriptor,
                RunSource.PI_INSTALLATION: pi_installation_descriptor,
                RunSource.PROJECT: project_descriptor,
                RunSource.REPORT: project_session_descriptors["reports"],
                RunSource.MEMORY: project_session_descriptors["memory"],
            }
            try:
                for source, descriptor in exact_sources.items():
                    duplicate = os.dup(descriptor)
                    os.set_inheritable(duplicate, False)
                    retained_sources[source] = duplicate
            except BaseException:
                for descriptor in retained_sources.values():
                    os.close(descriptor)
                retained_sources.clear()
                raise

        if retained_resources is not None:
            if retained_resources:
                _fail(
                    "retained resource output must be empty",
                    code="invalid_session_state",
                )
            try:
                for relative_path, content in resource_contents.items():
                    retained_resources[relative_path] = _sealed_resource_file(
                        relative_path,
                        content,
                    )
            except BaseException:
                for descriptor in retained_resources.values():
                    os.close(descriptor)
                retained_resources.clear()
                if retained_sources is not None:
                    for descriptor in retained_sources.values():
                        os.close(descriptor)
                    retained_sources.clear()
                raise

        return SessionRun(
            session_id=session_id,
            created_at=created_at,
            lock_sha256=expected_lock_hash,
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


def list_run_ids_from_state(
    state_root: str | pathlib.Path,
    profile_id: str,
) -> tuple[str, ...]:
    """Enumerate published run IDs without following state-tree links."""

    validated_root, validated_profile_id = validate_state_route(
        state_root,
        profile_id,
        require_existing=False,
    )
    state_descriptor = _open_optional_absolute_directory(
        validated_root,
        label="model-session state_root",
    )
    if state_descriptor is None:
        return ()
    sessions_root = validated_root / "sessions"
    profile_sessions = sessions_root / validated_profile_id
    with contextlib.ExitStack() as descriptors:
        descriptors.callback(os.close, state_descriptor)
        _validate_private_directory_descriptor(
            state_descriptor,
            path=validated_root,
            label="model-session state_root",
        )
        with _shared_materialization_lock(
            state_descriptor,
            validated_root,
        ) as has_materialization_lock:
            sessions_descriptor = _open_optional_private_child_directory(
                state_descriptor,
                "sessions",
                path=sessions_root,
                label="model-session sessions directory",
            )
            if sessions_descriptor is None:
                return ()
            descriptors.callback(os.close, sessions_descriptor)
            if not has_materialization_lock:
                _fail(
                    "session state exists without its stable "
                    "materialization lock",
                    code="unsafe_session_state",
                )
            profile_sessions_descriptor = (
                _open_optional_private_child_directory(
                    sessions_descriptor,
                    validated_profile_id,
                    path=profile_sessions,
                    label="profile sessions directory",
                )
            )
            if profile_sessions_descriptor is None:
                return ()
            descriptors.callback(os.close, profile_sessions_descriptor)
            try:
                entries = tuple(os.listdir(profile_sessions_descriptor))
            except OSError as error:
                raise ModelSessionError(
                    "cannot enumerate profile sessions "
                    f"{profile_sessions}: {error}",
                    code="unsafe_session_state",
                ) from error
            session_ids: list[str] = []
            for name in entries:
                path = profile_sessions / name
                if STAGING_NAME_PATTERN.fullmatch(name):
                    _fail(
                        "incomplete session materialization requires "
                        f"inspection: {path}",
                        code="incomplete_session_materialization",
                    )
                if not SESSION_ID_PATTERN.fullmatch(name):
                    _fail(
                        "unexpected entry in profile session state: "
                        f"{path}",
                        code="unsafe_session_state",
                    )
                descriptor = _open_private_child_directory(
                    profile_sessions_descriptor,
                    name,
                    path=path,
                    label="session root",
                )
                os.close(descriptor)
                session_ids.append(name)
            return tuple(sorted(session_ids, reverse=True))


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
