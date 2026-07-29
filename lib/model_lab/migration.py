"""Explicit, provider-free migration of legacy model-session profiles.

The migration copies user-authored and mutable history bytes, but rebuilds the
immutable run envelope.  A migrated run therefore has a canonical profile-v3
snapshot, current model-session runtime assets, and an explicit frozen service
binding while retaining its original session ID, timestamp, workspace, Pi
JSONL, and project report/memory trees.

Publication is profile-last.  Runtimes, project state, and the rewritten run
tree may be durably present after an interrupted attempt, but no active profile
route exists until every production loader has accepted the target runs.
Sources are opened read-only and are never renamed or deleted.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import hashlib
import json
import os
import pathlib
import re
import secrets
import stat
from collections.abc import Sequence
from typing import Any

from model_session.attachment import (
    ServiceEndpointBinding,
)
from model_session.lease import RunInspection, inspect_run_from_state
from model_session.pi_runtime import (
    PiInstallationIdentity,
    fingerprint_pi_installation,
    pi_runtime_assets,
)
from model_session.profile import (
    PROFILE_FILE_NAME,
    PROFILE_SCHEMA_V1,
    Profile,
    ProfileContract,
    SandboxContract,
    StorageContract,
    load_legacy_profile_for_migration,
    load_profile,
    parse_locked_profile,
)
from model_session.runs import (
    LOCK_SCHEMA_V2,
    RUN_SCHEMA,
    SessionRun,
    list_run_ids_from_state,
    load_run_from_state,
)
from model_session.service_endpoint import parse_service_endpoint_binding

from .documents import canonical_json_bytes, canonical_sha256
from .errors import ModelLabError
from .migration_files import (
    _absolute_normalized_path,
    _directory_flags,
    _ensure_directory,
    _ensure_model_session_lock,
    _entry_metadata,
    _fsync_path,
    _migration_lock,
    _open_directory_no_links,
    _open_source_directory,
    _paths_overlap,
    _publish_directory,
    _state_materialization_read_lock,
    _validate_destination_root,
    _validate_source_metadata,
    _write_file,
)
from .profile_binding import (
    PROFILE_BINDING_FILE_NAME,
    ProfileBinding,
    ProfileBindingStore,
)


MIGRATION_SCHEMA = "model-lab.legacy-profile-migration.v1"
MIGRATION_ATTEMPT_SCHEMA = "model-lab.legacy-profile-migration-attempt.v1"
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_COPY_BUFFER_BYTES = 1024 * 1024
_TREE_HASH_DOMAIN = b"model-lab.migration-tree.v1\0"


@dataclasses.dataclass(frozen=True)
class MigrationPolicy:
    """Explicit profile-v3 policy required when a legacy v1 profile has none."""

    storage: StorageContract
    sandbox: SandboxContract


@dataclasses.dataclass(frozen=True)
class MigratedRun:
    session_id: str
    source_lock_sha256: str
    target_lock_sha256: str
    workspace_sha256: str
    pi_sessions_sha256: str
    report_sha256: str
    memory_sha256: str
    recovered_sha256: str | None

    def normalized(self) -> dict[str, str | None]:
        return {
            "session_id": self.session_id,
            "source_lock_sha256": self.source_lock_sha256,
            "target_lock_sha256": self.target_lock_sha256,
            "workspace_sha256": self.workspace_sha256,
            "pi_sessions_sha256": self.pi_sessions_sha256,
            "report_sha256": self.report_sha256,
            "memory_sha256": self.memory_sha256,
            "recovered_sha256": self.recovered_sha256,
        }


@dataclasses.dataclass(frozen=True)
class MigrationResult:
    migration_id: str
    profile_root: pathlib.Path
    state_root: pathlib.Path
    profile_id: str
    project_id: str
    service_id: str
    workload_sha256: str
    runs: tuple[MigratedRun, ...]
    receipt_path: pathlib.Path


@dataclasses.dataclass(frozen=True)
class _RunMigrationPlan:
    """Exact immutable target envelope for one quiesced legacy run."""

    source: SessionRun
    contract: ProfileContract
    service_binding: ServiceEndpointBinding
    pi_identity: PiInstallationIdentity
    contents: dict[pathlib.PurePosixPath, bytes]
    manifest_bytes: bytes
    receipt_bytes: bytes

    @property
    def target_lock_sha256(self) -> str:
        return hashlib.sha256(self.manifest_bytes).hexdigest()


def _fail(message: str, *, code: str = "legacy_migration_failed") -> None:
    raise ModelLabError(message, code=code)


def _indented_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _identifier(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        _fail(
            f"{label} is not a valid lowercase identifier",
            code="invalid_legacy_migration_request",
        )
    return value


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _array(values: Sequence[str]) -> str:
    return "[" + ", ".join(_quote(value) for value in values) + "]"


def _policy_for(
    contract: ProfileContract,
    explicit_v1_policy: MigrationPolicy | None,
) -> MigrationPolicy:
    if contract.storage is not None and contract.sandbox is not None:
        return MigrationPolicy(
            storage=contract.storage,
            sandbox=contract.sandbox,
        )
    if contract.schema != PROFILE_SCHEMA_V1:
        raise AssertionError("legacy profile policy state is inconsistent")
    if explicit_v1_policy is None:
        _fail(
            "legacy profile v1 has no storage or sandbox contract; migration "
            "requires an explicit v1_policy",
            code="legacy_v1_policy_required",
        )
    return explicit_v1_policy


def _render_profile_v3(
    source: ProfileContract,
    *,
    profile_id: str,
    project_id: str,
    service_id: str,
    input_modalities: tuple[str, ...],
    policy: MigrationPolicy,
) -> bytes:
    pi = source.pi
    lines = [
        'schema = "model-session.profile.v3"',
        f"profile_id = {_quote(profile_id)}",
        f"project_id = {_quote(project_id)}",
        f"service_id = {_quote(service_id)}",
        "",
        "[endpoint]",
        "required_input_modalities = " + _array(input_modalities),
        "",
        "[pi]",
        f"version = {_quote(pi.version)}",
        "tools = " + _array(pi.tools),
    ]
    if pi.system_prompt_file is not None:
        lines.append("system_prompt_file = " + _quote(pi.system_prompt_file.as_posix()))
    if pi.append_system_prompt_file is not None:
        lines.append(
            "append_system_prompt_file = "
            + _quote(pi.append_system_prompt_file.as_posix())
        )
    storage = policy.storage
    sandbox = policy.sandbox
    lines.extend(
        [
            "",
            "[storage]",
            f"max_sessions = {storage.max_sessions}",
            f"work_bytes = {storage.work_bytes}",
            f"work_inodes = {storage.work_inodes}",
            f"history_bytes = {storage.history_bytes}",
            f"history_inodes = {storage.history_inodes}",
            f"checkpoint_bytes = {storage.checkpoint_bytes}",
            f"max_file_bytes = {storage.max_file_bytes}",
            f"max_logical_bytes = {storage.max_logical_bytes}",
            "",
            "[sandbox]",
            f"memory_bytes = {sandbox.memory_bytes}",
            f"max_tasks = {sandbox.max_tasks}",
            f"max_runtime_seconds = {sandbox.max_runtime_seconds}",
            f"idle_timeout_seconds = {sandbox.idle_timeout_seconds}",
            f"shutdown_grace_seconds = {sandbox.shutdown_grace_seconds}",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def _validated_binding(
    binding: ServiceEndpointBinding,
) -> ServiceEndpointBinding:
    try:
        parsed = parse_service_endpoint_binding(binding.as_dict())
    except Exception as error:
        raise ModelLabError(
            f"service_binding is invalid: {error}",
            code="invalid_legacy_migration_request",
        ) from error
    if parsed != binding:
        _fail(
            "service_binding does not round-trip through the canonical parser",
            code="invalid_legacy_migration_request",
        )
    return parsed


def _attest_legacy_workload(
    contract: ProfileContract,
    binding: ServiceEndpointBinding,
    *,
    label: str,
) -> tuple[str, ...]:
    model = contract.model
    runtime = contract.runtime
    if model is None or runtime is None:
        _fail(
            f"{label} is not a legacy model-bearing profile",
            code="invalid_legacy_migration_source",
        )
    workload = binding.workload
    expected = {
        "repository": model.repository,
        "revision": model.revision,
        "provider": runtime.provider,
        "model_id": runtime.model_id,
        "context_tokens": model.context_tokens,
        "max_output_tokens": model.max_output_tokens,
        "weight_format": model.weight_format,
        "kv_cache_dtype": model.kv_cache_dtype,
        "reasoning": runtime.reasoning,
    }
    actual = {
        "repository": workload.repository,
        "revision": workload.revision,
        "provider": workload.provider,
        "model_id": workload.model_id,
        "context_tokens": workload.context_tokens,
        "max_output_tokens": workload.max_output_tokens,
        "weight_format": workload.weight_format,
        "kv_cache_dtype": workload.kv_cache_dtype,
        "reasoning": workload.reasoning,
    }
    if actual != expected:
        differences = ", ".join(
            name for name in sorted(expected) if expected[name] != actual[name]
        )
        _fail(
            f"{label} does not match the explicit service workload ({differences})",
            code="legacy_service_workload_mismatch",
        )
    missing = set(runtime.input_modalities) - set(binding.input_modalities)
    if missing:
        _fail(
            f"{label} requires service capabilities absent from the explicit "
            f"binding: {', '.join(sorted(missing))}",
            code="legacy_service_capability_mismatch",
        )
    return runtime.input_modalities


def _stable_fields(metadata: os.stat_result) -> tuple[int, ...]:
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


def _frame(hasher: Any, value: bytes) -> None:
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _tree_header(
    hasher: Any,
    relative: bytes,
    kind: bytes,
    metadata: os.stat_result,
) -> None:
    _frame(hasher, relative)
    _frame(hasher, kind)
    hasher.update(stat.S_IMODE(metadata.st_mode).to_bytes(4, "big"))
    hasher.update(metadata.st_mtime_ns.to_bytes(16, "big", signed=True))


def _sparse_extents(
    descriptor: int,
    size: int,
    *,
    path: pathlib.Path,
) -> tuple[tuple[int, int], ...] | None:
    if not hasattr(os, "SEEK_DATA") or not hasattr(os, "SEEK_HOLE"):
        return None
    position = 0
    extents: list[tuple[int, int]] = []
    while position < size:
        try:
            data = os.lseek(descriptor, position, os.SEEK_DATA)
        except OSError as error:
            if error.errno == errno.ENXIO:
                break
            if error.errno in {
                errno.EINVAL,
                getattr(errno, "ENOTSUP", errno.EINVAL),
                getattr(errno, "EOPNOTSUPP", errno.EINVAL),
            }:
                return None
            raise ModelLabError(
                f"cannot inspect sparse extents in {path}: {error}",
                code="legacy_migration_copy_failed",
            ) from error
        try:
            hole = os.lseek(descriptor, data, os.SEEK_HOLE)
        except OSError as error:
            if error.errno == errno.ENXIO:
                hole = size
            else:
                raise ModelLabError(
                    f"cannot inspect sparse extents in {path}: {error}",
                    code="legacy_migration_copy_failed",
                ) from error
        end = min(hole, size)
        if data < position or data >= end:
            _fail(
                f"filesystem returned invalid sparse extents for {path}",
                code="legacy_migration_copy_failed",
            )
        extents.append((data, end))
        position = end
    return tuple(extents)


def _hash_zeroes(hasher: Any, count: int) -> None:
    zeroes = bytes(min(_COPY_BUFFER_BYTES, count))
    remaining = count
    while remaining:
        chunk = zeroes[: min(len(zeroes), remaining)]
        hasher.update(chunk)
        remaining -= len(chunk)


def _write_all(descriptor: int, content: bytes, *, path: pathlib.Path) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            _fail(
                f"short write while migrating {path}",
                code="legacy_migration_copy_failed",
            )
        view = view[written:]


def _copy_regular(
    source_parent: int,
    target_parent: int,
    name: str,
    *,
    source_path: pathlib.Path,
    target_path: pathlib.Path,
    relative: bytes,
    before: os.stat_result,
    hasher: Any,
) -> None:
    source_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    target_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        source = os.open(name, source_flags, dir_fd=source_parent)
    except OSError as error:
        raise ModelLabError(
            f"cannot open migration source file {source_path}: {error}",
            code="unsafe_legacy_migration_source",
        ) from error
    target: int | None = None
    try:
        opened = os.fstat(source)
        if not stat.S_ISREG(opened.st_mode) or _stable_fields(opened) != _stable_fields(
            before
        ):
            _fail(
                f"migration source changed while opening {source_path}",
                code="legacy_migration_source_changed",
            )
        _validate_source_metadata(opened, path=source_path)
        target = os.open(
            name,
            target_flags,
            0o600,
            dir_fd=target_parent,
        )
        _tree_header(hasher, relative, b"regular", opened)
        hasher.update(opened.st_size.to_bytes(16, "big"))
        extents = _sparse_extents(
            source,
            opened.st_size,
            path=source_path,
        )
        if extents is None:
            if opened.st_blocks * 512 < opened.st_size:
                _fail(
                    f"filesystem cannot safely preserve sparse file {source_path}",
                    code="legacy_sparse_copy_unsupported",
                )
            extents = ((0, opened.st_size),)
        position = 0
        for start, end in extents:
            _hash_zeroes(hasher, start - position)
            os.lseek(source, start, os.SEEK_SET)
            os.lseek(target, start, os.SEEK_SET)
            remaining = end - start
            while remaining:
                try:
                    chunk = os.read(
                        source,
                        min(_COPY_BUFFER_BYTES, remaining),
                    )
                except InterruptedError:
                    continue
                if not chunk:
                    _fail(
                        f"short read while migrating {source_path}",
                        code="legacy_migration_source_changed",
                    )
                hasher.update(chunk)
                _write_all(target, chunk, path=target_path)
                remaining -= len(chunk)
            position = end
        _hash_zeroes(hasher, opened.st_size - position)
        os.ftruncate(target, opened.st_size)
        after = os.fstat(source)
        named_after = os.stat(
            name,
            dir_fd=source_parent,
            follow_symlinks=False,
        )
        if _stable_fields(opened) != _stable_fields(after) or _stable_fields(
            after
        ) != _stable_fields(named_after):
            _fail(
                f"migration source changed while copying {source_path}",
                code="legacy_migration_source_changed",
            )
        os.fchmod(target, stat.S_IMODE(opened.st_mode))
        os.fsync(target)
        os.utime(
            name,
            ns=(opened.st_atime_ns, opened.st_mtime_ns),
            dir_fd=target_parent,
            follow_symlinks=False,
        )
    finally:
        if target is not None:
            os.close(target)
        os.close(source)


def _copy_directory_contents(
    source: int,
    target: int,
    *,
    source_path: pathlib.Path,
    target_path: pathlib.Path,
    relative_parts: tuple[str, ...],
    hasher: Any,
) -> None:
    source_before = os.fstat(source)
    _validate_source_metadata(source_before, path=source_path)
    try:
        names_before = sorted(os.listdir(source))
    except OSError as error:
        raise ModelLabError(
            f"cannot enumerate migration source {source_path}: {error}",
            code="unsafe_legacy_migration_source",
        ) from error
    for name in names_before:
        if (
            not isinstance(name, str)
            or not name
            or name in {".", ".."}
            or "/" in name
            or "\x00" in name
        ):
            _fail(
                f"migration source contains an invalid entry in {source_path}",
                code="unsafe_legacy_migration_source",
            )
        child_source_path = source_path / name
        child_target_path = target_path / name
        child_parts = (*relative_parts, name)
        relative = os.fsencode("/".join(child_parts))
        before = os.stat(name, dir_fd=source, follow_symlinks=False)
        if stat.S_ISREG(before.st_mode):
            _copy_regular(
                source,
                target,
                name,
                source_path=child_source_path,
                target_path=child_target_path,
                relative=relative,
                before=before,
                hasher=hasher,
            )
            continue
        if stat.S_ISLNK(before.st_mode):
            _validate_source_metadata(
                before,
                path=child_source_path,
                symlink=True,
            )
            try:
                link_target = os.readlink(name, dir_fd=source)
                os.symlink(link_target, name, dir_fd=target)
            except OSError as error:
                raise ModelLabError(
                    f"cannot migrate symlink {child_source_path}: {error}",
                    code="legacy_migration_copy_failed",
                ) from error
            after = os.stat(name, dir_fd=source, follow_symlinks=False)
            if _stable_fields(before) != _stable_fields(after):
                _fail(
                    f"migration source changed while copying {child_source_path}",
                    code="legacy_migration_source_changed",
                )
            _tree_header(hasher, relative, b"symlink", before)
            _frame(hasher, os.fsencode(link_target))
            try:
                os.utime(
                    name,
                    ns=(before.st_atime_ns, before.st_mtime_ns),
                    dir_fd=target,
                    follow_symlinks=False,
                )
            except (NotImplementedError, OSError):
                pass
            continue
        if stat.S_ISDIR(before.st_mode):
            _validate_source_metadata(before, path=child_source_path)
            try:
                child_source = os.open(
                    name,
                    _directory_flags(),
                    dir_fd=source,
                )
                os.mkdir(name, 0o700, dir_fd=target)
                child_target = os.open(
                    name,
                    _directory_flags(),
                    dir_fd=target,
                )
            except OSError as error:
                raise ModelLabError(
                    f"cannot migrate directory {child_source_path}: {error}",
                    code="legacy_migration_copy_failed",
                ) from error
            try:
                opened = os.fstat(child_source)
                if _stable_fields(opened) != _stable_fields(before):
                    _fail(
                        f"migration source changed while opening {child_source_path}",
                        code="legacy_migration_source_changed",
                    )
                _tree_header(hasher, relative, b"directory", opened)
                _copy_directory_contents(
                    child_source,
                    child_target,
                    source_path=child_source_path,
                    target_path=child_target_path,
                    relative_parts=child_parts,
                    hasher=hasher,
                )
                after = os.fstat(child_source)
                named_after = os.stat(
                    name,
                    dir_fd=source,
                    follow_symlinks=False,
                )
                if _stable_fields(opened) != _stable_fields(after) or _stable_fields(
                    after
                ) != _stable_fields(named_after):
                    _fail(
                        f"migration source changed while copying {child_source_path}",
                        code="legacy_migration_source_changed",
                    )
                os.fchmod(child_target, stat.S_IMODE(opened.st_mode))
                os.fsync(child_target)
                os.utime(
                    name,
                    ns=(opened.st_atime_ns, opened.st_mtime_ns),
                    dir_fd=target,
                    follow_symlinks=False,
                )
            finally:
                os.close(child_target)
                os.close(child_source)
            continue
        _fail(
            f"migration source contains an unsupported special file: "
            f"{child_source_path}",
            code="unsafe_legacy_migration_source",
        )
    names_after = sorted(os.listdir(source))
    source_after = os.fstat(source)
    if names_after != names_before or _stable_fields(source_before) != _stable_fields(
        source_after
    ):
        _fail(
            f"migration source changed while copying {source_path}",
            code="legacy_migration_source_changed",
        )
    os.fsync(target)


def _copy_tree(source_path: pathlib.Path, target_path: pathlib.Path) -> str:
    source = _open_source_directory(source_path)
    target: int | None = None
    target_parent: int | None = None
    try:
        source_metadata = os.fstat(source)
        _ensure_directory(target_path.parent)
        target_parent = _open_directory_no_links(
            target_path.parent,
            label="migration staging parent",
            code="unsafe_legacy_migration_destination",
        )
        if target_parent is None:
            raise AssertionError("migration staging parent is absent")
        try:
            os.mkdir(target_path.name, 0o700, dir_fd=target_parent)
        except OSError as error:
            raise ModelLabError(
                f"cannot create migration staging tree {target_path}: {error}",
                code="legacy_migration_copy_failed",
            ) from error
        target = os.open(
            target_path.name,
            _directory_flags(),
            dir_fd=target_parent,
        )
        hasher = hashlib.sha256()
        hasher.update(_TREE_HASH_DOMAIN)
        _tree_header(hasher, b".", b"directory", source_metadata)
        _copy_directory_contents(
            source,
            target,
            source_path=source_path,
            target_path=target_path,
            relative_parts=(),
            hasher=hasher,
        )
        source_after = os.fstat(source)
        if _stable_fields(source_metadata) != _stable_fields(source_after):
            _fail(
                f"migration source changed while copying {source_path}",
                code="legacy_migration_source_changed",
            )
        os.fchmod(target, stat.S_IMODE(source_metadata.st_mode))
        os.fsync(target)
        os.utime(
            target_path.name,
            ns=(source_metadata.st_atime_ns, source_metadata.st_mtime_ns),
            dir_fd=target_parent,
            follow_symlinks=False,
        )
        os.fsync(target_parent)
        return hasher.hexdigest()
    finally:
        if target is not None:
            os.close(target)
        if target_parent is not None:
            os.close(target_parent)
        os.close(source)


def _hash_tree_contents(
    descriptor: int,
    *,
    path: pathlib.Path,
    relative_parts: tuple[str, ...],
    hasher: Any,
) -> None:
    before = os.fstat(descriptor)
    _validate_source_metadata(before, path=path)
    names_before = sorted(os.listdir(descriptor))
    for name in names_before:
        child_path = path / name
        child_parts = (*relative_parts, name)
        relative = os.fsencode("/".join(child_parts))
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISREG(metadata.st_mode):
            _validate_source_metadata(metadata, path=child_path)
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            child = os.open(name, flags, dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                if _stable_fields(opened) != _stable_fields(metadata):
                    _fail(
                        f"migration tree changed while hashing {child_path}",
                        code="legacy_migration_source_changed",
                    )
                _tree_header(hasher, relative, b"regular", opened)
                hasher.update(opened.st_size.to_bytes(16, "big"))
                total = 0
                while True:
                    chunk = os.read(child, _COPY_BUFFER_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    hasher.update(chunk)
                after = os.fstat(child)
                if total != opened.st_size or _stable_fields(opened) != _stable_fields(
                    after
                ):
                    _fail(
                        f"migration tree changed while hashing {child_path}",
                        code="legacy_migration_source_changed",
                    )
            finally:
                os.close(child)
            continue
        if stat.S_ISLNK(metadata.st_mode):
            _validate_source_metadata(metadata, path=child_path, symlink=True)
            link_target = os.readlink(name, dir_fd=descriptor)
            _tree_header(hasher, relative, b"symlink", metadata)
            _frame(hasher, os.fsencode(link_target))
            continue
        if stat.S_ISDIR(metadata.st_mode):
            child = os.open(name, _directory_flags(), dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                if _stable_fields(opened) != _stable_fields(metadata):
                    _fail(
                        f"migration tree changed while hashing {child_path}",
                        code="legacy_migration_source_changed",
                    )
                _tree_header(hasher, relative, b"directory", opened)
                _hash_tree_contents(
                    child,
                    path=child_path,
                    relative_parts=child_parts,
                    hasher=hasher,
                )
            finally:
                os.close(child)
            continue
        _fail(
            f"migration tree contains an unsupported special file: {child_path}",
            code="unsafe_legacy_migration_source",
        )
    if sorted(os.listdir(descriptor)) != names_before or _stable_fields(
        os.fstat(descriptor)
    ) != _stable_fields(before):
        _fail(
            f"migration tree changed while hashing {path}",
            code="legacy_migration_source_changed",
        )


def _tree_sha256(path: pathlib.Path) -> str:
    descriptor = _open_source_directory(path)
    try:
        metadata = os.fstat(descriptor)
        hasher = hashlib.sha256()
        hasher.update(_TREE_HASH_DOMAIN)
        _tree_header(hasher, b".", b"directory", metadata)
        _hash_tree_contents(
            descriptor,
            path=path,
            relative_parts=(),
            hasher=hasher,
        )
        return hasher.hexdigest()
    finally:
        os.close(descriptor)


def _copy_or_attest_tree(
    source: pathlib.Path,
    destination: pathlib.Path,
    staging: pathlib.Path,
) -> str:
    source_hash = _tree_sha256(source)
    destination_metadata = _entry_metadata(
        destination,
        label="migration tree destination",
    )
    if destination_metadata is not None:
        if not stat.S_ISDIR(destination_metadata.st_mode):
            _fail(
                f"migration destination has the wrong type: {destination}",
                code="legacy_migration_destination_conflict",
            )
        target_hash = _tree_sha256(destination)
        if target_hash != source_hash:
            _fail(
                f"migration destination conflicts with source bytes: {destination}",
                code="legacy_migration_destination_conflict",
            )
        return source_hash
    copied_hash = _copy_tree(source, staging)
    if copied_hash != source_hash:
        _fail(
            f"migration copy differs from source: {source}",
            code="legacy_migration_copy_failed",
        )
    _publish_directory(staging, destination)
    target_hash = _tree_sha256(destination)
    if target_hash != source_hash:
        _fail(
            f"published migration tree differs from source: {destination}",
            code="published_migration_requires_recovery",
        )
    return source_hash


def _profile_resource_contents(
    profile: Profile,
) -> dict[pathlib.PurePosixPath, tuple[tuple[str, ...], bytes]]:
    return {
        resource.relative_path: (resource.roles, resource.content)
        for resource in profile.resources
    }


def _run_profile_resource_contents(
    run: SessionRun,
) -> dict[pathlib.PurePosixPath, tuple[tuple[str, ...], bytes]]:
    result: dict[pathlib.PurePosixPath, tuple[tuple[str, ...], bytes]] = {}
    for resource in run.resources:
        parts = resource.relative_path.parts
        if len(parts) < 2 or parts[0] != "profile":
            continue
        relative = pathlib.PurePosixPath(*parts[1:])
        if relative.as_posix() == PROFILE_FILE_NAME:
            continue
        try:
            content = resource.path.read_bytes()
        except OSError as error:
            raise ModelLabError(
                f"cannot read validated locked resource {resource.path}: {error}",
                code="legacy_migration_source_changed",
            ) from error
        if hashlib.sha256(content).hexdigest() != resource.sha256:
            _fail(
                f"locked profile resource changed after validation: {resource.path}",
                code="legacy_migration_source_changed",
            )
        result[relative] = (resource.roles, content)
    return result


def _write_profile_resources(
    root: pathlib.Path,
    document: bytes,
    resources: dict[pathlib.PurePosixPath, tuple[tuple[str, ...], bytes]],
) -> None:
    _write_file(root / PROFILE_FILE_NAME, document)
    for relative, (_roles, content) in sorted(
        resources.items(),
        key=lambda item: item[0].as_posix(),
    ):
        path = root.joinpath(*relative.parts)
        _ensure_directory(path.parent)
        _write_file(path, content)


def _resource_manifest(
    profile_document: bytes,
    profile_resources: dict[pathlib.PurePosixPath, tuple[tuple[str, ...], bytes]],
    contract: ProfileContract,
    binding: ServiceEndpointBinding,
) -> tuple[list[dict[str, Any]], dict[pathlib.PurePosixPath, bytes]]:
    contents: dict[pathlib.PurePosixPath, bytes] = {
        pathlib.PurePosixPath("profile", PROFILE_FILE_NAME): profile_document
    }
    roles: dict[pathlib.PurePosixPath, tuple[str, ...]] = {
        pathlib.PurePosixPath("profile", PROFILE_FILE_NAME): ("profile",)
    }
    for relative, (resource_roles, content) in profile_resources.items():
        path = pathlib.PurePosixPath("profile", *relative.parts)
        contents[path] = content
        roles[path] = resource_roles
    for asset in pi_runtime_assets(contract, binding):
        contents[asset.relative_path] = asset.content
        roles[asset.relative_path] = asset.roles
    entries: list[dict[str, Any]] = []
    for path in sorted(contents, key=lambda item: item.as_posix()):
        content = contents[path]
        entries.append(
            {
                "path": path.as_posix(),
                "roles": list(roles[path]),
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        )
    return entries, contents


def _plan_run(
    source: SessionRun,
    *,
    profile_id: str,
    project_id: str,
    project_root: pathlib.Path,
    profile_document: bytes,
    target_contract: ProfileContract,
    service_binding: ServiceEndpointBinding,
    pi_identity: PiInstallationIdentity,
) -> _RunMigrationPlan:
    profile_resources = _run_profile_resource_contents(source)
    entries, contents = _resource_manifest(
        profile_document,
        profile_resources,
        target_contract,
        service_binding,
    )
    manifest = {
        "schema": LOCK_SCHEMA_V2,
        "session_id": source.session_id,
        "created_at": source.created_at,
        "source_profile_root": str(
            target_contract.state_root / "profiles" / profile_id
        ),
        "profile": target_contract.as_dict(),
        "resources": entries,
        "pi_installation": pi_identity.as_dict(),
        "project": {
            "report_directory": str(project_root / "reports" / source.session_id),
            "memory_directory": str(project_root / "memory" / source.session_id),
        },
        "service": service_binding.as_dict(),
    }
    manifest_bytes = _indented_json_bytes(manifest)
    receipt = {
        "schema": RUN_SCHEMA,
        "session_id": source.session_id,
        "profile_id": profile_id,
        "project_id": project_id,
        "created_at": source.created_at,
        "lock_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }
    return _RunMigrationPlan(
        source=source,
        contract=target_contract,
        service_binding=service_binding,
        pi_identity=pi_identity,
        contents=contents,
        manifest_bytes=manifest_bytes,
        receipt_bytes=_indented_json_bytes(receipt),
    )


def _build_run_staging(
    staging: pathlib.Path,
    plan: _RunMigrationPlan,
) -> None:
    source = plan.source
    _ensure_directory(staging)
    snapshot = staging / "snapshot"
    workspace = staging / "workspace"
    pi_root = staging / "pi"
    _ensure_directory(snapshot)
    _copy_tree(source.workspace, workspace)
    _ensure_directory(pi_root)
    _copy_tree(source.pi_sessions, pi_root / "sessions")

    for relative, content in sorted(
        plan.contents.items(),
        key=lambda item: item[0].as_posix(),
    ):
        path = snapshot.joinpath(*relative.parts)
        _ensure_directory(path.parent)
        _write_file(path, content)
    _write_file(snapshot / "lock.json", plan.manifest_bytes)
    _write_file(staging / "run.json", plan.receipt_bytes)
    _fsync_path(snapshot)
    _fsync_path(workspace)
    _fsync_path(pi_root)
    _fsync_path(staging)


def _run_tree_identities(run: SessionRun) -> tuple[str, str, str, str]:
    return (
        _tree_sha256(run.workspace),
        _tree_sha256(run.pi_sessions),
        _tree_sha256(run.report_directory),
        _tree_sha256(run.memory_directory),
    )


def _attest_migrated_run(
    plan: _RunMigrationPlan,
    target: SessionRun,
    *,
    target_root: pathlib.Path,
    target_profile_id: str,
) -> MigratedRun:
    source = plan.source
    if (
        target.session_id != source.session_id
        or target.created_at != source.created_at
        or target.lock_sha256 != plan.target_lock_sha256
        or target.profile.as_dict() != plan.contract.as_dict()
        or target.service_binding != plan.service_binding
        or target.pi_installation != plan.pi_identity
    ):
        _fail(
            f"migrated run envelope conflicts with the exact migration plan: "
            f"{source.session_id}",
            code="published_migration_requires_recovery",
        )
    source_identities = _run_tree_identities(source)
    target_identities = _run_tree_identities(target)
    if source_identities != target_identities:
        _fail(
            f"migrated mutable history differs from source: {source.session_id}",
            code="published_migration_requires_recovery",
        )
    source_recovered = (
        source.profile.state_root
        / "recovered"
        / source.profile.profile_id
        / source.session_id
    )
    target_recovered = target_root / "recovered" / target_profile_id / target.session_id
    source_has_recovered = (
        _entry_metadata(
            source_recovered,
            label="legacy recovered history",
            code="unsafe_legacy_migration_source",
        )
        is not None
    )
    target_has_recovered = (
        _entry_metadata(
            target_recovered,
            label="target recovered history",
        )
        is not None
    )
    if source_has_recovered != target_has_recovered:
        _fail(
            f"migrated recovered history presence differs from source: "
            f"{source.session_id}",
            code="published_migration_requires_recovery",
        )
    recovered_sha256: str | None = None
    if source_has_recovered:
        source_recovered_sha256 = _tree_sha256(source_recovered)
        target_recovered_sha256 = _tree_sha256(target_recovered)
        if source_recovered_sha256 != target_recovered_sha256:
            _fail(
                f"migrated recovered history differs from source: {source.session_id}",
                code="published_migration_requires_recovery",
            )
        recovered_sha256 = target_recovered_sha256
    return MigratedRun(
        session_id=source.session_id,
        source_lock_sha256=source.lock_sha256,
        target_lock_sha256=target.lock_sha256,
        workspace_sha256=target_identities[0],
        pi_sessions_sha256=target_identities[1],
        report_sha256=target_identities[2],
        memory_sha256=target_identities[3],
        recovered_sha256=recovered_sha256,
    )


def _publish_receipt(
    root: pathlib.Path,
    *,
    migration_id: str,
    source_profile: Profile,
    profile_id: str,
    project_id: str,
    binding: ServiceEndpointBinding,
    migrated_runs: tuple[MigratedRun, ...],
) -> pathlib.Path:
    evidence = root / "evidence" / "migrations"
    _ensure_directory(evidence)
    path = evidence / f"{migration_id}.json"
    value = {
        "schema": MIGRATION_SCHEMA,
        "migration_id": migration_id,
        "source": {
            "profile_id": source_profile.contract.profile_id,
            "project_id": source_profile.contract.project_id,
            "profile_sha256": hashlib.sha256(source_profile.document).hexdigest(),
        },
        "target": {
            "profile_id": profile_id,
            "project_id": project_id,
            "service_id": binding.service_id,
            "workload_sha256": binding.workload_sha256,
        },
        "runs": [run.normalized() for run in migrated_runs],
    }
    payload = canonical_json_bytes(value)
    metadata = _entry_metadata(path, label="migration receipt")
    if metadata is not None:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            _fail(
                f"migration receipt has an unsafe identity: {path}",
                code="published_migration_requires_recovery",
            )
        parent = _open_directory_no_links(
            path.parent,
            label="migration receipt parent",
            code="published_migration_requires_recovery",
        )
        if parent is None:
            raise AssertionError("migration receipt parent is absent")
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
            opened = os.fstat(descriptor)
            if _stable_fields(opened) != _stable_fields(metadata):
                _fail(
                    f"migration receipt changed while opening it: {path}",
                    code="published_migration_requires_recovery",
                )
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            current = b"".join(chunks)
        except OSError as error:
            raise ModelLabError(
                f"cannot read migration receipt {path}: {error}",
                code="published_migration_requires_recovery",
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent)
        if current != payload:
            _fail(
                f"migration receipt conflicts with published output: {path}",
                code="published_migration_requires_recovery",
            )
        return path
    staging = evidence / (f".{migration_id}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    _write_file(staging, payload)
    _publish_directory(staging, path)
    return path


def _load_and_attest_profile(
    target_root: pathlib.Path,
    *,
    profile_id: str,
    expected_document: bytes,
    expected_resources: dict[pathlib.PurePosixPath, tuple[tuple[str, ...], bytes]],
    binding: ServiceEndpointBinding,
) -> Profile:
    target_profile_root = target_root / "profiles" / profile_id
    metadata = _entry_metadata(
        target_profile_root,
        label="published profile",
    )
    if metadata is not None and not stat.S_ISDIR(metadata.st_mode):
        _fail(
            f"published profile has an unsafe identity: {target_profile_root}",
            code="legacy_migration_destination_conflict",
        )
    try:
        loaded_profile = load_profile(target_profile_root)
    except Exception as error:
        raise ModelLabError(
            f"published migrated profile failed production validation: {error}",
            code="published_migration_requires_recovery",
        ) from error
    if loaded_profile.document != expected_document:
        _fail(
            "published migrated profile conflicts with the migration plan",
            code="legacy_migration_destination_conflict",
        )
    actual_resources = _profile_resource_contents(loaded_profile)
    if actual_resources != expected_resources:
        _fail(
            "published migrated profile resources differ from source",
            code="legacy_migration_destination_conflict",
        )
    profile_binding = ProfileBindingStore(target_root).load(profile_id)
    expected_binding = ProfileBinding(
        profile_id=profile_id,
        service_id=binding.service_id,
        workload_sha256=binding.workload_sha256,
    )
    if profile_binding != expected_binding:
        _fail(
            "published profile has an inconsistent permanent service binding",
            code="published_migration_requires_recovery",
        )
    return loaded_profile


def _attest_published_runs(
    target_root: pathlib.Path,
    *,
    profile_id: str,
    plans: Sequence[_RunMigrationPlan],
    allow_additional_runs: bool = False,
) -> tuple[MigratedRun, ...]:
    target_session_ids = list_run_ids_from_state(target_root, profile_id)
    expected_session_ids = tuple(
        sorted(
            (plan.source.session_id for plan in plans),
            reverse=True,
        )
    )
    session_set_matches = (
        set(expected_session_ids).issubset(target_session_ids)
        if allow_additional_runs
        else target_session_ids == expected_session_ids
    )
    if not session_set_matches:
        _fail(
            "target profile session set conflicts with the selected source history",
            code="legacy_migration_destination_conflict",
        )
    migrated_runs: list[MigratedRun] = []
    for plan in plans:
        try:
            target_run = load_run_from_state(
                target_root,
                profile_id,
                plan.source.session_id,
            )
        except Exception as error:
            raise ModelLabError(
                "published migrated run failed production validation "
                f"{plan.source.session_id}: {error}",
                code="published_migration_requires_recovery",
            ) from error
        migrated_runs.append(
            _attest_migrated_run(
                plan,
                target_run,
                target_root=target_root,
                target_profile_id=profile_id,
            )
        )
    return tuple(migrated_runs)


def _migration_identity(
    source_profile: Profile,
    runs: Sequence[SessionRun],
    *,
    profile_id: str,
    project_id: str,
    binding: ServiceEndpointBinding,
) -> str:
    return canonical_sha256(
        {
            "schema": MIGRATION_SCHEMA,
            "source_profile_id": source_profile.contract.profile_id,
            "source_project_id": source_profile.contract.project_id,
            "source_profile_sha256": hashlib.sha256(
                source_profile.document
            ).hexdigest(),
            "source_runs": [
                {
                    "session_id": run.session_id,
                    "lock_sha256": run.lock_sha256,
                }
                for run in runs
            ],
            "target_profile_id": profile_id,
            "target_project_id": project_id,
            "service": binding.as_dict(),
        }
    )


def _load_sources(
    profile: Profile,
    session_ids: Sequence[str] | None,
) -> tuple[SessionRun, ...]:
    available = list_run_ids_from_state(
        profile.contract.state_root,
        profile.contract.profile_id,
    )
    if session_ids is None:
        selected = available
    else:
        if not isinstance(session_ids, Sequence) or isinstance(
            session_ids, (str, bytes)
        ):
            _fail(
                "session_ids must be a sequence of exact session IDs",
                code="invalid_legacy_migration_request",
            )
        selected = tuple(session_ids)
        if any(not isinstance(session_id, str) for session_id in selected):
            _fail(
                "session_ids entries must be exact session ID strings",
                code="invalid_legacy_migration_request",
            )
        if len(selected) != len(set(selected)):
            _fail(
                "session_ids contains duplicates",
                code="invalid_legacy_migration_request",
            )
        unknown = set(selected) - set(available)
        if unknown:
            _fail(
                "session_ids contains sessions outside the source profile: "
                + ", ".join(sorted(unknown)),
                code="invalid_legacy_migration_request",
            )
    return tuple(
        load_run_from_state(
            profile.contract.state_root,
            profile.contract.profile_id,
            session_id,
        )
        for session_id in sorted(selected)
    )


def _acquire_source_runs(
    runs: Sequence[SessionRun],
    authorities: contextlib.ExitStack,
) -> tuple[SessionRun, ...]:
    acquired: list[SessionRun] = []
    for run in runs:
        try:
            inspection: RunInspection = authorities.enter_context(
                inspect_run_from_state(
                    run.profile.state_root,
                    run.profile.profile_id,
                    run.session_id,
                )
            )
            if not inspection.try_lock():
                _fail(
                    f"legacy run is active: {run.session_id}",
                    code="legacy_migration_source_in_use",
                )
        except Exception as error:
            if (
                isinstance(error, ModelLabError)
                and error.code == "legacy_migration_source_in_use"
            ):
                raise
            raise ModelLabError(
                f"legacy run is active or changed during migration admission "
                f"{run.session_id}: {error}",
                code="legacy_migration_source_in_use",
            ) from error
        if (
            inspection.run.created_at != run.created_at
            or inspection.run.lock_sha256 != run.lock_sha256
            or inspection.run.profile.as_dict() != run.profile.as_dict()
        ):
            _fail(
                f"legacy run changed during migration admission: {run.session_id}",
                code="legacy_migration_source_changed",
            )
        acquired.append(inspection.run)
    return tuple(acquired)


def _migrate_legacy_profile_quiesced(
    source_profile_root: os.PathLike[str] | str,
    destination_root: os.PathLike[str] | str,
    *,
    service_binding: ServiceEndpointBinding,
    target_profile_id: str | None = None,
    target_project_id: str | None = None,
    session_ids: Sequence[str] | None = None,
    v1_policy: MigrationPolicy | None = None,
    authorities: contextlib.ExitStack,
    locked_source_state_root: pathlib.Path,
) -> MigrationResult:
    source_root = _absolute_normalized_path(
        source_profile_root,
        label="source_profile_root",
    )
    target_root = _absolute_normalized_path(
        destination_root,
        label="destination_root",
    )
    _validate_destination_root(target_root)
    binding = _validated_binding(service_binding)
    try:
        source_profile = load_legacy_profile_for_migration(source_root)
    except Exception as error:
        raise ModelLabError(
            f"cannot load legacy source profile {source_root}: {error}",
            code=getattr(error, "code", "invalid_legacy_migration_source"),
        ) from error
    if source_profile.contract.state_root != locked_source_state_root:
        _fail(
            "legacy profile state_root changed while acquiring migration authority",
            code="legacy_migration_source_changed",
        )
    profile_id = _identifier(
        (
            source_profile.contract.profile_id
            if target_profile_id is None
            else target_profile_id
        ),
        label="target_profile_id",
    )
    project_id = _identifier(
        (
            source_profile.contract.project_id
            if target_project_id is None
            else target_project_id
        ),
        label="target_project_id",
    )
    _identifier(binding.service_id, label="service_binding.service_id")
    active_modalities = _attest_legacy_workload(
        source_profile.contract,
        binding,
        label="active legacy profile",
    )
    active_policy = _policy_for(source_profile.contract, v1_policy)
    source_runs = _acquire_source_runs(
        _load_sources(source_profile, session_ids),
        authorities,
    )
    for run in source_runs:
        _attest_legacy_workload(
            run.profile,
            binding,
            label=f"legacy run {run.session_id}",
        )
        _policy_for(run.profile, v1_policy)
    protected_sources = {
        "source profile": source_profile.contract.profile_root,
        "source state": source_profile.contract.state_root,
        "source project": source_profile.contract.project_root,
        "source Pi runtime": source_profile.contract.pi.installation_root,
    }
    for run in source_runs:
        protected_sources[f"source run {run.session_id} Pi runtime"] = (
            run.profile.pi.installation_root
        )
    for label, path in protected_sources.items():
        if _paths_overlap(target_root, path):
            _fail(
                f"destination_root overlaps {label}: {path}",
                code="invalid_legacy_migration_request",
            )

    migration_id = _migration_identity(
        source_profile,
        source_runs,
        profile_id=profile_id,
        project_id=project_id,
        binding=binding,
    )
    active_document = _render_profile_v3(
        source_profile.contract,
        profile_id=profile_id,
        project_id=project_id,
        service_id=binding.service_id,
        input_modalities=active_modalities,
        policy=active_policy,
    )
    expected_active_resources = _profile_resource_contents(source_profile)

    _ensure_directory(target_root)
    _validate_destination_root(target_root)
    with _migration_lock(target_root):
        authorities.enter_context(_state_materialization_read_lock(target_root))
        _ensure_model_session_lock(target_root)
        target_profile_root = target_root / "profiles" / profile_id
        attempt = target_root / ".migrations" / f"{migration_id}-{secrets.token_hex(8)}"

        source_contracts = [source_profile.contract]
        source_contracts.extend(run.profile for run in source_runs)
        project_root = target_root / "projects" / project_id
        reports_root = project_root / "reports"
        memory_root = project_root / "memory"
        _ensure_directory(reports_root)
        _ensure_directory(memory_root)

        source_pi_by_version: dict[
            str,
            tuple[
                pathlib.Path,
                PiInstallationIdentity,
                ProfileContract,
            ],
        ] = {}
        source_pi_identity_by_root: dict[
            tuple[pathlib.Path, pathlib.PurePosixPath],
            PiInstallationIdentity,
        ] = {}
        for contract in source_contracts:
            source_pi_key = (
                contract.pi.installation_root,
                contract.pi.executable,
            )
            identity = source_pi_identity_by_root.get(source_pi_key)
            if identity is None:
                identity = fingerprint_pi_installation(contract)
                source_pi_identity_by_root[source_pi_key] = identity
            current = source_pi_by_version.get(contract.pi.version)
            if current is not None and current[1] != identity:
                _fail(
                    "legacy sessions use different Pi installation identities "
                    f"for version {contract.pi.version}; they cannot share the "
                    "canonical target runtime path",
                    code="legacy_pi_runtime_collision",
                )
            source_pi_by_version[contract.pi.version] = (
                contract.pi.installation_root,
                identity,
                contract,
            )

        target_pi_identity_by_version: dict[str, PiInstallationIdentity] = {}
        for version, (source_pi_root, identity, source_contract) in sorted(
            source_pi_by_version.items()
        ):
            target_pi_root = target_root / "runtimes" / "pi" / version
            staged_pi_root = attempt / "runtimes" / "pi" / version
            copied = (
                _entry_metadata(
                    target_pi_root,
                    label=f"target Pi runtime {version}",
                )
                is None
            )
            if copied:
                _ensure_directory(staged_pi_root.parent)
                _copy_tree(source_pi_root, staged_pi_root)
                _publish_directory(staged_pi_root, target_pi_root)
            target_document = _render_profile_v3(
                source_contract,
                profile_id=profile_id,
                project_id=project_id,
                service_id=binding.service_id,
                input_modalities=_attest_legacy_workload(
                    source_contract,
                    binding,
                    label=f"Pi runtime {version}",
                ),
                policy=_policy_for(source_contract, v1_policy),
            )
            target_contract = parse_locked_profile(
                target_document,
                source_profile_root=str(target_root / "profiles" / profile_id),
            )
            target_identity = fingerprint_pi_installation(target_contract)
            if target_identity != identity:
                _fail(
                    f"target Pi runtime differs at {target_pi_root}",
                    code=(
                        "published_migration_requires_recovery"
                        if copied
                        else "legacy_pi_runtime_collision"
                    ),
                )
            target_pi_identity_by_version[version] = target_identity

        active_contract = parse_locked_profile(
            active_document,
            source_profile_root=str(target_root / "profiles" / profile_id),
        )

        run_plans: list[_RunMigrationPlan] = []
        for source_run in source_runs:
            run_modalities = _attest_legacy_workload(
                source_run.profile,
                binding,
                label=f"legacy run {source_run.session_id}",
            )
            run_binding = dataclasses.replace(
                binding,
                input_modalities=run_modalities,
            )
            run_document = _render_profile_v3(
                source_run.profile,
                profile_id=profile_id,
                project_id=project_id,
                service_id=binding.service_id,
                input_modalities=run_modalities,
                policy=_policy_for(source_run.profile, v1_policy),
            )
            run_contract = parse_locked_profile(
                run_document,
                source_profile_root=str(target_root / "profiles" / profile_id),
            )
            pi_identity = target_pi_identity_by_version[run_contract.pi.version]
            run_plans.append(
                _plan_run(
                    source_run,
                    profile_id=profile_id,
                    project_id=project_id,
                    project_root=project_root,
                    profile_document=run_document,
                    target_contract=run_contract,
                    service_binding=run_binding,
                    pi_identity=pi_identity,
                )
            )

        target_profile_metadata = _entry_metadata(
            target_profile_root,
            label="target profile",
        )
        if target_profile_metadata is not None:
            _load_and_attest_profile(
                target_root,
                profile_id=profile_id,
                expected_document=active_document,
                expected_resources=expected_active_resources,
                binding=binding,
            )
            migrated = _attest_published_runs(
                target_root,
                profile_id=profile_id,
                plans=run_plans,
                allow_additional_runs=True,
            )
            receipt_path = _publish_receipt(
                target_root,
                migration_id=migration_id,
                source_profile=source_profile,
                profile_id=profile_id,
                project_id=project_id,
                binding=binding,
                migrated_runs=migrated,
            )
            return MigrationResult(
                migration_id=migration_id,
                profile_root=target_profile_root,
                state_root=target_root,
                profile_id=profile_id,
                project_id=project_id,
                service_id=binding.service_id,
                workload_sha256=binding.workload_sha256,
                runs=migrated,
                receipt_path=receipt_path,
            )

        for plan in run_plans:
            source_run = plan.source
            for source_path, destination_path, staging_path in (
                (
                    source_run.report_directory,
                    reports_root / source_run.session_id,
                    attempt / "reports" / source_run.session_id,
                ),
                (
                    source_run.memory_directory,
                    memory_root / source_run.session_id,
                    attempt / "memory" / source_run.session_id,
                ),
            ):
                _ensure_directory(staging_path.parent)
                _copy_or_attest_tree(
                    source_path,
                    destination_path,
                    staging_path,
                )
            source_recovered = (
                source_run.profile.state_root
                / "recovered"
                / source_run.profile.profile_id
                / source_run.session_id
            )
            target_recovered = (
                target_root / "recovered" / profile_id / source_run.session_id
            )
            source_recovered_metadata = _entry_metadata(
                source_recovered,
                label="legacy recovered history",
                code="unsafe_legacy_migration_source",
            )
            target_recovered_metadata = _entry_metadata(
                target_recovered,
                label="target recovered history",
            )
            if source_recovered_metadata is not None:
                if not stat.S_ISDIR(source_recovered_metadata.st_mode):
                    _fail(
                        "legacy recovered history is not a directory: "
                        f"{source_recovered}",
                        code="unsafe_legacy_migration_source",
                    )
                staged_recovered = attempt / "recovered" / source_run.session_id
                _ensure_directory(staged_recovered.parent)
                _copy_or_attest_tree(
                    source_recovered,
                    target_recovered,
                    staged_recovered,
                )
            elif target_recovered_metadata is not None:
                _fail(
                    "target contains recovered history absent from the "
                    f"selected source run: {source_run.session_id}",
                    code="legacy_migration_destination_conflict",
                )

        target_profile_sessions = target_root / "sessions" / profile_id
        target_sessions_metadata = _entry_metadata(
            target_profile_sessions,
            label="target profile session state",
        )
        if target_sessions_metadata is not None:
            if not stat.S_ISDIR(target_sessions_metadata.st_mode):
                _fail(
                    "target profile session state has an unsafe identity: "
                    f"{target_profile_sessions}",
                    code="legacy_migration_destination_conflict",
                )
        else:
            staged_profile_sessions = attempt / "sessions" / profile_id
            _ensure_directory(staged_profile_sessions)
            for plan in run_plans:
                run_staging = staged_profile_sessions / plan.source.session_id
                _build_run_staging(
                    run_staging,
                    plan,
                )
            _publish_directory(
                staged_profile_sessions,
                target_profile_sessions,
            )

        migrated = _attest_published_runs(
            target_root,
            profile_id=profile_id,
            plans=run_plans,
        )

        if (
            _entry_metadata(
                target_profile_root,
                label="target profile",
            )
            is None
        ):
            staged_profile = attempt / "profile"
            _ensure_directory(staged_profile)
            _write_profile_resources(
                staged_profile,
                active_document,
                expected_active_resources,
            )
            profile_binding = ProfileBinding(
                profile_id=profile_id,
                service_id=binding.service_id,
                workload_sha256=binding.workload_sha256,
            )
            _write_file(
                staged_profile / PROFILE_BINDING_FILE_NAME,
                canonical_json_bytes(profile_binding.normalized()),
            )
            _publish_directory(staged_profile, target_profile_root)

        loaded_profile = _load_and_attest_profile(
            target_root,
            profile_id=profile_id,
            expected_document=active_document,
            expected_resources=expected_active_resources,
            binding=binding,
        )
        if loaded_profile.contract.as_dict() != active_contract.as_dict():
            _fail(
                "published migrated profile conflicts with the migration plan",
                code="legacy_migration_destination_conflict",
            )

        receipt_path = _publish_receipt(
            target_root,
            migration_id=migration_id,
            source_profile=source_profile,
            profile_id=profile_id,
            project_id=project_id,
            binding=binding,
            migrated_runs=migrated,
        )
        _write_file(
            attempt / "completed.json",
            canonical_json_bytes(
                {
                    "schema": MIGRATION_ATTEMPT_SCHEMA,
                    "migration_id": migration_id,
                    "receipt": str(receipt_path.relative_to(target_root)),
                }
            ),
        )
        return MigrationResult(
            migration_id=migration_id,
            profile_root=target_profile_root,
            state_root=target_root,
            profile_id=profile_id,
            project_id=project_id,
            service_id=binding.service_id,
            workload_sha256=binding.workload_sha256,
            runs=migrated,
            receipt_path=receipt_path,
        )


def migrate_legacy_profile(
    source_profile_root: os.PathLike[str] | str,
    destination_root: os.PathLike[str] | str,
    *,
    service_binding: ServiceEndpointBinding,
    target_profile_id: str | None = None,
    target_project_id: str | None = None,
    session_ids: Sequence[str] | None = None,
    v1_policy: MigrationPolicy | None = None,
) -> MigrationResult:
    """Migrate one quiesced legacy profile without contacting a provider.

    ``service_binding`` is deliberately explicit: migration never guesses a
    runtime compatibility identity, service hash, or supported capability.
    When ``session_ids`` is omitted the source state materialization lock makes
    the enumerated set complete for the whole transaction. Every selected run
    is held under its production exclusive lease while mutable bytes are copied.
    """

    source_root = _absolute_normalized_path(
        source_profile_root,
        label="source_profile_root",
    )
    try:
        source_profile = load_legacy_profile_for_migration(source_root)
    except Exception as error:
        raise ModelLabError(
            f"cannot load legacy source profile {source_root}: {error}",
            code=getattr(error, "code", "invalid_legacy_migration_source"),
        ) from error
    binding = _validated_binding(service_binding)
    _attest_legacy_workload(
        source_profile.contract,
        binding,
        label="active legacy profile",
    )
    _policy_for(source_profile.contract, v1_policy)
    with contextlib.ExitStack() as authorities:
        authorities.enter_context(
            _state_materialization_read_lock(source_profile.contract.state_root)
        )
        return _migrate_legacy_profile_quiesced(
            source_profile_root,
            destination_root,
            service_binding=service_binding,
            target_profile_id=target_profile_id,
            target_project_id=target_project_id,
            session_ids=session_ids,
            v1_policy=v1_policy,
            authorities=authorities,
            locked_source_state_root=source_profile.contract.state_root,
        )
