"""Deterministic configuration and installation identity for the Pi runtime.

This module is intentionally independent of session materialization.  It
produces immutable inputs that a materializer can snapshot and later verify
without granting the model-facing process any profile or administrative
authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import stat
from dataclasses import dataclass, field
from typing import Any

from .errors import ModelSessionError
from .profile import ProfileContract


PI_MODELS_ROLE = "pi_models"
PI_SETTINGS_ROLE = "pi_settings"
INFERENCE_RELAY_ROLE = "inference_relay"
SESSION_POLICY_ROLE = "session_policy"

PI_MODELS_PATH = pathlib.PurePosixPath("pi/models.json")
PI_SETTINGS_PATH = pathlib.PurePosixPath("pi/settings.json")
INFERENCE_RELAY_PATH = pathlib.PurePosixPath("runtime/relay.py")
SESSION_POLICY_PATH = pathlib.PurePosixPath("runtime/session-policy.js")

PI_INFERENCE_BASE_URL = "http://127.0.0.1:41111/v1"
PI_LOCAL_API_KEY = "model-session-local-no-secret"
PI_INSTALLATION_IDENTITY_SCHEMA = "model-session.pi-installation-tree.v1"

_TREE_HASH_DOMAIN = b"model-session.pi-installation-tree.v1\0"
_HASH_BUFFER_BYTES = 1024 * 1024


@dataclass(frozen=True)
class PiRuntimeAsset:
    """One immutable input destined for a locked session snapshot."""

    relative_path: pathlib.PurePosixPath
    roles: tuple[str, ...]
    content: bytes = field(repr=False)
    sha256: str
    size: int


@dataclass(frozen=True)
class PiInstallationIdentity:
    """Content and representation identity of one complete Pi installation.

    ``total_bytes`` is the sum of regular-file contents. Symlink target text is
    represented in the digest but is not counted as installed file data.
    """

    schema: str
    sha256: str
    entry_count: int
    directory_count: int
    regular_file_count: int
    symlink_count: int
    total_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "sha256": self.sha256,
            "entry_count": self.entry_count,
            "directory_count": self.directory_count,
            "regular_file_count": self.regular_file_count,
            "symlink_count": self.symlink_count,
            "total_bytes": self.total_bytes,
        }


@dataclass
class _TreeCounts:
    entry_count: int = 0
    directory_count: int = 0
    regular_file_count: int = 0
    symlink_count: int = 0
    total_bytes: int = 0


@dataclass(frozen=True)
class _ObjectSnapshot:
    stable_fields: tuple[int, ...]
    child_names: tuple[bytes, ...] | None = None
    symlink_target: bytes | None = None


def _fail(message: str, *, code: str = "unsafe_pi_installation") -> None:
    raise ModelSessionError(message, code=code)


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def render_pi_models_json(contract: ProfileContract) -> bytes:
    """Render the single fixed local provider admitted by a profile."""

    provider = contract.runtime.provider
    model_id = contract.runtime.model_id
    return _canonical_json_bytes(
        {
            "providers": {
                provider: {
                    "api": "openai-completions",
                    "apiKey": PI_LOCAL_API_KEY,
                    "baseUrl": PI_INFERENCE_BASE_URL,
                    "compat": {
                        "supportsDeveloperRole": False,
                        "supportsReasoningEffort": False,
                    },
                    "models": [
                        {
                            "contextWindow": contract.model.context_tokens,
                            "cost": {
                                "cacheRead": 0,
                                "cacheWrite": 0,
                                "input": 0,
                                "output": 0,
                            },
                            "id": model_id,
                            "input": list(contract.runtime.input_modalities),
                            "maxTokens": contract.model.max_output_tokens,
                            "reasoning": contract.runtime.reasoning,
                        }
                    ],
                }
            }
        }
    )


def render_pi_settings_json(contract: ProfileContract) -> bytes:
    """Render global Pi settings with one exact model in scope."""

    provider = contract.runtime.provider
    model_id = contract.runtime.model_id
    return _canonical_json_bytes(
        {
            "defaultModel": model_id,
            "defaultProjectTrust": "never",
            "defaultProvider": provider,
            "enableInstallTelemetry": False,
            "enabledModels": [f"{provider}/{model_id}"],
        }
    )


def _runtime_asset(
    relative_path: pathlib.PurePosixPath,
    role: str,
    content: bytes,
) -> PiRuntimeAsset:
    return PiRuntimeAsset(
        relative_path=relative_path,
        roles=(role,),
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
    )


def generated_pi_configuration_assets(
    contract: ProfileContract,
) -> tuple[PiRuntimeAsset, ...]:
    """Return canonical models.json and settings.json snapshot inputs."""

    return (
        _runtime_asset(
            PI_MODELS_PATH,
            PI_MODELS_ROLE,
            render_pi_models_json(contract),
        ),
        _runtime_asset(
            PI_SETTINGS_PATH,
            PI_SETTINGS_ROLE,
            render_pi_settings_json(contract),
        ),
    )


def _stable_stat_fields(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_stable_regular_file(path: pathlib.Path, *, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    else:
        _fail(
            "runtime assets require O_NOFOLLOW",
            code="pi_runtime_platform_unsupported",
        )
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ModelSessionError(
            f"cannot open {label} {path}: {error}",
            code="unsafe_runtime_asset",
        ) from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            _fail(
                f"{label} is not a regular file: {path}",
                code="unsafe_runtime_asset",
            )
        if hasattr(os, "getuid") and before.st_uid not in {0, os.getuid()}:
            _fail(
                f"{label} has an unexpected owner: {path}",
                code="unsafe_runtime_asset",
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, _HASH_BUFFER_BYTES)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if _stable_stat_fields(before) != _stable_stat_fields(after):
            _fail(
                f"{label} changed while it was read: {path}",
                code="runtime_asset_changed",
            )
        content = b"".join(chunks)
        if len(content) != before.st_size:
            _fail(
                f"{label} size changed while it was read: {path}",
                code="runtime_asset_changed",
            )
        return content
    finally:
        os.close(descriptor)


def committed_pi_runtime_assets() -> tuple[PiRuntimeAsset, ...]:
    """Load the repository-owned relay and Pi policy as immutable assets."""

    module_path = pathlib.Path(__file__)
    infrastructure_root = module_path.parents[2]
    relay_path = module_path.with_name("relay.py")
    policy_path = infrastructure_root / "model-session" / "session-policy.js"
    return (
        _runtime_asset(
            INFERENCE_RELAY_PATH,
            INFERENCE_RELAY_ROLE,
            _read_stable_regular_file(relay_path, label="inference relay"),
        ),
        _runtime_asset(
            SESSION_POLICY_PATH,
            SESSION_POLICY_ROLE,
            _read_stable_regular_file(policy_path, label="Pi session policy"),
        ),
    )


def pi_runtime_assets(contract: ProfileContract) -> tuple[PiRuntimeAsset, ...]:
    """Return every generated and repository-owned runtime snapshot input."""

    assets = (
        *generated_pi_configuration_assets(contract),
        *committed_pi_runtime_assets(),
    )
    roles = [role for asset in assets for role in asset.roles]
    paths = [asset.relative_path for asset in assets]
    if len(roles) != len(set(roles)) or len(paths) != len(set(paths)):
        _fail(
            "Pi runtime assets contain duplicate roles or paths",
            code="invalid_runtime_assets",
        )
    return assets


def _directory_open_flags() -> int:
    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required):
        _fail(
            "Pi installation identity requires O_DIRECTORY and O_NOFOLLOW",
            code="pi_runtime_platform_unsupported",
        )
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_absolute_directory(path: pathlib.Path) -> int:
    text = os.fspath(path)
    if (
        not path.is_absolute()
        or path.anchor != "/"
        or text != os.path.normpath(text)
        or path == pathlib.Path("/")
    ):
        _fail(f"Pi installation_root is not a dedicated normalized path: {path}")
    flags = _directory_open_flags()
    descriptor = os.open("/", flags)
    try:
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError as error:
        os.close(descriptor)
        raise ModelSessionError(
            f"cannot open Pi installation_root without following links: "
            f"{path}: {error}",
            code="unsafe_pi_installation",
        ) from error


def _validate_owner_and_mode(
    metadata: os.stat_result,
    *,
    relative_path: bytes,
    is_symlink: bool,
) -> None:
    display_path = os.fsdecode(relative_path)
    if hasattr(os, "getuid") and metadata.st_uid not in {0, os.getuid()}:
        _fail(f"Pi installation object has an unexpected owner: {display_path}")
    # Linux symlinks report mode 0777 regardless of their access policy. Their
    # ownership is validated, and they are never followed during this scan.
    if not is_symlink and metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        _fail(
            "Pi installation object is group- or world-writable: "
            f"{display_path}"
        )


def _update_framed(hasher: Any, value: bytes) -> None:
    hasher.update(len(value).to_bytes(8, byteorder="big", signed=False))
    hasher.update(value)


def _update_entry_header(
    hasher: Any,
    *,
    relative_path: bytes,
    kind: bytes,
    mode: int,
) -> None:
    _update_framed(hasher, b"entry")
    _update_framed(hasher, relative_path)
    _update_framed(hasher, kind)
    _update_framed(
        hasher,
        stat.S_IMODE(mode).to_bytes(4, byteorder="big", signed=False),
    )


def _entry_path(parent_path: bytes, name: str) -> bytes:
    name_bytes = os.fsencode(name)
    return name_bytes if parent_path == b"." else parent_path + b"/" + name_bytes


def _entry_parts(parent_parts: tuple[str, ...], name: str) -> tuple[str, ...]:
    return (*parent_parts, name)


def _validate_confined_symlink(
    target: str,
    *,
    parent_parts: tuple[str, ...],
    relative_path: bytes,
) -> None:
    if not target or os.path.isabs(target):
        _fail(
            "Pi installation symlink must have a nonempty relative target: "
            f"{os.fsdecode(relative_path)}"
        )
    resolved_parts = list(parent_parts)
    for component in target.split("/"):
        if component in {"", "."}:
            continue
        if component == "..":
            if not resolved_parts:
                _fail(
                    "Pi installation symlink target escapes installation_root: "
                    f"{os.fsdecode(relative_path)} -> {target}"
                )
            resolved_parts.pop()
            continue
        resolved_parts.append(component)


def _stat_child(
    parent_descriptor: int,
    name: str,
    *,
    relative_path: bytes,
) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError as error:
        raise ModelSessionError(
            "Pi installation changed or became unreadable while scanning "
            f"{os.fsdecode(relative_path)}: {error}",
            code="pi_installation_changed",
        ) from error


def _assert_unchanged(
    before: os.stat_result,
    after: os.stat_result,
    *,
    relative_path: bytes,
) -> None:
    if _stable_stat_fields(before) != _stable_stat_fields(after):
        _fail(
            "Pi installation changed while scanning "
            f"{os.fsdecode(relative_path)}",
            code="pi_installation_changed",
        )


def _scan_regular_file(
    parent_descriptor: int,
    name: str,
    *,
    relative_path: bytes,
    before: os.stat_result,
    hasher: Any,
    counts: _TreeCounts,
    snapshots: dict[bytes, _ObjectSnapshot],
) -> None:
    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as error:
        raise ModelSessionError(
            "cannot open Pi installation file without following links "
            f"{os.fsdecode(relative_path)}: {error}",
            code="pi_installation_changed",
        ) from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            _fail(
                "Pi installation entry changed type while scanning "
                f"{os.fsdecode(relative_path)}",
                code="pi_installation_changed",
            )
        _assert_unchanged(before, opened, relative_path=relative_path)
        _update_entry_header(
            hasher,
            relative_path=relative_path,
            kind=b"regular",
            mode=opened.st_mode,
        )
        hasher.update(
            opened.st_size.to_bytes(8, byteorder="big", signed=False)
        )
        bytes_read = 0
        while True:
            try:
                chunk = os.read(descriptor, _HASH_BUFFER_BYTES)
            except InterruptedError:
                continue
            if not chunk:
                break
            hasher.update(chunk)
            bytes_read += len(chunk)
        after = os.fstat(descriptor)
        _assert_unchanged(opened, after, relative_path=relative_path)
        named_after = _stat_child(
            parent_descriptor,
            name,
            relative_path=relative_path,
        )
        _assert_unchanged(after, named_after, relative_path=relative_path)
        if bytes_read != opened.st_size:
            _fail(
                "Pi installation file size changed while scanning "
                f"{os.fsdecode(relative_path)}",
                code="pi_installation_changed",
            )
        counts.regular_file_count += 1
        counts.total_bytes += bytes_read
        snapshots[relative_path] = _ObjectSnapshot(
            stable_fields=_stable_stat_fields(after),
        )
    finally:
        os.close(descriptor)


def _scan_symlink(
    parent_descriptor: int,
    name: str,
    *,
    relative_path: bytes,
    parent_parts: tuple[str, ...],
    before: os.stat_result,
    hasher: Any,
    counts: _TreeCounts,
    snapshots: dict[bytes, _ObjectSnapshot],
) -> None:
    try:
        target = os.readlink(name, dir_fd=parent_descriptor)
    except OSError as error:
        raise ModelSessionError(
            "cannot read Pi installation symlink "
            f"{os.fsdecode(relative_path)}: {error}",
            code="pi_installation_changed",
        ) from error
    after = _stat_child(
        parent_descriptor,
        name,
        relative_path=relative_path,
    )
    _assert_unchanged(before, after, relative_path=relative_path)
    _validate_confined_symlink(
        target,
        parent_parts=parent_parts,
        relative_path=relative_path,
    )
    _update_entry_header(
        hasher,
        relative_path=relative_path,
        kind=b"symlink",
        mode=before.st_mode,
    )
    _update_framed(hasher, os.fsencode(target))
    counts.symlink_count += 1
    snapshots[relative_path] = _ObjectSnapshot(
        stable_fields=_stable_stat_fields(after),
        symlink_target=os.fsencode(target),
    )


def _list_directory(
    descriptor: int,
    *,
    relative_path: bytes,
    error_code: str = "unsafe_pi_installation",
) -> list[str]:
    try:
        names = os.listdir(descriptor)
    except OSError as error:
        raise ModelSessionError(
            "cannot list Pi installation directory "
            f"{os.fsdecode(relative_path)}: {error}",
            code=error_code,
        ) from error
    return sorted(names, key=os.fsencode)


def _scan_directory(
    descriptor: int,
    *,
    relative_path: bytes,
    relative_parts: tuple[str, ...],
    before: os.stat_result,
    hasher: Any,
    counts: _TreeCounts,
    snapshots: dict[bytes, _ObjectSnapshot],
) -> None:
    _validate_owner_and_mode(
        before,
        relative_path=relative_path,
        is_symlink=False,
    )
    _update_entry_header(
        hasher,
        relative_path=relative_path,
        kind=b"directory",
        mode=before.st_mode,
    )
    counts.entry_count += 1
    counts.directory_count += 1

    names_before = _list_directory(
        descriptor,
        relative_path=relative_path,
    )
    for name in names_before:
        child_path = _entry_path(relative_path, name)
        child_before = _stat_child(
            descriptor,
            name,
            relative_path=child_path,
        )
        child_is_symlink = stat.S_ISLNK(child_before.st_mode)
        _validate_owner_and_mode(
            child_before,
            relative_path=child_path,
            is_symlink=child_is_symlink,
        )
        if stat.S_ISREG(child_before.st_mode):
            counts.entry_count += 1
            _scan_regular_file(
                descriptor,
                name,
                relative_path=child_path,
                before=child_before,
                hasher=hasher,
                counts=counts,
                snapshots=snapshots,
            )
            continue
        if child_is_symlink:
            counts.entry_count += 1
            _scan_symlink(
                descriptor,
                name,
                relative_path=child_path,
                parent_parts=relative_parts,
                before=child_before,
                hasher=hasher,
                counts=counts,
                snapshots=snapshots,
            )
            continue
        if stat.S_ISDIR(child_before.st_mode):
            try:
                child_descriptor = os.open(
                    name,
                    _directory_open_flags(),
                    dir_fd=descriptor,
                )
            except OSError as error:
                raise ModelSessionError(
                    "cannot open Pi installation directory without following "
                    f"links {os.fsdecode(child_path)}: {error}",
                    code="pi_installation_changed",
                ) from error
            try:
                opened = os.fstat(child_descriptor)
                _assert_unchanged(
                    child_before,
                    opened,
                    relative_path=child_path,
                )
                _scan_directory(
                    child_descriptor,
                    relative_path=child_path,
                    relative_parts=_entry_parts(relative_parts, name),
                    before=opened,
                    hasher=hasher,
                    counts=counts,
                    snapshots=snapshots,
                )
                after = os.fstat(child_descriptor)
                _assert_unchanged(opened, after, relative_path=child_path)
                named_after = _stat_child(
                    descriptor,
                    name,
                    relative_path=child_path,
                )
                _assert_unchanged(after, named_after, relative_path=child_path)
            finally:
                os.close(child_descriptor)
            continue
        _fail(
            "Pi installation contains an unsupported special file: "
            f"{os.fsdecode(child_path)}"
        )

    names_after = _list_directory(
        descriptor,
        relative_path=relative_path,
    )
    if names_after != names_before:
        _fail(
            "Pi installation directory entries changed while scanning "
            f"{os.fsdecode(relative_path)}",
            code="pi_installation_changed",
        )
    after = os.fstat(descriptor)
    _assert_unchanged(before, after, relative_path=relative_path)
    snapshots[relative_path] = _ObjectSnapshot(
        stable_fields=_stable_stat_fields(after),
        child_names=tuple(os.fsencode(name) for name in names_after),
    )


def _assert_matches_snapshot(
    metadata: os.stat_result,
    snapshot: _ObjectSnapshot,
    *,
    relative_path: bytes,
) -> None:
    if _stable_stat_fields(metadata) != snapshot.stable_fields:
        _fail(
            "Pi installation changed after scanning "
            f"{os.fsdecode(relative_path)}",
            code="pi_installation_changed",
        )


def _verify_tree_snapshot(
    descriptor: int,
    *,
    relative_path: bytes,
    snapshots: dict[bytes, _ObjectSnapshot],
    visited: set[bytes],
) -> None:
    snapshot = snapshots.get(relative_path)
    if snapshot is None or snapshot.child_names is None:
        _fail(
            "Pi installation snapshot is incomplete at "
            f"{os.fsdecode(relative_path)}",
            code="pi_installation_changed",
        )
    _assert_matches_snapshot(
        os.fstat(descriptor),
        snapshot,
        relative_path=relative_path,
    )
    visited.add(relative_path)
    names = _list_directory(
        descriptor,
        relative_path=relative_path,
        error_code="pi_installation_changed",
    )
    if tuple(os.fsencode(name) for name in names) != snapshot.child_names:
        _fail(
            "Pi installation directory entries changed after scanning "
            f"{os.fsdecode(relative_path)}",
            code="pi_installation_changed",
        )

    for name in names:
        child_path = _entry_path(relative_path, name)
        child_snapshot = snapshots.get(child_path)
        if child_snapshot is None:
            _fail(
                "Pi installation gained an unscanned object at "
                f"{os.fsdecode(child_path)}",
                code="pi_installation_changed",
            )
        child_metadata = _stat_child(
            descriptor,
            name,
            relative_path=child_path,
        )
        _assert_matches_snapshot(
            child_metadata,
            child_snapshot,
            relative_path=child_path,
        )
        visited.add(child_path)
        if stat.S_ISREG(child_metadata.st_mode):
            continue
        if stat.S_ISLNK(child_metadata.st_mode):
            try:
                target = os.readlink(name, dir_fd=descriptor)
            except OSError as error:
                raise ModelSessionError(
                    "cannot re-read Pi installation symlink "
                    f"{os.fsdecode(child_path)}: {error}",
                    code="pi_installation_changed",
                ) from error
            if os.fsencode(target) != child_snapshot.symlink_target:
                _fail(
                    "Pi installation symlink target changed after scanning "
                    f"{os.fsdecode(child_path)}",
                    code="pi_installation_changed",
                )
            child_after = _stat_child(
                descriptor,
                name,
                relative_path=child_path,
            )
            _assert_matches_snapshot(
                child_after,
                child_snapshot,
                relative_path=child_path,
            )
            continue
        if stat.S_ISDIR(child_metadata.st_mode):
            try:
                child_descriptor = os.open(
                    name,
                    _directory_open_flags(),
                    dir_fd=descriptor,
                )
            except OSError as error:
                raise ModelSessionError(
                    "cannot re-open Pi installation directory "
                    f"{os.fsdecode(child_path)}: {error}",
                    code="pi_installation_changed",
                ) from error
            try:
                _verify_tree_snapshot(
                    child_descriptor,
                    relative_path=child_path,
                    snapshots=snapshots,
                    visited=visited,
                )
                _assert_matches_snapshot(
                    os.fstat(child_descriptor),
                    child_snapshot,
                    relative_path=child_path,
                )
            finally:
                os.close(child_descriptor)
            named_after = _stat_child(
                descriptor,
                name,
                relative_path=child_path,
            )
            _assert_matches_snapshot(
                named_after,
                child_snapshot,
                relative_path=child_path,
            )
            continue
        _fail(
            "Pi installation object changed to an unsupported type at "
            f"{os.fsdecode(child_path)}",
            code="pi_installation_changed",
        )

    _assert_matches_snapshot(
        os.fstat(descriptor),
        snapshot,
        relative_path=relative_path,
    )


def fingerprint_pi_installation(
    contract: ProfileContract,
) -> PiInstallationIdentity:
    """Fingerprint the complete dedicated Pi tree without following links."""

    root = contract.pi.installation_root
    descriptor = _open_absolute_directory(root)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISDIR(before.st_mode):
            _fail(f"Pi installation_root is not a directory: {root}")
        hasher = hashlib.sha256()
        hasher.update(_TREE_HASH_DOMAIN)
        counts = _TreeCounts()
        snapshots: dict[bytes, _ObjectSnapshot] = {}
        _scan_directory(
            descriptor,
            relative_path=b".",
            relative_parts=(),
            before=before,
            hasher=hasher,
            counts=counts,
            snapshots=snapshots,
        )
        after = os.fstat(descriptor)
        _assert_unchanged(before, after, relative_path=b".")
        visited: set[bytes] = set()
        _verify_tree_snapshot(
            descriptor,
            relative_path=b".",
            snapshots=snapshots,
            visited=visited,
        )
        if visited != set(snapshots):
            _fail(
                "Pi installation snapshot contains unreachable objects",
                code="pi_installation_changed",
            )
    finally:
        os.close(descriptor)

    verification_descriptor = _open_absolute_directory(root)
    try:
        current = os.fstat(verification_descriptor)
        _assert_unchanged(before, current, relative_path=b".")
    finally:
        os.close(verification_descriptor)

    return PiInstallationIdentity(
        schema=PI_INSTALLATION_IDENTITY_SCHEMA,
        sha256=hasher.hexdigest(),
        entry_count=counts.entry_count,
        directory_count=counts.directory_count,
        regular_file_count=counts.regular_file_count,
        symlink_count=counts.symlink_count,
        total_bytes=counts.total_bytes,
    )
