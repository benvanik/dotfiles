"""Publish one exact Hugging Face closure onto ephemeral model storage."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import pathlib
import secrets
import stat
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Sequence

from model_lab.errors import ModelLabError

from .layout import REMOTE_SESSION_ROOT, RuntimeLayout
from .snapshot_stage import SNAPSHOT_STAGE_SCHEMA, SnapshotStage, verify_snapshot_stage
from .state import ensure_private_directory, open_advisory_lock


REMOTE_HUGGINGFACE_ROOT = pathlib.PurePosixPath("/workspace/.cache/huggingface")
REMOTE_HUGGINGFACE_HUB_ROOT = REMOTE_HUGGINGFACE_ROOT / "hub"
REMOTE_HUGGINGFACE_TOKEN = REMOTE_SESSION_ROOT / "secrets" / "huggingface" / "token"
SYSTEM_HUGGINGFACE_CLI = pathlib.Path("/usr/local/bin/hf")
HUGGINGFACE_ENDPOINT = "https://huggingface.co"
HUGGINGFACE_CACHE_WRITER_LOCK = "huggingface-cache-writer.lock"
COPY_BUFFER_BYTES = 64 * 1024 * 1024
MAX_DOWNLOAD_ARGUMENT_BYTES = 64 * 1024
SNAPSHOT_STAGE_FREE_SPACE_RESERVE_BYTES = 4 * 1024 * 1024 * 1024
HUGGINGFACE_CACHE_FREE_SPACE_RESERVE_BYTES = 4 * 1024 * 1024 * 1024
AT_FDCWD = -100
RENAME_NOREPLACE = 1

_SOURCE_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_SOURCE_FILE_FLAGS = (
    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_DESTINATION_FILE_FLAGS = (
    os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
)


def _fail(message: str, *, code: str = "huggingface_snapshot_stage_failed") -> None:
    raise ModelLabError(message, code=code)


def _mode(value: os.stat_result) -> int:
    return stat.S_IMODE(value.st_mode)


def _stat_identity(value: os.stat_result) -> dict[str, int]:
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
        "mode": _mode(value),
    }


def _rename_no_replace(
    source: pathlib.Path,
    destination: pathlib.Path,
) -> None:
    """Atomically publish one Linux path while refusing any destination."""

    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as error:
        raise ModelLabError(
            "Linux no-replace rename is unavailable",
            code="huggingface_snapshot_publication_unsupported",
        ) from error
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        AT_FDCWD,
        os.fsencode(source),
        AT_FDCWD,
        os.fsencode(destination),
        RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise ModelLabError(
            f"snapshot publication refuses an existing destination: {destination}",
            code="huggingface_snapshot_stage_collision",
        )
    raise ModelLabError(
        f"cannot atomically publish snapshot path {destination}: "
        f"{os.strerror(error_number)}",
        code="huggingface_snapshot_publication_failed",
    )


def _relative_parts(value: Any, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, str):
        _fail(f"{label} is not a path")
    path = pathlib.PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        _fail(f"{label} is not a safe relative path")
    return path.parts


def _validate_source_directory_stat(
    value: os.stat_result,
    *,
    label: str,
) -> None:
    """Validate identity, but not privacy, on the untrusted network volume."""

    if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.getuid():
        _fail(
            f"Hugging Face cache directory is unsafe: {label}",
            code="unsafe_huggingface_snapshot_source",
        )


def _validate_private_directory_stat(
    value: os.stat_result,
    *,
    label: str,
) -> None:
    if (
        not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.getuid()
        or _mode(value) != 0o700
    ):
        _fail(
            f"local snapshot directory is unsafe: {label}",
            code="unsafe_huggingface_snapshot_destination",
        )


def _open_absolute_source_directory(path: pathlib.Path) -> int:
    try:
        path_stat = path.lstat()
        descriptor = os.open(path, _SOURCE_DIRECTORY_FLAGS)
    except OSError as error:
        raise ModelLabError(
            f"cannot safely open Hugging Face cache root: {path}",
            code="unsafe_huggingface_snapshot_source",
        ) from error
    try:
        opened = os.fstat(descriptor)
        _validate_source_directory_stat(opened, label=str(path))
        if (
            stat.S_ISLNK(path_stat.st_mode)
            or opened.st_dev != path_stat.st_dev
            or opened.st_ino != path_stat.st_ino
        ):
            _fail(
                f"Hugging Face cache root changed while opening: {path}",
                code="unsafe_huggingface_snapshot_source",
            )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_child_directory(
    parent_descriptor: int,
    name: str,
    *,
    private: bool,
    missing_ok: bool = False,
) -> int | None:
    try:
        descriptor = os.open(
            name,
            _SOURCE_DIRECTORY_FLAGS,
            dir_fd=parent_descriptor,
        )
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    except OSError as error:
        raise ModelLabError(
            f"cannot safely open directory component {name!r}",
            code=(
                "unsafe_huggingface_snapshot_destination"
                if private
                else "unsafe_huggingface_snapshot_source"
            ),
        ) from error
    try:
        opened = os.fstat(descriptor)
        if private:
            _validate_private_directory_stat(opened, label=name)
        else:
            _validate_source_directory_stat(opened, label=name)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _create_or_open_private_child(
    parent_descriptor: int,
    name: str,
) -> int:
    descriptor = _open_child_directory(
        parent_descriptor,
        name,
        private=True,
        missing_ok=True,
    )
    if descriptor is not None:
        return descriptor
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    except FileExistsError:
        pass
    except OSError as error:
        raise ModelLabError(
            f"cannot create private snapshot directory {name!r}",
            code="unsafe_huggingface_snapshot_destination",
        ) from error
    descriptor = _open_child_directory(
        parent_descriptor,
        name,
        private=True,
    )
    if descriptor is None:
        _fail(
            f"private snapshot directory disappeared: {name}",
            code="unsafe_huggingface_snapshot_destination",
        )
    return descriptor


def _create_cache_directories(layout: RuntimeLayout) -> None:
    """Create only the fixed standard cache parents without following links."""

    descriptor = _open_absolute_source_directory(layout.workspace_root)
    try:
        for name in (".cache", "huggingface", "hub"):
            child = _open_child_directory(
                descriptor,
                name,
                private=False,
                missing_ok=True,
            )
            if child is None:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
                except OSError as error:
                    raise ModelLabError(
                        f"cannot create Hugging Face cache directory {name!r}",
                        code="unsafe_huggingface_snapshot_source",
                    ) from error
                child = _open_child_directory(
                    descriptor,
                    name,
                    private=False,
                )
            if child is None:
                _fail(
                    f"Hugging Face cache directory disappeared: {name}",
                    code="unsafe_huggingface_snapshot_source",
                )
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)


def _repository_cache_name(repository: str) -> str:
    parts = _relative_parts(repository, label="Hugging Face repository")
    if len(parts) != 2:
        _fail("Hugging Face repository must contain one namespace and name")
    return f"models--{parts[0]}--{parts[1]}"


def _expected_link_target(
    *,
    member_parts: Sequence[str],
    digest: str,
) -> str:
    return "/".join((*(".." for _ in range(len(member_parts) + 1)), "blobs", digest))


@dataclass
class _CacheSnapshot:
    """Open directory handles for one exact standard Hub cache snapshot."""

    revision_descriptor: int
    blobs_descriptor: int

    def close(self) -> None:
        os.close(self.revision_descriptor)
        os.close(self.blobs_descriptor)

    def __enter__(self) -> _CacheSnapshot:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _member_parent(
        self,
        member_parts: Sequence[str],
        *,
        missing_ok: bool,
    ) -> int | None:
        descriptor = os.dup(self.revision_descriptor)
        try:
            for component in member_parts[:-1]:
                child = _open_child_directory(
                    descriptor,
                    component,
                    private=False,
                    missing_ok=missing_ok,
                )
                if child is None:
                    os.close(descriptor)
                    return None
                os.close(descriptor)
                descriptor = child
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def member_available(self, record: dict[str, Any]) -> bool:
        member_parts = _relative_parts(
            record["path"],
            label="Hugging Face closure member",
        )
        parent = self._member_parent(member_parts, missing_ok=True)
        if parent is None:
            return False
        try:
            name = member_parts[-1]
            try:
                link_stat = os.stat(
                    name,
                    dir_fd=parent,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return False
            if not stat.S_ISLNK(link_stat.st_mode) or link_stat.st_uid != os.getuid():
                _fail(
                    f"Hub snapshot member is not an owned cache link: {record['path']}",
                    code="unsafe_huggingface_snapshot_source",
                )
            try:
                target = os.readlink(name, dir_fd=parent)
            except OSError as error:
                raise ModelLabError(
                    f"cannot read Hub snapshot link: {record['path']}",
                    code="unsafe_huggingface_snapshot_source",
                ) from error
            digest = record["identity"]["digest"]
            expected_target = _expected_link_target(
                member_parts=member_parts,
                digest=digest,
            )
            if target != expected_target:
                _fail(
                    f"Hub snapshot link escapes its exact closure blob: "
                    f"{record['path']}",
                    code="unsafe_huggingface_snapshot_source",
                )
            try:
                blob_stat = os.stat(
                    digest,
                    dir_fd=self.blobs_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return False
            if (
                not stat.S_ISREG(blob_stat.st_mode)
                or blob_stat.st_uid != os.getuid()
                or blob_stat.st_nlink < 1
                or blob_stat.st_size != record["bytes"]
            ):
                _fail(
                    f"Hub cache blob is unsafe: {record['path']}",
                    code="unsafe_huggingface_snapshot_source",
                )
            return True
        finally:
            os.close(parent)

    def open_member(self, record: dict[str, Any]) -> int:
        if not self.member_available(record):
            _fail(
                f"Hub cache omitted closure member: {record['path']}",
                code="huggingface_snapshot_source_incomplete",
            )
        digest = record["identity"]["digest"]
        try:
            descriptor = os.open(
                digest,
                _SOURCE_FILE_FLAGS,
                dir_fd=self.blobs_descriptor,
            )
        except OSError as error:
            raise ModelLabError(
                f"cannot safely open Hub cache blob: {record['path']}",
                code="unsafe_huggingface_snapshot_source",
            ) from error
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.getuid()
                or opened.st_nlink < 1
                or opened.st_size != record["bytes"]
            ):
                _fail(
                    f"Hub cache blob changed while opening: {record['path']}",
                    code="unsafe_huggingface_snapshot_source",
                )
            if hasattr(os, "posix_fadvise") and hasattr(
                os,
                "POSIX_FADV_SEQUENTIAL",
            ):
                os.posix_fadvise(
                    descriptor,
                    0,
                    0,
                    os.POSIX_FADV_SEQUENTIAL,
                )
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor


def _open_cache_snapshot(
    *,
    layout: RuntimeLayout,
    closure: dict[str, Any],
    missing_ok: bool,
) -> _CacheSnapshot | None:
    source = closure["source"]
    repository_cache_name = _repository_cache_name(source["repository"])
    revision = source["revision"]
    if len(revision) != 40 or any(
        character not in "0123456789abcdef" for character in revision
    ):
        _fail("Hugging Face closure revision is not an exact commit")

    descriptor = _open_absolute_source_directory(layout.workspace_root)
    opened: list[int] = [descriptor]
    try:
        for component in (
            ".cache",
            "huggingface",
            "hub",
            repository_cache_name,
        ):
            child = _open_child_directory(
                descriptor,
                component,
                private=False,
                missing_ok=missing_ok,
            )
            if child is None:
                return None
            opened.append(child)
            descriptor = child
        model_descriptor = descriptor
        blobs = _open_child_directory(
            model_descriptor,
            "blobs",
            private=False,
            missing_ok=missing_ok,
        )
        if blobs is None:
            return None
        opened.append(blobs)
        snapshots = _open_child_directory(
            model_descriptor,
            "snapshots",
            private=False,
            missing_ok=missing_ok,
        )
        if snapshots is None:
            return None
        opened.append(snapshots)
        revision_descriptor = _open_child_directory(
            snapshots,
            revision,
            private=False,
            missing_ok=missing_ok,
        )
        if revision_descriptor is None:
            return None
        opened.append(revision_descriptor)
        opened.remove(blobs)
        opened.remove(revision_descriptor)
        return _CacheSnapshot(
            revision_descriptor=revision_descriptor,
            blobs_descriptor=blobs,
        )
    finally:
        for item in reversed(opened):
            os.close(item)


def _missing_cache_members(
    *,
    layout: RuntimeLayout,
    closure: dict[str, Any],
) -> list[str]:
    snapshot = _open_cache_snapshot(
        layout=layout,
        closure=closure,
        missing_ok=True,
    )
    if snapshot is None:
        return [record["path"] for record in closure["files"]]
    with snapshot:
        return [
            record["path"]
            for record in closure["files"]
            if not snapshot.member_available(record)
        ]


def _require_huggingface_cli(path: pathlib.Path) -> None:
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise ModelLabError(
            f"pinned runtime Hugging Face CLI is absent: {path}",
            code="huggingface_snapshot_download_unavailable",
        ) from error
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or stat.S_ISLNK(path_stat.st_mode)
        or path_stat.st_uid != os.getuid()
        or path_stat.st_nlink != 1
        or path_stat.st_size <= 0
        or _mode(path_stat) & 0o111 == 0
        or _mode(path_stat) & 0o022
    ):
        _fail(
            f"pinned runtime Hugging Face CLI is unsafe: {path}",
            code="huggingface_snapshot_download_unavailable",
        )


def _validated_token_path(layout: RuntimeLayout) -> pathlib.Path | None:
    token_path = layout.localize(REMOTE_HUGGINGFACE_TOKEN)
    if not os.path.lexists(token_path):
        return None
    for directory in (token_path.parent.parent, token_path.parent):
        try:
            directory_stat = directory.lstat()
        except OSError as error:
            raise ModelLabError(
                "Hugging Face token lease parent is unavailable",
                code="unsafe_huggingface_token_lease",
            ) from error
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or stat.S_ISLNK(directory_stat.st_mode)
            or directory_stat.st_uid != os.getuid()
            or _mode(directory_stat) != 0o700
        ):
            _fail(
                "Hugging Face token lease parent is unsafe",
                code="unsafe_huggingface_token_lease",
            )
    try:
        token_stat = token_path.lstat()
        descriptor = os.open(token_path, _SOURCE_FILE_FLAGS)
    except OSError as error:
        raise ModelLabError(
            "cannot safely open Hugging Face token lease",
            code="unsafe_huggingface_token_lease",
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(token_stat.st_mode)
            or stat.S_ISLNK(token_stat.st_mode)
            or token_stat.st_uid != os.getuid()
            or token_stat.st_nlink != 1
            or _mode(token_stat) != 0o600
            or not 1 <= token_stat.st_size <= 4096
            or opened.st_dev != token_stat.st_dev
            or opened.st_ino != token_stat.st_ino
            or opened.st_size != token_stat.st_size
            or opened.st_mtime_ns != token_stat.st_mtime_ns
            or opened.st_ctime_ns != token_stat.st_ctime_ns
        ):
            _fail(
                "Hugging Face token lease has an unsafe identity",
                code="unsafe_huggingface_token_lease",
            )
    finally:
        os.close(descriptor)
    return token_path


def _download_chunks(paths: Sequence[str]) -> Iterator[tuple[str, ...]]:
    current: list[str] = []
    current_bytes = 0
    for path in paths:
        path_bytes = len(path.encode("utf-8")) + 1
        if path_bytes > MAX_DOWNLOAD_ARGUMENT_BYTES:
            _fail(
                "Hugging Face closure member exceeds the download argument bound",
                code="huggingface_snapshot_download_unavailable",
            )
        if current and current_bytes + path_bytes > MAX_DOWNLOAD_ARGUMENT_BYTES:
            yield tuple(current)
            current = []
            current_bytes = 0
        current.append(path)
        current_bytes += path_bytes
    if current:
        yield tuple(current)


def _download_missing_members(
    *,
    layout: RuntimeLayout,
    closure: dict[str, Any],
    paths: Sequence[str],
    command_runner: Callable[..., Any],
    huggingface_cli: pathlib.Path,
) -> str:
    _require_huggingface_cli(huggingface_cli)
    _create_cache_directories(layout)
    token_path = _validated_token_path(layout)
    huggingface_root = layout.localize(REMOTE_HUGGINGFACE_ROOT)
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(layout.session_root),
        "DO_NOT_TRACK": "1",
        "HF_ASSETS_CACHE": str(huggingface_root / "assets"),
        "HF_ENDPOINT": HUGGINGFACE_ENDPOINT,
        "HF_HOME": str(huggingface_root),
        "HF_HUB_CACHE": str(huggingface_root / "hub"),
        "HF_HUB_DISABLE_IMPLICIT_TOKEN": "0" if token_path else "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "HF_HUB_DISABLE_UPDATE_CHECK": "1",
        "HF_HUB_DISABLE_XET": "0",
        "HF_XET_CACHE": str(huggingface_root / "xet"),
        "HF_XET_CHUNK_CACHE_SIZE_BYTES": "0",
        "HF_XET_HIGH_PERFORMANCE": "1",
    }
    authentication = "anonymous"
    if token_path is not None:
        environment["HF_TOKEN_PATH"] = str(token_path)
        authentication = "leased-token"
    source = closure["source"]
    for chunk in _download_chunks(paths):
        command = (
            str(huggingface_cli),
            "download",
            "--revision",
            source["revision"],
            "--",
            source["repository"],
            *chunk,
        )
        try:
            command_runner(
                command,
                check=True,
                cwd=layout.session_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                umask=0o077,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise ModelLabError(
                "Hugging Face CLI could not populate the persistent cache",
                code="huggingface_snapshot_download_failed",
            ) from error
    return authentication


def _identity_hasher(record: dict[str, Any]) -> Any:
    identity = record["identity"]
    if identity["algorithm"] == "sha256":
        return hashlib.sha256()
    if identity["algorithm"] == "git-blob-sha1":
        hasher = hashlib.sha1()
        hasher.update(f"blob {record['bytes']}\0".encode("ascii"))
        return hasher
    _fail("Hugging Face closure member uses an unsupported identity")


def _hash_open_file(
    descriptor: int,
    *,
    record: dict[str, Any],
    opened: os.stat_result,
) -> None:
    hasher = _identity_hasher(record)
    observed_bytes = 0
    while True:
        chunk = os.read(descriptor, min(COPY_BUFFER_BYTES, record["bytes"] + 1))
        if not chunk:
            break
        hasher.update(chunk)
        observed_bytes += len(chunk)
        if observed_bytes > record["bytes"]:
            break
    final = os.fstat(descriptor)
    if (
        observed_bytes != record["bytes"]
        or final.st_size != opened.st_size
        or final.st_mtime_ns != opened.st_mtime_ns
        or final.st_ctime_ns != opened.st_ctime_ns
        or hasher.hexdigest() != record["identity"]["digest"]
    ):
        _fail(
            f"snapshot member content does not match closure: {record['path']}",
            code="huggingface_snapshot_content_mismatch",
        )


def _copy_member(
    *,
    cache: _CacheSnapshot,
    destination_parent: int,
    destination_name: str,
    record: dict[str, Any],
    existing_partial: os.stat_result | None,
) -> dict[str, Any]:
    source_descriptor = cache.open_member(record)
    destination_descriptor = -1
    try:
        if existing_partial is None:
            destination_descriptor = os.open(
                destination_name,
                _DESTINATION_FILE_FLAGS | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=destination_parent,
            )
        else:
            destination_descriptor = os.open(
                destination_name,
                _DESTINATION_FILE_FLAGS,
                dir_fd=destination_parent,
            )
            opened_partial = os.fstat(destination_descriptor)
            if (
                opened_partial.st_dev != existing_partial.st_dev
                or opened_partial.st_ino != existing_partial.st_ino
                or not stat.S_ISREG(opened_partial.st_mode)
                or opened_partial.st_uid != os.getuid()
                or opened_partial.st_nlink != 1
                or _mode(opened_partial) != 0o600
            ):
                _fail(
                    f"partial snapshot member changed before resume: {record['path']}",
                    code="unsafe_huggingface_snapshot_destination",
                )
            os.ftruncate(destination_descriptor, 0)
        source_opened = os.fstat(source_descriptor)
        hasher = _identity_hasher(record)
        copied_bytes = 0
        buffer = bytearray(min(COPY_BUFFER_BYTES, max(record["bytes"], 1)))
        view = memoryview(buffer)
        try:
            while True:
                read_bytes = os.readv(source_descriptor, [view])
                if read_bytes == 0:
                    break
                chunk = view[:read_bytes]
                try:
                    hasher.update(chunk)
                    position = 0
                    while position < read_bytes:
                        written = os.write(
                            destination_descriptor,
                            chunk[position:],
                        )
                        if written <= 0:
                            _fail("snapshot copy made no forward progress")
                        position += written
                finally:
                    chunk.release()
                copied_bytes += read_bytes
                if copied_bytes > record["bytes"]:
                    break
        finally:
            view.release()
        source_final = os.fstat(source_descriptor)
        if (
            copied_bytes != record["bytes"]
            or source_final.st_size != source_opened.st_size
            or source_final.st_mtime_ns != source_opened.st_mtime_ns
            or source_final.st_ctime_ns != source_opened.st_ctime_ns
            or hasher.hexdigest() != record["identity"]["digest"]
        ):
            _fail(
                f"Hub cache content does not match closure: {record['path']}",
                code="huggingface_snapshot_content_mismatch",
            )
        os.fchmod(destination_descriptor, 0o400)
        os.fsync(destination_descriptor)
        destination_stat = os.fstat(destination_descriptor)
        return {**record, **_stat_identity(destination_stat)}
    finally:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        os.close(source_descriptor)


def _destination_parent(
    root_descriptor: int,
    member_parts: Sequence[str],
    *,
    create: bool,
) -> int:
    descriptor = os.dup(root_descriptor)
    try:
        for component in member_parts[:-1]:
            if create:
                child = _create_or_open_private_child(descriptor, component)
            else:
                child = _open_child_directory(
                    descriptor,
                    component,
                    private=True,
                )
                if child is None:
                    _fail(
                        f"snapshot directory disappeared: {component}",
                        code="unsafe_huggingface_snapshot_destination",
                    )
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _existing_member(
    *,
    root_descriptor: int,
    record: dict[str, Any],
    allow_partial: bool,
) -> tuple[dict[str, Any] | None, os.stat_result | None]:
    member_parts = _relative_parts(
        record["path"],
        label="Hugging Face closure member",
    )
    try:
        parent = _destination_parent(
            root_descriptor,
            member_parts,
            create=False,
        )
    except FileNotFoundError:
        return None, None
    try:
        try:
            path_stat = os.stat(
                member_parts[-1],
                dir_fd=parent,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None, None
        allowed_modes = {0o400, 0o600} if allow_partial else {0o400}
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or path_stat.st_uid != os.getuid()
            or path_stat.st_nlink != 1
            or _mode(path_stat) not in allowed_modes
        ):
            _fail(
                f"snapshot member is unsafe: {record['path']}",
                code="unsafe_huggingface_snapshot_destination",
            )
        if _mode(path_stat) == 0o600:
            return None, path_stat
        try:
            descriptor = os.open(
                member_parts[-1],
                _SOURCE_FILE_FLAGS,
                dir_fd=parent,
            )
        except OSError as error:
            raise ModelLabError(
                f"cannot safely open snapshot member: {record['path']}",
                code="unsafe_huggingface_snapshot_destination",
            ) from error
        try:
            opened = os.fstat(descriptor)
            if (
                opened.st_dev != path_stat.st_dev
                or opened.st_ino != path_stat.st_ino
                or opened.st_uid != os.getuid()
                or opened.st_nlink != 1
                or opened.st_size != record["bytes"]
                or _mode(opened) != 0o400
            ):
                _fail(
                    f"snapshot member changed while opening: {record['path']}",
                    code="unsafe_huggingface_snapshot_destination",
                )
            _hash_open_file(descriptor, record=record, opened=opened)
            return {**record, **_stat_identity(opened)}, None
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def _expected_directories(closure: dict[str, Any]) -> set[str]:
    return {
        pathlib.PurePosixPath(
            *pathlib.PurePosixPath(record["path"]).parts[:position]
        ).as_posix()
        for record in closure["files"]
        for position in range(1, len(pathlib.PurePosixPath(record["path"]).parts))
    }


def _validate_tree_entries(
    root: pathlib.Path,
    *,
    closure: dict[str, Any],
    allow_partial: bool,
) -> None:
    expected_files = {record["path"] for record in closure["files"]}
    expected_directories = _expected_directories(closure)
    observed_files: set[str] = set()
    observed_directories: set[str] = set()
    try:
        walker = os.walk(root, topdown=True, followlinks=False)
        for directory, directory_names, file_names in walker:
            directory_path = pathlib.Path(directory)
            for name in list(directory_names):
                child = directory_path / name
                child_stat = child.lstat()
                _validate_private_directory_stat(
                    child_stat,
                    label=child.relative_to(root).as_posix(),
                )
                observed_directories.add(child.relative_to(root).as_posix())
            for name in file_names:
                child = directory_path / name
                child_stat = child.lstat()
                allowed_modes = {0o400, 0o600} if allow_partial else {0o400}
                if (
                    not stat.S_ISREG(child_stat.st_mode)
                    or stat.S_ISLNK(child_stat.st_mode)
                    or child_stat.st_uid != os.getuid()
                    or child_stat.st_nlink != 1
                    or _mode(child_stat) not in allowed_modes
                ):
                    _fail(
                        f"snapshot tree contains an unsafe entry: {child}",
                        code="unsafe_huggingface_snapshot_destination",
                    )
                observed_files.add(child.relative_to(root).as_posix())
    except OSError as error:
        raise ModelLabError(
            f"cannot enumerate local snapshot tree: {root}",
            code="unsafe_huggingface_snapshot_destination",
        ) from error
    if (
        not observed_files.issubset(expected_files)
        or not observed_directories.issubset(expected_directories)
        or (not allow_partial and observed_files != expected_files)
        or (not allow_partial and observed_directories != expected_directories)
    ):
        _fail(
            "local snapshot tree contains an unexpected entry set",
            code="unsafe_huggingface_snapshot_destination",
        )


def _private_root_descriptor(path: pathlib.Path) -> int:
    try:
        path_stat = path.lstat()
        descriptor = os.open(path, _SOURCE_DIRECTORY_FLAGS)
    except OSError as error:
        raise ModelLabError(
            f"cannot safely open local snapshot directory: {path}",
            code="unsafe_huggingface_snapshot_destination",
        ) from error
    try:
        opened = os.fstat(descriptor)
        _validate_private_directory_stat(opened, label=str(path))
        if (
            stat.S_ISLNK(path_stat.st_mode)
            or opened.st_dev != path_stat.st_dev
            or opened.st_ino != path_stat.st_ino
        ):
            _fail(
                f"local snapshot directory changed while opening: {path}",
                code="unsafe_huggingface_snapshot_destination",
            )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _available_filesystem_bytes(
    *,
    root: pathlib.Path,
    root_descriptor: int,
    filesystem_status_reader: Callable[[int], os.statvfs_result],
    label: str,
) -> int:
    """Read descriptor-bound capacity while proving the path identity stayed put."""

    try:
        before = os.fstat(root_descriptor)
        filesystem_status = filesystem_status_reader(root_descriptor)
        after = os.fstat(root_descriptor)
        path_stat = root.lstat()
    except OSError as error:
        raise ModelLabError(
            f"cannot inspect {label} filesystem capacity",
            code="huggingface_snapshot_capacity_unavailable",
        ) from error
    if (
        stat.S_ISLNK(path_stat.st_mode)
        or not stat.S_ISDIR(before.st_mode)
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_dev != path_stat.st_dev
        or before.st_ino != path_stat.st_ino
    ):
        _fail(
            f"{label} root changed during filesystem capacity inspection",
            code="huggingface_snapshot_capacity_unavailable",
        )
    fragment_size = filesystem_status.f_frsize
    available_blocks = filesystem_status.f_bavail
    if (
        isinstance(fragment_size, bool)
        or not isinstance(fragment_size, int)
        or fragment_size <= 0
        or isinstance(available_blocks, bool)
        or not isinstance(available_blocks, int)
        or available_blocks < 0
    ):
        _fail(
            f"{label} filesystem returned invalid capacity fields",
            code="huggingface_snapshot_capacity_unavailable",
        )
    return fragment_size * available_blocks


def _require_filesystem_headroom(
    *,
    root: pathlib.Path,
    root_descriptor: int,
    required_growth_bytes: int,
    reserve_bytes: int,
    filesystem_status_reader: Callable[[int], os.statvfs_result],
    label: str,
    insufficient_code: str,
) -> None:
    if required_growth_bytes < 0 or reserve_bytes < 0:
        _fail(
            f"{label} capacity requirement is invalid",
            code="huggingface_snapshot_capacity_unavailable",
        )
    available_bytes = _available_filesystem_bytes(
        root=root,
        root_descriptor=root_descriptor,
        filesystem_status_reader=filesystem_status_reader,
        label=label,
    )
    required_bytes = required_growth_bytes + reserve_bytes
    if available_bytes < required_bytes:
        raise ModelLabError(
            f"{label} has {available_bytes} available bytes; "
            f"{required_growth_bytes} closure bytes plus the explicit "
            f"{reserve_bytes}-byte safety reserve require {required_bytes}",
            code=insufficient_code,
        )


def _content_records(
    root: pathlib.Path,
    *,
    closure: dict[str, Any],
    allow_partial: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    _validate_tree_entries(
        root,
        closure=closure,
        allow_partial=allow_partial,
    )
    root_descriptor = _private_root_descriptor(root)
    try:
        complete: list[dict[str, Any]] = []
        incomplete: list[dict[str, Any]] = []
        for record in closure["files"]:
            existing, partial = _existing_member(
                root_descriptor=root_descriptor,
                record=record,
                allow_partial=allow_partial,
            )
            if existing is not None:
                complete.append(existing)
            else:
                incomplete.append(
                    {
                        "record": record,
                        "partial_stat": partial,
                    }
                )
        if not allow_partial and incomplete:
            _fail(
                "published local snapshot is incomplete",
                code="unsafe_huggingface_snapshot_destination",
            )
        return complete, incomplete
    finally:
        os.close(root_descriptor)


def _remaining_stage_bytes(incomplete: Sequence[dict[str, Any]]) -> int:
    """Return conservative final allocation growth for an interrupted stage."""

    remaining_bytes = 0
    for item in incomplete:
        record = item["record"]
        byte_count = record["bytes"]
        if isinstance(byte_count, bool) or not isinstance(byte_count, int):
            _fail("Hugging Face closure member byte count is invalid")
        partial = item["partial_stat"]
        retained_bytes = 0
        if partial is not None:
            allocated_blocks = partial.st_blocks
            if (
                isinstance(allocated_blocks, bool)
                or not isinstance(allocated_blocks, int)
                or allocated_blocks < 0
                or partial.st_size < 0
            ):
                _fail(
                    f"partial snapshot member has invalid allocation metadata: "
                    f"{record['path']}",
                    code="unsafe_huggingface_snapshot_destination",
                )
            allocated_bytes = allocated_blocks * 512
            retained_bytes = min(
                byte_count,
                partial.st_size,
                allocated_bytes,
            )
        remaining_bytes += byte_count - retained_bytes
    return remaining_bytes


def _closure_bytes_for_paths(
    *,
    closure: dict[str, Any],
    paths: Sequence[str],
) -> int:
    records_by_path = {record["path"]: record for record in closure["files"]}
    if len(records_by_path) != len(closure["files"]) or len(set(paths)) != len(paths):
        _fail("Hugging Face closure member paths are not unique")
    try:
        records = [records_by_path[path] for path in paths]
    except KeyError as error:
        _fail(f"unknown Hugging Face closure member: {error.args[0]}")
    return sum(record["bytes"] for record in records)


def _write_receipt_no_clobber(
    path: pathlib.Path,
    receipt: dict[str, Any],
) -> None:
    import json

    payload = (
        json.dumps(
            receipt,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("ascii")
    temporary = path.parent / f".{path.name}.{secrets.token_hex(16)}"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    temporary_stat: os.stat_result | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        temporary_stat = os.fstat(descriptor)
        position = 0
        while position < len(payload):
            written = os.write(descriptor, payload[position:])
            if written <= 0:
                _fail("snapshot receipt write made no forward progress")
            position += written
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if os.path.lexists(path):
            _fail(
                "snapshot stage receipt appeared during publication",
                code="huggingface_snapshot_stage_collision",
            )
        _rename_no_replace(temporary, path)
        parent_descriptor = _private_root_descriptor(path.parent)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_stat is not None and os.path.lexists(temporary):
            cleanup_descriptor = os.open(temporary, _SOURCE_FILE_FLAGS)
            try:
                current = os.fstat(cleanup_descriptor)
                path_stat = temporary.lstat()
                if (
                    current.st_dev != temporary_stat.st_dev
                    or current.st_ino != temporary_stat.st_ino
                    or path_stat.st_dev != temporary_stat.st_dev
                    or path_stat.st_ino != temporary_stat.st_ino
                    or not stat.S_ISREG(current.st_mode)
                    or current.st_uid != os.getuid()
                    or current.st_nlink != 1
                    or _mode(current) != 0o600
                ):
                    _fail(
                        "snapshot receipt temporary changed before cleanup",
                        code="unsafe_huggingface_snapshot_destination",
                    )
                temporary.unlink()
            finally:
                os.close(cleanup_descriptor)
        try:
            temporary_stat = temporary.lstat()
        except FileNotFoundError:
            pass
        else:
            if (
                not stat.S_ISREG(temporary_stat.st_mode)
                or temporary_stat.st_uid != os.getuid()
                or temporary_stat.st_nlink != 1
                or _mode(temporary_stat) != 0o600
            ):
                _fail(
                    "snapshot receipt temporary has an unsafe identity",
                    code="unsafe_huggingface_snapshot_destination",
                )
            temporary.unlink()


def _publish_receipt(
    *,
    closure: dict[str, Any],
    canonical_snapshot_root: pathlib.PurePosixPath,
    local_snapshot_root: pathlib.Path,
    receipt_path: pathlib.Path,
    boot_id: str,
    records: Sequence[dict[str, Any]],
) -> SnapshotStage:
    root_stat = local_snapshot_root.lstat()
    _validate_private_directory_stat(root_stat, label=str(local_snapshot_root))
    receipt = {
        "schema_version": SNAPSHOT_STAGE_SCHEMA,
        "closure_sha256": closure["closure_sha256"],
        "source": closure["source"],
        "checkpoint": closure["checkpoint"],
        "snapshot_root": str(canonical_snapshot_root),
        "boot_id": boot_id,
        "directory_stat": _stat_identity(root_stat),
        "file_count": closure["file_count"],
        "total_bytes": closure["total_bytes"],
        "files": list(records),
    }
    _write_receipt_no_clobber(receipt_path, receipt)
    return verify_snapshot_stage(
        closure=closure,
        canonical_snapshot_root=canonical_snapshot_root,
        local_snapshot_root=local_snapshot_root,
        receipt_path=receipt_path,
        boot_id=boot_id,
    )


@dataclass(frozen=True)
class SnapshotStagePublication:
    """One created, recovered, or reused local closure publication."""

    stage: SnapshotStage
    disposition: str
    cache_source: str
    authentication: str | None

    def summary(self) -> dict[str, Any]:
        return {
            **self.stage.summary(),
            "disposition": self.disposition,
            "cache_source": self.cache_source,
            "authentication": self.authentication,
        }


def stage_huggingface_snapshot(
    *,
    closure: dict[str, Any],
    canonical_snapshot_root: pathlib.PurePosixPath,
    local_snapshot_root: pathlib.Path,
    receipt_path: pathlib.Path,
    layout: RuntimeLayout,
    boot_id: str,
    allow_download: bool = True,
    command_runner: Callable[..., Any] = subprocess.run,
    huggingface_cli: pathlib.Path = SYSTEM_HUGGINGFACE_CLI,
    filesystem_status_reader: Callable[[int], os.statvfs_result] = os.fstatvfs,
) -> SnapshotStagePublication:
    """Materialize an exact closure from the persistent standard Hub cache."""

    ensure_private_directory(local_snapshot_root.parent, create=False)
    expected_root = local_snapshot_root.parent / closure["closure_sha256"]
    expected_receipt = local_snapshot_root.parent / (
        f"{closure['closure_sha256']}.stage.json"
    )
    if (
        local_snapshot_root != expected_root
        or receipt_path != expected_receipt
        or canonical_snapshot_root.name != closure["closure_sha256"]
    ):
        _fail(
            "snapshot stage paths do not match the closure identity",
            code="invalid_huggingface_snapshot_stage_path",
        )

    lock_path = local_snapshot_root.parent / (f"{closure['closure_sha256']}.stage.lock")
    staging_root = local_snapshot_root.parent / (
        f".{closure['closure_sha256']}.staging"
    )
    with open_advisory_lock(lock_path, create=True) as stage_lock:
        if not stage_lock.exclusive():
            _fail("cannot acquire Hugging Face snapshot stage lock")

        root_exists = os.path.lexists(local_snapshot_root)
        receipt_exists = os.path.lexists(receipt_path)
        if root_exists and receipt_exists:
            return SnapshotStagePublication(
                stage=verify_snapshot_stage(
                    closure=closure,
                    canonical_snapshot_root=canonical_snapshot_root,
                    local_snapshot_root=local_snapshot_root,
                    receipt_path=receipt_path,
                    boot_id=boot_id,
                ),
                disposition="reused",
                cache_source="existing-local-stage",
                authentication=None,
            )
        if receipt_exists and not root_exists:
            _fail(
                "snapshot receipt exists without its content root",
                code="huggingface_snapshot_stage_collision",
            )
        if root_exists:
            if os.path.lexists(staging_root):
                _fail(
                    "published snapshot conflicts with a partial staging root",
                    code="huggingface_snapshot_stage_collision",
                )
            records, incomplete = _content_records(
                local_snapshot_root,
                closure=closure,
                allow_partial=False,
            )
            if incomplete:
                _fail("published snapshot is incomplete")
            stage = _publish_receipt(
                closure=closure,
                canonical_snapshot_root=canonical_snapshot_root,
                local_snapshot_root=local_snapshot_root,
                receipt_path=receipt_path,
                boot_id=boot_id,
                records=records,
            )
            return SnapshotStagePublication(
                stage=stage,
                disposition="receipt-recovered",
                cache_source="existing-local-stage",
                authentication=None,
            )

        if not os.path.lexists(staging_root):
            try:
                staging_root.mkdir(mode=0o700)
            except OSError as error:
                raise ModelLabError(
                    f"cannot create snapshot staging root: {staging_root}",
                    code="unsafe_huggingface_snapshot_destination",
                ) from error
        complete, incomplete = _content_records(
            staging_root,
            closure=closure,
            allow_partial=True,
        )
        staging_descriptor = _private_root_descriptor(staging_root)
        try:
            _require_filesystem_headroom(
                root=staging_root,
                root_descriptor=staging_descriptor,
                required_growth_bytes=_remaining_stage_bytes(incomplete),
                reserve_bytes=SNAPSHOT_STAGE_FREE_SPACE_RESERVE_BYTES,
                filesystem_status_reader=filesystem_status_reader,
                label="ephemeral snapshot stage",
                insufficient_code="insufficient_huggingface_snapshot_stage_space",
            )
        finally:
            os.close(staging_descriptor)

        missing = _missing_cache_members(layout=layout, closure=closure)
        authentication: str | None = None
        cache_source = "network-volume"
        if missing:
            if not allow_download:
                _fail(
                    "persistent Hugging Face cache lacks closure members: "
                    + ", ".join(missing),
                    code="huggingface_snapshot_source_incomplete",
                )
            writer_lock_path = (
                local_snapshot_root.parent / HUGGINGFACE_CACHE_WRITER_LOCK
            )
            with open_advisory_lock(writer_lock_path, create=True) as writer_lock:
                if not writer_lock.exclusive():
                    _fail("cannot acquire the local Hugging Face cache writer lease")
                missing = _missing_cache_members(layout=layout, closure=closure)
                if missing:
                    workspace_descriptor = _open_absolute_source_directory(
                        layout.workspace_root
                    )
                    try:
                        _require_filesystem_headroom(
                            root=layout.workspace_root,
                            root_descriptor=workspace_descriptor,
                            required_growth_bytes=_closure_bytes_for_paths(
                                closure=closure,
                                paths=missing,
                            ),
                            reserve_bytes=HUGGINGFACE_CACHE_FREE_SPACE_RESERVE_BYTES,
                            filesystem_status_reader=filesystem_status_reader,
                            label="persistent Hugging Face cache",
                            insufficient_code="insufficient_huggingface_cache_space",
                        )
                    finally:
                        os.close(workspace_descriptor)
                    authentication = _download_missing_members(
                        layout=layout,
                        closure=closure,
                        paths=missing,
                        command_runner=command_runner,
                        huggingface_cli=huggingface_cli,
                    )
                    remaining = _missing_cache_members(
                        layout=layout,
                        closure=closure,
                    )
                    if remaining:
                        _fail(
                            "Hugging Face download did not populate closure "
                            "members: " + ", ".join(remaining),
                            code="huggingface_snapshot_download_incomplete",
                        )
                    cache_source = "huggingface-download"
                else:
                    cache_source = "network-volume-after-writer-lease"

        cache = _open_cache_snapshot(
            layout=layout,
            closure=closure,
            missing_ok=False,
        )
        if cache is None:
            _fail(
                "persistent Hugging Face cache disappeared",
                code="huggingface_snapshot_source_incomplete",
            )
        with cache:
            records_by_path = {record["path"]: record for record in complete}
            root_descriptor = _private_root_descriptor(staging_root)
            try:
                for item in incomplete:
                    record = item["record"]
                    member_parts = _relative_parts(
                        record["path"],
                        label="Hugging Face closure member",
                    )
                    parent = _destination_parent(
                        root_descriptor,
                        member_parts,
                        create=True,
                    )
                    try:
                        records_by_path[record["path"]] = _copy_member(
                            cache=cache,
                            destination_parent=parent,
                            destination_name=member_parts[-1],
                            record=record,
                            existing_partial=item["partial_stat"],
                        )
                        os.fsync(parent)
                    finally:
                        os.close(parent)
                os.fsync(root_descriptor)
            finally:
                os.close(root_descriptor)

        records, incomplete = _content_records(
            staging_root,
            closure=closure,
            allow_partial=False,
        )
        if incomplete:
            _fail("snapshot staging root is incomplete after copy")
        if os.path.lexists(local_snapshot_root) or os.path.lexists(receipt_path):
            _fail(
                "snapshot publication appeared while staging",
                code="huggingface_snapshot_stage_collision",
            )
        _rename_no_replace(staging_root, local_snapshot_root)
        parent_descriptor = _private_root_descriptor(local_snapshot_root.parent)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
        stage = _publish_receipt(
            closure=closure,
            canonical_snapshot_root=canonical_snapshot_root,
            local_snapshot_root=local_snapshot_root,
            receipt_path=receipt_path,
            boot_id=boot_id,
            records=records,
        )
        return SnapshotStagePublication(
            stage=stage,
            disposition="created",
            cache_source=cache_source,
            authentication=authentication,
        )
