"""Crash-safe publication of new isolated model-session runs."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import fcntl
import json
import os
import pathlib
import secrets
import stat
import time
from collections.abc import Callable, Iterator
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
    _read_private_file_at,
    _sha256,
    _validate_private_directory_descriptor,
    _validate_project_directory_descriptor,
    load_run,
)
from .service_endpoint import load_service_endpoint


MATERIALIZATION_CLEANUP_GRACE_SECONDS = 5.0
PROJECT_LEAF_OWNERSHIP_SCHEMA = "model-session.project-leaf-ownership.v1"
_PROJECT_LEAF_OWNERSHIP_FILES = {
    "memory": ".project-memory-ownership.json",
    "reports": ".project-reports-ownership.json",
}


@dataclasses.dataclass(frozen=True)
class _ProjectLeafOwnership:
    root_name: str
    staging_device: int
    staging_inode: int
    leaf_device: int
    leaf_inode: int
    leaf_ctime_ns: int


def _require_startup_budget(
    startup_deadline: float | None,
    monotonic: Callable[[], float],
) -> float | None:
    if startup_deadline is None:
        return None
    remaining = startup_deadline - monotonic()
    if remaining <= 0:
        _fail(
            "session materialization exceeded the service startup deadline",
            code="service_startup_timeout",
        )
    return remaining


def _require_cleanup_budget(
    cleanup_deadline: float,
    monotonic: Callable[[], float],
) -> float:
    remaining = cleanup_deadline - monotonic()
    if remaining <= 0:
        _fail(
            "session materialization cleanup exceeded its independent "
            f"{MATERIALIZATION_CLEANUP_GRACE_SECONDS:g}-second grace",
            code="session_materialization_cleanup_required",
        )
    return remaining


def _ensure_private_child_directory(
    parent_descriptor: int,
    name: str,
    *,
    path: pathlib.Path,
    label: str,
    startup_deadline: float | None,
    monotonic: Callable[[], float],
) -> int:
    _require_startup_budget(startup_deadline, monotonic)
    name = _child_name(name, label=label)
    try:
        descriptor = _open_private_child_directory(
            parent_descriptor,
            name,
            path=path,
            label=label,
        )
    except ModelSessionError as error:
        if not isinstance(error.__cause__, FileNotFoundError):
            raise
    else:
        try:
            _require_startup_budget(startup_deadline, monotonic)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor
    _require_startup_budget(startup_deadline, monotonic)
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
    _require_startup_budget(startup_deadline, monotonic)
    descriptor = _open_private_child_directory(
        parent_descriptor,
        name,
        path=path,
        label=label,
    )
    try:
        _require_startup_budget(startup_deadline, monotonic)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _create_private_child_directory(
    parent_descriptor: int,
    name: str,
    *,
    path: pathlib.Path,
    label: str,
    startup_deadline: float | None,
    monotonic: Callable[[], float],
    cleanup_deadline: Callable[[], float],
) -> int:
    _require_startup_budget(startup_deadline, monotonic)
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
    descriptor: int | None = None
    try:
        # Once mkdir succeeds, opening the child and syncing its parent are one
        # ownership unit. Until this function returns, rollback remains here.
        descriptor = _open_private_child_directory(
            parent_descriptor,
            name,
            path=path,
            label=label,
        )
        os.fsync(parent_descriptor)
        # Return the owned descriptor before consulting the startup deadline
        # again. The caller records this now-durable generated directory.
        return descriptor
    except BaseException as error:
        if descriptor is not None:
            os.close(descriptor)
        deadline = cleanup_deadline()
        try:
            _require_cleanup_budget(deadline, monotonic)
            try:
                os.rmdir(name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
            os.fsync(parent_descriptor)
        except (ModelSessionError, OSError) as cleanup_error:
            raise ModelSessionError(
                "generated session directory could not be rolled back; "
                f"retain its staging recovery identity for {path}: "
                f"{cleanup_error}",
                code="session_materialization_cleanup_required",
            ) from error
        raise


def _entry_exists_at(
    parent_descriptor: int,
    name: str,
    *,
    startup_deadline: float | None,
    monotonic: Callable[[], float],
) -> bool:
    _require_startup_budget(startup_deadline, monotonic)
    name = _child_name(name, label="session state entry")
    try:
        os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        _require_startup_budget(startup_deadline, monotonic)
        return False
    except OSError as error:
        raise ModelSessionError(
            f"cannot inspect session state entry {name}: {error}",
            code="unsafe_session_state",
        ) from error
    _require_startup_budget(startup_deadline, monotonic)
    return True


def _write_exclusive_at(
    parent_descriptor: int,
    name: str,
    content: bytes,
    *,
    path: pathlib.Path,
    startup_deadline: float | None,
    monotonic: Callable[[], float],
) -> None:
    _require_startup_budget(startup_deadline, monotonic)
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
        _require_startup_budget(startup_deadline, monotonic)
        os.fchmod(descriptor, 0o600)
        _require_startup_budget(startup_deadline, monotonic)
        view = memoryview(content)
        while view:
            _require_startup_budget(startup_deadline, monotonic)
            written = os.write(descriptor, view)
            if written <= 0:
                _fail(
                    f"short write while creating session file {path}",
                    code="session_materialization_failed",
                )
            view = view[written:]
            _require_startup_budget(startup_deadline, monotonic)
        _require_startup_budget(startup_deadline, monotonic)
        os.fsync(descriptor)
        _require_startup_budget(startup_deadline, monotonic)
    finally:
        os.close(descriptor)


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _directory_identity(descriptor: int) -> tuple[int, int, int]:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        _fail(
            "project ownership identity does not name a directory",
            code="unsafe_session_state",
        )
    return metadata.st_dev, metadata.st_ino, metadata.st_ctime_ns


def _write_project_leaf_attestation(
    staging_descriptor: int,
    staging: pathlib.Path,
    leaf_descriptor: int,
    *,
    session_id: str,
    root_name: str,
    startup_deadline: float | None,
    monotonic: Callable[[], float],
) -> _ProjectLeafOwnership:
    try:
        receipt_name = _PROJECT_LEAF_OWNERSHIP_FILES[root_name]
    except KeyError as error:
        raise AssertionError(
            f"unsupported project ownership root {root_name!r}"
        ) from error
    staging_device, staging_inode, _ = _directory_identity(
        staging_descriptor
    )
    leaf_device, leaf_inode, leaf_ctime_ns = _directory_identity(
        leaf_descriptor
    )
    ownership = _ProjectLeafOwnership(
        root_name=root_name,
        staging_device=staging_device,
        staging_inode=staging_inode,
        leaf_device=leaf_device,
        leaf_inode=leaf_inode,
        leaf_ctime_ns=leaf_ctime_ns,
    )
    receipt = {
        "schema": PROJECT_LEAF_OWNERSHIP_SCHEMA,
        "session_id": session_id,
        "root_name": root_name,
        "staging": {
            "device": staging_device,
            "inode": staging_inode,
        },
        "leaf": {
            "device": leaf_device,
            "inode": leaf_inode,
            "ctime_ns": leaf_ctime_ns,
        },
    }
    _write_exclusive_at(
        staging_descriptor,
        receipt_name,
        _json_bytes(receipt),
        path=staging / receipt_name,
        startup_deadline=startup_deadline,
        monotonic=monotonic,
    )
    _require_startup_budget(startup_deadline, monotonic)
    os.fsync(staging_descriptor)
    _require_startup_budget(startup_deadline, monotonic)
    return ownership


def _ownership_integer(value: Any, *, label: str, error_code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(f"{label} is invalid", code=error_code)
    return value


def _load_project_leaf_attestation(
    staging_descriptor: int,
    staging: pathlib.Path,
    *,
    session_id: str,
    root_name: str,
    error_code: str,
) -> _ProjectLeafOwnership:
    receipt_name = _PROJECT_LEAF_OWNERSHIP_FILES[root_name]
    receipt_path = staging / receipt_name
    try:
        content = _read_private_file_at(
            staging_descriptor,
            receipt_name,
            path=receipt_path,
            label="project leaf ownership attestation",
            maximum_bytes=4096,
        )
        value = json.loads(content)
        canonical = _json_bytes(value)
    except (
        ModelSessionError,
        UnicodeDecodeError,
        UnicodeEncodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
    ) as error:
        raise ModelSessionError(
            f"project leaf ownership attestation is unreadable: {receipt_path}",
            code=error_code,
        ) from error
    if (
        not isinstance(value, dict)
        or set(value)
        != {"schema", "session_id", "root_name", "staging", "leaf"}
        or value.get("schema") != PROJECT_LEAF_OWNERSHIP_SCHEMA
        or value.get("session_id") != session_id
        or value.get("root_name") != root_name
        or canonical != content
    ):
        _fail(
            f"project leaf ownership attestation is invalid: {receipt_path}",
            code=error_code,
        )
    staging_value = value["staging"]
    leaf_value = value["leaf"]
    if (
        not isinstance(staging_value, dict)
        or set(staging_value) != {"device", "inode"}
        or not isinstance(leaf_value, dict)
        or set(leaf_value) != {"device", "inode", "ctime_ns"}
    ):
        _fail(
            f"project leaf ownership attestation is invalid: {receipt_path}",
            code=error_code,
        )
    ownership = _ProjectLeafOwnership(
        root_name=root_name,
        staging_device=_ownership_integer(
            staging_value["device"],
            label="ownership staging device",
            error_code=error_code,
        ),
        staging_inode=_ownership_integer(
            staging_value["inode"],
            label="ownership staging inode",
            error_code=error_code,
        ),
        leaf_device=_ownership_integer(
            leaf_value["device"],
            label="ownership leaf device",
            error_code=error_code,
        ),
        leaf_inode=_ownership_integer(
            leaf_value["inode"],
            label="ownership leaf inode",
            error_code=error_code,
        ),
        leaf_ctime_ns=_ownership_integer(
            leaf_value["ctime_ns"],
            label="ownership leaf ctime",
            error_code=error_code,
        ),
    )
    if (
        ownership.staging_device,
        ownership.staging_inode,
    ) != _directory_identity(staging_descriptor)[:2]:
        _fail(
            "project leaf ownership attestation belongs to another staging "
            f"transaction: {receipt_path}",
            code=error_code,
        )
    return ownership


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
    *,
    startup_deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> Iterator[None]:
    lock_directory_path = state_root / "locks"
    lock_path = lock_directory_path / "materialize.lock"
    lock_directory_descriptor: int | None = None
    marker_descriptor: int | None = None
    state_locked = False
    try:
        if startup_deadline is None:
            fcntl.flock(state_descriptor, fcntl.LOCK_EX)
            state_locked = True
        else:
            while True:
                remaining = _require_startup_budget(
                    startup_deadline,
                    monotonic,
                )
                try:
                    fcntl.flock(
                        state_descriptor,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                    state_locked = True
                    break
                except BlockingIOError:
                    time.sleep(min(0.05, remaining))
            _require_startup_budget(startup_deadline, monotonic)
        _require_startup_budget(startup_deadline, monotonic)
        lock_directory_descriptor = _ensure_private_child_directory(
            state_descriptor,
            "locks",
            path=lock_directory_path,
            label="session lock directory",
            startup_deadline=startup_deadline,
            monotonic=monotonic,
        )
        _require_startup_budget(startup_deadline, monotonic)
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
        _require_startup_budget(startup_deadline, monotonic)
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
    staging_descriptor: int,
    staging: pathlib.Path,
    session_id: str,
    *,
    startup_deadline: float | None,
    monotonic: Callable[[], float],
    cleanup_deadline: Callable[[], float],
) -> tuple[pathlib.Path, pathlib.Path, int, int]:
    _require_startup_budget(startup_deadline, monotonic)
    reports_root = project_root / "reports"
    reports_descriptor = _ensure_private_child_directory(
        project_descriptor,
        "reports",
        path=reports_root,
        label="project reports directory",
        startup_deadline=startup_deadline,
        monotonic=monotonic,
    )
    memory_root = project_root / "memory"
    try:
        memory_descriptor = _ensure_private_child_directory(
            project_descriptor,
            "memory",
            path=memory_root,
            label="project memory directory",
            startup_deadline=startup_deadline,
            monotonic=monotonic,
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
            startup_deadline=startup_deadline,
            monotonic=monotonic,
            cleanup_deadline=cleanup_deadline,
        )
        report_created = True
        try:
            _write_project_leaf_attestation(
                staging_descriptor,
                staging,
                report_session_descriptor,
                session_id=session_id,
                root_name="reports",
                startup_deadline=startup_deadline,
                monotonic=monotonic,
            )
        finally:
            os.close(report_session_descriptor)
        memory_session_descriptor = _create_private_child_directory(
            memory_descriptor,
            session_id,
            path=memory_directory,
            label="session memory directory",
            startup_deadline=startup_deadline,
            monotonic=monotonic,
            cleanup_deadline=cleanup_deadline,
        )
        memory_created = True
        try:
            _write_project_leaf_attestation(
                staging_descriptor,
                staging,
                memory_session_descriptor,
                session_id=session_id,
                root_name="memory",
                startup_deadline=startup_deadline,
                monotonic=monotonic,
            )
        finally:
            os.close(memory_session_descriptor)
    except Exception as error:
        cleanup_failures = []
        deadline = cleanup_deadline()
        for parent_descriptor, path, created in (
            (memory_descriptor, memory_directory, memory_created),
            (reports_descriptor, report_directory, report_created),
        ):
            if not created:
                continue
            try:
                _require_cleanup_budget(deadline, monotonic)
                os.rmdir(session_id, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
                _require_cleanup_budget(deadline, monotonic)
            except (ModelSessionError, OSError) as cleanup_error:
                cleanup_failures.append(f"{path}: {cleanup_error}")
        if cleanup_failures:
            os.close(memory_descriptor)
            os.close(reports_descriptor)
            raise ModelSessionError(
                "generated project session directories remain after failed "
                "materialization and require recovery: "
                + "; ".join(cleanup_failures),
                code="session_materialization_cleanup_required",
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
    *,
    cleanup_deadline: float,
    monotonic: Callable[[], float],
) -> None:
    failures = []
    for parent_descriptor, path in (
        (memory_descriptor, memory_directory),
        (reports_descriptor, report_directory),
    ):
        if parent_descriptor is None or path is None:
            continue
        try:
            _require_cleanup_budget(cleanup_deadline, monotonic)
            os.rmdir(session_id, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
            _require_cleanup_budget(cleanup_deadline, monotonic)
        except FileNotFoundError:
            continue
        except (ModelSessionError, OSError) as error:
            failures.append(f"{path}: {error}")
    if failures:
        _fail(
            "generated project session directories remain after failed "
            "materialization and require recovery: "
            + "; ".join(failures),
            code="session_materialization_cleanup_required",
        )


def _rollback_unpublished_materialization(
    profile_sessions_descriptor: int,
    staging_name: str,
    staging: pathlib.Path,
    *,
    staging_created: bool,
    reports_descriptor: int | None,
    memory_descriptor: int | None,
    report_directory: pathlib.Path | None,
    memory_directory: pathlib.Path | None,
    session_id: str,
    cleanup_deadline: float,
    monotonic: Callable[[], float],
) -> None:
    # The staging name is the crash-recovery join key for its project
    # directories. Remove those leaves first so a bounded or failed tree
    # cleanup always retains enough state for the next recovery pass.
    _remove_empty_project_directories(
        reports_descriptor,
        memory_descriptor,
        report_directory,
        memory_directory,
        session_id,
        cleanup_deadline=cleanup_deadline,
        monotonic=monotonic,
    )
    if staging_created:
        _remove_private_tree_at(
            profile_sessions_descriptor,
            staging_name,
            path=staging,
            cleanup_deadline=cleanup_deadline,
            monotonic=monotonic,
        )


@dataclasses.dataclass(frozen=True)
class _RecoverableProjectLeaf:
    root_name: str
    path: pathlib.Path
    parent_descriptor: int
    leaf_descriptor: int
    ownership: _ProjectLeafOwnership


def _inspect_project_directory_for_recovery(
    project_descriptor: int,
    project_root: pathlib.Path,
    root_name: str,
    session_id: str,
    ownership: _ProjectLeafOwnership | None,
    descriptors: contextlib.ExitStack,
    *,
    startup_deadline: float | None,
    monotonic: Callable[[], float],
) -> _RecoverableProjectLeaf | None:
    _require_startup_budget(startup_deadline, monotonic)
    root_path = project_root / root_name
    root_descriptor = _open_optional_private_child_directory(
        project_descriptor,
        root_name,
        path=root_path,
        label=f"project {root_name} directory",
    )
    if root_descriptor is None:
        _require_startup_budget(startup_deadline, monotonic)
        return None
    descriptors.callback(os.close, root_descriptor)
    _require_startup_budget(startup_deadline, monotonic)
    session_path = root_path / session_id
    session_descriptor = _open_optional_private_child_directory(
        root_descriptor,
        session_id,
        path=session_path,
        label="incomplete project session directory",
    )
    if session_descriptor is None:
        _require_startup_budget(startup_deadline, monotonic)
        return None
    descriptors.callback(os.close, session_descriptor)
    if ownership is None:
        _fail(
            "incomplete project session directory has no durable ownership "
            f"attestation; refusing automatic recovery: {session_path}",
            code="session_recovery_required",
        )
    actual_identity = _directory_identity(session_descriptor)
    expected_identity = (
        ownership.leaf_device,
        ownership.leaf_inode,
        ownership.leaf_ctime_ns,
    )
    if actual_identity != expected_identity:
        _fail(
            "incomplete project session directory differs from its durable "
            f"ownership attestation: {session_path}",
            code="session_recovery_required",
        )
    _require_startup_budget(startup_deadline, monotonic)
    if os.listdir(session_descriptor):
        _fail(
            "incomplete project session directory is not empty; refusing "
            f"automatic recovery: {session_path}",
            code="session_recovery_required",
        )
    _require_startup_budget(startup_deadline, monotonic)
    return _RecoverableProjectLeaf(
        root_name=root_name,
        path=session_path,
        parent_descriptor=root_descriptor,
        leaf_descriptor=session_descriptor,
        ownership=ownership,
    )


def _remove_attested_project_directory(
    leaf: _RecoverableProjectLeaf,
    session_id: str,
    *,
    startup_deadline: float | None,
    monotonic: Callable[[], float],
) -> None:
    _require_startup_budget(startup_deadline, monotonic)
    try:
        metadata = os.stat(
            session_id,
            dir_fd=leaf.parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        raise ModelSessionError(
            "cannot revalidate incomplete project session directory "
            f"{leaf.path}: {error}",
            code="session_recovery_required",
        ) from error
    expected_identity = (
        leaf.ownership.leaf_device,
        leaf.ownership.leaf_inode,
        leaf.ownership.leaf_ctime_ns,
    )
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_ctime_ns,
        )
        != expected_identity
        or _directory_identity(leaf.leaf_descriptor) != expected_identity
    ):
        _fail(
            "incomplete project session directory changed during recovery: "
            f"{leaf.path}",
            code="session_recovery_required",
        )
    try:
        os.rmdir(session_id, dir_fd=leaf.parent_descriptor)
        os.fsync(leaf.parent_descriptor)
    except OSError as error:
        raise ModelSessionError(
            "cannot remove attested incomplete project session directory "
            f"{leaf.path}: {error}",
            code="session_recovery_required",
        ) from error
    _require_startup_budget(startup_deadline, monotonic)


def _remove_private_tree_at(
    parent_descriptor: int,
    name: str,
    *,
    path: pathlib.Path,
    cleanup_deadline: float,
    monotonic: Callable[[], float],
) -> None:
    _require_cleanup_budget(cleanup_deadline, monotonic)
    descriptor = _open_private_child_directory(
        parent_descriptor,
        name,
        path=path,
        label="session staging directory",
    )
    try:
        _require_cleanup_budget(cleanup_deadline, monotonic)
        for entry_name in os.listdir(descriptor):
            _require_cleanup_budget(cleanup_deadline, monotonic)
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
                    cleanup_deadline=cleanup_deadline,
                    monotonic=monotonic,
                )
                continue
            if not stat.S_ISREG(metadata.st_mode):
                _fail(
                    f"generated staging contains a non-regular entry: {entry_path}",
                    code="session_cleanup_failed",
                )
            try:
                _require_cleanup_budget(cleanup_deadline, monotonic)
                os.unlink(entry_name, dir_fd=descriptor)
                # A bounded cleanup may stop after this entry. Make each
                # removal durable before consulting its independent deadline.
                os.fsync(descriptor)
                _require_cleanup_budget(cleanup_deadline, monotonic)
            except OSError as error:
                raise ModelSessionError(
                    f"cannot remove generated staging file {entry_path}: {error}",
                    code="session_cleanup_failed",
                ) from error
    finally:
        os.close(descriptor)
    try:
        _require_cleanup_budget(cleanup_deadline, monotonic)
        os.rmdir(name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    except ModelSessionError:
        raise
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
    *,
    startup_deadline: float | None,
    monotonic: Callable[[], float],
) -> None:
    _require_startup_budget(startup_deadline, monotonic)
    for name in sorted(os.listdir(profile_sessions_descriptor)):
        _require_startup_budget(startup_deadline, monotonic)
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
            _require_startup_budget(startup_deadline, monotonic)
            entries = set(os.listdir(staging_descriptor))
            _require_startup_budget(startup_deadline, monotonic)
            ownership_files = set(_PROJECT_LEAF_OWNERSHIP_FILES.values())
            unexpected = entries - ownership_files
            if unexpected:
                _fail(
                    "incomplete session staging contains partial state; "
                    f"refusing automatic deletion: {path}",
                    code="session_recovery_required",
                )
            session_id = match.group(1)
            ownership: dict[str, _ProjectLeafOwnership] = {}
            for root_name, receipt_name in (
                _PROJECT_LEAF_OWNERSHIP_FILES.items()
            ):
                if receipt_name not in entries:
                    continue
                ownership[root_name] = _load_project_leaf_attestation(
                    staging_descriptor,
                    path,
                    session_id=session_id,
                    root_name=root_name,
                    error_code="session_recovery_required",
                )
            with contextlib.ExitStack() as project_descriptors:
                recoverable: list[_RecoverableProjectLeaf] = []
                for root_name in ("memory", "reports"):
                    leaf = _inspect_project_directory_for_recovery(
                        project_descriptor,
                        project_root,
                        root_name,
                        session_id,
                        ownership.get(root_name),
                        project_descriptors,
                        startup_deadline=startup_deadline,
                        monotonic=monotonic,
                    )
                    if leaf is not None:
                        recoverable.append(leaf)
                for leaf in recoverable:
                    _remove_attested_project_directory(
                        leaf,
                        session_id,
                        startup_deadline=startup_deadline,
                        monotonic=monotonic,
                    )
            _require_startup_budget(startup_deadline, monotonic)
            current_entries = set(os.listdir(staging_descriptor))
            if current_entries != entries:
                _fail(
                    "incomplete session staging changed during recovery: "
                    f"{path}",
                    code="session_recovery_required",
                )
            for receipt_name in sorted(entries):
                _require_startup_budget(startup_deadline, monotonic)
                try:
                    os.unlink(receipt_name, dir_fd=staging_descriptor)
                except OSError as error:
                    raise ModelSessionError(
                        "cannot remove project ownership attestation "
                        f"{path / receipt_name}: {error}",
                        code="session_recovery_required",
                    ) from error
            os.fsync(staging_descriptor)
            _require_startup_budget(startup_deadline, monotonic)
            if os.listdir(staging_descriptor):
                _fail(
                    "incomplete session staging changed during recovery: "
                    f"{path}",
                    code="session_recovery_required",
                )
        finally:
            os.close(staging_descriptor)
        try:
            _require_startup_budget(startup_deadline, monotonic)
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
    *,
    startup_deadline: float | None,
    monotonic: Callable[[], float],
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    _require_startup_budget(startup_deadline, monotonic)
    contents = {f"profile/{PROFILE_FILE_NAME}": profile.document}
    roles = {f"profile/{PROFILE_FILE_NAME}": ("profile",)}
    for resource in profile.resources:
        _require_startup_budget(startup_deadline, monotonic)
        relative = f"profile/{resource.relative_path.as_posix()}"
        contents[relative] = resource.content
        roles[relative] = resource.roles
    for asset in runtime_assets:
        _require_startup_budget(startup_deadline, monotonic)
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
        _require_startup_budget(startup_deadline, monotonic)
        content = contents[relative]
        entries.append(
            {
                "path": relative,
                "roles": list(roles[relative]),
                "sha256": _sha256(content),
                "size": len(content),
            }
        )
        _require_startup_budget(startup_deadline, monotonic)
    return entries, contents


def _open_relative_private_directory(
    root_descriptor: int,
    root: pathlib.Path,
    relative: pathlib.PurePosixPath,
    *,
    create_missing: bool,
    startup_deadline: float | None,
    monotonic: Callable[[], float],
) -> int:
    _require_startup_budget(startup_deadline, monotonic)
    descriptor = os.dup(root_descriptor)
    current_path = root
    try:
        for component in relative.parts:
            _require_startup_budget(startup_deadline, monotonic)
            current_path /= component
            if create_missing:
                child = _ensure_private_child_directory(
                    descriptor,
                    component,
                    path=current_path,
                    label="session snapshot directory",
                    startup_deadline=startup_deadline,
                    monotonic=monotonic,
                )
            else:
                child = _open_private_child_directory(
                    descriptor,
                    component,
                    path=current_path,
                    label="session snapshot directory",
                )
                _require_startup_budget(startup_deadline, monotonic)
            os.close(descriptor)
            descriptor = child
        _require_startup_budget(startup_deadline, monotonic)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _write_relative_exclusive(
    root_descriptor: int,
    root: pathlib.Path,
    relative: pathlib.PurePosixPath,
    content: bytes,
    *,
    startup_deadline: float | None,
    monotonic: Callable[[], float],
) -> None:
    parent_descriptor = _open_relative_private_directory(
        root_descriptor,
        root,
        relative.parent,
        create_missing=False,
        startup_deadline=startup_deadline,
        monotonic=monotonic,
    )
    try:
        _write_exclusive_at(
            parent_descriptor,
            relative.name,
            content,
            path=root.joinpath(*relative.parts),
            startup_deadline=startup_deadline,
            monotonic=monotonic,
        )
    finally:
        os.close(parent_descriptor)


def _make_directory_tree(
    root_descriptor: int,
    root: pathlib.Path,
    relative_paths: set[str],
    *,
    startup_deadline: float | None,
    monotonic: Callable[[], float],
) -> None:
    _require_startup_budget(startup_deadline, monotonic)
    directories: set[pathlib.PurePosixPath] = set()
    for relative in relative_paths:
        _require_startup_budget(startup_deadline, monotonic)
        parent = pathlib.PurePosixPath(relative).parent
        while parent != pathlib.PurePosixPath("."):
            _require_startup_budget(startup_deadline, monotonic)
            directories.add(parent)
            parent = parent.parent
    for relative in sorted(
        directories,
        key=lambda path: (len(path.parts), path.as_posix()),
    ):
        _require_startup_budget(startup_deadline, monotonic)
        descriptor = _open_relative_private_directory(
            root_descriptor,
            root,
            relative,
            create_missing=True,
            startup_deadline=startup_deadline,
            monotonic=monotonic,
        )
        os.close(descriptor)


def _fsync_directory_tree(
    descriptor: int,
    path: pathlib.Path,
    *,
    startup_deadline: float | None,
    monotonic: Callable[[], float],
) -> None:
    _require_startup_budget(startup_deadline, monotonic)
    for name in os.listdir(descriptor):
        _require_startup_budget(startup_deadline, monotonic)
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
                _fsync_directory_tree(
                    child,
                    path / name,
                    startup_deadline=startup_deadline,
                    monotonic=monotonic,
                )
            finally:
                os.close(child)
        elif not stat.S_ISREG(metadata.st_mode):
            _fail(
                f"session tree contains unsupported entry: {path / name}",
                code="session_materialization_failed",
            )
    _require_startup_budget(startup_deadline, monotonic)
    os.fsync(descriptor)
    _require_startup_budget(startup_deadline, monotonic)


def _retire_project_leaf_attestations(
    staging_descriptor: int,
    staging: pathlib.Path,
    reports_descriptor: int,
    memory_descriptor: int,
    report_directory: pathlib.Path,
    memory_directory: pathlib.Path,
    *,
    session_id: str,
    startup_deadline: float | None,
    monotonic: Callable[[], float],
) -> None:
    # The caller has already fsynced the complete staging tree. Requiring a
    # non-receipt entry keeps every crash after retirement on the conservative
    # partial-state path instead of making it look like an empty transaction.
    entries = set(os.listdir(staging_descriptor))
    receipt_names = set(_PROJECT_LEAF_OWNERSHIP_FILES.values())
    if not entries - receipt_names:
        _fail(
            "project ownership attestations cannot be retired before durable "
            f"session state exists: {staging}",
            code="session_materialization_cleanup_required",
        )
    for root_name, parent_descriptor, path in (
        ("reports", reports_descriptor, report_directory),
        ("memory", memory_descriptor, memory_directory),
    ):
        ownership = _load_project_leaf_attestation(
            staging_descriptor,
            staging,
            session_id=session_id,
            root_name=root_name,
            error_code="session_materialization_cleanup_required",
        )
        try:
            leaf_descriptor = _open_private_child_directory(
                parent_descriptor,
                session_id,
                path=path,
                label=f"session {root_name} directory",
            )
        except ModelSessionError as error:
            raise ModelSessionError(
                "project session directory cannot be revalidated before "
                f"publication: {path}",
                code="session_materialization_cleanup_required",
            ) from error
        try:
            expected_identity = (
                ownership.leaf_device,
                ownership.leaf_inode,
                ownership.leaf_ctime_ns,
            )
            if _directory_identity(leaf_descriptor) != expected_identity:
                _fail(
                    "project session directory changed before publication: "
                    f"{path}",
                    code="session_materialization_cleanup_required",
                )
            try:
                populated = bool(os.listdir(leaf_descriptor))
            except OSError as error:
                raise ModelSessionError(
                    "project session directory cannot be inspected before "
                    f"publication: {path}",
                    code="session_materialization_cleanup_required",
                ) from error
            if populated:
                _fail(
                    "project session directory was populated before "
                    f"publication: {path}",
                    code="session_materialization_cleanup_required",
                )
        finally:
            os.close(leaf_descriptor)
    for receipt_name in sorted(receipt_names):
        _require_startup_budget(startup_deadline, monotonic)
        try:
            os.unlink(receipt_name, dir_fd=staging_descriptor)
        except OSError as error:
            raise ModelSessionError(
                "cannot retire project ownership attestation "
                f"{staging / receipt_name}: {error}",
                code="session_materialization_cleanup_required",
            ) from error
    os.fsync(staging_descriptor)
    _require_startup_budget(startup_deadline, monotonic)


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
    startup_deadline: float | None,
    monotonic: Callable[[], float],
    cleanup_deadline: Callable[[], float],
) -> None:
    _require_startup_budget(startup_deadline, monotonic)
    snapshot_root = staging / "snapshot"
    workspace = staging / "workspace"
    pi_root = staging / "pi"
    with contextlib.ExitStack() as descriptors:
        snapshot_descriptor = _create_private_child_directory(
            staging_descriptor,
            "snapshot",
            path=snapshot_root,
            label="snapshot root",
            startup_deadline=startup_deadline,
            monotonic=monotonic,
            cleanup_deadline=cleanup_deadline,
        )
        descriptors.callback(os.close, snapshot_descriptor)
        workspace_descriptor = _create_private_child_directory(
            staging_descriptor,
            "workspace",
            path=workspace,
            label="session workspace",
            startup_deadline=startup_deadline,
            monotonic=monotonic,
            cleanup_deadline=cleanup_deadline,
        )
        descriptors.callback(os.close, workspace_descriptor)
        pi_descriptor = _create_private_child_directory(
            staging_descriptor,
            "pi",
            path=pi_root,
            label="private Pi root",
            startup_deadline=startup_deadline,
            monotonic=monotonic,
            cleanup_deadline=cleanup_deadline,
        )
        descriptors.callback(os.close, pi_descriptor)
        workspace_pi_descriptor = _create_private_child_directory(
            workspace_descriptor,
            ".pi",
            path=workspace / ".pi",
            label="masked workspace .pi mountpoint",
            startup_deadline=startup_deadline,
            monotonic=monotonic,
            cleanup_deadline=cleanup_deadline,
        )
        os.close(workspace_pi_descriptor)
        sessions_descriptor = _create_private_child_directory(
            pi_descriptor,
            "sessions",
            path=pi_root / "sessions",
            label="private Pi sessions directory",
            startup_deadline=startup_deadline,
            monotonic=monotonic,
            cleanup_deadline=cleanup_deadline,
        )
        os.close(sessions_descriptor)

        resource_entries, contents = _snapshot_file_entries(
            profile,
            runtime_assets,
            startup_deadline=startup_deadline,
            monotonic=monotonic,
        )
        _make_directory_tree(
            snapshot_descriptor,
            snapshot_root,
            set(contents),
            startup_deadline=startup_deadline,
            monotonic=monotonic,
        )
        for relative, content in sorted(contents.items()):
            _require_startup_budget(startup_deadline, monotonic)
            _write_relative_exclusive(
                snapshot_descriptor,
                snapshot_root,
                pathlib.PurePosixPath(relative),
                content,
                startup_deadline=startup_deadline,
                monotonic=monotonic,
            )

        agents = profile.resource_for_role("agents")
        if agents is None:
            _fail("validated profile has no AGENTS.md resource")
        _write_exclusive_at(
            workspace_descriptor,
            AGENTS_FILE_NAME,
            agents.content,
            path=workspace / AGENTS_FILE_NAME,
            startup_deadline=startup_deadline,
            monotonic=monotonic,
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
            startup_deadline=startup_deadline,
            monotonic=monotonic,
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
            startup_deadline=startup_deadline,
            monotonic=monotonic,
        )

        _fsync_directory_tree(
            staging_descriptor,
            staging,
            startup_deadline=startup_deadline,
            monotonic=monotonic,
        )


def _require_published_startup_budget(
    final: pathlib.Path,
    startup_deadline: float | None,
    monotonic: Callable[[], float],
) -> None:
    try:
        _require_startup_budget(startup_deadline, monotonic)
    except ModelSessionError as error:
        raise ModelSessionError(
            "session was durably published after its startup budget expired; "
            f"it remains available for explicit recovery at {final}",
            code="published_session_requires_recovery",
        ) from error


def _materialize_run(
    profile: Profile,
    *,
    endpoint_runtime_root: os.PathLike[str] | str | None = None,
    expected_workload_sha256: str | None = None,
    startup_deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> SessionRun:
    service_binding: ServiceEndpointBinding | None = None
    if profile.contract.schema == PROFILE_SCHEMA_V3:
        endpoint = load_service_endpoint(
            profile,
            runtime_root=endpoint_runtime_root,
            deadline=startup_deadline,
            monotonic=monotonic,
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
    _require_startup_budget(startup_deadline, monotonic)
    runtime_assets = pi_runtime_assets(
        profile.contract,
        service_binding,
    )
    _require_startup_budget(startup_deadline, monotonic)
    pi_installation = fingerprint_pi_installation(profile.contract)
    _require_startup_budget(startup_deadline, monotonic)
    state_root = profile.contract.state_root
    sessions_root = state_root / "sessions"
    profile_sessions = sessions_root / profile.contract.profile_id
    project_root = profile.contract.project_root

    with contextlib.ExitStack() as descriptors:
        _require_startup_budget(startup_deadline, monotonic)
        state_descriptor = _open_absolute_directory(
            state_root,
            label="model-session state_root",
            create_missing=True,
        )
        _require_startup_budget(startup_deadline, monotonic)
        descriptors.callback(os.close, state_descriptor)
        _validate_private_directory_descriptor(
            state_descriptor,
            path=state_root,
            label="model-session state_root",
        )
        _require_startup_budget(startup_deadline, monotonic)
        project_descriptor = _open_absolute_directory(
            project_root,
            label="project_root",
        )
        _require_startup_budget(startup_deadline, monotonic)
        descriptors.callback(os.close, project_descriptor)
        _validate_project_directory_descriptor(
            project_descriptor,
            path=project_root,
            label="project_root",
        )
        _require_startup_budget(startup_deadline, monotonic)

        with _materialization_lock(
            state_descriptor,
            state_root,
            startup_deadline=startup_deadline,
            monotonic=monotonic,
        ):
            sessions_descriptor = _ensure_private_child_directory(
                state_descriptor,
                "sessions",
                path=sessions_root,
                label="model-session sessions directory",
                startup_deadline=startup_deadline,
                monotonic=monotonic,
            )
            descriptors.callback(os.close, sessions_descriptor)
            profile_sessions_descriptor = _ensure_private_child_directory(
                sessions_descriptor,
                profile.contract.profile_id,
                path=profile_sessions,
                label="profile sessions directory",
                startup_deadline=startup_deadline,
                monotonic=monotonic,
            )
            descriptors.callback(os.close, profile_sessions_descriptor)
            _recover_incomplete_materializations(
                profile_sessions_descriptor,
                profile_sessions,
                project_descriptor,
                project_root,
                startup_deadline=startup_deadline,
                monotonic=monotonic,
            )
            _require_startup_budget(startup_deadline, monotonic)
            for _ in range(16):
                _require_startup_budget(startup_deadline, monotonic)
                session_id = _new_session_id()
                final = profile_sessions / session_id
                staging_name = f".creating-{session_id}"
                staging = profile_sessions / staging_name
                if _entry_exists_at(
                    profile_sessions_descriptor,
                    session_id,
                    startup_deadline=startup_deadline,
                    monotonic=monotonic,
                ) or _entry_exists_at(
                    profile_sessions_descriptor,
                    staging_name,
                    startup_deadline=startup_deadline,
                    monotonic=monotonic,
                ):
                    continue
                rollback_deadline: float | None = None

                def cleanup_deadline() -> float:
                    nonlocal rollback_deadline
                    if rollback_deadline is None:
                        rollback_deadline = (
                            monotonic()
                            + MATERIALIZATION_CLEANUP_GRACE_SECONDS
                        )
                    return rollback_deadline

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
                        startup_deadline=startup_deadline,
                        monotonic=monotonic,
                        cleanup_deadline=cleanup_deadline,
                    )
                    staging_created = True
                    descriptors.callback(os.close, staging_descriptor)
                    (
                        report_directory,
                        memory_directory,
                        reports_descriptor,
                        memory_descriptor,
                    ) = _make_project_session_directories(
                        project_descriptor,
                        project_root,
                        staging_descriptor,
                        staging,
                        session_id,
                        startup_deadline=startup_deadline,
                        monotonic=monotonic,
                        cleanup_deadline=cleanup_deadline,
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
                        startup_deadline=startup_deadline,
                        monotonic=monotonic,
                        cleanup_deadline=cleanup_deadline,
                    )
                    _retire_project_leaf_attestations(
                        staging_descriptor,
                        staging,
                        reports_descriptor,
                        memory_descriptor,
                        report_directory,
                        memory_directory,
                        session_id=session_id,
                        startup_deadline=startup_deadline,
                        monotonic=monotonic,
                    )
                    _require_startup_budget(
                        startup_deadline,
                        monotonic,
                    )
                    if _entry_exists_at(
                        profile_sessions_descriptor,
                        session_id,
                        startup_deadline=startup_deadline,
                        monotonic=monotonic,
                    ):
                        _fail(
                            f"generated session already exists: {final}",
                            code="session_id_collision",
                        )
                    _require_startup_budget(
                        startup_deadline,
                        monotonic,
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
                    _require_published_startup_budget(
                        final,
                        startup_deadline,
                        monotonic,
                    )
                    try:
                        run = load_run(profile, session_id)
                    except Exception as error:
                        raise ModelSessionError(
                            "session was durably published but its persisted state "
                            f"could not be validated; inspect {final}: {error}",
                            code="published_session_requires_recovery",
                        ) from error
                    _require_published_startup_budget(
                        final,
                        startup_deadline,
                        monotonic,
                    )
                    return run
                except ModelSessionError as error:
                    if (
                        not published
                        and error.code
                        == "session_materialization_cleanup_required"
                    ):
                        # A nested rollback could not prove its project leaves
                        # gone. Keep the staging join key intact for recovery.
                        raise
                    if not published:
                        try:
                            _rollback_unpublished_materialization(
                                profile_sessions_descriptor,
                                staging_name,
                                staging,
                                staging_created=staging_created,
                                reports_descriptor=reports_descriptor,
                                memory_descriptor=memory_descriptor,
                                report_directory=report_directory,
                                memory_directory=memory_directory,
                                session_id=session_id,
                                cleanup_deadline=cleanup_deadline(),
                                monotonic=monotonic,
                            )
                        except ModelSessionError as cleanup_error:
                            raise ModelSessionError(
                                "session materialization failed and its generated "
                                f"staging requires recovery at {staging}: "
                                f"{cleanup_error}",
                                code="session_materialization_cleanup_required",
                            ) from error
                    if error.code == "session_id_collision":
                        continue
                    raise
                except Exception as error:
                    if not published:
                        try:
                            _rollback_unpublished_materialization(
                                profile_sessions_descriptor,
                                staging_name,
                                staging,
                                staging_created=staging_created,
                                reports_descriptor=reports_descriptor,
                                memory_descriptor=memory_descriptor,
                                report_directory=report_directory,
                                memory_directory=memory_directory,
                                session_id=session_id,
                                cleanup_deadline=cleanup_deadline(),
                                monotonic=monotonic,
                            )
                        except ModelSessionError as cleanup_error:
                            raise ModelSessionError(
                                "session materialization failed and its generated "
                                f"staging requires recovery at {staging}: "
                                f"{cleanup_error}",
                                code="session_materialization_cleanup_required",
                            ) from error
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
    startup_deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
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
        startup_deadline=startup_deadline,
        monotonic=monotonic,
    )


def materialize_legacy_run_for_migration(profile: Profile) -> SessionRun:
    """Create a v1/v2 run only as an input to an explicit migration test."""

    if profile.contract.schema == PROFILE_SCHEMA_V3:
        _fail(
            "the legacy migration fixture path does not accept profile v3",
            code="invalid_legacy_migration_input",
        )
    return _materialize_run(profile)
