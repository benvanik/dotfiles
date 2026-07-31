"""Canonical crash-recovery journals for benchmarkd administration.

The journal is the durable admission fence for install and uninstall
transactions. Host orchestration belongs to :mod:`benchmark_lock.admin`; this
module owns only the fixed records, their secure publication, and interrupted
publish/phase-transition recovery.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import re
import stat
from collections.abc import Callable
from typing import TypeVar

from .administration_state import MAX_ADMINISTRATION_STATE_BYTES
from .configuration import (
    require_exact_fields,
    strict_json_document,
)
from .errors import BenchmarkLockError
from .generation_format import DIGEST_PATTERN
from .generation_store import MAX_GENERATIONS


INSTALL_INTENT_SCHEMA = "benchmarkd.install.v1"
UNINSTALL_INTENT_SCHEMA = "benchmarkd.uninstall.v1"
MAX_INTENT_BYTES = MAX_ADMINISTRATION_STATE_BYTES

_INSTALL_FIELDS = frozenset(
    {"phase", "prior_digest", "schema", "target_digest", "user_name"}
)
_UNINSTALL_FIELDS = frozenset(
    {"current_digest", "generation_digests", "phase", "schema"}
)
_INSTALL_PHASES = frozenset({"prepared", "rollback", "stopped"})
_UNINSTALL_PHASES = frozenset({"prepared", "stopped"})
_USER_PATTERN = re.compile(r"[a-z_][a-z0-9_-]{0,31}")


@dataclasses.dataclass(frozen=True)
class InstallIntent:
    """One immutable install target and its durable recovery phase."""

    prior_digest: str | None
    target_digest: str
    user_name: str
    phase: str


@dataclasses.dataclass(frozen=True)
class UninstallIntent:
    """One exact installed closure selected for conservative removal."""

    current_digest: str | None
    generation_digests: tuple[str, ...]
    phase: str


@dataclasses.dataclass(frozen=True)
class JournalPaths:
    """The fixed publication, committed, and transition names for one intent."""

    publish: pathlib.Path
    intent: pathlib.Path
    transition: pathlib.Path

    def __post_init__(self) -> None:
        paths = (self.publish, self.intent, self.transition)
        if (
            any(not path.is_absolute() for path in paths)
            or len({path.parent for path in paths}) != 1
            or len(set(paths)) != len(paths)
        ):
            raise ValueError("benchmark administration journal paths are invalid")


Intent = TypeVar("Intent", InstallIntent, UninstallIntent)
Reporter = Callable[[str], None]


def _journal_error(message: str, *, operation: str) -> BenchmarkLockError:
    return BenchmarkLockError(
        message,
        code=f"benchmark_admin_{operation}_invalid",
    )


def _canonical_json(document: object) -> bytes:
    import json

    return (
        json.dumps(
            document,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def canonical_install_intent(intent: InstallIntent) -> bytes:
    """Encode one canonical install intent."""

    return _canonical_json(
        {
            "phase": intent.phase,
            "prior_digest": intent.prior_digest,
            "schema": INSTALL_INTENT_SCHEMA,
            "target_digest": intent.target_digest,
            "user_name": intent.user_name,
        }
    )


def canonical_uninstall_intent(intent: UninstallIntent) -> bytes:
    """Encode one canonical uninstall intent."""

    return _canonical_json(
        {
            "current_digest": intent.current_digest,
            "generation_digests": list(intent.generation_digests),
            "phase": intent.phase,
            "schema": UNINSTALL_INTENT_SCHEMA,
        }
    )


def _strict_document(payload: bytes, *, operation: str) -> dict[str, object]:
    try:
        document = strict_json_document(
            payload,
            description=f"benchmark {operation} intent",
            maximum=MAX_INTENT_BYTES,
        )
    except BenchmarkLockError as error:
        raise _journal_error(str(error), operation=operation) from error
    if not isinstance(document, dict):
        raise _journal_error(
            f"benchmark {operation} intent is not an object",
            operation=operation,
        )
    return document


def parse_install_intent(payload: bytes) -> InstallIntent:
    """Parse and require one canonical, self-consistent install intent."""

    document = _strict_document(payload, operation="install")
    try:
        require_exact_fields(
            document,
            _INSTALL_FIELDS,
            description="benchmark install intent",
        )
    except BenchmarkLockError as error:
        raise _journal_error(str(error), operation="install") from error
    prior_digest = document["prior_digest"]
    target_digest = document["target_digest"]
    user_name = document["user_name"]
    phase = document["phase"]
    if (
        document["schema"] != INSTALL_INTENT_SCHEMA
        or (
            prior_digest is not None
            and (
                not isinstance(prior_digest, str)
                or not DIGEST_PATTERN.fullmatch(prior_digest)
            )
        )
        or not isinstance(target_digest, str)
        or not DIGEST_PATTERN.fullmatch(target_digest)
        or prior_digest == target_digest
        or not isinstance(user_name, str)
        or not _USER_PATTERN.fullmatch(user_name)
        or not isinstance(phase, str)
        or phase not in _INSTALL_PHASES
    ):
        raise _journal_error(
            "benchmark install intent fields are invalid",
            operation="install",
        )
    intent = InstallIntent(
        prior_digest=prior_digest,
        target_digest=target_digest,
        user_name=user_name,
        phase=phase,
    )
    if payload != canonical_install_intent(intent):
        raise _journal_error(
            "benchmark install intent is not canonical",
            operation="install",
        )
    return intent


def parse_uninstall_intent(payload: bytes) -> UninstallIntent:
    """Parse and require one canonical, self-consistent uninstall intent."""

    document = _strict_document(payload, operation="uninstall")
    try:
        require_exact_fields(
            document,
            _UNINSTALL_FIELDS,
            description="benchmark uninstall intent",
        )
    except BenchmarkLockError as error:
        raise _journal_error(str(error), operation="uninstall") from error
    current_digest = document["current_digest"]
    raw_generation_digests = document["generation_digests"]
    phase = document["phase"]
    if (
        document["schema"] != UNINSTALL_INTENT_SCHEMA
        or (
            current_digest is not None
            and (
                not isinstance(current_digest, str)
                or not DIGEST_PATTERN.fullmatch(current_digest)
            )
        )
        or not isinstance(raw_generation_digests, list)
        or len(raw_generation_digests) > MAX_GENERATIONS
        or any(
            not isinstance(digest, str) or not DIGEST_PATTERN.fullmatch(digest)
            for digest in raw_generation_digests
        )
        or not isinstance(phase, str)
        or phase not in _UNINSTALL_PHASES
    ):
        raise _journal_error(
            "benchmark uninstall intent fields are invalid",
            operation="uninstall",
        )
    generation_digests = tuple(sorted(raw_generation_digests))
    intent = UninstallIntent(
        current_digest=current_digest,
        generation_digests=generation_digests,
        phase=phase,
    )
    if (
        len(set(generation_digests)) != len(generation_digests)
        or (current_digest is not None and current_digest not in generation_digests)
        or payload != canonical_uninstall_intent(intent)
    ):
        raise _journal_error(
            "benchmark uninstall intent is not canonical or self-consistent",
            operation="uninstall",
        )
    return intent


def _mode(metadata: os.stat_result) -> int:
    return stat.S_IMODE(metadata.st_mode)


class AdministrationJournal:
    """Securely publish and recover the two administration intent families."""

    def __init__(
        self,
        *,
        install_paths: JournalPaths,
        uninstall_paths: JournalPaths,
        root_uid: int = 0,
        root_gid: int = 0,
        report: Reporter = print,
    ) -> None:
        if (
            install_paths.intent.parent != uninstall_paths.intent.parent
            or isinstance(root_uid, bool)
            or not isinstance(root_uid, int)
            or root_uid < 0
            or isinstance(root_gid, bool)
            or not isinstance(root_gid, int)
            or root_gid < 0
        ):
            raise ValueError("benchmark administration journal parameters are invalid")
        self.install_paths = install_paths
        self.uninstall_paths = uninstall_paths
        self.install_root = install_paths.intent.parent
        self.root_uid = root_uid
        self.root_gid = root_gid
        self.report = report

    @staticmethod
    def _fsync_directory(path: pathlib.Path) -> None:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _read_file(
        self,
        path: pathlib.Path,
        *,
        operation: str,
        parser: Callable[[bytes], Intent],
    ) -> Intent:
        try:
            metadata = os.lstat(path)
        except OSError as error:
            raise _journal_error(
                f"cannot inspect benchmark {operation} intent {path}: {error}",
                operation=operation,
            ) from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != self.root_uid
            or metadata.st_gid != self.root_gid
            or _mode(metadata) != 0o444
            or metadata.st_nlink != 1
            or metadata.st_size > MAX_INTENT_BYTES
        ):
            raise _journal_error(
                f"benchmark {operation} intent has unsafe metadata: {path}",
                operation=operation,
            )
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
            try:
                payload = os.read(descriptor, MAX_INTENT_BYTES + 1)
                opened = os.fstat(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise _journal_error(
                f"cannot read benchmark {operation} intent {path}: {error}",
                operation=operation,
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
            raise _journal_error(
                f"benchmark {operation} intent changed while being read: {path}",
                operation=operation,
            )
        return parser(payload)

    def _write_new_file(self, path: pathlib.Path, payload: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        created = False
        try:
            previous_mask = os.umask(0)
            try:
                descriptor = os.open(path, flags, 0o600)
            finally:
                os.umask(previous_mask)
            created = True
            try:
                os.fchown(descriptor, self.root_uid, self.root_gid)
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("journal write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
                os.fchmod(descriptor, 0o444)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except BaseException:
            if created and os.path.lexists(path):
                os.unlink(path)
                self._fsync_directory(path.parent)
            raise

    def _discard_incomplete(
        self,
        path: pathlib.Path,
        parse_error: BenchmarkLockError,
        *,
        operation: str,
    ) -> None:
        """Discard only an unpublished, securely owned partial fixed write."""

        try:
            metadata = os.lstat(path)
        except OSError:
            raise parse_error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != self.root_uid
            or metadata.st_gid != self.root_gid
            or metadata.st_nlink != 1
            or metadata.st_size > MAX_INTENT_BYTES
            or _mode(metadata) & ~0o644
        ):
            raise parse_error
        self.report(f"discarding incomplete benchmark {operation} journal {path}")
        os.unlink(path)
        self._fsync_directory(path.parent)

    @staticmethod
    def _has(paths: JournalPaths) -> bool:
        return any(
            os.path.lexists(path)
            for path in (paths.publish, paths.intent, paths.transition)
        )

    def has_install_state(self) -> bool:
        return self._has(self.install_paths)

    def has_uninstall_state(self) -> bool:
        return self._has(self.uninstall_paths)

    def require_mutually_exclusive(self) -> None:
        if self.has_install_state() and self.has_uninstall_state():
            raise BenchmarkLockError(
                "benchmark install and uninstall journals overlap",
                code="benchmark_admin_transaction_conflict",
            )

    def _recover(
        self,
        *,
        paths: JournalPaths,
        operation: str,
        parser: Callable[[bytes], Intent],
        stopped: Callable[[Intent], Intent],
        transition_sources: frozenset[str],
    ) -> Intent | None:
        intent_exists = os.path.lexists(paths.intent)
        publish_exists = os.path.lexists(paths.publish)
        transition_exists = os.path.lexists(paths.transition)
        if publish_exists:
            if intent_exists or transition_exists:
                raise _journal_error(
                    f"benchmark {operation} publication overlaps another journal state",
                    operation=operation,
                )
            try:
                staged = self._read_file(
                    paths.publish,
                    operation=operation,
                    parser=parser,
                )
            except BenchmarkLockError as error:
                self._discard_incomplete(
                    paths.publish,
                    error,
                    operation=operation,
                )
                return None
            if staged.phase != "prepared":
                raise _journal_error(
                    f"benchmark {operation} publication has the wrong phase",
                    operation=operation,
                )
            os.rename(paths.publish, paths.intent)
            self._fsync_directory(self.install_root)
            return staged
        if transition_exists:
            if not intent_exists:
                raise _journal_error(
                    f"benchmark {operation} transition lacks its prior intent",
                    operation=operation,
                )
            intent = self._read_file(
                paths.intent,
                operation=operation,
                parser=parser,
            )
            if intent.phase not in transition_sources:
                raise _journal_error(
                    f"benchmark {operation} transition overlaps phase {intent.phase!r}",
                    operation=operation,
                )
            try:
                transitioned = self._read_file(
                    paths.transition,
                    operation=operation,
                    parser=parser,
                )
            except BenchmarkLockError as error:
                self._discard_incomplete(
                    paths.transition,
                    error,
                    operation=operation,
                )
                return intent
            expected = stopped(intent)
            if transitioned != expected:
                raise _journal_error(
                    f"benchmark {operation} transition changed its recorded closure",
                    operation=operation,
                )
            os.replace(paths.transition, paths.intent)
            self._fsync_directory(self.install_root)
            return transitioned
        if not intent_exists:
            return None
        return self._read_file(
            paths.intent,
            operation=operation,
            parser=parser,
        )

    def recover_install(self) -> InstallIntent | None:
        """Resolve an interrupted install intent publication or phase change."""

        self.require_mutually_exclusive()
        return self._recover(
            paths=self.install_paths,
            operation="install",
            parser=parse_install_intent,
            stopped=lambda intent: dataclasses.replace(
                intent,
                phase=("stopped" if intent.phase == "prepared" else "rollback"),
            ),
            transition_sources=frozenset({"prepared", "stopped"}),
        )

    def recover_uninstall(self) -> UninstallIntent | None:
        """Resolve an interrupted uninstall intent publication or phase change."""

        self.require_mutually_exclusive()
        return self._recover(
            paths=self.uninstall_paths,
            operation="uninstall",
            parser=parse_uninstall_intent,
            stopped=lambda intent: dataclasses.replace(intent, phase="stopped"),
            transition_sources=frozenset({"prepared"}),
        )

    def _publish(
        self,
        intent: Intent,
        *,
        paths: JournalPaths,
        operation: str,
        canonical: Callable[[Intent], bytes],
    ) -> None:
        self.require_mutually_exclusive()
        if self._has(paths) or intent.phase != "prepared":
            raise _journal_error(
                f"benchmark {operation} journal already exists or has wrong phase",
                operation=operation,
            )
        self._write_new_file(paths.publish, canonical(intent))
        self._fsync_directory(self.install_root)
        os.rename(paths.publish, paths.intent)
        self._fsync_directory(self.install_root)

    def publish_install(self, intent: InstallIntent) -> None:
        if self.has_uninstall_state():
            raise BenchmarkLockError(
                "benchmark install cannot overlap an uninstall journal",
                code="benchmark_admin_transaction_conflict",
            )
        self._publish(
            intent,
            paths=self.install_paths,
            operation="install",
            canonical=canonical_install_intent,
        )

    def publish_uninstall(self, intent: UninstallIntent) -> None:
        if self.has_install_state():
            raise BenchmarkLockError(
                "benchmark uninstall cannot overlap an install journal",
                code="benchmark_admin_transaction_conflict",
            )
        self._publish(
            intent,
            paths=self.uninstall_paths,
            operation="uninstall",
            canonical=canonical_uninstall_intent,
        )

    def _transition(
        self,
        intent: Intent,
        *,
        phase: str,
        paths: JournalPaths,
        operation: str,
        parser: Callable[[bytes], Intent],
        canonical: Callable[[Intent], bytes],
        allowed: frozenset[tuple[str, str]],
    ) -> Intent:
        observed = self._read_file(
            paths.intent,
            operation=operation,
            parser=parser,
        )
        if observed != intent or (intent.phase, phase) not in allowed:
            raise _journal_error(
                f"benchmark {operation} intent changed during its transaction",
                operation=operation,
            )
        if os.path.lexists(paths.publish) or os.path.lexists(paths.transition):
            raise _journal_error(
                f"benchmark {operation} phase transition overlaps journal state",
                operation=operation,
            )
        transitioned = dataclasses.replace(intent, phase=phase)
        self._write_new_file(paths.transition, canonical(transitioned))
        self._fsync_directory(self.install_root)
        os.replace(paths.transition, paths.intent)
        self._fsync_directory(self.install_root)
        return transitioned

    def transition_install(
        self,
        intent: InstallIntent,
        *,
        phase: str,
    ) -> InstallIntent:
        return self._transition(
            intent,
            phase=phase,
            paths=self.install_paths,
            operation="install",
            parser=parse_install_intent,
            canonical=canonical_install_intent,
            allowed=frozenset(
                {
                    ("prepared", "stopped"),
                    ("stopped", "rollback"),
                }
            ),
        )

    def transition_uninstall(
        self,
        intent: UninstallIntent,
        *,
        phase: str,
    ) -> UninstallIntent:
        return self._transition(
            intent,
            phase=phase,
            paths=self.uninstall_paths,
            operation="uninstall",
            parser=parse_uninstall_intent,
            canonical=canonical_uninstall_intent,
            allowed=frozenset({("prepared", "stopped")}),
        )

    def remove_install(self, intent: InstallIntent) -> None:
        """Remove a terminal install fence after its projection is proven."""

        observed = self._read_file(
            self.install_paths.intent,
            operation="install",
            parser=parse_install_intent,
        )
        if (
            observed != intent
            or intent.phase not in {"stopped", "rollback"}
            or os.path.lexists(self.install_paths.publish)
            or os.path.lexists(self.install_paths.transition)
        ):
            raise _journal_error(
                "benchmark install intent is not terminal and quiescent",
                operation="install",
            )
        os.unlink(self.install_paths.intent)
        self._fsync_directory(self.install_root)
