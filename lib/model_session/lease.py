"""Descriptor-backed exclusive authority for one model-session launch."""

from __future__ import annotations

import contextlib
import enum
import fcntl
import os
import pathlib
import stat
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Self

from .errors import ModelSessionError

if TYPE_CHECKING:
    from .runs import SessionRun


class RunSource(enum.Enum):
    """A host object retained by a run lease for one sandbox launch."""

    WORKSPACE = "workspace"
    PI_SESSIONS = "pi_sessions"
    PI_INSTALLATION = "pi_installation"
    PROJECT = "project"
    REPORT = "report"
    MEMORY = "memory"


_RUN_LEASE_AUTHORITY = object()


@dataclass(init=False)
class RunLease:
    """Exclusive launch authority over one exact, validated session run."""

    _run: SessionRun
    _root_descriptor: int
    _receipt_descriptor: int
    _sources: dict[RunSource, int]
    _source_identities: dict[RunSource, tuple[int, int, int, int, int]]
    _resources: dict[pathlib.PurePosixPath, int]
    _resource_identities: dict[
        pathlib.PurePosixPath,
        tuple[int, int, int, int, int, int, int],
    ]
    _authority: object
    _plan_owner: object | None
    _closed: bool

    def __init__(
        self,
        *,
        run: SessionRun,
        root_descriptor: int,
        receipt_descriptor: int,
        sources: dict[RunSource, int],
        source_identities: dict[RunSource, tuple[int, int, int, int, int]],
        resources: dict[pathlib.PurePosixPath, int],
        resource_identities: dict[
            pathlib.PurePosixPath,
            tuple[int, int, int, int, int, int, int],
        ],
        authority: object,
    ) -> None:
        if authority is not _RUN_LEASE_AUTHORITY:
            _fail(
                "RunLease instances can only be created by validated "
                "session acquisition",
                code="session_lease_required",
            )
        self._run = run
        self._root_descriptor = root_descriptor
        self._receipt_descriptor = receipt_descriptor
        self._sources = sources
        self._source_identities = source_identities
        self._resources = resources
        self._resource_identities = resource_identities
        self._authority = authority
        self._plan_owner = None
        self._closed = False

    @property
    def run(self) -> SessionRun:
        return self._run

    @property
    def closed(self) -> bool:
        return self._closed

    def duplicate_source(self, source: RunSource) -> int:
        """Duplicate one retained mount source without reopening its path."""

        self._require_open()
        descriptor = self._sources.get(source)
        if descriptor is None:
            _fail(
                f"run lease does not retain source {source.value}",
                code="invalid_session_state",
            )
        metadata = os.fstat(descriptor)
        actual_identity = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_nlink if stat.S_ISREG(metadata.st_mode) else 0,
        )
        if actual_identity != self._source_identities[source]:
            _fail(
                f"retained run source changed identity: {source.value}",
                code="session_reference_changed",
            )
        if hasattr(fcntl, "F_DUPFD_CLOEXEC"):
            return fcntl.fcntl(descriptor, fcntl.F_DUPFD_CLOEXEC, 0)
        duplicate = os.dup(descriptor)
        os.set_inheritable(duplicate, False)
        return duplicate

    def duplicate_resource(
        self,
        relative_path: pathlib.PurePosixPath,
    ) -> int:
        """Duplicate one immutable, sealed snapshot resource."""

        self._require_open()
        if not isinstance(relative_path, pathlib.PurePosixPath):
            _fail(
                "locked resource identity must be a PurePosixPath",
                code="invalid_session_state",
            )
        descriptor = self._resources.get(relative_path)
        if descriptor is None:
            _fail(
                "run lease does not retain locked resource "
                f"{relative_path.as_posix()}",
                code="invalid_session_state",
            )
        actual_identity = _sealed_resource_identity(descriptor)
        if actual_identity != self._resource_identities[relative_path]:
            _fail(
                "retained locked resource changed identity: "
                f"{relative_path.as_posix()}",
                code="session_reference_changed",
            )
        try:
            duplicate = os.open(
                f"/proc/self/fd/{descriptor}",
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError as error:
            raise ModelSessionError(
                "cannot create an independent snapshot resource reader: "
                f"{error}",
                code="session_platform_unsupported",
            ) from error
        try:
            if (
                _sealed_resource_identity(duplicate)
                != self._resource_identities[relative_path]
            ):
                _fail(
                    "independent snapshot resource reader changed identity: "
                    f"{relative_path.as_posix()}",
                    code="session_reference_changed",
                )
            return duplicate
        except BaseException:
            os.close(duplicate)
            raise

    def list_pi_session_names(self) -> tuple[str, ...]:
        """Return the sole Pi session name without unbounded enumeration."""

        self._require_open()
        try:
            names: list[str] = []
            with os.scandir(self._sources[RunSource.PI_SESSIONS]) as entries:
                for entry in entries:
                    names.append(entry.name)
                    if len(names) == 2:
                        _fail(
                            "outer run contains more than one Pi session "
                            "entry",
                            code="ambiguous_pi_session",
                        )
            return tuple(names)
        except OSError as error:
            raise ModelSessionError(
                "cannot enumerate the retained Pi session directory: "
                f"{error}",
                code="unsafe_session_state",
            ) from error

    def open_pi_session(self, name: str) -> int:
        """Open and validate one Pi JSONL file relative to the retained root."""

        self._require_open()
        return open_pi_session_at(
            self._sources[RunSource.PI_SESSIONS],
            name,
        )

    def _require_open(self) -> None:
        if getattr(self, "_authority", None) is not _RUN_LEASE_AUTHORITY:
            _fail(
                "sandbox launch authority is not a validated RunLease",
                code="session_lease_required",
            )
        if self._closed:
            _fail("cannot use a closed run lease", code="session_lease_closed")
        if self._plan_owner is not None:
            _fail(
                "run lease authority has transferred to a sandbox plan",
                code="session_lease_owned",
            )

    def _claim_for_plan(self, owner: object) -> None:
        self._require_open()
        self._plan_owner = owner

    def _release_plan_claim(self, owner: object) -> None:
        if self._plan_owner is not owner:
            _fail(
                "sandbox plan claim does not own this run lease",
                code="session_lease_required",
            )
        self._plan_owner = None

    def _close_from_plan(self, owner: object) -> None:
        if self._plan_owner is not owner:
            _fail(
                "sandbox plan does not own this run lease",
                code="session_lease_required",
            )
        self._plan_owner = None
        self._close_unowned()

    def close(self) -> None:
        if getattr(self, "_plan_owner", None) is not None:
            _fail(
                "run lease is owned by a live sandbox plan",
                code="session_lease_owned",
            )
        self._close_unowned()

    def _close_unowned(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        try:
            fcntl.flock(self._root_descriptor, fcntl.LOCK_UN)
        finally:
            for descriptor in reversed(tuple(self._resources.values())):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            self._resources.clear()
            for descriptor in reversed(tuple(self._sources.values())):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            self._sources.clear()
            try:
                os.close(self._receipt_descriptor)
            finally:
                os.close(self._root_descriptor)

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
        if getattr(self, "_plan_owner", None) is None:
            self._close_unowned()


@dataclass
class RunInspection:
    """Exact root and receipt authority retained across a resume picker."""

    run: SessionRun
    _root_descriptor: int
    _receipt_descriptor: int
    _locked: bool = False
    _closed: bool = False

    @property
    def locked(self) -> bool:
        return self._locked

    @property
    def closed(self) -> bool:
        return self._closed

    def try_lock(self) -> bool:
        """Lock the stable run directory before reading mutable history."""

        self._require_open()
        if self._locked:
            return True
        try:
            fcntl.flock(
                self._root_descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            return False
        self._locked = True
        return True

    def unlock(self) -> None:
        self._require_open()
        if not self._locked:
            return
        fcntl.flock(self._root_descriptor, fcntl.LOCK_UN)
        self._locked = False

    def open_pi_sessions(self) -> int:
        """Open the exact Pi sessions directory while this run is locked."""

        self._require_open()
        if not self._locked:
            _fail(
                "mutable Pi history requires the run-directory lock",
                code="session_lock_required",
            )
        from . import runs as run_state

        pi_root = self.run.root / "pi"
        pi_descriptor = run_state._open_private_child_directory(
            self._root_descriptor,
            "pi",
            path=pi_root,
            label="private Pi root",
        )
        try:
            return run_state._open_private_child_directory(
                pi_descriptor,
                "sessions",
                path=self.run.pi_sessions,
                label="private Pi sessions",
            )
        finally:
            os.close(pi_descriptor)

    def acquire(self) -> RunLease:
        """Convert this exact picker authority into a full launch lease."""

        self._require_open()
        if not self.try_lock():
            _fail(
                f"session {self.run.session_id} is already active",
                code="session_in_use",
            )
        from . import runs as run_state

        sources: dict[RunSource, int] = {}
        resources: dict[pathlib.PurePosixPath, int] = {}
        try:
            run = run_state._load_run(
                self.run.profile.state_root,
                self.run.profile.profile_id,
                self.run.session_id,
                preopened_root_descriptor=self._root_descriptor,
                preopened_receipt_descriptor=self._receipt_descriptor,
                retained_sources=sources,
                retained_resources=resources,
                validate_pi_installation=True,
            )
            if (
                run.created_at != self.run.created_at
                or run.lock_sha256 != self.run.lock_sha256
                or run.profile.project_id != self.run.profile.project_id
            ):
                _fail(
                    "session receipt changed after history enumeration",
                    code="session_reference_changed",
                )
            source_identities = {
                source: _descriptor_identity(descriptor)
                for source, descriptor in sources.items()
            }
            resource_identities = {
                relative_path: _sealed_resource_identity(descriptor)
                for relative_path, descriptor in resources.items()
            }
            lease = RunLease(
                run=run,
                root_descriptor=self._root_descriptor,
                receipt_descriptor=self._receipt_descriptor,
                sources=sources,
                source_identities=source_identities,
                resources=resources,
                resource_identities=resource_identities,
                authority=_RUN_LEASE_AUTHORITY,
            )
        except BaseException:
            for descriptor in sources.values():
                os.close(descriptor)
            for descriptor in resources.values():
                os.close(descriptor)
            self.unlock()
            raise

        self._root_descriptor = -1
        self._receipt_descriptor = -1
        self._locked = False
        self._closed = True
        return lease

    def _require_open(self) -> None:
        if self._closed:
            _fail(
                "cannot use a closed run inspection",
                code="session_inspection_closed",
            )

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        try:
            if self._locked:
                fcntl.flock(self._root_descriptor, fcntl.LOCK_UN)
                self._locked = False
        finally:
            try:
                os.close(self._receipt_descriptor)
            finally:
                os.close(self._root_descriptor)

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


def inspect_run_from_state(
    state_root: os.PathLike[str] | str,
    profile_id: str,
    session_id: str,
) -> RunInspection:
    """Retain exact run authority without validating mutable Pi history."""

    from . import runs as run_state
    from .profile import validate_state_route

    root, validated_profile_id = validate_state_route(state_root, profile_id)
    run_state._validate_session_id(session_id)
    sessions_root = root / "sessions"
    profile_sessions = sessions_root / validated_profile_id
    run_root = profile_sessions / session_id
    with contextlib.ExitStack() as descriptors:
        state_descriptor = run_state._open_absolute_directory(
            root,
            label="model-session state_root",
        )
        descriptors.callback(os.close, state_descriptor)
        run_state._validate_private_directory_descriptor(
            state_descriptor,
            path=root,
            label="model-session state_root",
        )
        sessions_descriptor = run_state._open_private_child_directory(
            state_descriptor,
            "sessions",
            path=sessions_root,
            label="model-session sessions directory",
        )
        descriptors.callback(os.close, sessions_descriptor)
        profile_descriptor = run_state._open_private_child_directory(
            sessions_descriptor,
            validated_profile_id,
            path=profile_sessions,
            label="profile sessions directory",
        )
        descriptors.callback(os.close, profile_descriptor)
        root_descriptor = run_state._open_private_child_directory(
            profile_descriptor,
            session_id,
            path=run_root,
            label="session root",
        )
        try:
            receipt_descriptor = run_state._open_private_regular_file_at(
                root_descriptor,
                "run.json",
                path=run_root / "run.json",
                label="session receipt",
            )
        except BaseException:
            os.close(root_descriptor)
            raise
        try:
            run = run_state._load_run(
                root,
                validated_profile_id,
                session_id,
                preopened_root_descriptor=root_descriptor,
                preopened_receipt_descriptor=receipt_descriptor,
                validate_pi_installation=False,
            )
        except BaseException:
            os.close(receipt_descriptor)
            os.close(root_descriptor)
            raise
        return RunInspection(
            run=run,
            _root_descriptor=root_descriptor,
            _receipt_descriptor=receipt_descriptor,
        )


def acquire_run_from_state(
    state_root: os.PathLike[str] | str,
    profile_id: str,
    session_id: str,
) -> RunLease:
    """Acquire a fresh exclusive launch lease by canonical run identity."""

    inspection = inspect_run_from_state(state_root, profile_id, session_id)
    try:
        return inspection.acquire()
    finally:
        inspection.close()


def open_pi_session_at(sessions_descriptor: int, name: str) -> int:
    """Open one private Pi JSONL relative to an already-retained directory."""

    _validate_child_name(name)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=sessions_descriptor)
    except OSError as error:
        raise ModelSessionError(
            "cannot open Pi session file "
            f"{_safe_child_name(name)}: {error.strerror or error.errno}",
            code="unsafe_session_state",
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _fail(
                "Pi session entry is not a regular file: "
                f"{_safe_child_name(name)}",
                code="unsafe_session_state",
            )
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            _fail(
                "Pi session file has an unexpected owner: "
                f"{_safe_child_name(name)}",
                code="unsafe_session_state",
            )
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            _fail(
                "Pi session file permissions must be exactly 0600: "
                f"{_safe_child_name(name)}",
                code="unsafe_session_permissions",
            )
        if metadata.st_nlink != 1:
            _fail(
                "Pi session file must not have filesystem aliases: "
                f"{_safe_child_name(name)}",
                code="unsafe_session_state",
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _descriptor_identity(
    descriptor: int,
) -> tuple[int, int, int, int, int]:
    metadata = os.fstat(descriptor)
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink if stat.S_ISREG(metadata.st_mode) else 0,
    )


def _sealed_resource_identity(
    descriptor: int,
) -> tuple[int, int, int, int, int, int, int]:
    required_seals = (
        getattr(fcntl, "F_SEAL_WRITE", 0)
        | getattr(fcntl, "F_SEAL_GROW", 0)
        | getattr(fcntl, "F_SEAL_SHRINK", 0)
        | getattr(fcntl, "F_SEAL_SEAL", 0)
    )
    if (
        required_seals == 0
        or not hasattr(fcntl, "F_GET_SEALS")
    ):
        _fail(
            "sealed snapshot resources are unsupported on this platform",
            code="session_platform_unsupported",
        )
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o400
        or metadata.st_nlink != 0
    ):
        _fail(
            "retained snapshot resource is not an owned sealed memory file",
            code="session_reference_changed",
        )
    try:
        seals = fcntl.fcntl(descriptor, fcntl.F_GET_SEALS)
    except OSError as error:
        raise ModelSessionError(
            f"cannot inspect retained snapshot resource seals: {error}",
            code="session_reference_changed",
        ) from error
    if seals & required_seals != required_seals:
        _fail(
            "retained snapshot resource is not immutable",
            code="session_reference_changed",
        )
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_size,
        seals,
    )


def _validate_child_name(name: str) -> None:
    if not isinstance(name, str):
        _fail(
            "Pi session file name is not a string",
            code="unsafe_session_state",
        )
    path = pathlib.PurePosixPath(name)
    if (
        not name
        or "\x00" in name
        or path.is_absolute()
        or len(path.parts) != 1
        or path.name != name
        or name in {".", ".."}
    ):
        _fail(
            "Pi session file name is not one filesystem component",
            code="unsafe_session_state",
        )


def _safe_child_name(name: str) -> str:
    rendered = ascii(name)
    if len(rendered) <= 320:
        return rendered
    return rendered[:316] + "...'"


def _fail(message: str, *, code: str) -> None:
    raise ModelSessionError(message, code=code)
