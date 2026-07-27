"""Versioned binary representation for model-session checkpoints.

Version 1 is a deterministic uncompressed stream:

* one fixed header declares entry and aggregate byte counts;
* strict path-ordered records carry a component-framed UTF-8 path, optional
  sparse extents, payload, and per-entry SHA-256 payload digest;
* one fixed footer authenticates every preceding byte and its exact length.

This module validates the complete stream and produces an immutable hydration
plan. Descriptor-tree discovery and filesystem mutation live in
``checkpoint_tree``.
"""

from __future__ import annotations

import hashlib
import os
import stat
import struct
import unicodedata
from dataclasses import dataclass
from typing import Final

from .errors import ModelSessionError


CHECKPOINT_SCHEMA = "model-session.checkpoint.v1"

_PACK_MAGIC: Final = b"MSCPK1\0\0"
_FOOTER_MAGIC: Final = b"MSCFTR1\0"
_FORMAT_VERSION: Final = 1
_FORMAT_FLAGS: Final = 0
_NO_LINK: Final = (1 << 64) - 1
_UINT16_MAX: Final = (1 << 16) - 1
_UINT32_MAX: Final = (1 << 32) - 1
_UINT64_MAX: Final = (1 << 64) - 1
_MAX_IMPLEMENTATION_DEPTH: Final = 256

_ENTRY_DIRECTORY: Final = 1
_ENTRY_REGULAR: Final = 2
_ENTRY_SYMLINK: Final = 3
_ENTRY_HARDLINK: Final = 4
_ENTRY_FLAG_SPARSE: Final = 1

_ROOT_WORK: Final = 0
_ROOT_SESSIONS: Final = 1
_ROOT_NAMES: Final = {
    _ROOT_WORK: "work",
    _ROOT_SESSIONS: "sessions",
}

_HEADER = struct.Struct("<8sHHIQQQ")
_RECORD = struct.Struct("<BBHHHIIQQQ")
_EXTENT = struct.Struct("<QQ")
_FOOTER = struct.Struct("<8sQ32s")
_EMPTY_DIGEST: Final = hashlib.sha256(b"").digest()


