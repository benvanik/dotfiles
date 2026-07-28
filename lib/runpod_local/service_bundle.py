"""Content-bound generic runtime bundles and generated service manifests."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import stat
from dataclasses import dataclass
from typing import Any

from .errors import RunpodLocalError
from .service_compile_cache import COMPILE_CACHE_SCHEMA
from .service_definition import InferenceServiceDefinition
from .service_huggingface import (
    HuggingFaceClosure,
    parse_huggingface_closure,
)
from .service_vllm import (
    DEFAULT_REMOTE_PORT,
    build_vllm_argv,
    build_vllm_deployment_plan,
)

BUNDLE_PLAN_SCHEMA = "runpod.inference-service-bundle-plan.v1"
IMPLEMENTATION_BUNDLE_SCHEMA = "runpod.inference-service-implementation-bundle.v1"
DEPLOYMENT_MANIFEST_SCHEMA = "runpod.inference-service-deployment-manifest.v1"
IMPLEMENTATION_ID = "runpod-inference-service-runtime-v1"
REMOTE_IMPLEMENTATION_PARENT = pathlib.PurePosixPath(
    "/root/runpod-session/control/inference-service-runtime"
)
REMOTE_SERVICE_PARENT = pathlib.PurePosixPath("/root/runpod-session/services")
REMOTE_SNAPSHOT_PARENT = pathlib.PurePosixPath("/root/runpod-session/model-snapshots")
RELATIVE_ENTRYPOINT = pathlib.PurePosixPath("bin/runpod-service-runtime")
ENTRYPOINT_ACTIONS = ("setup", "start", "status", "stop")
MAX_IMPLEMENTATION_FILE_BYTES = 256 * 1024
MAX_IMPLEMENTATION_BUNDLE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ImplementationMember:
    """One exact repository source and its immutable remote representation."""

    source_path: str
    bundle_path: str
    mode: str


IMPLEMENTATION_MEMBERS = (
    ImplementationMember(
        "runpod/service_runtime/run-service",
        "bin/runpod-service-runtime",
        "0755",
    ),
    ImplementationMember(
        "runpod/service_runtime/__init__.py",
        "service_runtime/__init__.py",
        "0644",
    ),
    ImplementationMember(
        "runpod/service_runtime/collaborators.py",
        "service_runtime/collaborators.py",
        "0644",
    ),
    ImplementationMember(
        "runpod/service_runtime/controller.py",
        "service_runtime/controller.py",
        "0644",
    ),
    ImplementationMember(
        "runpod/service_runtime/document.py",
        "service_runtime/document.py",
        "0644",
    ),
    ImplementationMember(
        "runpod/service_runtime/layout.py",
        "service_runtime/layout.py",
        "0644",
    ),
    ImplementationMember(
        "runpod/service_runtime/platform.py",
        "service_runtime/platform.py",
        "0644",
    ),
    ImplementationMember(
        "runpod/service_runtime/snapshot_stage.py",
        "service_runtime/snapshot_stage.py",
        "0644",
    ),
    ImplementationMember(
        "runpod/service_runtime/state.py",
        "service_runtime/state.py",
        "0644",
    ),
    ImplementationMember(
        "runpod/service_runtime/vllm.py",
        "service_runtime/vllm.py",
        "0644",
    ),
    ImplementationMember(
        "lib/runpod_local/__init__.py",
        "runpod_local/__init__.py",
        "0644",
    ),
    ImplementationMember(
        "lib/runpod_local/errors.py",
        "runpod_local/errors.py",
        "0644",
    ),
    ImplementationMember(
        "lib/runpod_local/service_compile_cache.py",
        "runpod_local/service_compile_cache.py",
        "0644",
    ),
)


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("ascii")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _entrypoint_contract() -> dict[str, Any]:
    return {
        "actions": list(ENTRYPOINT_ACTIONS),
        "manifest_argument": "--manifest",
        "instantiation_input_count": 1,
        "instantiation_schema_version": DEPLOYMENT_MANIFEST_SCHEMA,
    }


def _safe_relative_path(value: str) -> bool:
    if not isinstance(value, str):
        return False
    path = pathlib.PurePosixPath(value)
    return (
        bool(value)
        and not path.is_absolute()
        and str(path) == value
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _validate_implementation_members() -> None:
    source_paths: set[str] = set()
    bundle_paths: set[str] = set()
    for member in IMPLEMENTATION_MEMBERS:
        if (
            not _safe_relative_path(member.source_path)
            or not _safe_relative_path(member.bundle_path)
            or member.mode not in {"0644", "0755"}
            or member.source_path in source_paths
            or member.bundle_path in bundle_paths
        ):
            raise RunpodLocalError(
                "service implementation allowlist is invalid",
                code="invalid_service_implementation_allowlist",
            )
        source_paths.add(member.source_path)
        bundle_paths.add(member.bundle_path)
    entrypoints = [
        member
        for member in IMPLEMENTATION_MEMBERS
        if member.bundle_path == str(RELATIVE_ENTRYPOINT)
    ]
    if len(entrypoints) != 1 or entrypoints[0].mode != "0755":
        raise RunpodLocalError(
            "service implementation entrypoint is not uniquely executable",
            code="invalid_service_implementation_allowlist",
        )


def _read_implementation_member(
    source_root: pathlib.Path,
    member: ImplementationMember,
) -> bytes:
    source_path = source_root.joinpath(*pathlib.PurePosixPath(member.source_path).parts)
    try:
        resolved = source_path.resolve(strict=True)
        expected = source_path.absolute()
        metadata = source_path.lstat()
    except OSError as error:
        raise RunpodLocalError(
            f"cannot inspect implementation member {member.source_path}",
            code="unsafe_service_implementation_member",
        ) from error
    if (
        resolved != expected
        or source_path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size < 1
        or metadata.st_size > MAX_IMPLEMENTATION_FILE_BYTES
        or metadata.st_mode & 0o002
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        raise RunpodLocalError(
            f"implementation member is unsafe: {member.source_path}",
            code="unsafe_service_implementation_member",
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source_path, flags)
    except OSError as error:
        raise RunpodLocalError(
            f"cannot open implementation member {member.source_path}",
            code="unsafe_service_implementation_member",
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_size != metadata.st_size
        ):
            raise RunpodLocalError(
                f"implementation member changed while opening: {member.source_path}",
                code="service_implementation_member_drift",
            )
        chunks: list[bytes] = []
        observed_bytes = 0
        while observed_bytes <= MAX_IMPLEMENTATION_FILE_BYTES:
            chunk = os.read(
                descriptor,
                min(
                    64 * 1024,
                    MAX_IMPLEMENTATION_FILE_BYTES + 1 - observed_bytes,
                ),
            )
            if not chunk:
                break
            chunks.append(chunk)
            observed_bytes += len(chunk)
        final = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        observed_bytes != metadata.st_size
        or final.st_size != opened.st_size
        or final.st_mtime_ns != opened.st_mtime_ns
        or final.st_ctime_ns != opened.st_ctime_ns
    ):
        raise RunpodLocalError(
            f"implementation member changed while reading: {member.source_path}",
            code="service_implementation_member_drift",
        )
    return b"".join(chunks)


def build_implementation_bundle(
    *,
    source_root: pathlib.Path,
) -> dict[str, Any]:
    """Inventory the one reusable remote implementation without model data."""

    _validate_implementation_members()
    files = []
    total_bytes = 0
    for member in IMPLEMENTATION_MEMBERS:
        payload = _read_implementation_member(source_root, member)
        total_bytes += len(payload)
        if total_bytes > MAX_IMPLEMENTATION_BUNDLE_BYTES:
            raise RunpodLocalError(
                "service implementation bundle exceeds its size limit",
                code="oversized_service_implementation_bundle",
            )
        files.append(
            {
                "source_path": member.source_path,
                "bundle_path": member.bundle_path,
                "mode": member.mode,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    content_identity = {
        "schema_version": IMPLEMENTATION_BUNDLE_SCHEMA,
        "implementation_id": IMPLEMENTATION_ID,
        "relative_entrypoint": str(RELATIVE_ENTRYPOINT),
        "entrypoint_contract": _entrypoint_contract(),
        "files": [
            {key: value for key, value in entry.items() if key != "source_path"}
            for entry in files
        ],
    }
    bundle_sha256 = _sha256(content_identity)
    remote_root = REMOTE_IMPLEMENTATION_PARENT / bundle_sha256
    return {
        **content_identity,
        "files": files,
        "file_count": len(files),
        "total_bytes": total_bytes,
        "materialization": {
            "source_verification": ("reopen-no-follow-and-match-bytes-and-sha256"),
            "target_mode_policy": "exact",
        },
        "bundle_sha256": bundle_sha256,
        "remote_root": str(remote_root),
        "entrypoint": str(remote_root / RELATIVE_ENTRYPOINT),
    }


def _validated_closure(
    definition: InferenceServiceDefinition,
    closure: HuggingFaceClosure,
) -> tuple[HuggingFaceClosure, dict[str, Any]]:
    validated = parse_huggingface_closure(closure.as_dict())
    document = validated.as_dict()
    model = definition.normalized_plan()["model"]
    expected_source = {
        "kind": model["source"],
        "repository": model["repository"],
        "revision": model["revision"],
    }
    if document["source"] != expected_source:
        raise RunpodLocalError(
            "generated Hugging Face closure does not match the service model",
            code="mismatched_service_huggingface_closure",
        )
    if document["checkpoint"]["requested_selector"] != model["checkpoint"]:
        raise RunpodLocalError(
            "generated Hugging Face closure does not match the checkpoint selector",
            code="mismatched_service_huggingface_closure",
        )
    if definition.normalized_plan()["vllm"]["load_format"] == "safetensors":
        resolved_index = document["checkpoint"]["resolved_index"]
        weight_files = document["checkpoint"]["weight_files"]
        if (
            resolved_index is not None
            and not resolved_index.endswith(".safetensors.index.json")
        ) or any(not path.endswith(".safetensors") for path in weight_files):
            raise RunpodLocalError(
                "generated Hugging Face closure is incompatible with the "
                "service safetensors load format",
                code="incompatible_service_huggingface_closure",
            )
    return validated, document


def _compile_cache_requirement(
    *,
    deployment: dict[str, Any],
    closure_sha256: str,
) -> dict[str, Any]:
    return {
        "status": "requires-observed-gpu",
        "contract_schema_version": COMPILE_CACHE_SCHEMA,
        "inputs": {
            "driver": deployment["driver"],
            "runtime": deployment["runtime"],
            "huggingface_closure_sha256": closure_sha256,
            "compile_affecting_launch_sha256": deployment["launch"][
                "compile_affecting_sha256"
            ],
        },
        "observed_gpu": None,
    }


def build_deployment_manifest(
    definition: InferenceServiceDefinition,
    *,
    runtime: dict[str, Any],
    closure: HuggingFaceClosure,
    implementation_bundle: dict[str, Any],
    remote_port: int = DEFAULT_REMOTE_PORT,
) -> dict[str, Any]:
    """Generate the sole model-specific input consumed by the remote runtime."""

    validated_closure, closure_document = _validated_closure(
        definition,
        closure,
    )
    service = definition.normalized_plan()
    deployment_plan = build_vllm_deployment_plan(
        definition,
        runtime=runtime,
        remote_port=remote_port,
    )
    service_root = REMOTE_SERVICE_PARENT / service["service_id"]
    manifest_path = service_root / "deployment.json"
    snapshot_root = REMOTE_SNAPSHOT_PARENT / validated_closure.closure_sha256
    arguments = list(
        build_vllm_argv(
            definition,
            model_path=str(snapshot_root),
            remote_port=remote_port,
        )
    )
    deployment = {
        "service_root": str(service_root),
        "manifest_path": str(manifest_path),
        "process": deployment_plan["process"],
        "model_snapshot": {
            "root": str(snapshot_root),
            "closure_sha256": validated_closure.closure_sha256,
        },
        "launch": {
            **deployment_plan["launch"],
            "argv": arguments,
        },
    }
    deployment["launch"].pop("argv_template")
    deployment["launch"].pop("api_key_source")
    implementation = {
        "implementation_id": implementation_bundle["implementation_id"],
        "bundle_sha256": implementation_bundle["bundle_sha256"],
        "remote_root": implementation_bundle["remote_root"],
        "entrypoint": implementation_bundle["entrypoint"],
    }
    return {
        "schema_version": DEPLOYMENT_MANIFEST_SCHEMA,
        "definition": {
            "source_sha256": definition.source_sha256,
            "source_bytes": definition.source_size,
            "service_plan_sha256": definition.plan_sha256,
            "service": service,
        },
        "runtime": runtime,
        "huggingface_closure": closure_document,
        "implementation": implementation,
        "deployment": deployment,
        "compile_cache": _compile_cache_requirement(
            deployment=deployment_plan,
            closure_sha256=validated_closure.closure_sha256,
        ),
    }


def build_service_bundle_plan(
    definition: InferenceServiceDefinition,
    *,
    source_root: pathlib.Path,
    runtime: dict[str, Any],
    closure: HuggingFaceClosure,
    remote_port: int = DEFAULT_REMOTE_PORT,
) -> dict[str, Any]:
    """Build a bundle and manifest plan without local or remote mutation."""

    _validated_closure(definition, closure)
    implementation_bundle = build_implementation_bundle(source_root=source_root)
    manifest = build_deployment_manifest(
        definition,
        runtime=runtime,
        closure=closure,
        implementation_bundle=implementation_bundle,
        remote_port=remote_port,
    )
    payload = _canonical_bytes(manifest)
    manifest_descriptor = {
        "schema_version": DEPLOYMENT_MANIFEST_SCHEMA,
        "remote_path": manifest["deployment"]["manifest_path"],
        "mode": "0600",
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "document": manifest,
    }
    identity = {
        "implementation_bundle_sha256": implementation_bundle["bundle_sha256"],
        "deployment_manifest_sha256": manifest_descriptor["sha256"],
    }
    return {
        "schema_version": BUNDLE_PLAN_SCHEMA,
        "executed": False,
        "implementation_bundle": implementation_bundle,
        "deployment_manifest": manifest_descriptor,
        "plan_sha256": _sha256(identity),
    }
