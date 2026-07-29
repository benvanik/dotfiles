"""Crash-safe publication of new isolated model-session runs."""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import pathlib
import secrets
import stat
from collections.abc import Iterator
from typing import Any

from .attachment import (
    ServiceEndpointBinding,
)
from .errors import ModelSessionError
from .pi_runtime import (
    PiInstallationIdentity,
    PiRuntimeAsset,
    fingerprint_pi_installation,
    pi_runtime_assets,
)
from .profile import (
    AGENTS_FILE_NAME,
    PROFILE_FILE_NAME,
    PROFILE_SCHEMA_V3,
    Profile,
)
from .runs import (
    LOCK_SCHEMA,
    LOCK_SCHEMA_V1,
    RUN_SCHEMA,
    STAGING_NAME_PATTERN,
    SessionRun,
    _child_name,
    _fail,
    _open_absolute_directory,
    _open_materialization_lock_file,
    _open_optional_private_child_directory,
    _open_private_child_directory,
    _sha256,
    _validate_private_directory_descriptor,
    _validate_project_directory_descriptor,
    load_run,
)
from .service_endpoint import load_service_endpoint


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


@contextlib.contextmanager
def _materialization_lock(
    state_descriptor: int,
    state_root: pathlib.Path,
) -> Iterator[None]:
    lock_directory_path = state_root / "locks"
    lock_path = lock_directory_path / "materialize.lock"
    lock_directory_descriptor: int | None = None
    marker_descriptor: int | None = None
    state_locked = False
    try:
        fcntl.flock(state_descriptor, fcntl.LOCK_EX)
        state_locked = True
        lock_directory_descriptor = _ensure_private_child_directory(
            state_descriptor,
            "locks",
            path=lock_directory_path,
            label="session lock directory",
        )
        marker_descriptor = _open_materialization_lock_file(
            lock_directory_descriptor,
            lock_path,
            create=True,
        )
        if marker_descriptor is None:
            _fail(
                f"cannot create session materialization lock {lock_path}",
                code="session_lock_failed",
            )
        try:
            os.fsync(marker_descriptor)
            os.fsync(lock_directory_descriptor)
        except OSError as error:
            raise ModelSessionError(
                "cannot make the session materialization lock durable "
                f"{lock_path}: {error}",
                code="session_lock_failed",
            ) from error
        yield
    finally:
        if marker_descriptor is not None:
            os.close(marker_descriptor)
        if lock_directory_descriptor is not None:
            os.close(lock_directory_descriptor)
        if state_locked:
            fcntl.flock(state_descriptor, fcntl.LOCK_UN)


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
    service_binding: ServiceEndpointBinding | None,
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
            "schema": (LOCK_SCHEMA if service_binding is not None else LOCK_SCHEMA_V1),
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
        if service_binding is not None:
            manifest["service"] = service_binding.as_dict()
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


def _materialize_run(
    profile: Profile,
    *,
    endpoint_runtime_root: os.PathLike[str] | str | None = None,
    expected_workload_sha256: str | None = None,
) -> SessionRun:
    service_binding: ServiceEndpointBinding | None = None
    if profile.contract.schema == PROFILE_SCHEMA_V3:
        endpoint = load_service_endpoint(
            profile,
            runtime_root=endpoint_runtime_root,
        )
        if endpoint.binding.service_id != profile.contract.service_id:
            _fail(
                "service endpoint does not match the active profile",
                code="service_endpoint_mismatch",
            )
        if (
            expected_workload_sha256 is not None
            and endpoint.binding.workload_sha256 != expected_workload_sha256
        ):
            _fail(
                "service endpoint workload does not match the model-lab use authority",
                code="model_lab_use_authority_workload_mismatch",
            )
        if profile.contract.endpoint is None:
            raise AssertionError("profile v3 endpoint requirement is absent")
        service_binding = ServiceEndpointBinding(
            service_id=endpoint.binding.service_id,
            service_sha256=endpoint.binding.service_sha256,
            workload=endpoint.binding.workload,
            workload_sha256=endpoint.binding.workload_sha256,
            input_modalities=(profile.contract.endpoint.required_input_modalities),
        )
    elif endpoint_runtime_root is not None:
        _fail(
            "legacy profile materialization cannot consume a service-scoped endpoint",
            code="invalid_service_endpoint_binding",
        )
    runtime_assets = pi_runtime_assets(
        profile.contract,
        service_binding,
    )
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
                        service_binding,
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


def materialize_new_run(
    profile: Profile,
    *,
    endpoint_runtime_root: os.PathLike[str] | str | None = None,
    expected_workload_sha256: str | None = None,
) -> SessionRun:
    """Create one profile-v3 run; no caller-controlled ID or legacy fallback."""

    if profile.contract.schema != PROFILE_SCHEMA_V3:
        _fail(
            "new sessions require an active profile v3; legacy profiles are "
            "accepted only by the explicit migration fixture path",
            code="legacy_profile_requires_migration",
        )
    return _materialize_run(
        profile,
        endpoint_runtime_root=endpoint_runtime_root,
        expected_workload_sha256=expected_workload_sha256,
    )


def materialize_legacy_run_for_migration(profile: Profile) -> SessionRun:
    """Create a v1/v2 run only as an input to an explicit migration test."""

    if profile.contract.schema == PROFILE_SCHEMA_V3:
        _fail(
            "the legacy migration fixture path does not accept profile v3",
            code="invalid_legacy_migration_input",
        )
    return _materialize_run(profile)
