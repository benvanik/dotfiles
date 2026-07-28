"""Validate and plan one config-only inference-service deployment."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import stat
from typing import Any

from .errors import RunpodLocalError
from .service_definition import InferenceServiceDefinition
from .service_vllm import DEFAULT_REMOTE_PORT, build_vllm_deployment_plan

VALIDATION_SCHEMA = "runpod.inference-service-validation.v1"
DEPLOYMENT_PLAN_SCHEMA = "runpod.inference-service-deployment-plan.v1"
PLANNING_SOURCE_SCHEMA = "runpod.inference-service-planning-source.v1"
PLANNING_SOURCE_ID = "runpod-inference-service-planner-v1"
MAX_PLANNING_SOURCE_BYTES = 1024 * 1024
PLANNING_SOURCE_FILES = (
    "lib/runpod_local/__init__.py",
    "lib/runpod_local/errors.py",
    "lib/runpod_local/service_definition.py",
    "lib/runpod_local/service_vllm.py",
    "lib/runpod_local/service_controller.py",
)


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _trusted_source_bytes(
    source_root: pathlib.Path,
    relative: str,
) -> bytes:
    path = source_root.joinpath(*pathlib.PurePosixPath(relative).parts)
    try:
        resolved = path.resolve(strict=True)
        expected = path.absolute()
        metadata = path.lstat()
    except OSError as error:
        raise RunpodLocalError(
            f"cannot inspect planning source {relative}",
            code="unsafe_service_planning_source",
        ) from error
    if (
        resolved != expected
        or path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size < 1
        or metadata.st_size > MAX_PLANNING_SOURCE_BYTES
        or metadata.st_mode & 0o002
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        raise RunpodLocalError(
            f"planning source is unsafe: {relative}",
            code="unsafe_service_planning_source",
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RunpodLocalError(
            f"cannot open planning source {relative}",
            code="unsafe_service_planning_source",
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_size != metadata.st_size
        ):
            raise RunpodLocalError(
                f"planning source changed while opening: {relative}",
                code="service_planning_source_drift",
            )
        chunks: list[bytes] = []
        observed_bytes = 0
        while observed_bytes <= MAX_PLANNING_SOURCE_BYTES:
            chunk = os.read(
                descriptor,
                min(
                    64 * 1024,
                    MAX_PLANNING_SOURCE_BYTES + 1 - observed_bytes,
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
            f"planning source changed while reading: {relative}",
            code="service_planning_source_drift",
        )
    return b"".join(chunks)


def build_planning_source_closure(
    *,
    source_root: pathlib.Path,
) -> dict[str, Any]:
    """Inventory reusable local planner code; model config is not a member."""

    files = []
    for relative in PLANNING_SOURCE_FILES:
        payload = _trusted_source_bytes(source_root, relative)
        files.append(
            {
                "source": relative,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    identity = {
        "planning_source_id": PLANNING_SOURCE_ID,
        "files": files,
    }
    return {
        "schema_version": PLANNING_SOURCE_SCHEMA,
        "planning_source_id": PLANNING_SOURCE_ID,
        "files": files,
        "source_sha256": _canonical_sha256(identity),
    }


def _config_input(
    definition: InferenceServiceDefinition,
    *,
    source_path: pathlib.Path,
) -> dict[str, Any]:
    service_id = definition.normalized_plan()["service_id"]
    return {
        "source_path": str(source_path),
        "bytes": definition.source_size,
        "sha256": definition.source_sha256,
        "remote_path": str(
            pathlib.PurePosixPath("/root/runpod-session/services")
            / service_id
            / "service.toml"
        ),
        "companion_inputs": 0,
    }


def build_service_validation(
    definition: InferenceServiceDefinition,
    *,
    source_path: pathlib.Path,
    runtime: dict[str, Any],
) -> dict[str, Any]:
    """Return the validated semantic contract without remote side effects."""

    return {
        "schema_version": VALIDATION_SCHEMA,
        "valid": True,
        "service": definition.normalized_plan(),
        "service_plan_sha256": definition.plan_sha256,
        "runtime": runtime,
        "config_input": _config_input(
            definition,
            source_path=source_path,
        ),
    }


def build_service_deployment_plan(
    definition: InferenceServiceDefinition,
    *,
    source_path: pathlib.Path,
    source_root: pathlib.Path,
    runtime: dict[str, Any],
    remote_port: int = DEFAULT_REMOTE_PORT,
) -> dict[str, Any]:
    """Resolve an inspectable deployment plan and execute nothing."""

    validation = build_service_validation(
        definition,
        source_path=source_path,
        runtime=runtime,
    )
    planning_source = build_planning_source_closure(source_root=source_root)
    deployment = build_vllm_deployment_plan(
        definition,
        runtime=runtime,
        remote_port=remote_port,
    )
    remote_controller_requirement = {
        "status": "unresolved",
        "required_capabilities": [
            "setup",
            "start",
            "status",
            "stop",
        ],
        "generic_implementation_required": True,
        "config_input_count": 1,
        "definition_path": deployment["definition_path"],
        "driver": deployment["driver"],
    }
    identity = {
        "service_plan_sha256": definition.plan_sha256,
        "config_input": {
            key: value
            for key, value in validation["config_input"].items()
            if key != "source_path"
        },
        "planning_source_sha256": planning_source["source_sha256"],
        "remote_controller_requirement": remote_controller_requirement,
        "deployment": deployment,
    }
    return {
        "schema_version": DEPLOYMENT_PLAN_SCHEMA,
        "executed": False,
        "service": validation["service"],
        "service_plan_sha256": definition.plan_sha256,
        "runtime": runtime,
        "config_input": validation["config_input"],
        "planning_source_closure": planning_source,
        "remote_controller_requirement": remote_controller_requirement,
        "deployment": deployment,
        "plan_sha256": _canonical_sha256(identity),
    }
