"""Immutable benchmarkd software generations and crash-safe transactions.

Publication first links a complete manifest from an unnamed inode, then builds
one fixed journal-backed tree. Canonical digest directories are therefore
always complete. Retirement hard-links that manifest before moving the tree to
its fixed deletion name.

The caller owns serialization across administrative operations and must pass
the digest selected by any live projection as ``protected_digest``.
"""

from __future__ import annotations

import ctypes
import dataclasses
import hashlib
import os
import pathlib
import re
import stat
from collections.abc import Callable

from .errors import BenchmarkLockError
from .generation_format import (
    DIGEST_PATTERN as _DIGEST_PATTERN,
    GENERATION_DIRECTORIES as _GENERATION_DIRECTORIES,
    MAX_GENERATION_FILES,
    MAX_MANIFEST_BYTES,
    MAX_SOURCE_FILE_BYTES,
    Generation,
    GenerationEntry,
    GenerationManifest as _Manifest,
    GenerationManifestFile as _ManifestFile,
    generation_digest as _generation_digest,
    parse_generation_manifest as _parse_manifest_payload,
    validate_generation as _validated_generation,
)


MAX_GENERATIONS = 128

_PUBLICATION_MANIFEST_PATTERN = re.compile(
    r"\.publish-(?P<digest>[0-9a-f]{64})\.manifest"
)
_PUBLICATION_TREE_PATTERN = re.compile(r"\.publish-(?P<digest>[0-9a-f]{64})\.tree")
_REMOVAL_MANIFEST_PATTERN = re.compile(r"\.remove-(?P<digest>[0-9a-f]{64})\.manifest")
_REMOVAL_TREE_PATTERN = re.compile(r"\.remove-(?P<digest>[0-9a-f]{64})\.tree")
_DIRECTORY_REMOVAL_ORDER = (
    pathlib.PurePosixPath("lib/benchmark_lock"),
    pathlib.PurePosixPath("lib"),
    pathlib.PurePosixPath("bin"),
    pathlib.PurePosixPath("share/systemd"),
    pathlib.PurePosixPath("share/sysusers"),
    pathlib.PurePosixPath("share"),
)
_PUBLICATION_SEAL_ORDER = (
    *reversed(_GENERATION_DIRECTORIES),
    pathlib.PurePosixPath("."),
)

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


def _generation_error(message: str, *, code: str) -> BenchmarkLockError:
    return BenchmarkLockError(message, code=code)


@dataclasses.dataclass(frozen=True)
class VerifiedGeneration:
    """A complete generation verified against its canonical manifest."""

    digest: str
    root: pathlib.Path
    entries: tuple[GenerationEntry, ...]
    manifest: bytes


@dataclasses.dataclass(frozen=True)
class _RemovalState:
    digest: str
    intent: pathlib.Path
    live: pathlib.Path | None
    retired: pathlib.Path | None


@dataclasses.dataclass(frozen=True)
class _PublicationState:
    digest: str
    intent: pathlib.Path
    live: pathlib.Path | None
    staging: pathlib.Path | None


@dataclasses.dataclass(frozen=True)
class _Inventory:
    live: tuple[tuple[str, pathlib.Path], ...]
    publications: tuple[_PublicationState, ...]
    removals: tuple[_RemovalState, ...]


@dataclasses.dataclass(frozen=True)
class _RemovalPlan:
    state: _RemovalState
    manifest: _Manifest
    phase: str


@dataclasses.dataclass(frozen=True)
class _PublicationPlan:
    state: _PublicationState
    manifest: _Manifest
    phase: str


def _mode(metadata: os.stat_result) -> int:
    return stat.S_IMODE(metadata.st_mode)


