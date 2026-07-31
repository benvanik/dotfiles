"""Canonical benchmarkd generation format and source-closure construction.

This module owns the cold immutable identity contract consumed by the
transactional generation store. Source construction reads one repository
checkout; digesting, in-memory validation, and manifest parsing are otherwise
independent of the root-owned installed store.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import re
import stat
from collections.abc import Sequence
from typing import NoReturn

from .errors import BenchmarkLockError


GENERATION_MANIFEST_SCHEMA = "benchmarkd.generation.v1"
MAX_SOURCE_FILE_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_GENERATION_FILES = 128

BROKER_RUNTIME_MODULE_NAMES = (
    "__init__.py",
    "administration_state.py",
    "broker.py",
    "configuration.py",
    "daemon.py",
    "errors.py",
    "fdstore.py",
    "linux.py",
    "policy.py",
    "protocol.py",
    "scheduler.py",
)

DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
GENERATION_DIRECTORIES = (
    pathlib.PurePosixPath("bin"),
    pathlib.PurePosixPath("lib"),
    pathlib.PurePosixPath("lib/benchmark_lock"),
    pathlib.PurePosixPath("share"),
    pathlib.PurePosixPath("share/systemd"),
    pathlib.PurePosixPath("share/sysusers"),
)

_MANIFEST_FIELDS = frozenset({"digest", "files", "schema"})
_MANIFEST_FILE_FIELDS = frozenset({"mode", "path", "sha256", "size"})


@dataclasses.dataclass(frozen=True)
class GenerationEntry:
    """One byte-exact regular file in an immutable generation."""

    path: pathlib.PurePosixPath
    content: bytes
    mode: int


@dataclasses.dataclass(frozen=True)
class Generation:
    """One complete in-memory generation ready for publication."""

    digest: str
    entries: tuple[GenerationEntry, ...]
    manifest: bytes


@dataclasses.dataclass(frozen=True)
class GenerationManifestFile:
    """One expected file identity parsed from a canonical manifest."""

    path: pathlib.PurePosixPath
    mode: int
    size: int
    sha256: str


@dataclasses.dataclass(frozen=True)
class GenerationManifest:
    """The parsed and byte-exact canonical manifest for one generation."""

    digest: str
    files: tuple[GenerationManifestFile, ...]
    payload: bytes


def _generation_error(message: str, *, code: str) -> BenchmarkLockError:
    return BenchmarkLockError(message, code=code)


def _canonical_json(document: object) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _strict_json(payload: bytes, *, description: str, maximum: int) -> object:
    if not payload or len(payload) > maximum:
        raise _generation_error(
            f"{description} is empty or exceeds {maximum} bytes",
            code="benchmark_admin_generation_invalid",
        )
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as error:
        raise _generation_error(
            f"{description} is not ASCII JSON",
            code="benchmark_admin_generation_invalid",
        ) from error

    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate object key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> NoReturn:
        raise ValueError(f"non-finite number {value!r}")

    try:
        return json.loads(
            text,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except ValueError as error:
        raise _generation_error(
            f"{description} is not strict JSON: {error}",
            code="benchmark_admin_generation_invalid",
        ) from error


def _require_exact_fields(
    value: dict[str, object],
    expected: frozenset[str],
    *,
    description: str,
) -> None:
    observed = frozenset(value)
    if observed == expected:
        return
    missing = sorted(expected - observed)
    unknown = sorted(observed - expected)
    details: list[str] = []
    if missing:
        details.append("missing " + ", ".join(missing))
    if unknown:
        details.append("unknown " + ", ".join(unknown))
    raise _generation_error(
        f"{description} has invalid fields ({'; '.join(details)})",
        code="benchmark_admin_generation_invalid",
    )


def _read_source_file(path: pathlib.Path) -> bytes:
    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise _generation_error(
            f"cannot inspect installation source {path}: {error}",
            code="benchmark_admin_source_invalid",
        ) from error
    if not stat.S_ISREG(metadata.st_mode):
        raise _generation_error(
            f"installation source is not a regular file: {path}",
            code="benchmark_admin_source_invalid",
        )
    if metadata.st_size > MAX_SOURCE_FILE_BYTES:
        raise _generation_error(
            f"installation source is too large: {path}",
            code="benchmark_admin_source_invalid",
        )
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            content = os.read(descriptor, MAX_SOURCE_FILE_BYTES + 1)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise _generation_error(
            f"cannot read installation source {path}: {error}",
            code="benchmark_admin_source_invalid",
        ) from error
    if len(content) != metadata.st_size:
        raise _generation_error(
            f"installation source changed while being read: {path}",
            code="benchmark_admin_source_invalid",
        )
    return content


def generation_digest(entries: Sequence[GenerationEntry]) -> str:
    """Return the generation-v1 digest for sorted canonical entries."""

    digest = hashlib.sha256()
    digest.update(b"benchmarkd-generation-v1\0")
    for entry in entries:
        encoded_path = entry.path.as_posix().encode("ascii")
        digest.update(len(encoded_path).to_bytes(4, "big"))
        digest.update(encoded_path)
        digest.update(entry.mode.to_bytes(4, "big"))
        digest.update(len(entry.content).to_bytes(8, "big"))
        digest.update(entry.content)
    return digest.hexdigest()


def _manifest_payload(
    digest: str,
    entries: Sequence[GenerationEntry],
) -> bytes:
    return _canonical_json(
        {
            "digest": digest,
            "files": [
                {
                    "mode": entry.mode,
                    "path": entry.path.as_posix(),
                    "sha256": hashlib.sha256(entry.content).hexdigest(),
                    "size": len(entry.content),
                }
                for entry in entries
            ],
            "schema": GENERATION_MANIFEST_SCHEMA,
        }
    )


def _safe_relative_file(value: object) -> pathlib.PurePosixPath:
    if not isinstance(value, str):
        raise _generation_error(
            "generation manifest path is not a string",
            code="benchmark_admin_generation_invalid",
        )
    path = pathlib.PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.parts[0] not in {"bin", "lib", "share"}
    ):
        raise _generation_error(
            "generation manifest contains an unsafe path",
            code="benchmark_admin_generation_invalid",
        )
    return path


def parse_generation_manifest(payload: bytes) -> GenerationManifest:
    """Parse and require the canonical generation-v1 manifest encoding."""

    document = _strict_json(
        payload,
        description="benchmark generation manifest",
        maximum=MAX_MANIFEST_BYTES,
    )
    if not isinstance(document, dict):
        raise _generation_error(
            "benchmark generation manifest is not an object",
            code="benchmark_admin_generation_invalid",
        )
    _require_exact_fields(
        document,
        _MANIFEST_FIELDS,
        description="benchmark generation manifest",
    )
    if document["schema"] != GENERATION_MANIFEST_SCHEMA:
        raise _generation_error(
            "benchmark generation manifest schema is unsupported",
            code="benchmark_admin_generation_invalid",
        )
    digest = document["digest"]
    raw_files = document["files"]
    if (
        not isinstance(digest, str)
        or not DIGEST_PATTERN.fullmatch(digest)
        or not isinstance(raw_files, list)
        or not raw_files
        or len(raw_files) > MAX_GENERATION_FILES
    ):
        raise _generation_error(
            "benchmark generation manifest identity or file list is invalid",
            code="benchmark_admin_generation_invalid",
        )
    files: list[GenerationManifestFile] = []
    observed_paths: set[pathlib.PurePosixPath] = set()
    for raw_file in raw_files:
        if not isinstance(raw_file, dict):
            raise _generation_error(
                "benchmark generation manifest file is not an object",
                code="benchmark_admin_generation_invalid",
            )
        _require_exact_fields(
            raw_file,
            _MANIFEST_FILE_FIELDS,
            description="benchmark generation manifest file",
        )
        relative = _safe_relative_file(raw_file["path"])
        if relative in observed_paths:
            raise _generation_error(
                "benchmark generation manifest repeats a file path",
                code="benchmark_admin_generation_invalid",
            )
        observed_paths.add(relative)
        mode = raw_file["mode"]
        size = raw_file["size"]
        expected_hash = raw_file["sha256"]
        if (
            isinstance(mode, bool)
            or mode not in {0o444, 0o555}
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 0 <= size <= MAX_SOURCE_FILE_BYTES
            or not isinstance(expected_hash, str)
            or not DIGEST_PATTERN.fullmatch(expected_hash)
        ):
            raise _generation_error(
                "benchmark generation manifest file metadata is invalid",
                code="benchmark_admin_generation_invalid",
            )
        files.append(
            GenerationManifestFile(
                path=relative,
                mode=mode,
                size=size,
                sha256=expected_hash,
            )
        )
    files.sort(key=lambda entry: entry.path.as_posix())
    normalized = tuple(files)
    expected_payload = _canonical_json(
        {
            "digest": digest,
            "files": [
                {
                    "mode": entry.mode,
                    "path": entry.path.as_posix(),
                    "sha256": entry.sha256,
                    "size": entry.size,
                }
                for entry in normalized
            ],
            "schema": GENERATION_MANIFEST_SCHEMA,
        }
    )
    if payload != expected_payload:
        raise _generation_error(
            "benchmark generation manifest is not canonical",
            code="benchmark_admin_generation_invalid",
        )
    return GenerationManifest(digest=digest, files=normalized, payload=payload)


def validate_generation(generation: Generation) -> Generation:
    """Require an in-memory generation to match its canonical identity."""

    if (
        not DIGEST_PATTERN.fullmatch(generation.digest)
        or not generation.entries
        or len(generation.entries) > MAX_GENERATION_FILES
    ):
        raise _generation_error(
            "in-memory benchmark generation identity is invalid",
            code="benchmark_admin_source_invalid",
        )
    entries = tuple(sorted(generation.entries, key=lambda entry: entry.path.as_posix()))
    observed: set[pathlib.PurePosixPath] = set()
    for entry in entries:
        if (
            _safe_relative_file(entry.path.as_posix()) != entry.path
            or entry.path in observed
            or entry.mode not in {0o444, 0o555}
            or not isinstance(entry.content, bytes)
            or len(entry.content) > MAX_SOURCE_FILE_BYTES
        ):
            raise _generation_error(
                "in-memory benchmark generation entry is invalid",
                code="benchmark_admin_source_invalid",
            )
        observed.add(entry.path)
    digest = generation_digest(entries)
    manifest = _manifest_payload(digest, entries)
    if (
        entries != generation.entries
        or digest != generation.digest
        or manifest != generation.manifest
    ):
        raise _generation_error(
            "in-memory benchmark generation is not canonical",
            code="benchmark_admin_source_invalid",
        )
    return generation


def build_generation(source_root: pathlib.Path) -> Generation:
    """Build the exact generation-v1 closure from one repository checkout."""

    source_root = pathlib.Path(source_root)
    if not source_root.is_absolute():
        raise ValueError("benchmark source root must be absolute")
    package_root = source_root / "lib/benchmark_lock"
    deployment_root = source_root / "benchmarkd"
    try:
        source_names = tuple(sorted(path.name for path in package_root.iterdir()))
    except OSError as error:
        raise _generation_error(
            f"cannot enumerate benchmark package sources: {error}",
            code="benchmark_admin_source_invalid",
        ) from error
    missing_names = frozenset(BROKER_RUNTIME_MODULE_NAMES) - frozenset(source_names)
    if missing_names:
        missing = ", ".join(sorted(missing_names))
        raise _generation_error(
            f"benchmark broker runtime is incomplete; missing {missing}",
            code="benchmark_admin_source_invalid",
        )
    entries: list[GenerationEntry] = []
    for name in BROKER_RUNTIME_MODULE_NAMES:
        entries.append(
            GenerationEntry(
                path=pathlib.PurePosixPath("lib/benchmark_lock") / name,
                content=_read_source_file(package_root / name),
                mode=0o444,
            )
        )
    entries.append(
        GenerationEntry(
            path=pathlib.PurePosixPath("bin/benchmarkd"),
            content=_read_source_file(deployment_root / "bin/benchmarkd"),
            mode=0o555,
        )
    )
    for source, destination in (
        (
            deployment_root / "systemd/benchmarkd.socket",
            pathlib.PurePosixPath("share/systemd/benchmarkd.socket"),
        ),
        (
            deployment_root / "systemd/benchmarkd.service",
            pathlib.PurePosixPath("share/systemd/benchmarkd.service"),
        ),
        (
            deployment_root / "sysusers/benchmarkd.conf",
            pathlib.PurePosixPath("share/sysusers/benchmarkd.conf"),
        ),
    ):
        entries.append(
            GenerationEntry(
                path=destination,
                content=_read_source_file(source),
                mode=0o444,
            )
        )
    entries.sort(key=lambda entry: entry.path.as_posix())
    if len(entries) > MAX_GENERATION_FILES:
        raise _generation_error(
            "benchmark generation exceeds its fixed file-count limit",
            code="benchmark_admin_source_invalid",
        )
    normalized = tuple(entries)
    digest = generation_digest(normalized)
    return Generation(
        digest=digest,
        entries=normalized,
        manifest=_manifest_payload(digest, normalized),
    )
