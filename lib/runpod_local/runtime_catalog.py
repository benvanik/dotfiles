"""Reviewed upstream runtimes accepted by the Runpod template controller."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import stat
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .errors import RunpodLocalError
from .template import (
    build_private_template_contract,
    docker_arguments_summary,
)

RUNTIME_MANIFEST_SCHEMA = "runpod.upstream-runtime.v1"
MAX_RUNTIME_MANIFEST_BYTES = 64 * 1024
MAX_RUNTIME_BOOTSTRAP_BYTES = 16 * 1024
MAX_RUNTIME_VERIFIER_BYTES = 1024 * 1024
REMOTE_RUNTIME_CONTROL_ROOT = "/root/runpod-session/control/runtime-verifier"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_FIELDS = frozenset(
    {
        "architecture",
        "image",
        "image_compressed_bytes",
        "launch_overlay",
        "oci_entrypoint",
        "oci_working_directory",
        "runtime_id",
        "schema_version",
        "upstream_revision",
        "upstream_source",
        "versions",
    }
)
OVERLAY_FIELDS = frozenset(
    {
        "bootstrap_id",
        "bootstrap_path",
        "bootstrap_sha256",
        "docker_entrypoint",
    }
)
VERSION_FIELDS = frozenset(
    {
        "cuda",
        "flashinfer",
        "flashinfer_jit_cache",
        "python",
        "torch",
        "vllm",
    }
)


@dataclass(frozen=True)
class _RuntimeCatalogEntry:
    runtime_id: str
    manifest_path: str
    manifest_sha256: str
    verifier_path: str
    verifier_sha256: str
    bootstrap_id: str
    bootstrap_path: str
    bootstrap_sha256: str
    image: str
    docker_entrypoint: tuple[str, ...]
    container_disk_gb: int
    volume_mount_path: str


@dataclass(frozen=True)
class RuntimeDefinition:
    """An exact reviewed runtime and its verified tiny launch overlay."""

    runtime_id: str
    manifest_path: str
    manifest_sha256: str
    manifest_bytes: bytes = field(repr=False)
    verifier_path: str
    verifier_sha256: str
    verifier_bytes: bytes = field(repr=False)
    bootstrap_id: str
    bootstrap_path: str
    bootstrap_sha256: str
    bootstrap_text: str
    image: str
    docker_entrypoint: tuple[str, ...]
    container_disk_gb: int
    volume_mount_path: str

    def template_contract(
        self,
        *,
        name: str,
        template_id: str | None = None,
    ) -> dict[str, Any]:
        return build_private_template_contract(
            name=name,
            image=self.image,
            docker_entrypoint=list(self.docker_entrypoint),
            docker_start_cmd=[self.bootstrap_text],
            container_disk_gb=self.container_disk_gb,
            volume_in_gb=0,
            volume_mount_path=self.volume_mount_path,
            template_id=template_id,
        )

    def safe_summary(self) -> dict[str, Any]:
        return {
            "schema_version": "runpod.runtime-selection.v1",
            "runtime_id": self.runtime_id,
            "image": self.image,
            "manifest": {
                "path": self.manifest_path,
                "remote_path": (f"{REMOTE_RUNTIME_CONTROL_ROOT}/runtime-manifest.json"),
                "sha256": self.manifest_sha256,
                "bytes": len(self.manifest_bytes),
            },
            "verifier": {
                "path": self.verifier_path,
                "remote_path": (f"{REMOTE_RUNTIME_CONTROL_ROOT}/verify-runtime.py"),
                "sha256": self.verifier_sha256,
                "bytes": len(self.verifier_bytes),
            },
            "launch_overlay": {
                "bootstrap_id": self.bootstrap_id,
                "bootstrap_path": self.bootstrap_path,
                "bootstrap_sha256": self.bootstrap_sha256,
                "bootstrap_bytes": len(self.bootstrap_text.encode("utf-8")),
                "docker_entrypoint_summary": docker_arguments_summary(
                    list(self.docker_entrypoint)
                ),
                "docker_start_cmd_summary": docker_arguments_summary(
                    [self.bootstrap_text]
                ),
            },
            "container_disk_gb": self.container_disk_gb,
            "volume_in_gb": 0,
            "volume_mount_path": self.volume_mount_path,
        }


_RUNTIME_CATALOG = MappingProxyType(
    {
        "vllm-cu129-v0.25.1": _RuntimeCatalogEntry(
            runtime_id="vllm-cu129-v0.25.1",
            manifest_path=("runpod/runtimes/vllm-cu129/runtime-manifest.json"),
            manifest_sha256=(
                "4d77609df0e21a1776d66b6bb504dadf9f9fb38300bb26af4f95438ef4347f5a"
            ),
            verifier_path=("runpod/runtimes/vllm-cu129/verify-runtime.py"),
            verifier_sha256=(
                "d2628b9e9ef2f4ae0d77c3830097793370fd8bee85a9662ac92562319b0cbb22"
            ),
            bootstrap_id="ubuntu-openssh-server-v1",
            bootstrap_path="runpod/bootstrap/ssh/bootstrap.sh",
            bootstrap_sha256=(
                "53debc1afa74b41fcc03855eb8047abf66daf2015e4bc29b73df2a3523b763ee"
            ),
            image=(
                "vllm/vllm-openai@sha256:"
                "fb463d6a216c7ee82bf947f321cae7dd"
                "7105bfb5084ea35827c2ceb816994b15"
            ),
            docker_entrypoint=("/bin/bash", "-c"),
            container_disk_gb=50,
            volume_mount_path="/workspace",
        ),
    }
)


def available_runtime_ids() -> tuple[str, ...]:
    return tuple(sorted(_RUNTIME_CATALOG))


def _source_root() -> pathlib.Path:
    try:
        return pathlib.Path(__file__).resolve(strict=True).parents[2]
    except (OSError, IndexError) as error:
        raise RunpodLocalError(
            "cannot resolve the installed Runpod runtime catalog root",
            code="unsafe_runtime_catalog",
        ) from error


def _safe_relative_parts(relative_path: str) -> tuple[str, ...]:
    if not isinstance(relative_path, str):
        raise RunpodLocalError(
            "runtime catalog path is not a string",
            code="unsafe_runtime_catalog",
        )
    path = pathlib.PurePosixPath(relative_path)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise RunpodLocalError(
            "runtime catalog contains an unsafe source path",
            code="unsafe_runtime_catalog",
        )
    return path.parts


def _require_owned(
    metadata: os.stat_result,
    *,
    label: str,
    directory: bool,
) -> None:
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(metadata.st_mode):
        raise RunpodLocalError(
            f"{label} is not a {'directory' if directory else 'regular file'}",
            code="unsafe_runtime_catalog",
        )
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise RunpodLocalError(
            f"{label} is not owned by the current user",
            code="unsafe_runtime_catalog",
        )
    # This workspace deliberately uses a private primary group for collaborative
    # 0775/0664 access. Current-UID ownership plus no world-write is the trusted
    # local-principal boundary; rejecting private-group write would reject the
    # repository's own storage policy without excluding another principal.
    if metadata.st_mode & 0o002:
        raise RunpodLocalError(
            f"{label} is writable by another account",
            code="unsafe_runtime_catalog",
        )


def _read_catalog_file(
    source_root: pathlib.Path,
    relative_path: str,
    *,
    label: str,
    maximum_bytes: int,
) -> bytes:
    """Read one owned regular source file through one no-follow descriptor."""

    if not all(
        hasattr(os, name) for name in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")
    ):
        raise RunpodLocalError(
            "this platform cannot safely open the runtime catalog",
            code="unsafe_runtime_catalog",
        )
    parts = _safe_relative_parts(relative_path)
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptors: list[int] = []
    try:
        root_descriptor = os.open(source_root, directory_flags)
        descriptors.append(root_descriptor)
        _require_owned(
            os.fstat(root_descriptor),
            label="runtime catalog source root",
            directory=True,
        )
        parent_descriptor = root_descriptor
        for part in parts[:-1]:
            descriptor = os.open(
                part,
                directory_flags,
                dir_fd=parent_descriptor,
            )
            descriptors.append(descriptor)
            _require_owned(
                os.fstat(descriptor),
                label=f"{label} parent",
                directory=True,
            )
            parent_descriptor = descriptor
        file_descriptor = os.open(
            parts[-1],
            file_flags,
            dir_fd=parent_descriptor,
        )
        descriptors.append(file_descriptor)
        metadata = os.fstat(file_descriptor)
        _require_owned(metadata, label=label, directory=False)
        if metadata.st_size < 1 or metadata.st_size > maximum_bytes:
            raise RunpodLocalError(
                f"{label} exceeds its bounded source size",
                code="unsafe_runtime_catalog",
            )
        chunks: list[bytes] = []
        observed_bytes = 0
        while True:
            chunk = os.read(
                file_descriptor,
                min(64 * 1024, maximum_bytes + 1 - observed_bytes),
            )
            if not chunk:
                break
            chunks.append(chunk)
            observed_bytes += len(chunk)
            if observed_bytes > maximum_bytes:
                raise RunpodLocalError(
                    f"{label} exceeds its bounded source size",
                    code="unsafe_runtime_catalog",
                )
        value = b"".join(chunks)
        final = os.fstat(file_descriptor)
        if (
            len(value) != metadata.st_size
            or final.st_dev != metadata.st_dev
            or final.st_ino != metadata.st_ino
            or not stat.S_ISREG(final.st_mode)
            or final.st_uid != metadata.st_uid
            or final.st_nlink != metadata.st_nlink
            or stat.S_IMODE(final.st_mode) != stat.S_IMODE(metadata.st_mode)
            or final.st_size != metadata.st_size
            or final.st_mtime_ns != metadata.st_mtime_ns
            or final.st_ctime_ns != metadata.st_ctime_ns
        ):
            raise RunpodLocalError(
                f"{label} changed while it was being read",
                code="runtime_catalog_drift",
            )
        return value
    except RunpodLocalError:
        raise
    except OSError as error:
        raise RunpodLocalError(
            f"cannot safely read {label}",
            code="unsafe_runtime_catalog",
        ) from error
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _parse_manifest(
    manifest_bytes: bytes,
    *,
    entry: _RuntimeCatalogEntry,
) -> dict[str, Any]:
    observed_hash = hashlib.sha256(manifest_bytes).hexdigest()
    if observed_hash != entry.manifest_sha256:
        raise RunpodLocalError(
            "reviewed runtime manifest identity drifted",
            code="runtime_catalog_drift",
        )
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunpodLocalError(
            "reviewed runtime manifest is not canonical UTF-8 JSON",
            code="invalid_runtime_manifest",
        ) from error
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_FIELDS:
        raise RunpodLocalError(
            "reviewed runtime manifest has unsupported or missing fields",
            code="invalid_runtime_manifest",
        )
    overlay = manifest.get("launch_overlay")
    versions = manifest.get("versions")
    if (
        not isinstance(overlay, dict)
        or set(overlay) != OVERLAY_FIELDS
        or not isinstance(versions, dict)
        or set(versions) != VERSION_FIELDS
        or not all(
            isinstance(version, str) and version for version in versions.values()
        )
    ):
        raise RunpodLocalError(
            "reviewed runtime manifest has an invalid runtime description",
            code="invalid_runtime_manifest",
        )
    if (
        manifest.get("schema_version") != RUNTIME_MANIFEST_SCHEMA
        or manifest.get("runtime_id") != entry.runtime_id
        or manifest.get("architecture") != "linux/amd64"
        or manifest.get("image") != entry.image
        or manifest.get("oci_entrypoint") != ["vllm", "serve"]
        or manifest.get("oci_working_directory") != "/vllm-workspace"
        or manifest.get("upstream_source") != "https://github.com/vllm-project/vllm"
        or not isinstance(manifest.get("upstream_revision"), str)
        or not re.fullmatch(
            r"[0-9a-f]{40}",
            manifest["upstream_revision"],
        )
        or not isinstance(manifest.get("image_compressed_bytes"), int)
        or isinstance(manifest.get("image_compressed_bytes"), bool)
        or manifest["image_compressed_bytes"] <= 0
        or overlay.get("bootstrap_id") != entry.bootstrap_id
        or overlay.get("bootstrap_path") != entry.bootstrap_path
        or overlay.get("bootstrap_sha256") != entry.bootstrap_sha256
        or overlay.get("docker_entrypoint") != list(entry.docker_entrypoint)
    ):
        raise RunpodLocalError(
            "reviewed runtime manifest drifted from its catalog entry",
            code="runtime_catalog_drift",
        )
    return manifest


def _load_runtime_entry(
    source_root: pathlib.Path,
    entry: _RuntimeCatalogEntry,
) -> RuntimeDefinition:
    manifest_bytes = _read_catalog_file(
        source_root,
        entry.manifest_path,
        label="reviewed runtime manifest",
        maximum_bytes=MAX_RUNTIME_MANIFEST_BYTES,
    )
    manifest = _parse_manifest(manifest_bytes, entry=entry)
    verifier_bytes = _read_catalog_file(
        source_root,
        entry.verifier_path,
        label="reviewed runtime verifier",
        maximum_bytes=MAX_RUNTIME_VERIFIER_BYTES,
    )
    if hashlib.sha256(verifier_bytes).hexdigest() != entry.verifier_sha256:
        raise RunpodLocalError(
            "reviewed runtime verifier identity drifted",
            code="runtime_catalog_drift",
        )
    bootstrap_bytes = _read_catalog_file(
        source_root,
        entry.bootstrap_path,
        label="reviewed runtime bootstrap",
        maximum_bytes=MAX_RUNTIME_BOOTSTRAP_BYTES,
    )
    observed_bootstrap_hash = hashlib.sha256(bootstrap_bytes).hexdigest()
    manifest_bootstrap_hash = manifest["launch_overlay"]["bootstrap_sha256"]
    if (
        observed_bootstrap_hash != entry.bootstrap_sha256
        or observed_bootstrap_hash != manifest_bootstrap_hash
        or not SHA256_PATTERN.fullmatch(observed_bootstrap_hash)
    ):
        raise RunpodLocalError(
            "reviewed runtime bootstrap identity drifted",
            code="runtime_catalog_drift",
        )
    try:
        bootstrap_text = bootstrap_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RunpodLocalError(
            "reviewed runtime bootstrap is not UTF-8",
            code="invalid_runtime_bootstrap",
        ) from error
    return RuntimeDefinition(
        runtime_id=entry.runtime_id,
        manifest_path=entry.manifest_path,
        manifest_sha256=entry.manifest_sha256,
        manifest_bytes=manifest_bytes,
        verifier_path=entry.verifier_path,
        verifier_sha256=entry.verifier_sha256,
        verifier_bytes=verifier_bytes,
        bootstrap_id=entry.bootstrap_id,
        bootstrap_path=entry.bootstrap_path,
        bootstrap_sha256=entry.bootstrap_sha256,
        bootstrap_text=bootstrap_text,
        image=entry.image,
        docker_entrypoint=entry.docker_entrypoint,
        container_disk_gb=entry.container_disk_gb,
        volume_mount_path=entry.volume_mount_path,
    )


def load_runtime(runtime_id: Any) -> RuntimeDefinition:
    if not isinstance(runtime_id, str) or runtime_id not in _RUNTIME_CATALOG:
        available = ", ".join(available_runtime_ids())
        raise RunpodLocalError(
            f"unknown reviewed runtime; available runtimes: {available}",
            code="unknown_runtime",
        )
    return _load_runtime_entry(_source_root(), _RUNTIME_CATALOG[runtime_id])


def _same_typed_document(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            _same_typed_document(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same_typed_document(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


def validate_runtime_identity(value: Any) -> RuntimeDefinition:
    if not isinstance(value, dict):
        raise RunpodLocalError(
            "runtime identity is not an object",
            code="invalid_runtime_identity",
        )
    runtime = load_runtime(value.get("runtime_id"))
    if not _same_typed_document(value, runtime.safe_summary()):
        raise RunpodLocalError(
            "runtime identity drifted from the reviewed catalog",
            code="runtime_catalog_drift",
        )
    return runtime
