"""Descriptor-relative discovery and hydration for model-session checkpoints."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass

from . import checkpoint_format as pack
from .checkpoint_format import (
    CheckpointLimits,
    CheckpointSummary,
    DecodedEntry,
)
from .errors import ModelSessionError


_SPECIAL_PERMISSION_BITS = stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX


@dataclass(frozen=True)
class _ScannedEntry:
    root: int
    components: tuple[str, ...]
    encoded_path: bytes
    entry_type: int
    mode: int
    logical_size: int
    payload_size: int
    extents: tuple[tuple[int, int], ...]
    symlink_target: bytes | None
    hardlink_index: int | None
    source_metadata: tuple[int, ...]


@dataclass
class _Scan:
    entries: list[_ScannedEntry]
    inode_primaries: dict[tuple[int, int, int, int], int]
    logical_bytes: int = 0
    payload_bytes: int = 0
    sparse_extent_count: int = 0


def _ordinary_mode(metadata: os.stat_result, *, label: str) -> int:
    permission_bits = stat.S_IMODE(metadata.st_mode)
    if permission_bits & _SPECIAL_PERMISSION_BITS:
        pack._fail(
            f"{label} has set-id or sticky permission bits",
            code="unsupported_checkpoint_entry",
        )
    return permission_bits & 0o777


def _stable_metadata(metadata: os.stat_result) -> tuple[int, ...]:
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
        getattr(metadata, "st_blocks", 0),
    )


def _validate_directory_descriptor(
    descriptor: int,
    *,
    label: str,
) -> os.stat_result:
    try:
        metadata = os.fstat(descriptor)
    except OSError as error:
        raise ModelSessionError(
            f"cannot inspect {label}: {error}",
            code="invalid_checkpoint_descriptor",
        ) from error
    if not stat.S_ISDIR(metadata.st_mode):
        pack._fail(
            f"{label} is not a directory descriptor",
            code="invalid_checkpoint_descriptor",
        )
    _ordinary_mode(metadata, label=label)
    return metadata


def _list_xattrs(descriptor: int, *, label: str) -> None:
    try:
        attributes = os.listxattr(descriptor)
    except OSError as error:
        raise ModelSessionError(
            f"cannot inspect extended attributes on {label}: {error}",
            code="unsupported_checkpoint_entry",
        ) from error
    if attributes:
        pack._fail(
            f"{label} has extended attributes",
            code="unsupported_checkpoint_entry",
        )


def _list_symlink_xattrs(
    parent_descriptor: int,
    name: str,
    *,
    label: str,
) -> None:
    proc_path = f"/proc/self/fd/{parent_descriptor}/{name}"
    try:
        attributes = os.listxattr(proc_path, follow_symlinks=False)
    except OSError as error:
        raise ModelSessionError(
            f"cannot inspect extended attributes on {label}: {error}",
            code="unsupported_checkpoint_entry",
        ) from error
    if attributes:
        pack._fail(
            f"{label} has extended attributes",
            code="unsupported_checkpoint_entry",
        )


def _directory_flags() -> int:
    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required):
        pack._fail(
            "checkpoint codec requires O_DIRECTORY and O_NOFOLLOW",
            code="checkpoint_platform_unsupported",
        )
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _file_flags(*, writable: bool = False) -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        pack._fail(
            "checkpoint codec requires O_NOFOLLOW",
            code="checkpoint_platform_unsupported",
        )
    access = os.O_WRONLY if writable else os.O_RDONLY
    return (
        access
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | (0 if writable else getattr(os, "O_NONBLOCK", 0))
    )


def _open_directory_at(
    parent_descriptor: int,
    name: str,
    *,
    label: str,
) -> int:
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    except OSError as error:
        raise ModelSessionError(
            f"cannot open {label} without following links: {error}",
            code="checkpoint_source_changed",
        ) from error
    try:
        _validate_directory_descriptor(descriptor, label=label)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_regular_at(
    parent_descriptor: int,
    name: str,
    *,
    label: str,
) -> int:
    try:
        descriptor = os.open(name, _file_flags(), dir_fd=parent_descriptor)
    except OSError as error:
        raise ModelSessionError(
            f"cannot open {label} without following links: {error}",
            code="checkpoint_source_changed",
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            pack._fail(
                f"{label} changed from a regular file",
                code="checkpoint_source_changed",
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_directory_path(
    root_descriptor: int,
    components: tuple[str, ...],
    *,
    label: str,
) -> int:
    descriptor = os.dup(root_descriptor)
    try:
        for component in components:
            child = _open_directory_at(descriptor, component, label=label)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _stat_at(
    parent_descriptor: int,
    name: str,
    *,
    label: str,
) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError as error:
        raise ModelSessionError(
            f"cannot inspect {label}: {error}",
            code="checkpoint_source_changed",
        ) from error


def _bounded_directory_names(
    directory_descriptor: int,
    maximum_count: int,
    *,
    label: str,
    mutation_check: bool,
    limits: CheckpointLimits,
) -> tuple[tuple[str, bytes], ...]:
    """Enumerate at most ``maximum_count`` canonical names from one directory."""

    names: list[tuple[str, bytes]] = []
    failure_code = (
        "checkpoint_source_changed"
        if mutation_check
        else "checkpoint_limit_exceeded"
    )
    try:
        with os.scandir(directory_descriptor) as iterator:
            for directory_entry in iterator:
                if len(names) >= maximum_count:
                    pack._fail(
                        f"{label} exceeds the checkpoint entry bound",
                        code=failure_code,
                    )
                try:
                    canonical = pack._canonical_component(
                        directory_entry.name,
                        limits=limits,
                    )
                except ModelSessionError as error:
                    if not mutation_check:
                        raise
                    raise ModelSessionError(
                        f"{label} changed to a noncanonical name",
                        code="checkpoint_source_changed",
                    ) from error
                names.append(canonical)
    except OSError as error:
        code = (
            "checkpoint_source_changed"
            if mutation_check
            else "invalid_checkpoint_source"
        )
        raise ModelSessionError(
            f"cannot enumerate {label}: {error}",
            code=code,
        ) from error
    names.sort(key=lambda item: item[1])
    return tuple(names)


def _sparse_extents(
    descriptor: int,
    logical_size: int,
    *,
    limits: CheckpointLimits,
) -> tuple[tuple[int, int], ...] | None:
    if (
        logical_size == 0
        or not hasattr(os, "SEEK_DATA")
        or not hasattr(os, "SEEK_HOLE")
    ):
        return None
    metadata = os.fstat(descriptor)
    if getattr(metadata, "st_blocks", 0) * 512 >= logical_size:
        return None
    extents: list[tuple[int, int]] = []
    position = 0
    try:
        while position < logical_size:
            try:
                data_offset = os.lseek(descriptor, position, os.SEEK_DATA)
            except OSError as error:
                if error.errno == errno.ENXIO:
                    break
                if error.errno in {
                    errno.EINVAL,
                    getattr(errno, "ENOTSUP", errno.EINVAL),
                    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
                }:
                    return None
                raise
            hole_offset = min(
                os.lseek(descriptor, data_offset, os.SEEK_HOLE),
                logical_size,
            )
            if (
                data_offset < position
                or data_offset >= logical_size
                or hole_offset <= data_offset
            ):
                return None
            extents.append((data_offset, hole_offset - data_offset))
            if len(extents) > limits.max_sparse_extents_per_file:
                return None
            position = hole_offset
    except OSError:
        return None
    payload_size = sum(length for _offset, length in extents)
    if payload_size >= logical_size:
        return None
    return tuple(extents)


def _entry(
    *,
    root: int,
    components: tuple[str, ...],
    entry_type: int,
    mode: int,
    metadata: os.stat_result,
    limits: CheckpointLimits,
    logical_size: int = 0,
    payload_size: int = 0,
    extents: tuple[tuple[int, int], ...] = (),
    symlink_target: bytes | None = None,
    hardlink_index: int | None = None,
) -> _ScannedEntry:
    encoded_path, _component_key = pack._encode_path(components, limits=limits)
    return _ScannedEntry(
        root=root,
        components=components,
        encoded_path=encoded_path,
        entry_type=entry_type,
        mode=mode,
        logical_size=logical_size,
        payload_size=payload_size,
        extents=extents,
        symlink_target=symlink_target,
        hardlink_index=hardlink_index,
        source_metadata=_stable_metadata(metadata),
    )


def _append_entry(
    scan: _Scan,
    entry: _ScannedEntry,
    *,
    limits: CheckpointLimits,
) -> None:
    if len(scan.entries) >= limits.max_entries:
        pack._fail(
            f"checkpoint contains more than {limits.max_entries} entries",
            code="checkpoint_limit_exceeded",
        )
    scan.logical_bytes = pack._add_bounded(
        scan.logical_bytes,
        entry.logical_size,
        limits.max_logical_bytes,
        label="logical data",
    )
    scan.payload_bytes = pack._add_bounded(
        scan.payload_bytes,
        entry.payload_size,
        limits.max_payload_bytes,
        label="payload data",
    )
    if (
        scan.sparse_extent_count
        > limits.max_sparse_extents - len(entry.extents)
    ):
        pack._fail(
            "checkpoint aggregate sparse extent count exceeds its limit",
            code="checkpoint_limit_exceeded",
        )
    scan.sparse_extent_count += len(entry.extents)
    scan.entries.append(entry)


def _entry_label(root: int, components: tuple[str, ...]) -> str:
    suffix = "/".join(pack._safe_component(part) for part in components)
    return f"{pack._ROOT_NAMES[root]}:{suffix}"


def _scan_root(
    root_descriptor: int,
    root: int,
    scan: _Scan,
    *,
    limits: CheckpointLimits,
) -> None:
    label = f"{pack._ROOT_NAMES[root]} root"
    metadata = _validate_directory_descriptor(root_descriptor, label=label)
    _list_xattrs(root_descriptor, label=label)
    _append_entry(
        scan,
        _entry(
            root=root,
            components=(),
            entry_type=pack._ENTRY_DIRECTORY,
            mode=_ordinary_mode(metadata, label=label),
            metadata=metadata,
            limits=limits,
        ),
        limits=limits,
    )
    _scan_directory(root_descriptor, root, (), scan, limits=limits)
    if _stable_metadata(os.fstat(root_descriptor)) != _stable_metadata(metadata):
        pack._fail(
            f"{label} changed while it was scanned",
            code="checkpoint_source_changed",
        )


def _validate_hardlink_closure(scan: _Scan) -> None:
    observations: dict[tuple[int, int, int], tuple[int, int]] = {}
    for entry in scan.entries:
        entry_type = entry.entry_type
        if entry_type == pack._ENTRY_HARDLINK:
            if entry.hardlink_index is None:
                raise AssertionError("scanned hardlink target is absent")
            entry_type = scan.entries[entry.hardlink_index].entry_type
        if entry_type not in {pack._ENTRY_REGULAR, pack._ENTRY_SYMLINK}:
            continue
        device = entry.source_metadata[0]
        inode = entry.source_metadata[1]
        link_count = entry.source_metadata[5]
        key = (device, inode, entry_type)
        expected, observed = observations.get(key, (link_count, 0))
        if expected != link_count:
            pack._fail(
                "source hardlink metadata changed during checkpoint scan",
                code="checkpoint_source_changed",
            )
        observations[key] = (expected, observed + 1)
    if any(expected != observed for expected, observed in observations.values()):
        pack._fail(
            "source hardlinks are not closed over work and sessions",
            code="unsupported_checkpoint_entry",
        )


def _scan_directory(
    directory_descriptor: int,
    root: int,
    parent_components: tuple[str, ...],
    scan: _Scan,
    *,
    limits: CheckpointLimits,
) -> None:
    before = os.fstat(directory_descriptor)
    canonical_names = _bounded_directory_names(
        directory_descriptor,
        limits.max_entries - len(scan.entries),
        label="checkpoint source directory",
        mutation_check=False,
        limits=limits,
    )
    for name, _encoded_name in canonical_names:
        components = (*parent_components, name)
        label = _entry_label(root, components)
        metadata = _stat_at(directory_descriptor, name, label=label)
        mode = _ordinary_mode(metadata, label=label)
        if stat.S_ISDIR(metadata.st_mode):
            child = _open_directory_at(directory_descriptor, name, label=label)
            try:
                if _stable_metadata(os.fstat(child)) != _stable_metadata(metadata):
                    pack._fail(
                        f"{label} changed while it was opened",
                        code="checkpoint_source_changed",
                    )
                _list_xattrs(child, label=label)
                _append_entry(
                    scan,
                    _entry(
                        root=root,
                        components=components,
                        entry_type=pack._ENTRY_DIRECTORY,
                        mode=mode,
                        metadata=metadata,
                        limits=limits,
                    ),
                    limits=limits,
                )
                _scan_directory(child, root, components, scan, limits=limits)
                if _stable_metadata(os.fstat(child)) != _stable_metadata(metadata):
                    pack._fail(
                        f"{label} changed while it was scanned",
                        code="checkpoint_source_changed",
                    )
            finally:
                os.close(child)
            continue

        if stat.S_ISREG(metadata.st_mode):
            inode_key = (
                root,
                metadata.st_dev,
                metadata.st_ino,
                pack._ENTRY_REGULAR,
            )
            primary = scan.inode_primaries.get(inode_key)
            if primary is not None:
                _append_entry(
                    scan,
                    _entry(
                        root=root,
                        components=components,
                        entry_type=pack._ENTRY_HARDLINK,
                        mode=mode,
                        metadata=metadata,
                        limits=limits,
                        hardlink_index=primary,
                    ),
                    limits=limits,
                )
                continue
            descriptor = _open_regular_at(directory_descriptor, name, label=label)
            try:
                opened = os.fstat(descriptor)
                if _stable_metadata(opened) != _stable_metadata(metadata):
                    pack._fail(
                        f"{label} changed while it was opened",
                        code="checkpoint_source_changed",
                    )
                _list_xattrs(descriptor, label=label)
                if metadata.st_size > limits.max_file_logical_bytes:
                    pack._fail(
                        f"{label} exceeds {limits.max_file_logical_bytes} bytes",
                        code="checkpoint_limit_exceeded",
                    )
                extents = _sparse_extents(
                    descriptor,
                    metadata.st_size,
                    limits=limits,
                )
                payload_size = (
                    metadata.st_size
                    if extents is None
                    else sum(length for _offset, length in extents)
                )
                entry = _entry(
                    root=root,
                    components=components,
                    entry_type=pack._ENTRY_REGULAR,
                    mode=mode,
                    metadata=metadata,
                    limits=limits,
                    logical_size=metadata.st_size,
                    payload_size=payload_size,
                    extents=() if extents is None else extents,
                )
                primary_index = len(scan.entries)
                _append_entry(scan, entry, limits=limits)
                scan.inode_primaries[inode_key] = primary_index
            finally:
                os.close(descriptor)
            continue

        if stat.S_ISLNK(metadata.st_mode):
            if mode != 0o777:
                pack._fail(
                    f"{label} has a noncanonical symlink mode",
                    code="unsupported_checkpoint_entry",
                )
            inode_key = (
                root,
                metadata.st_dev,
                metadata.st_ino,
                pack._ENTRY_SYMLINK,
            )
            primary = scan.inode_primaries.get(inode_key)
            if primary is not None:
                _append_entry(
                    scan,
                    _entry(
                        root=root,
                        components=components,
                        entry_type=pack._ENTRY_HARDLINK,
                        mode=mode,
                        metadata=metadata,
                        limits=limits,
                        hardlink_index=primary,
                    ),
                    limits=limits,
                )
                continue
            try:
                target = os.readlink(name, dir_fd=directory_descriptor)
            except OSError as error:
                raise ModelSessionError(
                    f"cannot read {label} without following it: {error}",
                    code="checkpoint_source_changed",
                ) from error
            target_bytes = pack._canonical_symlink_target(target, limits=limits)
            _list_symlink_xattrs(directory_descriptor, name, label=label)
            after = _stat_at(directory_descriptor, name, label=label)
            if _stable_metadata(after) != _stable_metadata(metadata):
                pack._fail(
                    f"{label} changed while it was read",
                    code="checkpoint_source_changed",
                )
            entry = _entry(
                root=root,
                components=components,
                entry_type=pack._ENTRY_SYMLINK,
                mode=mode,
                metadata=metadata,
                limits=limits,
                logical_size=len(target_bytes),
                payload_size=len(target_bytes),
                symlink_target=target_bytes,
            )
            primary_index = len(scan.entries)
            _append_entry(scan, entry, limits=limits)
            scan.inode_primaries[inode_key] = primary_index
            continue

        pack._fail(
            f"{label} is a device, FIFO, socket, or unsupported object",
            code="unsupported_checkpoint_entry",
        )

    after_names = _bounded_directory_names(
        directory_descriptor,
        len(canonical_names),
        label="checkpoint source directory",
        mutation_check=True,
        limits=limits,
    )
    if (
        after_names != canonical_names
        or _stable_metadata(os.fstat(directory_descriptor))
        != _stable_metadata(before)
    ):
        pack._fail(
            "checkpoint source directory changed while it was scanned",
            code="checkpoint_source_changed",
        )


def _source_root(
    root: int,
    work_descriptor: int,
    sessions_descriptor: int,
) -> int:
    return work_descriptor if root == pack._ROOT_WORK else sessions_descriptor


def _validate_entry_source(
    entry: _ScannedEntry,
    *,
    work_descriptor: int,
    sessions_descriptor: int,
    limits: CheckpointLimits,
) -> tuple[int | None, int | None]:
    root_descriptor = _source_root(
        entry.root,
        work_descriptor,
        sessions_descriptor,
    )
    label = _entry_label(entry.root, entry.components)
    if not entry.components:
        metadata = os.fstat(root_descriptor)
        if _stable_metadata(metadata) != entry.source_metadata:
            pack._fail(
                f"{label} changed after checkpoint scan",
                code="checkpoint_source_changed",
            )
        _list_xattrs(root_descriptor, label=label)
        return None, None

    parent = _open_directory_path(
        root_descriptor,
        entry.components[:-1],
        label=label,
    )
    keep_parent = False
    name = entry.components[-1]
    try:
        if entry.entry_type == pack._ENTRY_DIRECTORY:
            child = _open_directory_at(parent, name, label=label)
            try:
                metadata = os.fstat(child)
                if _stable_metadata(metadata) != entry.source_metadata:
                    pack._fail(
                        f"{label} changed after checkpoint scan",
                        code="checkpoint_source_changed",
                    )
                _list_xattrs(child, label=label)
            finally:
                os.close(child)
            return None, None

        if entry.entry_type == pack._ENTRY_REGULAR:
            descriptor = _open_regular_at(parent, name, label=label)
            try:
                metadata = os.fstat(descriptor)
                if _stable_metadata(metadata) != entry.source_metadata:
                    pack._fail(
                        f"{label} changed after checkpoint scan",
                        code="checkpoint_source_changed",
                    )
                _list_xattrs(descriptor, label=label)
            except BaseException:
                os.close(descriptor)
                raise
            keep_parent = True
            return parent, descriptor

        metadata = _stat_at(parent, name, label=label)
        if _stable_metadata(metadata) != entry.source_metadata:
            pack._fail(
                f"{label} changed after checkpoint scan",
                code="checkpoint_source_changed",
            )
        if entry.entry_type == pack._ENTRY_SYMLINK:
            try:
                target = os.readlink(name, dir_fd=parent)
                target_bytes = pack._canonical_symlink_target(
                    target,
                    limits=limits,
                )
            except (OSError, ModelSessionError) as error:
                raise ModelSessionError(
                    f"{label} changed after checkpoint scan",
                    code="checkpoint_source_changed",
                ) from error
            if target_bytes != entry.symlink_target:
                pack._fail(
                    f"{label} changed after checkpoint scan",
                    code="checkpoint_source_changed",
                )
            _list_symlink_xattrs(parent, name, label=label)
        elif stat.S_ISLNK(metadata.st_mode):
            _list_symlink_xattrs(parent, name, label=label)
        else:
            descriptor = _open_regular_at(parent, name, label=label)
            try:
                _list_xattrs(descriptor, label=label)
            finally:
                os.close(descriptor)
        return None, None
    finally:
        if not keep_parent:
            os.close(parent)


def _iter_pread(
    descriptor: int,
    offset: int,
    length: int,
    *,
    chunk_bytes: int,
) -> Iterator[tuple[int, bytes]]:
    remaining = length
    position = offset
    while remaining:
        try:
            chunk = os.pread(descriptor, min(remaining, chunk_bytes), position)
        except OSError as error:
            raise ModelSessionError(
                f"cannot read checkpoint source file: {error}",
                code="checkpoint_io_error",
            ) from error
        if not chunk:
            pack._fail(
                "checkpoint source file shrank while it was read",
                code="checkpoint_source_changed",
            )
        yield position, chunk
        position += len(chunk)
        remaining -= len(chunk)


def write_tree_checkpoint(
    work_descriptor: int,
    sessions_descriptor: int,
    output_descriptor: int,
    *,
    limits: CheckpointLimits,
) -> CheckpointSummary:
    """Scan two retained roots and emit one deterministic pack."""

    work_metadata = _validate_directory_descriptor(
        work_descriptor,
        label="work root",
    )
    sessions_metadata = _validate_directory_descriptor(
        sessions_descriptor,
        label="sessions root",
    )
    if (work_metadata.st_dev, work_metadata.st_ino) == (
        sessions_metadata.st_dev,
        sessions_metadata.st_ino,
    ):
        pack._fail(
            "work and sessions roots must be distinct directories",
            code="invalid_checkpoint_descriptor",
        )

    scan = _Scan(entries=[], inode_primaries={})
    _scan_root(work_descriptor, pack._ROOT_WORK, scan, limits=limits)
    _scan_root(sessions_descriptor, pack._ROOT_SESSIONS, scan, limits=limits)
    _validate_hardlink_closure(scan)
    writer = pack.PackWriter(output_descriptor, limits.max_pack_bytes)
    writer.write(
        pack._HEADER.pack(
            pack._PACK_MAGIC,
            pack._FORMAT_VERSION,
            pack._FORMAT_FLAGS,
            pack._HEADER.size,
            len(scan.entries),
            scan.logical_bytes,
            scan.payload_bytes,
        )
    )

    for index, entry in enumerate(scan.entries):
        parent_descriptor: int | None = None
        file_descriptor: int | None = None
        try:
            parent_descriptor, file_descriptor = _validate_entry_source(
                entry,
                work_descriptor=work_descriptor,
                sessions_descriptor=sessions_descriptor,
                limits=limits,
            )
            sparse = (
                entry.entry_type == pack._ENTRY_REGULAR
                and entry.payload_size < entry.logical_size
            )
            extents = entry.extents if sparse else ()
            writer.write(
                pack._RECORD.pack(
                    entry.entry_type,
                    entry.root,
                    entry.mode,
                    len(entry.components),
                    pack._ENTRY_FLAG_SPARSE if sparse else 0,
                    len(entry.encoded_path),
                    len(extents),
                    entry.logical_size,
                    entry.payload_size,
                    (
                        pack._NO_LINK
                        if entry.hardlink_index is None
                        else entry.hardlink_index
                    ),
                )
            )
            writer.write(entry.encoded_path)
            for offset, length in extents:
                writer.write(pack._EXTENT.pack(offset, length))

            payload_hasher = hashlib.sha256()
            if entry.entry_type == pack._ENTRY_SYMLINK:
                if entry.symlink_target is None:
                    raise AssertionError("symlink target is absent")
                writer.write(entry.symlink_target)
                payload_hasher.update(entry.symlink_target)
            elif entry.entry_type == pack._ENTRY_REGULAR:
                if file_descriptor is None:
                    raise AssertionError("regular file descriptor is absent")
                ranges = extents if sparse else ((0, entry.logical_size),)
                if sparse and _sparse_extents(
                    file_descriptor,
                    entry.logical_size,
                    limits=limits,
                ) != extents:
                    pack._fail(
                        "sparse file extents changed after checkpoint scan",
                        code="checkpoint_source_changed",
                    )
                for offset, length in ranges:
                    for _position, chunk in _iter_pread(
                        file_descriptor,
                        offset,
                        length,
                        chunk_bytes=limits.io_chunk_bytes,
                    ):
                        writer.write(chunk)
                        payload_hasher.update(chunk)
                if (
                    _stable_metadata(os.fstat(file_descriptor))
                    != entry.source_metadata
                ):
                    pack._fail(
                        "regular file changed while checkpoint payload was read",
                        code="checkpoint_source_changed",
                    )
            payload_digest = payload_hasher.digest()
            writer.write(payload_digest)
            if (
                entry.entry_type
                in {pack._ENTRY_DIRECTORY, pack._ENTRY_HARDLINK}
                and payload_digest != pack._EMPTY_DIGEST
            ):
                raise AssertionError(f"entry {index} has unexpected payload")
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            if parent_descriptor is not None:
                os.close(parent_descriptor)

    prefix_length = writer.offset
    prefix_digest = writer.hasher.digest()
    footer = pack._FOOTER.pack(
        pack._FOOTER_MAGIC,
        prefix_length,
        prefix_digest,
    )
    full_hasher = writer.hasher.copy()
    full_hasher.update(footer)
    writer.write(footer, hashed=False)
    try:
        os.fsync(output_descriptor)
    except OSError as error:
        raise ModelSessionError(
            f"cannot fsync checkpoint pack: {error}",
            code="checkpoint_io_error",
        ) from error
    return CheckpointSummary(
        schema=pack.CHECKPOINT_SCHEMA,
        entry_count=len(scan.entries),
        logical_bytes=scan.logical_bytes,
        payload_bytes=scan.payload_bytes,
        pack_bytes=writer.offset,
        pack_sha256=full_hasher.hexdigest(),
    )


def _validate_empty_target(
    descriptor: int,
    *,
    label: str,
) -> os.stat_result:
    metadata = _validate_directory_descriptor(descriptor, label=label)
    _list_xattrs(descriptor, label=label)
    try:
        with os.scandir(descriptor) as iterator:
            occupied = next(iterator, None) is not None
    except OSError as error:
        raise ModelSessionError(
            f"cannot enumerate {label}: {error}",
            code="unsafe_checkpoint_target",
        ) from error
    if occupied:
        pack._fail(
            f"{label} must be empty before hydration",
            code="unsafe_checkpoint_target",
        )
    return metadata


def _create_parent_descriptor(
    root_descriptor: int,
    components: tuple[str, ...],
) -> int:
    return _open_directory_path(
        root_descriptor,
        components,
        label="checkpoint target parent",
    )


def _pwrite_all(descriptor: int, content: bytes, offset: int) -> None:
    position = 0
    while position < len(content):
        try:
            written = os.pwrite(
                descriptor,
                content[position:],
                offset + position,
            )
        except OSError as error:
            raise ModelSessionError(
                f"cannot hydrate checkpoint file: {error}",
                code="checkpoint_io_error",
            ) from error
        if written <= 0:
            pack._fail(
                "checkpoint target stopped accepting bytes",
                code="checkpoint_io_error",
            )
        position += written


def _hydrate_regular(
    input_descriptor: int,
    parent_descriptor: int,
    name: str,
    entry: DecodedEntry,
    *,
    limits: CheckpointLimits,
) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(
            name,
            flags,
            0o600,
            dir_fd=parent_descriptor,
        )
    except OSError as error:
        raise ModelSessionError(
            f"cannot create checkpoint file {pack._safe_component(name)}: {error}",
            code="unsafe_checkpoint_target",
        ) from error
    try:
        os.ftruncate(descriptor, entry.logical_size)
        payload_hasher = hashlib.sha256()
        payload_position = entry.payload_offset
        ranges = (
            entry.extents
            if entry.payload_size < entry.logical_size
            else ((0, entry.logical_size),)
        )
        for file_offset, length in ranges:
            remaining = length
            target_offset = file_offset
            while remaining:
                chunk_size = min(remaining, limits.io_chunk_bytes)
                try:
                    chunk = os.pread(
                        input_descriptor,
                        chunk_size,
                        payload_position,
                    )
                except OSError as error:
                    raise ModelSessionError(
                        f"cannot reread checkpoint payload: {error}",
                        code="checkpoint_io_error",
                    ) from error
                if len(chunk) != chunk_size:
                    pack._fail(
                        "checkpoint pack changed during hydration",
                        code="checkpoint_source_changed",
                    )
                _pwrite_all(descriptor, chunk, target_offset)
                payload_hasher.update(chunk)
                payload_position += len(chunk)
                target_offset += len(chunk)
                remaining -= len(chunk)
        if payload_hasher.digest() != entry.payload_digest:
            pack._fail(
                "checkpoint payload changed after validation",
                code="checkpoint_source_changed",
            )
        os.fchmod(descriptor, entry.mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def hydrate_tree_checkpoint(
    input_descriptor: int,
    work_descriptor: int,
    sessions_descriptor: int,
    *,
    limits: CheckpointLimits,
) -> CheckpointSummary:
    """Validate completely, then hydrate two empty retained roots."""

    validated = pack.validate_pack(input_descriptor, limits=limits)
    work_metadata = _validate_empty_target(
        work_descriptor,
        label="work hydration root",
    )
    sessions_metadata = _validate_empty_target(
        sessions_descriptor,
        label="sessions hydration root",
    )
    if (work_metadata.st_dev, work_metadata.st_ino) == (
        sessions_metadata.st_dev,
        sessions_metadata.st_ino,
    ):
        pack._fail(
            "work and sessions hydration roots must be distinct",
            code="invalid_checkpoint_descriptor",
        )
    if (
        pack._pack_metadata(os.fstat(input_descriptor))
        != validated.source_metadata
    ):
        pack._fail(
            "checkpoint pack changed before hydration",
            code="checkpoint_source_changed",
        )

    # Retained descriptors authorize filling restrictive roots. Change their
    # modes only after the pack and both target identities are fully validated.
    os.fchmod(work_descriptor, 0o700)
    os.fchmod(sessions_descriptor, 0o700)
    directory_entries: list[DecodedEntry] = []
    for index, entry in enumerate(validated.entries):
        root_descriptor = _source_root(
            entry.root,
            work_descriptor,
            sessions_descriptor,
        )
        if not entry.components:
            directory_entries.append(entry)
            continue
        parent = _create_parent_descriptor(
            root_descriptor,
            entry.components[:-1],
        )
        name = entry.components[-1]
        try:
            if entry.entry_type == pack._ENTRY_DIRECTORY:
                try:
                    os.mkdir(name, 0o700, dir_fd=parent)
                except OSError as error:
                    raise ModelSessionError(
                        "cannot create checkpoint directory "
                        f"{pack._safe_component(name)}: {error}",
                        code="unsafe_checkpoint_target",
                    ) from error
                directory_entries.append(entry)
            elif entry.entry_type == pack._ENTRY_REGULAR:
                _hydrate_regular(
                    input_descriptor,
                    parent,
                    name,
                    entry,
                    limits=limits,
                )
            elif entry.entry_type == pack._ENTRY_SYMLINK:
                if entry.symlink_target is None:
                    raise AssertionError("decoded symlink target is absent")
                try:
                    os.symlink(entry.symlink_target, name, dir_fd=parent)
                except OSError as error:
                    raise ModelSessionError(
                        "cannot create checkpoint symlink "
                        f"{pack._safe_component(name)}: {error}",
                        code="unsafe_checkpoint_target",
                    ) from error
            elif entry.entry_type == pack._ENTRY_HARDLINK:
                if entry.hardlink_index is None:
                    raise AssertionError("decoded hardlink target is absent")
                target = validated.entries[entry.hardlink_index]
                target_root = _source_root(
                    target.root,
                    work_descriptor,
                    sessions_descriptor,
                )
                source_parent = _create_parent_descriptor(
                    target_root,
                    target.components[:-1],
                )
                try:
                    os.link(
                        target.components[-1],
                        name,
                        src_dir_fd=source_parent,
                        dst_dir_fd=parent,
                        follow_symlinks=False,
                    )
                except OSError as error:
                    raise ModelSessionError(
                        "cannot create checkpoint hardlink "
                        f"{pack._safe_component(name)}: {error}",
                        code="unsafe_checkpoint_target",
                    ) from error
                finally:
                    os.close(source_parent)
            else:
                raise AssertionError(
                    f"decoded checkpoint entry {index} has an unknown type"
                )
        finally:
            os.close(parent)

    for entry in sorted(
        directory_entries,
        key=lambda value: len(value.components),
        reverse=True,
    ):
        root_descriptor = _source_root(
            entry.root,
            work_descriptor,
            sessions_descriptor,
        )
        descriptor = _open_directory_path(
            root_descriptor,
            entry.components,
            label="hydrated checkpoint directory",
        )
        try:
            os.fchmod(descriptor, entry.mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    if (
        pack._pack_metadata(os.fstat(input_descriptor))
        != validated.source_metadata
    ):
        pack._fail(
            "checkpoint pack changed during hydration",
            code="checkpoint_source_changed",
        )
    return validated.summary