@dataclass(frozen=True)
class CheckpointLimits:
    """Explicit resource envelope for encoding, validation, and hydration."""

    max_entries: int = 100_000
    max_depth: int = 64
    max_component_bytes: int = 255
    max_path_bytes: int = 4096
    max_file_logical_bytes: int = 16 * 1024**3
    max_symlink_bytes: int = 64 * 1024
    max_logical_bytes: int = 64 * 1024**3
    max_payload_bytes: int = 64 * 1024**3
    max_pack_bytes: int = 65 * 1024**3
    max_sparse_extents_per_file: int = 1_000_000
    max_sparse_extents: int = 1_000_000
    io_chunk_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        integer_fields = (
            "max_entries",
            "max_depth",
            "max_component_bytes",
            "max_path_bytes",
            "max_file_logical_bytes",
            "max_symlink_bytes",
            "max_logical_bytes",
            "max_payload_bytes",
            "max_pack_bytes",
            "max_sparse_extents_per_file",
            "max_sparse_extents",
            "io_chunk_bytes",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_entries < 2:
            raise ValueError("max_entries must admit both logical roots")
        if self.max_entries > _UINT64_MAX:
            raise ValueError("max_entries exceeds the checkpoint representation")
        if self.max_depth > _MAX_IMPLEMENTATION_DEPTH:
            raise ValueError(
                "max_depth exceeds the bounded recursive implementation"
            )
        if self.max_component_bytes > _UINT16_MAX:
            raise ValueError(
                "max_component_bytes exceeds the checkpoint representation"
            )
        if self.max_path_bytes > _UINT32_MAX:
            raise ValueError("max_path_bytes exceeds the checkpoint representation")
        if self.max_sparse_extents_per_file > _UINT32_MAX:
            raise ValueError(
                "max_sparse_extents_per_file exceeds the checkpoint representation"
            )
        if self.max_sparse_extents > _UINT64_MAX:
            raise ValueError(
                "max_sparse_extents exceeds the checkpoint representation"
            )
        if self.io_chunk_bytes > _UINT32_MAX:
            raise ValueError("io_chunk_bytes exceeds the checkpoint representation")
        for name in (
            "max_file_logical_bytes",
            "max_symlink_bytes",
            "max_logical_bytes",
            "max_payload_bytes",
            "max_pack_bytes",
        ):
            if getattr(self, name) > _UINT64_MAX:
                raise ValueError(f"{name} exceeds the checkpoint representation")


DEFAULT_CHECKPOINT_LIMITS = CheckpointLimits()


def _maximum_encoded_path_bytes(limits: CheckpointLimits) -> int:
    maximum = 0
    feasible_depth = min(
        limits.max_depth,
        (limits.max_path_bytes + 1) // 2,
    )
    for depth in range(1, feasible_depth + 1):
        component_bytes = min(
            depth * limits.max_component_bytes,
            limits.max_path_bytes - depth + 1,
        )
        maximum = max(
            maximum,
            min(_UINT32_MAX, component_bytes + 2 * depth),
        )
    return maximum


def maximum_encoded_bytes(limits: CheckpointLimits) -> int:
    """Return a safe exact-arithmetic upper bound for a v1 pack.

    The bound charges the two mandatory zero-path roots, the maximum encoded
    path for every other entry, every fixed record and payload digest, the
    aggregate sparse-extent budget, and aggregate payload bytes. It deliberately
    ignores ``max_pack_bytes``: callers compare their storage budget with this
    representation bound, while the codec independently enforces the actual
    pack ceiling during I/O.
    """

    nonroot_entries = limits.max_entries - 2
    return (
        _HEADER.size
        + _FOOTER.size
        + limits.max_entries * (_RECORD.size + len(_EMPTY_DIGEST))
        + nonroot_entries * _maximum_encoded_path_bytes(limits)
        + limits.max_sparse_extents * _EXTENT.size
        + limits.max_payload_bytes
    )


@dataclass(frozen=True)
class CheckpointSummary:
    schema: str
    entry_count: int
    logical_bytes: int
    payload_bytes: int
    pack_bytes: int
    pack_sha256: str


@dataclass(frozen=True)
class DecodedEntry:
    root: int
    components: tuple[str, ...]
    component_key: tuple[bytes, ...]
    entry_type: int
    mode: int
    logical_size: int
    payload_size: int
    extents: tuple[tuple[int, int], ...]
    payload_offset: int
    payload_digest: bytes
    symlink_target: str | None
    hardlink_index: int | None


@dataclass(frozen=True)
class ValidatedCheckpoint:
    summary: CheckpointSummary
    entries: tuple[DecodedEntry, ...]
    source_metadata: tuple[int, ...]


def _fail(message: str, *, code: str = "invalid_checkpoint") -> None:
    raise ModelSessionError(message, code=code)


def _safe_component(component: str) -> str:
    rendered = ascii(component)
    if len(rendered) <= 160:
        return rendered
    return rendered[:156] + "...'"


def _pack_metadata(metadata: os.stat_result) -> tuple[int, ...]:
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


def _canonical_component(
    component: str,
    *,
    limits: CheckpointLimits,
) -> tuple[str, bytes]:
    if (
        not isinstance(component, str)
        or not component
        or component in {".", ".."}
        or "/" in component
        or "\x00" in component
    ):
        _fail(
            "checkpoint path contains an invalid component",
            code="invalid_checkpoint_path",
        )
    if unicodedata.normalize("NFC", component) != component:
        _fail(
            f"checkpoint path component is not NFC: {_safe_component(component)}",
            code="invalid_checkpoint_path",
        )
    try:
        encoded = component.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ModelSessionError(
            "checkpoint path component is not canonical UTF-8: "
            f"{_safe_component(component)}",
            code="invalid_checkpoint_path",
        ) from error
    if len(encoded) > limits.max_component_bytes:
        _fail(
            f"checkpoint path component exceeds {limits.max_component_bytes} bytes",
            code="checkpoint_limit_exceeded",
        )
    return component, encoded


def _encode_path(
    components: tuple[str, ...],
    *,
    limits: CheckpointLimits,
) -> tuple[bytes, tuple[bytes, ...]]:
    if len(components) > limits.max_depth:
        _fail(
            f"checkpoint path exceeds depth {limits.max_depth}",
            code="checkpoint_limit_exceeded",
        )
    encoded_components = tuple(
        _canonical_component(component, limits=limits)[1]
        for component in components
    )
    path_bytes = sum(len(component) for component in encoded_components)
    if encoded_components:
        path_bytes += len(encoded_components) - 1
    if path_bytes > limits.max_path_bytes:
        _fail(
            f"checkpoint path exceeds {limits.max_path_bytes} bytes",
            code="checkpoint_limit_exceeded",
        )
    representation = b"".join(
        struct.pack("<H", len(component)) + component
        for component in encoded_components
    )
    if len(representation) > _UINT32_MAX:
        _fail(
            "checkpoint encoded path exceeds the format",
            code="checkpoint_limit_exceeded",
        )
    return representation, encoded_components


def _canonical_symlink_target(
    target: str,
    *,
    limits: CheckpointLimits,
) -> bytes:
    if not target or "\x00" in target:
        _fail(
            "symlink target is empty or contains NUL",
            code="invalid_checkpoint_path",
        )
    if unicodedata.normalize("NFC", target) != target:
        _fail("symlink target is not NFC", code="invalid_checkpoint_path")
    try:
        encoded = target.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ModelSessionError(
            "symlink target is not canonical UTF-8",
            code="invalid_checkpoint_path",
        ) from error
    if len(encoded) > limits.max_symlink_bytes:
        _fail(
            f"symlink target exceeds {limits.max_symlink_bytes} bytes",
            code="checkpoint_limit_exceeded",
        )
    return encoded


def _add_bounded(
    current: int,
    increment: int,
    maximum: int,
    *,
    label: str,
) -> int:
    if increment < 0 or current > maximum - increment:
        _fail(
            f"checkpoint {label} exceeds {maximum} bytes",
            code="checkpoint_limit_exceeded",
        )
    value = current + increment
    if value > _UINT64_MAX:
        _fail(
            f"checkpoint {label} exceeds the format",
            code="checkpoint_limit_exceeded",
        )
    return value


class PackWriter:
    """Bounded positional writer that hashes the authenticated prefix."""

    def __init__(self, descriptor: int, maximum_bytes: int) -> None:
        try:
            metadata = os.fstat(descriptor)
        except OSError as error:
            raise ModelSessionError(
                f"cannot inspect checkpoint output: {error}",
                code="invalid_checkpoint_descriptor",
            ) from error
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != 0:
            _fail(
                "checkpoint output must be an empty regular file",
                code="invalid_checkpoint_descriptor",
            )
        self.descriptor = descriptor
        self.maximum_bytes = maximum_bytes
        self.offset = 0
        self.hasher = hashlib.sha256()

    def write(self, content: bytes, *, hashed: bool = True) -> None:
        if self.offset > self.maximum_bytes - len(content):
            _fail(
                f"checkpoint pack exceeds {self.maximum_bytes} bytes",
                code="checkpoint_limit_exceeded",
            )
        position = 0
        while position < len(content):
            try:
                written = os.pwrite(
                    self.descriptor,
                    content[position:],
                    self.offset + position,
                )
            except OSError as error:
                raise ModelSessionError(
                    f"cannot write checkpoint pack: {error}",
                    code="checkpoint_io_error",
                ) from error
            if written <= 0:
                _fail(
                    "checkpoint output stopped accepting bytes",
                    code="checkpoint_io_error",
                )
            position += written
        self.offset += len(content)
        if hashed:
            self.hasher.update(content)


class _PackReader:
    def __init__(
        self,
        descriptor: int,
        *,
        limits: CheckpointLimits,
    ) -> None:
        try:
            metadata = os.fstat(descriptor)
        except OSError as error:
            raise ModelSessionError(
                f"cannot inspect checkpoint input: {error}",
                code="invalid_checkpoint_descriptor",
            ) from error
        if not stat.S_ISREG(metadata.st_mode):
            _fail(
                "checkpoint input must be a regular file",
                code="invalid_checkpoint_descriptor",
            )
        if metadata.st_size > limits.max_pack_bytes:
            _fail(
                f"checkpoint pack exceeds {limits.max_pack_bytes} bytes",
                code="checkpoint_limit_exceeded",
            )
        self.descriptor = descriptor
        self.limits = limits
        self.metadata = metadata
        self.offset = 0
        self.hasher = hashlib.sha256()

    def read(self, length: int, *, hashed: bool = True) -> bytes:
        if length < 0 or self.offset > self.metadata.st_size - length:
            _fail("checkpoint pack is truncated", code="invalid_checkpoint_pack")
        chunks: list[bytes] = []
        remaining = length
        while remaining:
            try:
                chunk = os.pread(
                    self.descriptor,
                    min(remaining, self.limits.io_chunk_bytes),
                    self.offset,
                )
            except OSError as error:
                raise ModelSessionError(
                    f"cannot read checkpoint pack: {error}",
                    code="checkpoint_io_error",
                ) from error
            if not chunk:
                _fail(
                    "checkpoint pack is truncated",
                    code="invalid_checkpoint_pack",
                )
            self.offset += len(chunk)
            remaining -= len(chunk)
            chunks.append(chunk)
            if hashed:
                self.hasher.update(chunk)
        return b"".join(chunks)

    def consume_payload(
        self,
        length: int,
        *,
        capture: bool,
    ) -> tuple[bytes | None, bytes]:
        captured = bytearray() if capture else None
        digest = hashlib.sha256()
        remaining = length
        while remaining:
            chunk = self.read(min(remaining, self.limits.io_chunk_bytes))
            digest.update(chunk)
            if captured is not None:
                captured.extend(chunk)
            remaining -= len(chunk)
        return (
            None if captured is None else bytes(captured),
            digest.digest(),
        )


def _decode_path(
    representation: bytes,
    depth: int,
    *,
    limits: CheckpointLimits,
) -> tuple[tuple[str, ...], tuple[bytes, ...]]:
    components: list[str] = []
    encoded_components: list[bytes] = []
    offset = 0
    for _ in range(depth):
        if offset > len(representation) - 2:
            _fail(
                "checkpoint path component length is truncated",
                code="invalid_checkpoint_pack",
            )
        length = struct.unpack_from("<H", representation, offset)[0]
        offset += 2
        if length == 0 or offset > len(representation) - length:
            _fail(
                "checkpoint path component is empty or truncated",
                code="invalid_checkpoint_pack",
            )
        encoded = representation[offset : offset + length]
        offset += length
        try:
            component = encoded.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ModelSessionError(
                "checkpoint path component is not UTF-8",
                code="invalid_checkpoint_path",
            ) from error
        canonical, canonical_bytes = _canonical_component(
            component,
            limits=limits,
        )
        if canonical_bytes != encoded:
            _fail(
                "checkpoint path component has a noncanonical encoding",
                code="invalid_checkpoint_path",
            )
        components.append(canonical)
        encoded_components.append(encoded)
    if offset != len(representation):
        _fail(
            "checkpoint path representation has trailing bytes",
            code="invalid_checkpoint_pack",
        )
    encoded_path_bytes = sum(len(component) for component in encoded_components)
    if encoded_components:
        encoded_path_bytes += len(encoded_components) - 1
    if encoded_path_bytes > limits.max_path_bytes:
        _fail(
            f"checkpoint path exceeds {limits.max_path_bytes} bytes",
            code="checkpoint_limit_exceeded",
        )
    return tuple(components), tuple(encoded_components)


def validate_pack(
    input_descriptor: int,
    *,
    limits: CheckpointLimits,
) -> ValidatedCheckpoint:
    """Validate every pack byte and return a descriptor-independent plan."""

    reader = _PackReader(input_descriptor, limits=limits)
    initial_metadata = _pack_metadata(reader.metadata)
    if reader.metadata.st_size < _HEADER.size + _FOOTER.size:
        _fail(
            "checkpoint pack is shorter than its header and footer",
            code="invalid_checkpoint_pack",
        )
    (
        magic,
        version,
        flags,
        header_size,
        entry_count,
        declared_logical_bytes,
        declared_payload_bytes,
    ) = _HEADER.unpack(reader.read(_HEADER.size))
    if (
        magic != _PACK_MAGIC
        or version != _FORMAT_VERSION
        or flags != _FORMAT_FLAGS
        or header_size != _HEADER.size
    ):
        _fail(
            "checkpoint header is not the exact supported v1 format",
            code="invalid_checkpoint_pack",
        )
    if entry_count < 2:
        _fail(
            "checkpoint omits one or both logical roots",
            code="invalid_checkpoint_pack",
        )
    if entry_count > limits.max_entries:
        _fail(
            f"checkpoint entry count exceeds {limits.max_entries}",
            code="checkpoint_limit_exceeded",
        )
    if declared_logical_bytes > limits.max_logical_bytes:
        _fail(
            f"checkpoint logical bytes exceed {limits.max_logical_bytes}",
            code="checkpoint_limit_exceeded",
        )
    if declared_payload_bytes > limits.max_payload_bytes:
        _fail(
            f"checkpoint payload bytes exceed {limits.max_payload_bytes}",
            code="checkpoint_limit_exceeded",
        )

    entries: list[DecodedEntry] = []
    paths: dict[tuple[int, tuple[str, ...]], int] = {}
    prior_key: tuple[int, tuple[bytes, ...]] | None = None
    logical_bytes = 0
    payload_bytes = 0
    sparse_extent_count = 0
    root_records: set[int] = set()
    for index in range(entry_count):
        (
            entry_type,
            root,
            mode,
            depth,
            entry_flags,
            encoded_path_length,
            extent_count,
            logical_size,
            payload_size,
            hardlink_raw,
        ) = _RECORD.unpack(reader.read(_RECORD.size))
        if root not in _ROOT_NAMES:
            _fail(
                "checkpoint entry has an unknown logical root",
                code="invalid_checkpoint_pack",
            )
        if mode & ~0o777:
            _fail(
                "checkpoint entry mode contains forbidden permission bits",
                code="invalid_checkpoint_pack",
            )
        if depth > limits.max_depth:
            _fail(
                f"checkpoint path exceeds depth {limits.max_depth}",
                code="checkpoint_limit_exceeded",
            )
        if encoded_path_length > limits.max_path_bytes + depth * 2:
            _fail(
                "checkpoint encoded path exceeds its bounded representation",
                code="checkpoint_limit_exceeded",
            )
        components, component_key = _decode_path(
            reader.read(encoded_path_length),
            depth,
            limits=limits,
        )
        key = (root, component_key)
        if prior_key is not None and key <= prior_key:
            _fail(
                "checkpoint entries are not in canonical strict path order",
                code="invalid_checkpoint_pack",
            )
        prior_key = key
        path_identity = (root, components)
        if path_identity in paths:
            _fail(
                "checkpoint contains a duplicate path",
                code="invalid_checkpoint_pack",
            )
        if not components:
            if entry_type != _ENTRY_DIRECTORY or root in root_records:
                _fail(
                    "checkpoint root records are missing or duplicated",
                    code="invalid_checkpoint_pack",
                )
            root_records.add(root)
        else:
            parent = paths.get((root, components[:-1]))
            if parent is None or entries[parent].entry_type != _ENTRY_DIRECTORY:
                _fail(
                    "checkpoint entry appears before its directory parent",
                    code="invalid_checkpoint_pack",
                )

        extents: list[tuple[int, int]] = []
        if extent_count > limits.max_sparse_extents_per_file:
            _fail(
                "checkpoint sparse extent count exceeds its limit",
                code="checkpoint_limit_exceeded",
            )
        if sparse_extent_count > limits.max_sparse_extents - extent_count:
            _fail(
                "checkpoint aggregate sparse extent count exceeds its limit",
                code="checkpoint_limit_exceeded",
            )
        sparse_extent_count += extent_count
        for _extent_index in range(extent_count):
            offset, length = _EXTENT.unpack(reader.read(_EXTENT.size))
            if (
                length == 0
                or offset > logical_size
                or length > logical_size - offset
                or (
                    extents
                    and offset <= extents[-1][0] + extents[-1][1]
                )
            ):
                _fail(
                    "checkpoint sparse extents overlap or exceed the file",
                    code="invalid_checkpoint_pack",
                )
            extents.append((offset, length))

        hardlink_index = None if hardlink_raw == _NO_LINK else hardlink_raw
        if entry_type == _ENTRY_DIRECTORY:
            valid = (
                entry_flags == 0
                and extent_count == 0
                and logical_size == 0
                and payload_size == 0
                and hardlink_index is None
            )
        elif entry_type == _ENTRY_REGULAR:
            if logical_size > limits.max_file_logical_bytes:
                _fail(
                    "checkpoint file exceeds its logical byte limit",
                    code="checkpoint_limit_exceeded",
                )
            if entry_flags == 0:
                valid = (
                    extent_count == 0
                    and payload_size == logical_size
                    and hardlink_index is None
                )
            elif entry_flags == _ENTRY_FLAG_SPARSE:
                valid = (
                    payload_size < logical_size
                    and sum(length for _offset, length in extents) == payload_size
                    and hardlink_index is None
                )
            else:
                valid = False
        elif entry_type == _ENTRY_SYMLINK:
            valid = (
                entry_flags == 0
                and extent_count == 0
                and 0 < logical_size == payload_size
                and payload_size <= limits.max_symlink_bytes
                and hardlink_index is None
                and mode == 0o777
            )
        elif entry_type == _ENTRY_HARDLINK:
            valid = (
                entry_flags == 0
                and extent_count == 0
                and logical_size == 0
                and payload_size == 0
                and hardlink_index is not None
                and hardlink_index < index
                and entries[hardlink_index].entry_type
                in {_ENTRY_REGULAR, _ENTRY_SYMLINK}
                and entries[hardlink_index].root == root
                and mode == entries[hardlink_index].mode
            )
        else:
            valid = False
        if not valid:
            _fail(
                f"checkpoint entry {index} has invalid type fields",
                code="invalid_checkpoint_pack",
            )

        logical_bytes = _add_bounded(
            logical_bytes,
            logical_size,
            limits.max_logical_bytes,
            label="logical data",
        )
        payload_bytes = _add_bounded(
            payload_bytes,
            payload_size,
            limits.max_payload_bytes,
            label="payload data",
        )
        payload_offset = reader.offset
        captured, calculated_payload_digest = reader.consume_payload(
            payload_size,
            capture=entry_type == _ENTRY_SYMLINK,
        )
        expected_payload_digest = reader.read(32)
        if expected_payload_digest != calculated_payload_digest:
            _fail(
                f"checkpoint entry {index} payload digest does not match",
                code="checkpoint_hash_mismatch",
            )
        symlink_target = None
        if entry_type == _ENTRY_SYMLINK:
            if captured is None:
                raise AssertionError("captured symlink target is absent")
            try:
                symlink_target = captured.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ModelSessionError(
                    "checkpoint symlink target is not UTF-8",
                    code="invalid_checkpoint_path",
                ) from error
            if (
                _canonical_symlink_target(symlink_target, limits=limits)
                != captured
            ):
                _fail(
                    "checkpoint symlink target is not canonical",
                    code="invalid_checkpoint_path",
                )
        elif expected_payload_digest != _EMPTY_DIGEST and payload_size == 0:
            _fail(
                "checkpoint empty entry has a nonempty digest",
                code="checkpoint_hash_mismatch",
            )
        entries.append(
            DecodedEntry(
                root=root,
                components=components,
                component_key=component_key,
                entry_type=entry_type,
                mode=mode,
                logical_size=logical_size,
                payload_size=payload_size,
                extents=tuple(extents),
                payload_offset=payload_offset,
                payload_digest=expected_payload_digest,
                symlink_target=symlink_target,
                hardlink_index=hardlink_index,
            )
        )
        paths[path_identity] = index

    if root_records != {_ROOT_WORK, _ROOT_SESSIONS}:
        _fail(
            "checkpoint does not contain both logical roots",
            code="invalid_checkpoint_pack",
        )
    if (
        logical_bytes != declared_logical_bytes
        or payload_bytes != declared_payload_bytes
    ):
        _fail(
            "checkpoint header byte totals do not match its records",
            code="invalid_checkpoint_pack",
        )
    prefix_length = reader.offset
    prefix_digest = reader.hasher.digest()
    footer_bytes = reader.read(_FOOTER.size, hashed=False)
    footer_magic, declared_prefix_length, declared_digest = _FOOTER.unpack(
        footer_bytes
    )
    if (
        footer_magic != _FOOTER_MAGIC
        or declared_prefix_length != prefix_length
        or declared_digest != prefix_digest
    ):
        _fail(
            "checkpoint footer or global digest does not match",
            code="checkpoint_hash_mismatch",
        )
    if reader.offset != reader.metadata.st_size:
        _fail(
            "checkpoint pack has trailing bytes",
            code="invalid_checkpoint_pack",
        )
    if _pack_metadata(os.fstat(input_descriptor)) != initial_metadata:
        _fail(
            "checkpoint pack changed while it was validated",
            code="checkpoint_source_changed",
        )
    full_hasher = reader.hasher.copy()
    full_hasher.update(footer_bytes)
    return ValidatedCheckpoint(
        summary=CheckpointSummary(
            schema=CHECKPOINT_SCHEMA,
            entry_count=entry_count,
            logical_bytes=logical_bytes,
            payload_bytes=payload_bytes,
            pack_bytes=reader.offset,
            pack_sha256=full_hasher.hexdigest(),
        ),
        entries=tuple(entries),
        source_metadata=initial_metadata,
    )
