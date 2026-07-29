"""Safe model-lab representation for exact vLLM compiled-cache trees."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import pathlib
import stat
from typing import Any

from model_lab.errors import ModelLabError


COMPILE_CACHE_INVENTORY_SCHEMA = "model-lab.vllm-compile-cache-inventory.v1"
COMPILE_CACHE_HEADROOM_SCHEMA = "model-lab.vllm-compile-cache-headroom.v1"
COMPILE_CACHE_SUBDIRECTORIES = (
    "cuda",
    "flashinfer",
    "torch",
    "torchinductor",
    "triton",
    "vllm",
    "xdg",
)
MAX_DOCUMENT_BYTES = 256 * 1024 * 1024
MAX_MEASUREMENT_BYTES = 4 * 1024 * 1024
MAX_CACHE_FILES = 1_000_000
MAX_CACHE_DIRECTORIES = 1_000_000
MAX_CACHE_BYTES = 64 * 1024 * 1024 * 1024
EMPTY_CACHE_GROWTH_RESERVE_BYTES = 16 * 1024 * 1024 * 1024
STAGED_CACHE_GROWTH_RESERVE_BYTES = 8 * 1024 * 1024 * 1024
AUTHOR_PUBLICATION_RESERVE_BYTES = 1024 * 1024 * 1024
COPY_BUFFER_BYTES = 8 * 1024 * 1024
DIRECTORY_STAT_FIELDS = frozenset({"device", "inode", "mtime_ns", "ctime_ns", "mode"})
_INVENTORY_FIELDS = frozenset(
    {
        "schema_version",
        "directories",
        "files",
        "directory_count",
        "file_count",
        "total_bytes",
        "sha256",
    }
)
_DIRECTORY_RECORD_FIELDS = frozenset({"path", "mode"})
_FILE_RECORD_FIELDS = frozenset({"path", "mode", "bytes", "sha256"})


def fail(message: str) -> None:
    raise ModelLabError(message, code="compile_cache_operation_failed")


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("ascii")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_document(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def preflight_compile_cache_headroom(
    *,
    filesystem_root: pathlib.Path,
    purpose: str,
    archive_bytes: int,
    inventory_bytes: int,
    document_bytes: int = 0,
    reserve_name: str,
    reserve_bytes: int,
) -> dict[str, Any]:
    """Fail before cache creation unless exact inputs plus reserve fit."""

    byte_values = {
        "archive bytes": archive_bytes,
        "inventory bytes": inventory_bytes,
        "document bytes": document_bytes,
        "reserve bytes": reserve_bytes,
    }
    if (
        not isinstance(purpose, str)
        or not purpose
        or not isinstance(reserve_name, str)
        or not reserve_name
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in byte_values.values()
        )
        or reserve_bytes <= 0
    ):
        fail("compiled-cache headroom request is malformed")
    try:
        filesystem = os.statvfs(filesystem_root)
    except OSError as error:
        raise ModelLabError(
            "cannot inspect compiled-cache destination filesystem headroom",
            code="compile_cache_operation_failed",
        ) from error
    available_bytes = filesystem.f_bavail * filesystem.f_frsize
    required_bytes = archive_bytes + inventory_bytes + document_bytes + reserve_bytes
    if available_bytes < required_bytes:
        fail(
            "insufficient compiled-cache destination headroom for "
            f"{purpose}: need {required_bytes} bytes including "
            f"{reserve_name}, have {available_bytes} bytes"
        )
    return {
        "schema_version": COMPILE_CACHE_HEADROOM_SCHEMA,
        "purpose": purpose,
        "archive_bytes": archive_bytes,
        "inventory_bytes": inventory_bytes,
        "document_bytes": document_bytes,
        "reserve": {
            "name": reserve_name,
            "bytes": reserve_bytes,
        },
        "required_bytes": required_bytes,
        "available_bytes": available_bytes,
    }


def file_mode(value: os.stat_result) -> int:
    return stat.S_IMODE(value.st_mode)


def directory_stat(value: os.stat_result) -> dict[str, int]:
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
        "mode": file_mode(value),
    }


def safe_relative(value: Any, *, label: str) -> pathlib.PurePosixPath:
    if not isinstance(value, str):
        fail(f"{label} must be a relative POSIX path")
    path = pathlib.PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\x00" in value
        or len(value.encode("utf-8")) > 4096
    ):
        fail(f"{label} is unsafe")
    if path.parts[0] not in COMPILE_CACHE_SUBDIRECTORIES:
        fail(f"{label} escapes the compiled-cache members")
    return path


def require_directory(
    path: pathlib.Path,
    *,
    exact_mode: int | None,
) -> os.stat_result:
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise ModelLabError(
            f"compiled-cache directory is absent: {path}",
            code="compile_cache_operation_failed",
        ) from error
    if (
        not stat.S_ISDIR(path_stat.st_mode)
        or stat.S_ISLNK(path_stat.st_mode)
        or path_stat.st_uid != os.getuid()
        or file_mode(path_stat) & 0o002
        or (exact_mode is not None and file_mode(path_stat) != exact_mode)
    ):
        fail(f"compiled-cache directory has an unsafe identity: {path}")
    return path_stat


def directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def open_owned_untrusted_directory(path: pathlib.Path) -> int:
    """Open one persistent-volume directory without trusting permission bits."""

    descriptor = -1
    try:
        path_stat = path.lstat()
        descriptor = os.open(path, directory_flags())
    except OSError as error:
        raise ModelLabError(
            f"untrusted compiled-cache directory is absent or unsafe: {path}",
            code="compile_cache_operation_failed",
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(path_stat.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or path_stat.st_uid != os.getuid()
            or not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_dev != path_stat.st_dev
            or opened.st_ino != path_stat.st_ino
        ):
            fail(f"untrusted compiled-cache directory is unsafe: {path}")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def require_owned_untrusted_directory(path: pathlib.Path) -> os.stat_result:
    """Bind an owned non-symlink volume directory while ignoring forced 0777."""

    descriptor = open_owned_untrusted_directory(path)
    try:
        return os.fstat(descriptor)
    finally:
        os.close(descriptor)


def ensure_private_parents(
    *,
    anchor: pathlib.Path,
    parent: pathlib.Path,
) -> None:
    """Create exact mode-0700 descendants without following path components."""

    try:
        relative = parent.relative_to(anchor)
    except ValueError as error:
        raise ModelLabError(
            "compiled-cache path escapes its runtime root",
            code="compile_cache_operation_failed",
        ) from error
    anchor_descriptor = os.open(anchor, directory_flags())
    current_descriptor = anchor_descriptor
    try:
        anchor_stat = os.fstat(anchor_descriptor)
        if (
            not stat.S_ISDIR(anchor_stat.st_mode)
            or anchor_stat.st_uid != os.getuid()
            or file_mode(anchor_stat) & 0o002
        ):
            fail(f"compiled-cache anchor is unsafe: {anchor}")
        for component in relative.parts:
            try:
                os.mkdir(component, 0o700, dir_fd=current_descriptor)
            except FileExistsError:
                pass
            child_descriptor = os.open(
                component,
                directory_flags(),
                dir_fd=current_descriptor,
            )
            child_stat = os.fstat(child_descriptor)
            if (
                not stat.S_ISDIR(child_stat.st_mode)
                or child_stat.st_uid != os.getuid()
                or file_mode(child_stat) != 0o700
            ):
                os.close(child_descriptor)
                fail(f"compiled-cache private parent has an unsafe identity: {parent}")
            if current_descriptor != anchor_descriptor:
                os.close(current_descriptor)
            current_descriptor = child_descriptor
    finally:
        if current_descriptor != anchor_descriptor:
            os.close(current_descriptor)
        os.close(anchor_descriptor)


def ensure_untrusted_parents(
    *,
    anchor: pathlib.Path,
    parent: pathlib.Path,
) -> None:
    """Create owned persistent descendants without treating modes as authority."""

    try:
        relative = parent.relative_to(anchor)
    except ValueError as error:
        raise ModelLabError(
            "compiled-cache path escapes its persistent runtime root",
            code="compile_cache_operation_failed",
        ) from error
    if any(component in {"", ".", ".."} for component in relative.parts):
        fail("compiled-cache persistent parent is not a strict descendant")
    current_descriptor = open_owned_untrusted_directory(anchor)
    try:
        for component in relative.parts:
            try:
                os.mkdir(component, 0o700, dir_fd=current_descriptor)
            except FileExistsError:
                pass
            child_descriptor = os.open(
                component,
                directory_flags(),
                dir_fd=current_descriptor,
            )
            child_stat = os.fstat(child_descriptor)
            if not stat.S_ISDIR(child_stat.st_mode) or child_stat.st_uid != os.getuid():
                os.close(child_descriptor)
                fail(
                    f"compiled-cache persistent parent has an unsafe identity: {parent}"
                )
            os.close(current_descriptor)
            current_descriptor = child_descriptor
    finally:
        os.close(current_descriptor)


def mkdir_exclusive(path: pathlib.Path, *, mode: int = 0o700) -> None:
    parent_descriptor = os.open(path.parent, directory_flags())
    try:
        os.mkdir(path.name, mode, dir_fd=parent_descriptor)
        descriptor = os.open(
            path.name,
            directory_flags(),
            dir_fd=parent_descriptor,
        )
        try:
            os.fchmod(descriptor, mode)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or opened.st_uid != os.getuid()
                or file_mode(opened) != mode
            ):
                fail(f"new compiled-cache directory is unsafe: {path}")
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)


def mkdir_untrusted_exclusive(path: pathlib.Path) -> None:
    """Create one owned persistent directory without requiring a retained mode."""

    parent_descriptor = open_owned_untrusted_directory(path.parent)
    try:
        os.mkdir(path.name, 0o700, dir_fd=parent_descriptor)
        descriptor = os.open(
            path.name,
            directory_flags(),
            dir_fd=parent_descriptor,
        )
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.getuid():
                fail(f"new persistent compiled-cache directory is unsafe: {path}")
        finally:
            os.close(descriptor)
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def fsync_directory(path: pathlib.Path) -> None:
    descriptor = os.open(path, directory_flags())
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.getuid():
            fail(f"cannot sync unsafe compiled-cache directory: {path}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_directory_noreplace(
    *,
    source: pathlib.Path,
    destination: pathlib.Path,
) -> None:
    """Atomically publish one sibling directory without a clobber window."""

    if source.parent != destination.parent or source.name == destination.name:
        fail("compiled-cache publication paths are not distinct siblings")
    parent_descriptor = open_owned_untrusted_directory(source.parent)
    source_descriptor = -1
    try:
        source_descriptor = os.open(
            source.name,
            directory_flags(),
            dir_fd=parent_descriptor,
        )
        source_stat = os.fstat(source_descriptor)
        current = os.stat(
            source.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(source_stat.st_mode)
            or source_stat.st_uid != os.getuid()
            or current.st_dev != source_stat.st_dev
            or current.st_ino != source_stat.st_ino
        ):
            fail("compiled-cache publication source changed while opening")
        try:
            renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
        except AttributeError as error:
            raise ModelLabError(
                "renameat2 is unavailable for no-clobber cache publication",
                code="compile_cache_operation_failed",
            ) from error
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            parent_descriptor,
            os.fsencode(source.name),
            parent_descriptor,
            os.fsencode(destination.name),
            1,  # RENAME_NOREPLACE
        )
        if result != 0:
            error_number = ctypes.get_errno()
            raise ModelLabError(
                "cannot atomically publish compiled-cache generation: "
                f"{os.strerror(error_number)}",
                code="compile_cache_operation_failed",
            )
        published_descriptor = os.open(
            destination.name,
            directory_flags(),
            dir_fd=parent_descriptor,
        )
        try:
            published = os.fstat(published_descriptor)
            if (
                published.st_dev != source_stat.st_dev
                or published.st_ino != source_stat.st_ino
            ):
                fail("compiled-cache publication changed during rename")
        finally:
            os.close(published_descriptor)
        os.fsync(parent_descriptor)
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        os.close(parent_descriptor)


def publish_file_noreplace(
    *,
    source: pathlib.Path,
    destination: pathlib.Path,
    mode: int,
) -> None:
    """Atomically move one exact regular file to an absent destination."""

    source_parent = open_owned_untrusted_directory(source.parent)
    destination_parent = open_owned_untrusted_directory(destination.parent)
    source_descriptor = -1
    try:
        source_descriptor = os.open(
            source.name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=source_parent,
        )
        source_stat = os.fstat(source_descriptor)
        current = os.stat(
            source.name,
            dir_fd=source_parent,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(source_stat.st_mode)
            or source_stat.st_uid != os.getuid()
            or source_stat.st_nlink != 1
            or file_mode(source_stat) != mode
            or current.st_dev != source_stat.st_dev
            or current.st_ino != source_stat.st_ino
        ):
            fail("compiled-cache publication file has an unsafe identity")
        try:
            renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
        except AttributeError as error:
            raise ModelLabError(
                "renameat2 is unavailable for no-clobber cache publication",
                code="compile_cache_operation_failed",
            ) from error
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            source_parent,
            os.fsencode(source.name),
            destination_parent,
            os.fsencode(destination.name),
            1,  # RENAME_NOREPLACE
        )
        if result != 0:
            error_number = ctypes.get_errno()
            raise ModelLabError(
                "cannot atomically publish compiled-cache document: "
                f"{os.strerror(error_number)}",
                code="compile_cache_operation_failed",
            )
        published_descriptor = os.open(
            destination.name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=destination_parent,
        )
        try:
            published = os.fstat(published_descriptor)
            if (
                published.st_dev != source_stat.st_dev
                or published.st_ino != source_stat.st_ino
            ):
                fail("compiled-cache document changed during publication")
        finally:
            os.close(published_descriptor)
        os.fsync(destination_parent)
        if source.parent != destination.parent:
            os.fsync(source_parent)
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        os.close(destination_parent)
        os.close(source_parent)


def probe_directory_noreplace(
    *,
    parent: pathlib.Path,
    cache_id: str,
) -> None:
    """Prove the persistent filesystem supports atomic no-clobber rename."""

    if not is_sha256(cache_id):
        fail("compiled-cache publication probe identity is malformed")
    parent_descriptor = open_owned_untrusted_directory(parent)
    source_name = f".{cache_id}.rename-probe-source"
    destination_name = f".{cache_id}.rename-probe-destination"

    def remove_empty_probe(name: str) -> None:
        try:
            opened = os.open(
                name,
                directory_flags(),
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            return
        try:
            value = os.fstat(opened)
            if (
                not stat.S_ISDIR(value.st_mode)
                or value.st_uid != os.getuid()
                or list(os.scandir(opened))
            ):
                fail("compiled-cache publication probe state is unsafe")
        finally:
            os.close(opened)
        os.rmdir(name, dir_fd=parent_descriptor)

    try:
        remove_empty_probe(source_name)
        remove_empty_probe(destination_name)
        os.mkdir(source_name, 0o500, dir_fd=parent_descriptor)
        os.mkdir(destination_name, 0o700, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
        try:
            renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
        except AttributeError as error:
            raise ModelLabError(
                "renameat2 is unavailable for cache publication probe",
                code="compile_cache_operation_failed",
            ) from error
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        collision = renameat2(
            parent_descriptor,
            os.fsencode(source_name),
            parent_descriptor,
            os.fsencode(destination_name),
            1,
        )
        if collision == 0 or ctypes.get_errno() != errno.EEXIST:
            fail("persistent filesystem lacks proven RENAME_NOREPLACE semantics")
        remove_empty_probe(destination_name)
        published = renameat2(
            parent_descriptor,
            os.fsencode(source_name),
            parent_descriptor,
            os.fsencode(destination_name),
            1,
        )
        if published != 0:
            error_number = ctypes.get_errno()
            raise ModelLabError(
                "persistent filesystem rejected RENAME_NOREPLACE probe: "
                f"{os.strerror(error_number)}",
                code="compile_cache_operation_failed",
            )
        remove_empty_probe(destination_name)
        os.fsync(parent_descriptor)
    except BaseException:
        remove_empty_probe(source_name)
        remove_empty_probe(destination_name)
        os.fsync(parent_descriptor)
        raise
    finally:
        os.close(parent_descriptor)


def write_all(descriptor: int, payload: bytes | memoryview) -> None:
    view = memoryview(payload)
    position = 0
    try:
        while position < len(view):
            try:
                written = os.write(descriptor, view[position:])
            except InterruptedError:
                continue
            if written <= 0:
                fail("compiled-cache write made no progress")
            position += written
    finally:
        view.release()


def write_json_exclusive(
    path: pathlib.Path,
    value: dict[str, Any],
    *,
    mode: int,
    durable: bool,
) -> tuple[bytes, str]:
    payload = canonical_bytes(value)
    if not 1 <= len(payload) <= MAX_DOCUMENT_BYTES:
        fail("compiled-cache document exceeds its size bound")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, mode)
    except OSError as error:
        raise ModelLabError(
            f"refusing to replace compiled-cache artifact: {path}",
            code="compile_cache_operation_failed",
        ) from error
    try:
        write_all(descriptor, payload)
        os.fchmod(descriptor, mode)
        if durable:
            os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return payload, sha256_bytes(payload)


def write_json_resumable(
    path: pathlib.Path,
    value: dict[str, Any],
    *,
    mode: int,
    durable: bool,
) -> tuple[bytes, str]:
    """Resume one deterministic small document without unlink or truncation."""

    payload = canonical_bytes(value)
    if not 1 <= len(payload) <= MAX_DOCUMENT_BYTES:
        fail("compiled-cache document exceeds its size bound")
    if os.path.lexists(path):
        path_stat = path.lstat()
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or path_stat.st_uid != os.getuid()
            or path_stat.st_nlink != 1
            or file_mode(path_stat) not in {0o600, mode}
            or path_stat.st_size > len(payload)
        ):
            fail("resumable compiled-cache document has an unsafe identity")
        writable = file_mode(path_stat) == 0o600
        descriptor_value = os.open(
            path,
            (os.O_RDWR if writable else os.O_RDONLY)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    else:
        writable = True
        descriptor_value = os.open(
            path,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        path_stat = os.fstat(descriptor_value)
    try:
        existing = b""
        while len(existing) < path_stat.st_size:
            chunk = os.pread(
                descriptor_value,
                path_stat.st_size - len(existing),
                len(existing),
            )
            if not chunk:
                fail("resumable compiled-cache document read made no progress")
            existing += chunk
        if not payload.startswith(existing):
            fail("interrupted compiled-cache document prefix changed")
        if len(existing) < len(payload):
            if not writable:
                fail("completed compiled-cache document is truncated")
            tail = payload[len(existing) :]
            position = 0
            while position < len(tail):
                written = os.pwrite(
                    descriptor_value,
                    tail[position:],
                    len(existing) + position,
                )
                if written <= 0:
                    fail("resumed compiled-cache document write made no progress")
                position += written
        final = os.fstat(descriptor_value)
        if final.st_size != len(payload):
            fail("resumed compiled-cache document has an unexpected tail")
        if file_mode(final) != mode:
            os.fchmod(descriptor_value, mode)
        if durable:
            os.fsync(descriptor_value)
    finally:
        os.close(descriptor_value)
    return payload, sha256_bytes(payload)


def hash_regular_file(path: pathlib.Path) -> tuple[str, os.stat_result]:
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise ModelLabError(
            f"compiled-cache file is absent: {path}",
            code="compile_cache_operation_failed",
        ) from error
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or stat.S_ISLNK(path_stat.st_mode)
        or path_stat.st_uid != os.getuid()
        or path_stat.st_nlink != 1
        or file_mode(path_stat) & 0o022
    ):
        fail(f"compiled-cache file has an unsafe identity: {path}")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != path_stat.st_dev
            or opened.st_ino != path_stat.st_ino
            or opened.st_nlink != 1
        ):
            fail(f"compiled-cache file changed while opening: {path}")
        while True:
            try:
                chunk = os.read(descriptor, COPY_BUFFER_BYTES)
            except InterruptedError:
                continue
            if not chunk:
                break
            digest.update(chunk)
        final = os.fstat(descriptor)
        if (
            final.st_size != opened.st_size
            or final.st_mtime_ns != opened.st_mtime_ns
            or final.st_ctime_ns != opened.st_ctime_ns
            or final.st_nlink != opened.st_nlink
        ):
            fail(f"compiled-cache file changed while hashing: {path}")
    finally:
        os.close(descriptor)
    return digest.hexdigest(), final


def inventory_compile_cache(root: pathlib.Path) -> dict[str, Any]:
    """Hash one private local cache tree into a canonical exact inventory."""

    require_directory(root, exact_mode=0o700)
    directories: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    try:
        for directory, directory_names, file_names in os.walk(
            root,
            topdown=True,
            followlinks=False,
        ):
            directory_path = pathlib.Path(directory)
            directory_names.sort()
            file_names.sort()
            for name in directory_names:
                child = directory_path / name
                child_stat = child.lstat()
                if (
                    not stat.S_ISDIR(child_stat.st_mode)
                    or stat.S_ISLNK(child_stat.st_mode)
                    or child_stat.st_uid != os.getuid()
                    or file_mode(child_stat) & 0o022
                ):
                    fail(f"compiled-cache tree contains an unsafe directory: {child}")
                relative = child.relative_to(root).as_posix()
                safe_relative(relative, label="compiled-cache directory")
                directories.append({"path": relative, "mode": file_mode(child_stat)})
                if len(directories) > MAX_CACHE_DIRECTORIES:
                    fail("compiled-cache tree has too many directories")
            for name in file_names:
                child = directory_path / name
                relative = child.relative_to(root).as_posix()
                safe_relative(relative, label="compiled-cache file")
                digest, child_stat = hash_regular_file(child)
                files.append(
                    {
                        "path": relative,
                        "mode": file_mode(child_stat),
                        "bytes": child_stat.st_size,
                        "sha256": digest,
                    }
                )
                if len(files) > MAX_CACHE_FILES:
                    fail("compiled-cache tree has too many files")
    except OSError as error:
        raise ModelLabError(
            f"cannot enumerate compiled-cache tree: {root}",
            code="compile_cache_operation_failed",
        ) from error
    directories.sort(key=lambda record: record["path"])
    files.sort(key=lambda record: record["path"])
    top_level = {
        record["path"]
        for record in directories
        if len(pathlib.PurePosixPath(record["path"]).parts) == 1
    }
    if top_level != set(COMPILE_CACHE_SUBDIRECTORIES):
        fail("compiled-cache tree does not have the exact required members")
    total_bytes = sum(record["bytes"] for record in files)
    if total_bytes > MAX_CACHE_BYTES:
        fail("compiled-cache tree exceeds its byte bound")
    unsigned = {
        "schema_version": COMPILE_CACHE_INVENTORY_SCHEMA,
        "directories": directories,
        "files": files,
        "directory_count": len(directories),
        "file_count": len(files),
        "total_bytes": total_bytes,
    }
    return {**unsigned, "sha256": sha256_document(unsigned)}


def validate_inventory(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _INVENTORY_FIELDS:
        fail("compiled-cache inventory fields are malformed")
    if value["schema_version"] != COMPILE_CACHE_INVENTORY_SCHEMA:
        fail("compiled-cache inventory schema is unsupported")
    directories = value["directories"]
    files = value["files"]
    if (
        not isinstance(directories, list)
        or not isinstance(files, list)
        or len(directories) > MAX_CACHE_DIRECTORIES
        or len(files) > MAX_CACHE_FILES
    ):
        fail("compiled-cache inventory collections are malformed")
    observed_directories: set[str] = set()
    for record in directories:
        if (
            not isinstance(record, dict)
            or set(record) != _DIRECTORY_RECORD_FIELDS
            or isinstance(record["mode"], bool)
            or not isinstance(record["mode"], int)
            or not 0 <= record["mode"] <= 0o7777
            or record["mode"] & 0o022
        ):
            fail("compiled-cache inventory has a malformed directory")
        relative = safe_relative(
            record["path"],
            label="compiled-cache inventory directory",
        ).as_posix()
        if relative in observed_directories:
            fail("compiled-cache inventory repeats a directory")
        observed_directories.add(relative)
    observed_files: set[str] = set()
    total_bytes = 0
    for record in files:
        if (
            not isinstance(record, dict)
            or set(record) != _FILE_RECORD_FIELDS
            or isinstance(record["mode"], bool)
            or not isinstance(record["mode"], int)
            or not 0 <= record["mode"] <= 0o7777
            or record["mode"] & 0o022
            or isinstance(record["bytes"], bool)
            or not isinstance(record["bytes"], int)
            or not 0 <= record["bytes"] <= MAX_CACHE_BYTES
            or not is_sha256(record["sha256"])
        ):
            fail("compiled-cache inventory has a malformed file")
        relative = safe_relative(
            record["path"],
            label="compiled-cache inventory file",
        ).as_posix()
        if relative in observed_files or relative in observed_directories:
            fail("compiled-cache inventory repeats a path")
        observed_files.add(relative)
        total_bytes += record["bytes"]
    for relative in observed_directories | observed_files:
        parent = pathlib.PurePosixPath(relative).parent.as_posix()
        if parent != "." and parent not in observed_directories:
            fail("compiled-cache inventory entry has no declared parent")
    top_level = {
        relative
        for relative in observed_directories
        if len(pathlib.PurePosixPath(relative).parts) == 1
    }
    unsigned = {key: item for key, item in value.items() if key != "sha256"}
    if (
        top_level != set(COMPILE_CACHE_SUBDIRECTORIES)
        or value["directories"]
        != sorted(value["directories"], key=lambda record: record["path"])
        or value["files"] != sorted(value["files"], key=lambda record: record["path"])
        or value["directory_count"] != len(directories)
        or value["file_count"] != len(files)
        or value["total_bytes"] != total_bytes
        or total_bytes > MAX_CACHE_BYTES
        or not is_sha256(value["sha256"])
        or sha256_document(unsigned) != value["sha256"]
    ):
        fail("compiled-cache inventory closure is inconsistent")
    return value


def descriptor(path: pathlib.Path) -> dict[str, Any]:
    digest, path_stat = hash_regular_file(path)
    return {
        "name": path.name,
        "bytes": path_stat.st_size,
        "sha256": digest,
    }


def read_exact_file(
    path: pathlib.Path,
    *,
    mode: int,
    maximum_bytes: int,
) -> tuple[bytes, os.stat_result]:
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise ModelLabError(
            f"compiled-cache artifact is absent: {path}",
            code="compile_cache_operation_failed",
        ) from error
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or stat.S_ISLNK(path_stat.st_mode)
        or path_stat.st_uid != os.getuid()
        or path_stat.st_nlink != 1
        or file_mode(path_stat) != mode
        or not 1 <= path_stat.st_size <= maximum_bytes
    ):
        fail(f"compiled-cache artifact has an unsafe identity: {path}")
    descriptor_value = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    chunks: list[bytes] = []
    try:
        opened = os.fstat(descriptor_value)
        if (
            opened.st_dev != path_stat.st_dev
            or opened.st_ino != path_stat.st_ino
            or opened.st_nlink != 1
        ):
            fail(f"compiled-cache artifact changed while opening: {path}")
        remaining = maximum_bytes + 1
        while remaining:
            try:
                chunk = os.read(
                    descriptor_value,
                    min(COPY_BUFFER_BYTES, remaining),
                )
            except InterruptedError:
                continue
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        final = os.fstat(descriptor_value)
        if (
            len(payload) > maximum_bytes
            or final.st_size != opened.st_size
            or final.st_mtime_ns != opened.st_mtime_ns
            or final.st_ctime_ns != opened.st_ctime_ns
        ):
            fail(f"compiled-cache artifact changed while reading: {path}")
    finally:
        os.close(descriptor_value)
    return payload, final


def read_exact_json(
    path: pathlib.Path,
    *,
    mode: int,
    maximum_bytes: int = MAX_DOCUMENT_BYTES,
) -> tuple[dict[str, Any], bytes]:
    payload, _ = read_exact_file(
        path,
        mode=mode,
        maximum_bytes=maximum_bytes,
    )

    def reject_duplicate_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, item in pairs:
            if key in document:
                fail(f"compiled-cache document repeats field {key!r}")
            document[key] = item
        return document

    try:
        value = json.loads(payload, object_pairs_hook=reject_duplicate_fields)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ModelLabError(
            f"compiled-cache document is malformed: {path}",
            code="compile_cache_operation_failed",
        ) from error
    if not isinstance(value, dict) or canonical_bytes(value) != payload:
        fail(f"compiled-cache document is not canonical: {path}")
    return value, payload
