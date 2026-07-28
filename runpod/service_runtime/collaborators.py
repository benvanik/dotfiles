"""Typed prerequisites consumed by the lifecycle controller."""

from __future__ import annotations

import hashlib
import os
import pathlib
import re
import stat
from dataclasses import dataclass
from typing import Any

from runpod_local.errors import RunpodLocalError
from runpod_local.service_compile_cache import build_compile_cache_contract

from .compile_cache_document import (
    COMPILE_CACHE_STAGE_SCHEMA,
    compile_cache_receipt_path,
)
from .compile_cache_files import COMPILE_CACHE_SUBDIRECTORIES
from .document import DeploymentManifest
from .layout import RuntimeLayout
from .state import read_private_json


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "cache_id",
        "contract",
        "boot_id",
        "persistent_root",
        "local_root",
        "directory_stat",
        "file_count",
        "total_bytes",
        "files_sha256",
    }
)
_DIRECTORY_STAT_FIELDS = frozenset({"device", "inode", "mtime_ns", "ctime_ns", "mode"})


def _fail(message: str) -> None:
    raise RunpodLocalError(message, code="compile_cache_stage_unavailable")


def _mode(value: os.stat_result) -> int:
    return stat.S_IMODE(value.st_mode)


def _directory_stat(value: os.stat_result) -> dict[str, int]:
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
        "mode": _mode(value),
    }


def _require_private_directory(path: pathlib.Path) -> os.stat_result:
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise RunpodLocalError(
            f"compiled-cache stage is absent: {path}",
            code="compile_cache_stage_unavailable",
        ) from error
    if (
        not stat.S_ISDIR(path_stat.st_mode)
        or stat.S_ISLNK(path_stat.st_mode)
        or path_stat.st_uid != os.getuid()
        or _mode(path_stat) != 0o700
    ):
        _fail(f"compiled-cache directory is unsafe: {path}")
    return path_stat


@dataclass(frozen=True)
class CompileCacheStage:
    """One exact-driver local cache stage accepted for launch."""

    contract: dict[str, Any]
    receipt: dict[str, Any]
    receipt_sha256: str
    local_root: pathlib.Path

    @property
    def cache_id(self) -> str:
        return self.contract["cache_id"]

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": COMPILE_CACHE_STAGE_SCHEMA,
            "cache_id": self.cache_id,
            "receipt_sha256": self.receipt_sha256,
            "persistent_root": self.contract["persistent_root"],
            "local_root": self.contract["local_root"],
            "file_count": self.receipt["file_count"],
            "total_bytes": self.receipt["total_bytes"],
            "files_sha256": self.receipt["files_sha256"],
        }


def compile_cache_contract(
    *,
    manifest: DeploymentManifest,
    observed_gpu: dict[str, Any],
    runtime_execution_environment: dict[str, Any],
) -> dict[str, Any]:
    return build_compile_cache_contract(
        driver=manifest.service["driver"],
        runtime=manifest.runtime,
        runtime_execution_environment=runtime_execution_environment,
        implementation_bundle_sha256=(
            manifest.implementation_bundle_sha256
        ),
        huggingface_closure_sha256=manifest.closure_sha256,
        compile_affecting_launch_sha256=(manifest.compile_affecting_launch_sha256),
        observed_gpu=observed_gpu,
    )


def verify_compile_cache_stage(
    *,
    contract: dict[str, Any],
    layout: RuntimeLayout,
    boot_id: str,
) -> CompileCacheStage:
    canonical_root = pathlib.PurePosixPath(contract["local_root"])
    local_root = layout.localize(canonical_root)
    receipt_path = layout.localize(compile_cache_receipt_path(contract))
    try:
        receipt, payload = read_private_json(
            receipt_path,
            maximum_bytes=4 * 1024 * 1024,
        )
    except RunpodLocalError as error:
        raise RunpodLocalError(
            f"compiled-cache stage receipt is unavailable: {receipt_path}",
            code="compile_cache_stage_unavailable",
        ) from error
    if set(receipt) != _RECEIPT_FIELDS:
        _fail("compiled-cache stage receipt fields are malformed")
    if (
        receipt["schema_version"] != COMPILE_CACHE_STAGE_SCHEMA
        or receipt["cache_id"] != contract["cache_id"]
        or receipt["contract"] != contract
        or receipt["boot_id"] != boot_id
        or receipt["persistent_root"] != contract["persistent_root"]
        or receipt["local_root"] != contract["local_root"]
    ):
        _fail("compiled-cache stage receipt does not match this deployment")
    for field in ("file_count", "total_bytes"):
        if (
            isinstance(receipt[field], bool)
            or not isinstance(receipt[field], int)
            or receipt[field] < 0
        ):
            _fail(f"compiled-cache stage {field} is malformed")
    if not isinstance(receipt["files_sha256"], str) or not _SHA256.fullmatch(
        receipt["files_sha256"]
    ):
        _fail("compiled-cache stage file inventory identity is malformed")
    expected_directory_stat = receipt["directory_stat"]
    if (
        not isinstance(expected_directory_stat, dict)
        or set(expected_directory_stat) != _DIRECTORY_STAT_FIELDS
        or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in expected_directory_stat.values()
        )
        or expected_directory_stat["inode"] <= 0
        or expected_directory_stat["mode"] != 0o700
    ):
        _fail("compiled-cache stage directory stats are malformed")
    root_stat = _require_private_directory(local_root)
    if _directory_stat(root_stat) != expected_directory_stat:
        _fail("compiled-cache stage root changed after publication")
    for relative in COMPILE_CACHE_SUBDIRECTORIES:
        _require_private_directory(local_root / relative)
    return CompileCacheStage(
        contract=contract,
        receipt=receipt,
        receipt_sha256=hashlib.sha256(payload).hexdigest(),
        local_root=local_root,
    )