class GenerationStore:
    """Root-owned immutable generation publication and retirement store."""

    def __init__(
        self,
        *,
        generation_directory: pathlib.Path,
        root_uid: int = 0,
        root_gid: int = 0,
        report: Reporter = print,
    ) -> None:
        self.generation_directory = pathlib.Path(generation_directory)
        if not self.generation_directory.is_absolute():
            raise ValueError("benchmark generation directory must be absolute")
        self.root_uid = root_uid
        self.root_gid = root_gid
        self.report = report

    def _require_store_directory(self) -> None:
        try:
            metadata = os.lstat(self.generation_directory)
        except OSError as error:
            raise _generation_error(
                f"benchmark generation directory is unavailable: {error}",
                code="benchmark_admin_layout_invalid",
            ) from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != self.root_uid
            or metadata.st_gid != self.root_gid
            or _mode(metadata) != 0o755
        ):
            raise _generation_error(
                "benchmark generation directory has unsafe ownership or mode",
                code="benchmark_admin_layout_invalid",
            )

    @staticmethod
    def _fsync_directory(path: pathlib.Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _read_regular(
        self,
        path: pathlib.Path,
        *,
        maximum: int,
        expected_mode: int,
    ) -> bytes:
        try:
            metadata = os.lstat(path)
        except OSError as error:
            raise _generation_error(
                f"cannot inspect managed generation file {path}: {error}",
                code="benchmark_admin_generation_invalid",
            ) from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != self.root_uid
            or metadata.st_gid != self.root_gid
            or _mode(metadata) != expected_mode
            or metadata.st_size > maximum
        ):
            raise _generation_error(
                f"managed generation file has unsafe metadata: {path}",
                code="benchmark_admin_generation_invalid",
            )
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
            try:
                content = os.read(descriptor, maximum + 1)
                opened = os.fstat(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise _generation_error(
                f"cannot read managed generation file {path}: {error}",
                code="benchmark_admin_generation_invalid",
            ) from error
        if (
            len(content) != metadata.st_size
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_uid != metadata.st_uid
            or opened.st_gid != metadata.st_gid
            or _mode(opened) != _mode(metadata)
            or opened.st_size != metadata.st_size
        ):
            raise _generation_error(
                f"managed generation file changed while being read: {path}",
                code="benchmark_admin_generation_invalid",
            )
        return content

    def _write_new_file(
        self,
        path: pathlib.Path,
        content: bytes,
        *,
        mode: int,
    ) -> None:
        """Atomically link one fully written inode into a managed transaction."""

        flags = os.O_WRONLY | os.O_CLOEXEC | os.O_TMPFILE
        try:
            descriptor = os.open(path.parent, flags, mode)
        except OSError as error:
            raise _generation_error(
                f"cannot create unnamed managed file for {path}: {error}",
                code="benchmark_admin_generation_invalid",
            ) from error
        try:
            try:
                os.fchown(descriptor, self.root_uid, self.root_gid)
                os.fchmod(descriptor, mode)
                view = memoryview(content)
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
                opened = os.fstat(descriptor)
                linked = os.lstat(path)
                if (
                    opened.st_dev != linked.st_dev
                    or opened.st_ino != linked.st_ino
                    or linked.st_uid != self.root_uid
                    or linked.st_gid != self.root_gid
                    or _mode(linked) != mode
                    or linked.st_size != len(content)
                    or linked.st_nlink != 1
                ):
                    raise _generation_error(
                        f"new managed file identity changed while linking: {path}",
                        code="benchmark_admin_generation_invalid",
                    )
            except OSError as error:
                raise _generation_error(
                    f"cannot publish managed file {path}: {error}",
                    code="benchmark_admin_generation_invalid",
                ) from error
        finally:
            os.close(descriptor)

    def _mkdir_managed_directory(self, path: pathlib.Path) -> None:
        # Administrative operations are serialized in one thread. Neutralizing
        # umask here makes the directory exact at its first visible instant.
        previous_mask = os.umask(0)
        try:
            os.mkdir(path, 0o700)
        finally:
            os.umask(previous_mask)
        os.chown(path, self.root_uid, self.root_gid)
        os.chmod(path, 0o700)

    def _digest_path(self, digest: str) -> pathlib.Path:
        if not _DIGEST_PATTERN.fullmatch(digest):
            raise _generation_error(
                f"benchmark generation digest is invalid: {digest!r}",
                code="benchmark_admin_generation_invalid",
            )
        return self.generation_directory / digest

    def _removal_intent_path(self, digest: str) -> pathlib.Path:
        return self.generation_directory / f".remove-{digest}.manifest"

    def _retired_path(self, digest: str) -> pathlib.Path:
        return self.generation_directory / f".remove-{digest}.tree"

    def _publication_intent_path(self, digest: str) -> pathlib.Path:
        return self.generation_directory / f".publish-{digest}.manifest"

    def _publication_staging_path(self, digest: str) -> pathlib.Path:
        return self.generation_directory / f".publish-{digest}.tree"

    def publish(self, generation: Generation) -> VerifiedGeneration:
        """Recover pending transactions, then publish one immutable generation."""

        self._require_store_directory()
        generation = _validated_generation(generation)
        (
            _inventory,
            verified_live,
            publication_plans,
            removal_plans,
        ) = self._validated_inventory(protected_digest=None)
        if removal_plans:
            digests = ", ".join(plan.state.digest for plan in removal_plans)
            raise _generation_error(
                f"benchmark generation removals require recovery: {digests}",
                code="benchmark_admin_generation_removal_pending",
            )
        blocked_publications = tuple(
            plan
            for plan in publication_plans
            if plan.phase in {"prepared", "building"}
            and plan.state.digest != generation.digest
        )
        if blocked_publications:
            digests = ", ".join(plan.state.digest for plan in blocked_publications)
            raise _generation_error(
                "incomplete benchmark generation publications require their "
                f"original source bytes: {digests}",
                code="benchmark_admin_generation_publication_pending",
            )

        for plan in publication_plans:
            matching_generation = (
                generation if plan.state.digest == generation.digest else None
            )
            self._recover_publication(plan, generation=matching_generation)

        (
            _inventory,
            verified_live,
            publication_plans,
            removal_plans,
        ) = self._validated_inventory(protected_digest=None)
        if publication_plans or removal_plans:
            raise AssertionError("generation transaction recovery did not quiesce")
        existing = {verified.digest: verified for verified in verified_live}
        if generation.digest in existing:
            verified = existing[generation.digest]
            if verified.manifest != generation.manifest:
                raise _generation_error(
                    "installed benchmark generation digest collision",
                    code="benchmark_admin_generation_invalid",
                )
            return verified
        if len(existing) >= MAX_GENERATIONS:
            raise _generation_error(
                "benchmark generation store reached its fixed generation limit",
                code="benchmark_admin_generation_limit",
            )

        intent = self._publication_intent_path(generation.digest)
        self._write_new_file(intent, generation.manifest, mode=0o444)
        self._fsync_directory(self.generation_directory)
        state = _PublicationState(
            digest=generation.digest,
            intent=intent,
            live=None,
            staging=None,
        )
        plan = self._validate_publication_state(state)
        verified = self._recover_publication(plan, generation=generation)
        if verified is None:
            raise AssertionError("matching publication transaction was discarded")
        return verified

    def _read_manifest_entry(
        self,
        root: pathlib.Path,
        expected: _ManifestFile,
    ) -> bytes:
        content = self._read_regular(
            root / expected.path,
            maximum=MAX_SOURCE_FILE_BYTES,
            expected_mode=expected.mode,
        )
        if (
            len(content) != expected.size
            or hashlib.sha256(content).hexdigest() != expected.sha256
        ):
            raise _generation_error(
                f"benchmark generation file identity changed: {expected.path}",
                code="benchmark_admin_generation_invalid",
            )
        return content

    def _verify_root(self, generation_root: pathlib.Path) -> VerifiedGeneration:
        try:
            metadata = os.lstat(generation_root)
        except OSError as error:
            raise _generation_error(
                f"cannot inspect benchmark generation: {error}",
                code="benchmark_admin_generation_invalid",
            ) from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != self.root_uid
            or metadata.st_gid != self.root_gid
            or _mode(metadata) != 0o555
            or generation_root.parent != self.generation_directory
            or not _DIGEST_PATTERN.fullmatch(generation_root.name)
        ):
            raise _generation_error(
                f"benchmark generation root is invalid: {generation_root}",
                code="benchmark_admin_generation_invalid",
            )
        manifest = _parse_manifest_payload(
            self._read_regular(
                generation_root / "manifest.json",
                maximum=MAX_MANIFEST_BYTES,
                expected_mode=0o444,
            )
        )
        if manifest.digest != generation_root.name:
            raise _generation_error(
                "benchmark generation directory and content identities differ",
                code="benchmark_admin_generation_invalid",
            )
        entries: list[GenerationEntry] = []
        for expected in manifest.files:
            content = self._read_manifest_entry(generation_root, expected)
            entries.append(
                GenerationEntry(
                    path=expected.path,
                    content=content,
                    mode=expected.mode,
                )
            )
        normalized = tuple(entries)
        if _generation_digest(normalized) != manifest.digest:
            raise _generation_error(
                "benchmark generation manifest is not self-consistent",
                code="benchmark_admin_generation_invalid",
            )
        expected_files = {entry.path.as_posix() for entry in normalized}
        expected_files.add("manifest.json")
        expected_directories = {
            ".",
            *(path.as_posix() for path in _GENERATION_DIRECTORIES),
        }
        observed_files: set[str] = set()
        observed_directories: set[str] = {"."}
        pending = [generation_root]
        observed_nodes = 0
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(directory) as iterator:
                    children = tuple(iterator)
            except OSError as error:
                raise _generation_error(
                    f"cannot enumerate benchmark generation: {error}",
                    code="benchmark_admin_generation_invalid",
                ) from error
            for child in children:
                observed_nodes += 1
                if (
                    observed_nodes
                    > MAX_GENERATION_FILES + len(_GENERATION_DIRECTORIES) + 1
                ):
                    raise _generation_error(
                        "benchmark generation exceeds its fixed node limit",
                        code="benchmark_admin_generation_invalid",
                    )
                path = pathlib.Path(child.path)
                relative = path.relative_to(generation_root).as_posix()
                try:
                    child_metadata = os.lstat(path)
                except OSError as error:
                    raise _generation_error(
                        f"cannot inspect benchmark generation node: {error}",
                        code="benchmark_admin_generation_invalid",
                    ) from error
                if (
                    child_metadata.st_uid != self.root_uid
                    or child_metadata.st_gid != self.root_gid
                ):
                    raise _generation_error(
                        f"benchmark generation node has wrong owner: {relative}",
                        code="benchmark_admin_generation_invalid",
                    )
                if stat.S_ISDIR(child_metadata.st_mode):
                    if _mode(child_metadata) != 0o555:
                        raise _generation_error(
                            f"benchmark generation directory mode changed: {relative}",
                            code="benchmark_admin_generation_invalid",
                        )
                    observed_directories.add(relative)
                    pending.append(path)
                elif stat.S_ISREG(child_metadata.st_mode):
                    observed_files.add(relative)
                else:
                    raise _generation_error(
                        f"benchmark generation contains a special node: {relative}",
                        code="benchmark_admin_generation_invalid",
                    )
        if (
            observed_files != expected_files
            or observed_directories != expected_directories
        ):
            raise _generation_error(
                "benchmark generation contains missing or unrecorded nodes",
                code="benchmark_admin_generation_invalid",
            )
        return VerifiedGeneration(
            digest=manifest.digest,
            root=generation_root,
            entries=normalized,
            manifest=manifest.payload,
        )

    def verify(self, digest: str) -> VerifiedGeneration:
        """Verify one canonical digest directory and every byte below it."""

        self._require_store_directory()
        return self._verify_root(self._digest_path(digest))

    def _inventory(self) -> _Inventory:
        self._require_store_directory()
        try:
            with os.scandir(self.generation_directory) as iterator:
                paths = tuple(
                    sorted(
                        (pathlib.Path(entry.path) for entry in iterator),
                        key=os.fspath,
                    )
                )
        except OSError as error:
            raise _generation_error(
                f"cannot enumerate installed benchmark generations: {error}",
                code="benchmark_admin_layout_invalid",
            ) from error
        if len(paths) > MAX_GENERATIONS * 3:
            raise _generation_error(
                "benchmark generation store exceeds its fixed entry limit",
                code="benchmark_admin_layout_invalid",
            )
        live: dict[str, pathlib.Path] = {}
        publication_intents: dict[str, pathlib.Path] = {}
        publication_staging: dict[str, pathlib.Path] = {}
        intents: dict[str, pathlib.Path] = {}
        retired: dict[str, pathlib.Path] = {}
        for path in paths:
            if _DIGEST_PATTERN.fullmatch(path.name):
                live[path.name] = path
                continue
            publication_intent_match = _PUBLICATION_MANIFEST_PATTERN.fullmatch(
                path.name
            )
            if publication_intent_match is not None:
                publication_intents[publication_intent_match.group("digest")] = path
                continue
            publication_tree_match = _PUBLICATION_TREE_PATTERN.fullmatch(path.name)
            if publication_tree_match is not None:
                publication_staging[publication_tree_match.group("digest")] = path
                continue
            intent_match = _REMOVAL_MANIFEST_PATTERN.fullmatch(path.name)
            if intent_match is not None:
                intents[intent_match.group("digest")] = path
                continue
            retired_match = _REMOVAL_TREE_PATTERN.fullmatch(path.name)
            if retired_match is not None:
                retired[retired_match.group("digest")] = path
                continue
            raise _generation_error(
                f"unknown benchmark generation-store entry: {path}",
                code="benchmark_admin_layout_invalid",
            )
        publications: list[_PublicationState] = []
        removals: list[_RemovalState] = []
        ordinary_live: list[tuple[str, pathlib.Path]] = []
        all_digests = (
            set(live)
            | set(publication_intents)
            | set(publication_staging)
            | set(intents)
            | set(retired)
        )
        for digest in sorted(all_digests):
            live_path = live.get(digest)
            publication_intent = publication_intents.get(digest)
            staging_path = publication_staging.get(digest)
            intent = intents.get(digest)
            retired_path = retired.get(digest)
            if (publication_intent is not None or staging_path is not None) and (
                intent is not None or retired_path is not None
            ):
                raise _generation_error(
                    f"benchmark generation has publication and removal state: {digest}",
                    code="benchmark_admin_generation_invalid",
                )
            if staging_path is not None and publication_intent is None:
                raise _generation_error(
                    f"staged benchmark generation lacks publication intent: {digest}",
                    code="benchmark_admin_generation_invalid",
                )
            if (
                publication_intent is not None
                and live_path is not None
                and staging_path is not None
            ):
                raise _generation_error(
                    f"benchmark generation has live and staged publication trees: {digest}",
                    code="benchmark_admin_generation_invalid",
                )
            if publication_intent is not None:
                publications.append(
                    _PublicationState(
                        digest=digest,
                        intent=publication_intent,
                        live=live_path,
                        staging=staging_path,
                    )
                )
                continue
            if live_path is not None and retired_path is not None:
                raise _generation_error(
                    f"benchmark generation has both live and retired trees: {digest}",
                    code="benchmark_admin_generation_invalid",
                )
            if retired_path is not None and intent is None:
                raise _generation_error(
                    f"retired benchmark generation lacks its removal intent: {digest}",
                    code="benchmark_admin_generation_invalid",
                )
            if intent is None:
                if live_path is not None:
                    ordinary_live.append((digest, live_path))
                continue
            removals.append(
                _RemovalState(
                    digest=digest,
                    intent=intent,
                    live=live_path,
                    retired=retired_path,
                )
            )
        if len(ordinary_live) + len(publications) + len(removals) > MAX_GENERATIONS:
            raise _generation_error(
                "benchmark generation store exceeds its fixed generation limit",
                code="benchmark_admin_layout_invalid",
            )
        return _Inventory(
            live=tuple(ordinary_live),
            publications=tuple(publications),
            removals=tuple(removals),
        )

    def _read_publication_manifest(
        self,
        state: _PublicationState,
    ) -> tuple[_Manifest, os.stat_result]:
        return self._read_transaction_manifest(
            state.intent,
            digest=state.digest,
            expected=self._publication_intent_path(state.digest),
            transaction="publication",
        )

    def _read_transaction_manifest(
        self,
        path: pathlib.Path,
        *,
        digest: str,
        expected: pathlib.Path,
        transaction: str,
    ) -> tuple[_Manifest, os.stat_result]:
        try:
            metadata = os.lstat(path)
        except OSError as error:
            raise _generation_error(
                f"cannot inspect benchmark generation {transaction} intent: {error}",
                code="benchmark_admin_generation_invalid",
            ) from error
        if (
            path != expected
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != self.root_uid
            or metadata.st_gid != self.root_gid
            or _mode(metadata) != 0o444
            or metadata.st_size > MAX_MANIFEST_BYTES
        ):
            raise _generation_error(
                f"benchmark generation {transaction} intent is unsafe: {path}",
                code="benchmark_admin_generation_invalid",
            )
        payload = self._read_regular(
            path,
            maximum=MAX_MANIFEST_BYTES,
            expected_mode=0o444,
        )
        manifest = _parse_manifest_payload(payload)
        if manifest.digest != digest:
            raise _generation_error(
                f"benchmark generation {transaction} intent identity differs "
                "from its name",
                code="benchmark_admin_generation_invalid",
            )
        return manifest, metadata

    def _verify_publication_tree(
        self,
        state: _PublicationState,
        manifest: _Manifest,
        intent_metadata: os.stat_result,
    ) -> str:
        if state.staging is None:
            raise AssertionError("publication-tree verification requires a tree")
        staging = state.staging
        try:
            root_metadata = os.lstat(staging)
        except OSError as error:
            raise _generation_error(
                f"cannot inspect staged benchmark generation: {error}",
                code="benchmark_admin_generation_invalid",
            ) from error
        if (
            staging != self._publication_staging_path(state.digest)
            or not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != self.root_uid
            or root_metadata.st_gid != self.root_gid
            or _mode(root_metadata) not in {0o700, 0o555}
        ):
            raise _generation_error(
                f"staged benchmark generation root is unsafe: {staging}",
                code="benchmark_admin_generation_invalid",
            )

        expected_files = {entry.path.as_posix(): entry for entry in manifest.files}
        expected_directories = {path.as_posix() for path in _GENERATION_DIRECTORIES}
        observed_files: set[str] = set()
        observed_directories: dict[str, int] = {".": _mode(root_metadata)}
        pending = [staging]
        observed_nodes = 0
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(directory) as iterator:
                    children = tuple(iterator)
            except OSError as error:
                raise _generation_error(
                    f"cannot enumerate staged benchmark generation: {error}",
                    code="benchmark_admin_generation_invalid",
                ) from error
            for child in children:
                observed_nodes += 1
                if (
                    observed_nodes
                    > MAX_GENERATION_FILES + len(_GENERATION_DIRECTORIES) + 1
                ):
                    raise _generation_error(
                        "staged benchmark generation exceeds its fixed node limit",
                        code="benchmark_admin_generation_invalid",
                    )
                path = pathlib.Path(child.path)
                relative = path.relative_to(staging).as_posix()
                try:
                    metadata = os.lstat(path)
                except OSError as error:
                    raise _generation_error(
                        f"cannot inspect staged generation node: {error}",
                        code="benchmark_admin_generation_invalid",
                    ) from error
                if metadata.st_uid != self.root_uid or metadata.st_gid != self.root_gid:
                    raise _generation_error(
                        f"staged generation node has wrong owner: {relative}",
                        code="benchmark_admin_generation_invalid",
                    )
                if stat.S_ISDIR(metadata.st_mode):
                    if relative not in expected_directories or _mode(metadata) not in {
                        0o700,
                        0o555,
                    }:
                        raise _generation_error(
                            f"staged generation directory is unsafe: {relative}",
                            code="benchmark_admin_generation_invalid",
                        )
                    observed_directories[relative] = _mode(metadata)
                    pending.append(path)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise _generation_error(
                        f"staged generation contains a special node: {relative}",
                        code="benchmark_admin_generation_invalid",
                    )
                if relative == "manifest.json":
                    payload = self._read_regular(
                        path,
                        maximum=MAX_MANIFEST_BYTES,
                        expected_mode=0o444,
                    )
                    if payload != manifest.payload:
                        raise _generation_error(
                            "staged generation manifest bytes changed",
                            code="benchmark_admin_generation_invalid",
                        )
                else:
                    expected = expected_files.get(relative)
                    if expected is None:
                        raise _generation_error(
                            f"staged generation contains an unrecorded file: {relative}",
                            code="benchmark_admin_generation_invalid",
                        )
                    self._read_manifest_entry(staging, expected)
                observed_files.add(relative)

        missing_directory = False
        for relative in _GENERATION_DIRECTORIES:
            is_present = relative.as_posix() in observed_directories
            if not is_present:
                missing_directory = True
            elif missing_directory:
                raise _generation_error(
                    "staged generation directory construction is not a prefix",
                    code="benchmark_admin_generation_invalid",
                )
        directories_complete = len(observed_directories) == (
            len(_GENERATION_DIRECTORIES) + 1
        )
        manifest_present = "manifest.json" in observed_files
        payload_missing = False
        for expected in manifest.files:
            is_present = expected.path.as_posix() in observed_files
            if not is_present:
                payload_missing = True
            elif payload_missing:
                raise _generation_error(
                    "staged generation file construction is not a prefix",
                    code="benchmark_admin_generation_invalid",
                )
        payloads_complete = all(
            expected.path.as_posix() in observed_files for expected in manifest.files
        )
        if not directories_complete and observed_files:
            raise _generation_error(
                "staged generation contains files before its directory closure",
                code="benchmark_admin_generation_invalid",
            )
        if manifest_present and not payloads_complete:
            raise _generation_error(
                "staged generation published its manifest before all payloads",
                code="benchmark_admin_generation_invalid",
            )
        if not manifest_present:
            if intent_metadata.st_nlink != 1:
                raise _generation_error(
                    "unsealed publication intent has an unexpected hard link",
                    code="benchmark_admin_generation_invalid",
                )
            if any(mode != 0o700 for mode in observed_directories.values()):
                raise _generation_error(
                    "staged generation sealed directories before its manifest",
                    code="benchmark_admin_generation_invalid",
                )
            return "building"

        self._require_same_manifest_link(
            intent_metadata,
            staging / "manifest.json",
        )
        if not directories_complete:
            raise _generation_error(
                "staged generation manifest lacks its directory closure",
                code="benchmark_admin_generation_invalid",
            )
        observed_unsealed = False
        for relative in _PUBLICATION_SEAL_ORDER:
            mode = observed_directories[relative.as_posix()]
            if mode == 0o700:
                observed_unsealed = True
            elif observed_unsealed:
                raise _generation_error(
                    "staged generation directory sealing is not a prefix",
                    code="benchmark_admin_generation_invalid",
                )
        if all(mode == 0o555 for mode in observed_directories.values()):
            return "sealed"
        return "building"

    def _validate_publication_state(
        self,
        state: _PublicationState,
    ) -> _PublicationPlan:
        manifest, intent_metadata = self._read_publication_manifest(state)
        if state.live is not None:
            verified = self._verify_root(state.live)
            if verified.manifest != manifest.payload:
                raise _generation_error(
                    "generation publication intent differs from its live tree",
                    code="benchmark_admin_generation_invalid",
                )
            self._require_same_manifest_link(
                intent_metadata,
                state.live / "manifest.json",
            )
            return _PublicationPlan(
                state=state,
                manifest=manifest,
                phase="committed",
            )
        if state.staging is not None:
            phase = self._verify_publication_tree(
                state,
                manifest,
                intent_metadata,
            )
            return _PublicationPlan(state=state, manifest=manifest, phase=phase)
        if intent_metadata.st_nlink != 1:
            raise _generation_error(
                "prepared generation publication left an unexpected hard link",
                code="benchmark_admin_generation_invalid",
            )
        return _PublicationPlan(state=state, manifest=manifest, phase="prepared")

    def _start_publication_tree(self, plan: _PublicationPlan) -> _PublicationPlan:
        staging = self._publication_staging_path(plan.state.digest)
        self._mkdir_managed_directory(staging)
        self._fsync_directory(staging)
        self._fsync_directory(self.generation_directory)
        state = _PublicationState(
            digest=plan.state.digest,
            intent=plan.state.intent,
            live=None,
            staging=staging,
        )
        return self._validate_publication_state(state)

    def _build_publication_tree(
        self,
        plan: _PublicationPlan,
        generation: Generation,
    ) -> _PublicationPlan:
        if plan.manifest.payload != generation.manifest:
            raise _generation_error(
                "pending generation publication differs from requested bytes",
                code="benchmark_admin_generation_invalid",
            )
        if plan.phase == "prepared":
            plan = self._start_publication_tree(plan)
        if plan.phase != "building" or plan.state.staging is None:
            raise AssertionError("generation publication is not buildable")
        staging = plan.state.staging
        for relative in _GENERATION_DIRECTORIES:
            directory = staging / relative
            if os.path.lexists(directory):
                continue
            self._mkdir_managed_directory(directory)
            self._fsync_directory(directory)
            self._fsync_directory(directory.parent)
        for entry in generation.entries:
            output = staging / entry.path
            if os.path.lexists(output):
                continue
            self._write_new_file(output, entry.content, mode=entry.mode)
            self._fsync_directory(output.parent)
        tree_manifest = staging / "manifest.json"
        if not os.path.lexists(tree_manifest):
            os.link(plan.state.intent, tree_manifest, follow_symlinks=False)
            self._fsync_directory(staging)
        for relative in _PUBLICATION_SEAL_ORDER:
            directory = staging / relative
            if _mode(os.lstat(directory)) == 0o555:
                continue
            os.chmod(directory, 0o555)
            self._fsync_directory(directory)
        state = _PublicationState(
            digest=plan.state.digest,
            intent=plan.state.intent,
            live=None,
            staging=staging,
        )
        sealed = self._validate_publication_state(state)
        if sealed.phase != "sealed":
            raise AssertionError("completed generation publication is not sealed")
        return sealed

    def _finish_publication_intent(self, state: _PublicationState) -> None:
        manifest, intent_metadata = self._read_publication_manifest(state)
        if state.live is None:
            if intent_metadata.st_nlink != 1:
                raise _generation_error(
                    "publication intent cannot be discarded safely",
                    code="benchmark_admin_generation_invalid",
                )
        else:
            verified = self._verify_root(state.live)
            if verified.manifest != manifest.payload:
                raise _generation_error(
                    "committed publication differs from its intent",
                    code="benchmark_admin_generation_invalid",
                )
            self._require_same_manifest_link(
                intent_metadata,
                state.live / "manifest.json",
            )
        self.report(f"finishing benchmark generation publication {state.digest}")
        os.unlink(state.intent)
        self._fsync_directory(self.generation_directory)

    def _commit_publication(self, plan: _PublicationPlan) -> VerifiedGeneration:
        if plan.phase != "sealed" or plan.state.staging is None:
            raise AssertionError("only a sealed generation can be committed")
        destination = self._digest_path(plan.state.digest)
        if os.path.lexists(destination):
            raise _generation_error(
                "benchmark generation appeared during publication",
                code="benchmark_admin_generation_invalid",
            )
        os.rename(plan.state.staging, destination)
        self._fsync_directory(self.generation_directory)
        state = _PublicationState(
            digest=plan.state.digest,
            intent=plan.state.intent,
            live=destination,
            staging=None,
        )
        committed = self._validate_publication_state(state)
        if committed.phase != "committed":
            raise AssertionError("renamed generation publication is not committed")
        self._finish_publication_intent(state)
        return self.verify(plan.state.digest)

    def _recover_publication(
        self,
        plan: _PublicationPlan,
        *,
        generation: Generation | None,
    ) -> VerifiedGeneration | None:
        if generation is not None and generation.digest != plan.state.digest:
            raise AssertionError("publication recovery received the wrong generation")
        if plan.phase == "committed":
            if plan.state.live is None:
                raise AssertionError("committed publication lacks a live tree")
            digest = plan.state.digest
            self._finish_publication_intent(plan.state)
            return self.verify(digest)
        if plan.phase == "sealed":
            return self._commit_publication(plan)
        if generation is None:
            raise _generation_error(
                "incomplete benchmark generation publication requires its "
                f"original source bytes: {plan.state.digest}",
                code="benchmark_admin_generation_publication_pending",
            )
        sealed = self._build_publication_tree(plan, generation)
        return self._commit_publication(sealed)

    def _read_removal_manifest(
        self,
        state: _RemovalState,
    ) -> tuple[_Manifest, os.stat_result]:
        return self._read_transaction_manifest(
            state.intent,
            digest=state.digest,
            expected=self._removal_intent_path(state.digest),
            transaction="removal",
        )

    @staticmethod
    def _require_same_manifest_link(
        intent_metadata: os.stat_result,
        tree_manifest: pathlib.Path,
    ) -> None:
        try:
            tree_metadata = os.lstat(tree_manifest)
        except OSError as error:
            raise _generation_error(
                f"cannot inspect generation tree manifest link: {error}",
                code="benchmark_admin_generation_invalid",
            ) from error
        if (
            not stat.S_ISREG(tree_metadata.st_mode)
            or intent_metadata.st_dev != tree_metadata.st_dev
            or intent_metadata.st_ino != tree_metadata.st_ino
            or intent_metadata.st_nlink != 2
            or tree_metadata.st_nlink != 2
        ):
            raise _generation_error(
                "generation removal intent is not the exact tree manifest link",
                code="benchmark_admin_generation_invalid",
            )

    def _verify_retired_tree(
        self,
        state: _RemovalState,
        manifest: _Manifest,
        intent_metadata: os.stat_result,
    ) -> None:
        if state.retired is None:
            raise AssertionError("retired-tree verification requires a tree")
        retired_root = state.retired
        try:
            root_metadata = os.lstat(retired_root)
        except OSError as error:
            raise _generation_error(
                f"cannot inspect retired benchmark generation: {error}",
                code="benchmark_admin_generation_invalid",
            ) from error
        if (
            retired_root.parent != self.generation_directory
            or retired_root != self._retired_path(state.digest)
            or not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != self.root_uid
            or root_metadata.st_gid != self.root_gid
            or _mode(root_metadata) not in {0o555, 0o700}
        ):
            raise _generation_error(
                f"retired benchmark generation root is unsafe: {retired_root}",
                code="benchmark_admin_generation_invalid",
            )
        expected_files = {entry.path.as_posix(): entry for entry in manifest.files}
        expected_directories = {path.as_posix() for path in _GENERATION_DIRECTORIES}
        observed_files: set[str] = set()
        observed_directories: dict[str, int] = {".": _mode(root_metadata)}
        pending = [retired_root]
        observed_nodes = 0
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(directory) as iterator:
                    children = tuple(iterator)
            except OSError as error:
                raise _generation_error(
                    f"cannot enumerate retired benchmark generation: {error}",
                    code="benchmark_admin_generation_invalid",
                ) from error
            for child in children:
                observed_nodes += 1
                if (
                    observed_nodes
                    > MAX_GENERATION_FILES + len(_GENERATION_DIRECTORIES) + 1
                ):
                    raise _generation_error(
                        "retired benchmark generation exceeds its fixed node limit",
                        code="benchmark_admin_generation_invalid",
                    )
                path = pathlib.Path(child.path)
                relative = path.relative_to(retired_root).as_posix()
                try:
                    metadata = os.lstat(path)
                except OSError as error:
                    raise _generation_error(
                        f"cannot inspect retired generation node: {error}",
                        code="benchmark_admin_generation_invalid",
                    ) from error
                if metadata.st_uid != self.root_uid or metadata.st_gid != self.root_gid:
                    raise _generation_error(
                        f"retired generation node has wrong owner: {relative}",
                        code="benchmark_admin_generation_invalid",
                    )
                if stat.S_ISDIR(metadata.st_mode):
                    if relative not in expected_directories or _mode(metadata) not in {
                        0o555,
                        0o700,
                    }:
                        raise _generation_error(
                            f"retired generation directory is unsafe: {relative}",
                            code="benchmark_admin_generation_invalid",
                        )
                    observed_directories[relative] = _mode(metadata)
                    pending.append(path)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise _generation_error(
                        f"retired generation contains a special node: {relative}",
                        code="benchmark_admin_generation_invalid",
                    )
                if relative == "manifest.json":
                    payload = self._read_regular(
                        path,
                        maximum=MAX_MANIFEST_BYTES,
                        expected_mode=0o444,
                    )
                    if payload != manifest.payload:
                        raise _generation_error(
                            "retired generation manifest bytes changed",
                            code="benchmark_admin_generation_invalid",
                        )
                else:
                    expected = expected_files.get(relative)
                    if expected is None:
                        raise _generation_error(
                            f"retired generation contains an unrecorded file: {relative}",
                            code="benchmark_admin_generation_invalid",
                        )
                    self._read_manifest_entry(retired_root, expected)
                observed_files.add(relative)

        ordered_paths = [entry.path.as_posix() for entry in manifest.files]
        present = [path in observed_files for path in ordered_paths]
        observed_present = False
        for is_present in present:
            if is_present:
                observed_present = True
            elif observed_present:
                raise _generation_error(
                    "retired generation deletion progress is not a prefix",
                    code="benchmark_admin_generation_invalid",
                )
        payloads_all_present = all(present)
        payloads_all_missing = not any(present)
        manifest_present = "manifest.json" in observed_files
        all_directories = {".", *expected_directories}

        if not payloads_all_present and observed_directories.keys() != all_directories:
            if not payloads_all_missing or manifest_present:
                raise _generation_error(
                    "retired generation removed directories before its files",
                    code="benchmark_admin_generation_invalid",
                )
        if not manifest_present and not payloads_all_missing:
            raise _generation_error(
                "retired generation removed its manifest before payload files",
                code="benchmark_admin_generation_invalid",
            )
        if payloads_all_present or manifest_present:
            if observed_directories.keys() != all_directories:
                raise _generation_error(
                    "retired generation directory closure is incomplete",
                    code="benchmark_admin_generation_invalid",
                )
        else:
            directory_presence = [
                directory.as_posix() in observed_directories
                for directory in _DIRECTORY_REMOVAL_ORDER
            ]
            observed_directory = False
            for is_present in directory_presence:
                if is_present:
                    observed_directory = True
                elif observed_directory:
                    raise _generation_error(
                        "retired generation directory deletion is not a prefix",
                        code="benchmark_admin_generation_invalid",
                    )
        if not payloads_all_present and any(
            mode != 0o700 for mode in observed_directories.values()
        ):
            raise _generation_error(
                "retired generation began file deletion before writable preparation",
                code="benchmark_admin_generation_invalid",
            )
        if manifest_present:
            self._require_same_manifest_link(
                intent_metadata,
                retired_root / "manifest.json",
            )
        elif intent_metadata.st_nlink != 1:
            raise _generation_error(
                "completed tree-manifest removal left an unexpected hard link",
                code="benchmark_admin_generation_invalid",
            )

    def _validate_removal_state(
        self,
        state: _RemovalState,
        *,
        protected_digest: str | None,
    ) -> _RemovalPlan:
        if state.digest == protected_digest:
            raise _generation_error(
                f"refusing to retire selected benchmark generation {state.digest}",
                code="benchmark_admin_generation_removal_conflict",
            )
        manifest, intent_metadata = self._read_removal_manifest(state)
        if state.live is not None:
            verified = self._verify_root(state.live)
            if verified.manifest != manifest.payload:
                raise _generation_error(
                    "generation removal intent differs from its live tree",
                    code="benchmark_admin_generation_invalid",
                )
            self._require_same_manifest_link(
                intent_metadata,
                state.live / "manifest.json",
            )
            return _RemovalPlan(state=state, manifest=manifest, phase="prepared")
        if state.retired is not None:
            self._verify_retired_tree(state, manifest, intent_metadata)
            return _RemovalPlan(state=state, manifest=manifest, phase="retired")
        if intent_metadata.st_nlink != 1:
            raise _generation_error(
                "completed generation removal left an unexpected hard link",
                code="benchmark_admin_generation_invalid",
            )
        return _RemovalPlan(state=state, manifest=manifest, phase="committed")

    def _validated_inventory(
        self,
        *,
        protected_digest: str | None,
    ) -> tuple[
        _Inventory,
        tuple[VerifiedGeneration, ...],
        tuple[_PublicationPlan, ...],
        tuple[_RemovalPlan, ...],
    ]:
        if protected_digest is not None and not _DIGEST_PATTERN.fullmatch(
            protected_digest
        ):
            raise _generation_error(
                "protected benchmark generation digest is invalid",
                code="benchmark_admin_generation_invalid",
            )
        inventory = self._inventory()
        verified_live = tuple(
            self._verify_root(path) for _digest, path in inventory.live
        )
        publication_plans = tuple(
            self._validate_publication_state(state) for state in inventory.publications
        )
        plans = tuple(
            self._validate_removal_state(
                state,
                protected_digest=protected_digest,
            )
            for state in inventory.removals
        )
        return inventory, verified_live, publication_plans, plans

    def require_quiescent(self) -> tuple[VerifiedGeneration, ...]:
        """Verify all generations and require no publication or retirement."""

        (
            _inventory,
            verified,
            publication_plans,
            removal_plans,
        ) = self._validated_inventory(protected_digest=None)
        if publication_plans:
            digests = ", ".join(plan.state.digest for plan in publication_plans)
            raise _generation_error(
                f"benchmark generation publications require recovery: {digests}",
                code="benchmark_admin_generation_publication_pending",
            )
        if removal_plans:
            digests = ", ".join(plan.state.digest for plan in removal_plans)
            raise _generation_error(
                f"benchmark generation removals require recovery: {digests}",
                code="benchmark_admin_generation_removal_pending",
            )
        return verified

    def inventory_digests(
        self,
        *,
        protected_digest: str | None = None,
    ) -> tuple[str, ...]:
        """Validate the store and return every live or transacting digest."""

        inventory, _verified, publication_plans, removal_plans = (
            self._validated_inventory(protected_digest=protected_digest)
        )
        digests = {digest for digest, _path in inventory.live}
        digests.update(plan.state.digest for plan in publication_plans)
        digests.update(plan.state.digest for plan in removal_plans)
        return tuple(sorted(digests))

    def _publish_removal_intent(
        self,
        verified: VerifiedGeneration,
    ) -> _RemovalState:
        intent = self._removal_intent_path(verified.digest)
        retired = self._retired_path(verified.digest)
        if os.path.lexists(intent) or os.path.lexists(retired):
            raise _generation_error(
                f"benchmark generation removal state already exists: {verified.digest}",
                code="benchmark_admin_generation_invalid",
            )
        os.link(
            verified.root / "manifest.json",
            intent,
            follow_symlinks=False,
        )
        state = _RemovalState(
            digest=verified.digest,
            intent=intent,
            live=verified.root,
            retired=None,
        )
        manifest, intent_metadata = self._read_removal_manifest(state)
        if manifest.payload != verified.manifest:
            raise _generation_error(
                "new generation removal intent differs from its source manifest",
                code="benchmark_admin_generation_invalid",
            )
        self._require_same_manifest_link(
            intent_metadata,
            verified.root / "manifest.json",
        )
        self._fsync_directory(self.generation_directory)
        return state

    def _retire_prepared(self, plan: _RemovalPlan) -> _RemovalPlan:
        if plan.state.live is None:
            raise AssertionError("prepared removal must retain its live path")
        retired = self._retired_path(plan.state.digest)
        os.rename(plan.state.live, retired)
        self._fsync_directory(self.generation_directory)
        state = _RemovalState(
            digest=plan.state.digest,
            intent=plan.state.intent,
            live=None,
            retired=retired,
        )
        return self._validate_removal_state(state, protected_digest=None)

    def _prepare_retired_directories(self, state: _RemovalState) -> None:
        if state.retired is None:
            raise AssertionError("directory preparation requires a retired tree")
        for relative in (*_GENERATION_DIRECTORIES, pathlib.PurePosixPath(".")):
            directory = state.retired / relative
            if not os.path.lexists(directory):
                continue
            os.chmod(directory, 0o700)
            self._fsync_directory(directory)

    def _finish_intent(self, state: _RemovalState) -> None:
        manifest, metadata = self._read_removal_manifest(state)
        if manifest.digest != state.digest or metadata.st_nlink != 1:
            raise _generation_error(
                "generation removal intent cannot be committed safely",
                code="benchmark_admin_generation_invalid",
            )
        self.report(f"removing benchmark generation intent {state.intent}")
        os.unlink(state.intent)
        self._fsync_directory(self.generation_directory)

    def _purge_retired(self, plan: _RemovalPlan) -> None:
        state = plan.state
        if state.retired is None:
            raise AssertionError("retired purge requires a retired tree")
        _manifest, intent_metadata = self._read_removal_manifest(state)
        self._verify_retired_tree(state, plan.manifest, intent_metadata)
        self._prepare_retired_directories(state)
        _manifest, intent_metadata = self._read_removal_manifest(state)
        self._verify_retired_tree(state, plan.manifest, intent_metadata)

        for expected in plan.manifest.files:
            path = state.retired / expected.path
            if not os.path.lexists(path):
                continue
            self._read_manifest_entry(state.retired, expected)
            self.report(f"removing managed benchmark file {path}")
            os.unlink(path)
            self._fsync_directory(path.parent)

        tree_manifest = state.retired / "manifest.json"
        if os.path.lexists(tree_manifest):
            _manifest, intent_metadata = self._read_removal_manifest(state)
            self._require_same_manifest_link(intent_metadata, tree_manifest)
            payload = self._read_regular(
                tree_manifest,
                maximum=MAX_MANIFEST_BYTES,
                expected_mode=0o444,
            )
            if payload != plan.manifest.payload:
                raise _generation_error(
                    "retired generation manifest bytes changed",
                    code="benchmark_admin_generation_invalid",
                )
            self.report(f"removing managed benchmark file {tree_manifest}")
            os.unlink(tree_manifest)
            self._fsync_directory(state.retired)

        _manifest, intent_metadata = self._read_removal_manifest(state)
        self._verify_retired_tree(state, plan.manifest, intent_metadata)
        for relative in _DIRECTORY_REMOVAL_ORDER:
            directory = state.retired / relative
            if not os.path.lexists(directory):
                continue
            os.rmdir(directory)
            self._fsync_directory(directory.parent)
        os.rmdir(state.retired)
        self._fsync_directory(self.generation_directory)
        completed = _RemovalState(
            digest=state.digest,
            intent=state.intent,
            live=None,
            retired=None,
        )
        self._finish_intent(completed)

    def _resume_plan(self, plan: _RemovalPlan) -> None:
        if plan.phase == "prepared":
            plan = self._retire_prepared(plan)
        if plan.phase == "retired":
            self._purge_retired(plan)
            return
        if plan.phase == "committed":
            self._finish_intent(plan.state)
            return
        raise AssertionError(f"unknown generation removal phase {plan.phase!r}")

    def recover_removals(
        self,
        *,
        protected_digest: str | None = None,
    ) -> tuple[str, ...]:
        """Validate every store entry, then resume all durable removals."""

        _inventory, _verified, publication_plans, plans = self._validated_inventory(
            protected_digest=protected_digest
        )
        if publication_plans:
            digests = ", ".join(plan.state.digest for plan in publication_plans)
            raise _generation_error(
                f"benchmark generation publications require recovery: {digests}",
                code="benchmark_admin_generation_publication_pending",
            )
        for plan in plans:
            self._resume_plan(plan)
        self._fsync_directory(self.generation_directory)
        return tuple(plan.state.digest for plan in plans)

    def remove(
        self,
        digest: str,
        *,
        protected_digest: str | None = None,
    ) -> bool:
        """Idempotently retire one digest after validating the complete store."""

        self._digest_path(digest)
        if digest == protected_digest:
            raise _generation_error(
                f"refusing to retire selected benchmark generation {digest}",
                code="benchmark_admin_generation_removal_conflict",
            )
        inventory, verified_live, publication_plans, plans = self._validated_inventory(
            protected_digest=protected_digest
        )
        if publication_plans:
            digests = ", ".join(plan.state.digest for plan in publication_plans)
            raise _generation_error(
                f"benchmark generation publications require recovery: {digests}",
                code="benchmark_admin_generation_publication_pending",
            )
        target_was_pending = any(plan.state.digest == digest for plan in plans)
        for plan in plans:
            self._resume_plan(plan)
        if target_was_pending:
            return True
        verified_by_digest = {verified.digest: verified for verified in verified_live}
        verified = verified_by_digest.get(digest)
        if verified is None:
            return False
        if not any(live_digest == digest for live_digest, _path in inventory.live):
            raise AssertionError("verified generation is absent from inventory")
        state = self._publish_removal_intent(verified)
        manifest, _metadata = self._read_removal_manifest(state)
        plan = _RemovalPlan(state=state, manifest=manifest, phase="prepared")
        self._resume_plan(plan)
        return True
