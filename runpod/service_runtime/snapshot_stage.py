"""Fast verifier for a content-checked local Hugging Face snapshot stage."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import stat
from dataclasses import dataclass
from typing import Any

from runpod_local.errors import RunpodLocalError


SNAPSHOT_STAGE_SCHEMA = "runpod.local-huggingface-stage.v1"
MAX_SNAPSHOT_RECEIPT_BYTES = 32 * 1024 * 1024
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "closure_sha256",
        "source",
        "checkpoint",
        "snapshot_root",
        "boot_id",
        "directory_stat",
        "file_count",
        "total_bytes",
        "files",
    }
)
_DIRECTORY_STAT_FIELDS = frozenset({"device", "inode", "mtime_ns", "ctime_ns", "mode"})
_FILE_FIELDS = frozenset(
    {
        "path",
        "bytes",
        "role",
        "identity",
        "device",
        "inode",
        "mtime_ns",
        "ctime_ns",
        "mode",
    }
)


def _fail(message: str) -> None:
    raise RunpodLocalError(message, code="invalid_huggingface_snapshot_stage")


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


def _read_private_receipt(path: pathlib.Path) -> tuple[dict[str, Any], str]:
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise RunpodLocalError(
            f"snapshot stage receipt is absent: {path}",
            code="huggingface_snapshot_stage_unavailable",
        ) from error
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or stat.S_ISLNK(path_stat.st_mode)
        or path_stat.st_uid != os.getuid()
        or path_stat.st_nlink != 1
        or _mode(path_stat) != 0o600
        or not 1 <= path_stat.st_size <= MAX_SNAPSHOT_RECEIPT_BYTES
    ):
        _fail(f"snapshot stage receipt has an unsafe identity: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RunpodLocalError(
            f"cannot safely open snapshot stage receipt: {path}",
            code="invalid_huggingface_snapshot_stage",
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != path_stat.st_dev
            or opened.st_ino != path_stat.st_ino
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != path_stat.st_uid
            or opened.st_nlink != path_stat.st_nlink
            or _mode(opened) != _mode(path_stat)
            or opened.st_size != path_stat.st_size
        ):
            _fail("snapshot stage receipt changed while opening")
        chunks: list[bytes] = []
        remaining = MAX_SNAPSHOT_RECEIPT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        final = os.fstat(descriptor)
        if (
            len(payload) > MAX_SNAPSHOT_RECEIPT_BYTES
            or final.st_size != opened.st_size
            or final.st_mtime_ns != opened.st_mtime_ns
            or final.st_ctime_ns != opened.st_ctime_ns
            or final.st_uid != opened.st_uid
            or final.st_nlink != opened.st_nlink
            or _mode(final) != _mode(opened)
        ):
            _fail("snapshot stage receipt changed while reading")
    finally:
        os.close(descriptor)

    def reject_duplicate_fields(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                _fail(f"snapshot stage receipt repeats field {key!r}")
            value[key] = item
        return value

    try:
        value = json.loads(
            payload,
            object_pairs_hook=reject_duplicate_fields,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunpodLocalError(
            "snapshot stage receipt is not valid JSON",
            code="invalid_huggingface_snapshot_stage",
        ) from error
    if not isinstance(value, dict):
        _fail("snapshot stage receipt must be an object")
    return value, hashlib.sha256(payload).hexdigest()


def _validate_integer_stat(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(f"{label} is not a nonnegative integer")
    return value


def _validate_directory(
    path: pathlib.Path,
    *,
    expected_stat: dict[str, Any] | None = None,
) -> os.stat_result:
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise RunpodLocalError(
            f"staged snapshot directory is absent: {path}",
            code="huggingface_snapshot_stage_unavailable",
        ) from error
    if (
        not stat.S_ISDIR(path_stat.st_mode)
        or stat.S_ISLNK(path_stat.st_mode)
        or path_stat.st_uid != os.getuid()
        or _mode(path_stat) != 0o700
    ):
        _fail(f"staged snapshot directory is unsafe: {path}")
    if expected_stat is not None and _stat_identity(path_stat) != expected_stat:
        _fail(f"staged snapshot directory changed: {path}")
    return path_stat


def _validate_receipt_file(
    *,
    root: pathlib.Path,
    closure_record: dict[str, Any],
    receipt_record: Any,
) -> None:
    if not isinstance(receipt_record, dict) or set(receipt_record) != _FILE_FIELDS:
        _fail("snapshot stage receipt has a malformed file record")
    expected_prefix = {
        "path": closure_record["path"],
        "bytes": closure_record["bytes"],
        "role": closure_record["role"],
        "identity": closure_record["identity"],
    }
    if any(receipt_record.get(name) != item for name, item in expected_prefix.items()):
        _fail(
            f"snapshot stage receipt disagrees with closure: {closure_record['path']}"
        )
    for name in ("device", "inode", "mtime_ns", "ctime_ns", "mode"):
        _validate_integer_stat(
            receipt_record.get(name),
            label=f"snapshot stage {closure_record['path']} {name}",
        )
    if receipt_record["inode"] <= 0 or receipt_record["mode"] != 0o400:
        _fail(f"snapshot stage file stats are invalid: {closure_record['path']}")
    relative = pathlib.PurePosixPath(closure_record["path"])
    parent = root
    for component in relative.parts[:-1]:
        parent /= component
        _validate_directory(parent)
    path = root.joinpath(*relative.parts)
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise RunpodLocalError(
            f"staged snapshot file is absent: {closure_record['path']}",
            code="invalid_huggingface_snapshot_stage",
        ) from error
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or stat.S_ISLNK(path_stat.st_mode)
        or path_stat.st_uid != os.getuid()
        or path_stat.st_nlink != 1
        or path_stat.st_size != closure_record["bytes"]
        or _mode(path_stat) != 0o400
    ):
        _fail(f"staged snapshot file is unsafe: {closure_record['path']}")
    observed_stat = _stat_identity(path_stat)
    expected_stat = {
        name: receipt_record[name]
        for name in ("device", "inode", "mtime_ns", "ctime_ns", "mode")
    }
    if observed_stat != expected_stat:
        _fail(f"staged snapshot file changed: {closure_record['path']}")


def _tree_entries(root: pathlib.Path) -> tuple[set[str], set[str]]:
    observed_files: set[str] = set()
    observed_directories: set[str] = set()
    try:
        walker = os.walk(root, topdown=True, followlinks=False)
        for directory, directory_names, file_names in walker:
            directory_path = pathlib.Path(directory)
            for name in list(directory_names):
                child = directory_path / name
                _validate_directory(child)
                observed_directories.add(child.relative_to(root).as_posix())
            for name in file_names:
                child = directory_path / name
                try:
                    child_stat = child.lstat()
                except OSError as error:
                    raise RunpodLocalError(
                        f"cannot inspect staged snapshot entry: {child}",
                        code="invalid_huggingface_snapshot_stage",
                    ) from error
                if not stat.S_ISREG(child_stat.st_mode):
                    _fail(f"staged snapshot contains a non-file entry: {child}")
                observed_files.add(child.relative_to(root).as_posix())
    except OSError as error:
        raise RunpodLocalError(
            f"cannot enumerate staged snapshot: {root}",
            code="invalid_huggingface_snapshot_stage",
        ) from error
    return observed_files, observed_directories


@dataclass(frozen=True)
class SnapshotStage:
    """Verified closure stage suitable for a shell-free vLLM launch."""

    receipt: dict[str, Any]
    receipt_sha256: str
    root: pathlib.Path

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": SNAPSHOT_STAGE_SCHEMA,
            "closure_sha256": self.receipt["closure_sha256"],
            "snapshot_root": self.receipt["snapshot_root"],
            "receipt_sha256": self.receipt_sha256,
            "file_count": self.receipt["file_count"],
            "total_bytes": self.receipt["total_bytes"],
        }


def verify_snapshot_stage(
    *,
    closure: dict[str, Any],
    canonical_snapshot_root: pathlib.PurePosixPath,
    local_snapshot_root: pathlib.Path,
    receipt_path: pathlib.Path,
    boot_id: str,
) -> SnapshotStage:
    """Verify receipt-bound file stats without rehashing multi-GB weights."""

    receipt, receipt_sha256 = _read_private_receipt(receipt_path)
    if set(receipt) != _RECEIPT_FIELDS:
        _fail("snapshot stage receipt fields are malformed")
    if (
        receipt["schema_version"] != SNAPSHOT_STAGE_SCHEMA
        or receipt["closure_sha256"] != closure["closure_sha256"]
        or receipt["source"] != closure["source"]
        or receipt["checkpoint"] != closure["checkpoint"]
        or receipt["snapshot_root"] != str(canonical_snapshot_root)
        or receipt["boot_id"] != boot_id
        or receipt["file_count"] != closure["file_count"]
        or receipt["total_bytes"] != closure["total_bytes"]
    ):
        _fail("snapshot stage receipt does not match this deployment and boot")
    directory_stat = receipt["directory_stat"]
    if (
        not isinstance(directory_stat, dict)
        or set(directory_stat) != _DIRECTORY_STAT_FIELDS
    ):
        _fail("snapshot stage receipt has malformed directory stats")
    for name, item in directory_stat.items():
        _validate_integer_stat(item, label=f"snapshot stage root {name}")
    if directory_stat["inode"] <= 0 or directory_stat["mode"] != 0o700:
        _fail("snapshot stage receipt has impossible directory stats")
    _validate_directory(
        local_snapshot_root,
        expected_stat=directory_stat,
    )
    files = receipt["files"]
    if (
        not isinstance(files, list)
        or len(files) != len(closure["files"])
        or [record.get("path") for record in files if isinstance(record, dict)]
        != [record["path"] for record in closure["files"]]
    ):
        _fail("snapshot stage receipt has the wrong sorted file set")
    for closure_record, receipt_record in zip(closure["files"], files, strict=True):
        _validate_receipt_file(
            root=local_snapshot_root,
            closure_record=closure_record,
            receipt_record=receipt_record,
        )
    observed_files, observed_directories = _tree_entries(local_snapshot_root)
    expected_files = {record["path"] for record in closure["files"]}
    expected_directories = {
        pathlib.PurePosixPath(*pathlib.PurePosixPath(path).parts[:index]).as_posix()
        for path in expected_files
        for index in range(1, len(pathlib.PurePosixPath(path).parts))
    }
    if observed_files != expected_files or observed_directories != expected_directories:
        _fail("staged snapshot tree contains unexpected entries")
    return SnapshotStage(
        receipt=receipt,
        receipt_sha256=receipt_sha256,
        root=local_snapshot_root,
    )
