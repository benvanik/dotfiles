"""Strict generated documents for the vLLM compiled-cache lifecycle."""

from __future__ import annotations

import pathlib
from typing import Any, Literal, TypeAlias

from runpod_local.errors import RunpodLocalError
from runpod_local.service_compile_cache import (
    COMPILE_CACHE_SCHEMA,
    build_compile_cache_contract,
)

from .compile_cache_files import (
    DIRECTORY_STAT_FIELDS,
    MAX_MEASUREMENT_BYTES,
    fail,
    is_sha256,
    read_exact_json,
    safe_relative,
)
from .execution_environment import validate_runtime_execution_environment
from .layout import RuntimeLayout


COMPILE_CACHE_BUNDLE_MANIFEST_SCHEMA = "runpod.vllm-compile-cache-bundle-manifest.v1"
COMPILE_CACHE_PREPARATION_SCHEMA = "runpod.vllm-compile-cache-preparation.v1"
COMPILE_CACHE_AUTHOR_CANDIDATE_SCHEMA = "runpod.vllm-compile-cache-author-candidate.v1"
COMPILE_CACHE_ACCEPTANCE_SCHEMA = "runpod.vllm-compile-cache-acceptance.v1"
COMPILE_CACHE_STAGE_SOURCE_SCHEMA = "runpod.vllm-compile-cache-stage-source.v1"
COMPILE_CACHE_STAGE_SCHEMA = "runpod.local-vllm-compile-cache-stage.v1"
COMPILE_CACHE_PREREQUISITE_SUMMARY_SCHEMA = "runpod.vllm-compile-cache-prerequisite.v1"
COMPILE_CACHE_LAUNCH_MEASUREMENT_SCHEMA = (
    "runpod.vllm-compile-cache-launch-measurement.v1"
)
COMPILE_CACHE_STARTUP_PROOF_SCHEMA = "runpod.vllm-compile-cache-startup-proof.v1"
VLLM_CACHE_EVIDENCE_SCHEMA = "runpod.vllm-openai-cache-evidence.v1"
MAX_CANDIDATE_READY_DURATION_NS = 5 * 60 * 1_000_000_000
CompileCacheMode: TypeAlias = Literal[
    "ephemeral",
    "author",
    "candidate-proof",
    "accepted",
]

BUNDLE_NAME = "bundle.tar"
MANIFEST_NAME = "manifest.json"
AUTHORED_NAME = "authored.json"
ACCEPTED_NAME = "accepted.json"
CANDIDATE_ENTRIES = frozenset({BUNDLE_NAME, MANIFEST_NAME, AUTHORED_NAME})
ACCEPTED_ENTRIES = frozenset({*CANDIDATE_ENTRIES, ACCEPTED_NAME})

_CONTRACT_FIELDS = frozenset(
    {"schema_version", "cache_id", "identity", "persistent_root", "local_root"}
)
_MEASUREMENT_FIELDS = frozenset(
    {
        "schema_version",
        "mode",
        "cache_id",
        "contract",
        "boot_id",
        "service_manifest_sha256",
        "prerequisite_receipt_sha256",
        "runtime_execution_environment",
        "runtime_execution_environment_sha256",
        "started_monotonic_ns",
        "ready_monotonic_ns",
        "stopped_monotonic_ns",
        "ready",
        "process_stopped",
        "pre_inventory_sha256",
        "post_inventory_sha256",
        "cache_evidence",
    }
)
_CACHE_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "driver",
        "mode",
        "cache_root",
        "produced_artifacts",
        "loaded_artifacts",
        "cold_compile_observed",
        "unexpected_cache_paths",
    }
)


def validate_contract(contract: Any) -> dict[str, Any]:
    """Re-derive the exact contract, rejecting extra host identities."""

    if not isinstance(contract, dict) or set(contract) != _CONTRACT_FIELDS:
        fail("compiled-cache contract fields are malformed")
    identity = contract.get("identity")
    if not isinstance(identity, dict):
        fail("compiled-cache contract identity is malformed")
    gpu = identity.get("gpu")
    if not isinstance(gpu, dict) or set(gpu) != {
        "name",
        "compute_capability",
        "memory_mib",
        "product_sha256",
        "driver_version",
    }:
        fail("compiled-cache GPU identity is malformed")
    observed_gpu = {
        "name": gpu["name"],
        "compute_capability": gpu["compute_capability"],
        "memory_mib": gpu["memory_mib"],
        "driver_version": gpu["driver_version"],
    }
    runtime = identity.get("runtime")
    if not isinstance(runtime, dict) or set(runtime) != {
        "runtime_id",
        "image",
        "manifest_sha256",
    }:
        fail("compiled-cache runtime identity is malformed")
    try:
        expected = build_compile_cache_contract(
            driver=identity["driver"],
            runtime={
                "runtime_id": runtime["runtime_id"],
                "image": runtime["image"],
                "manifest": {"sha256": runtime["manifest_sha256"]},
            },
            runtime_execution_environment=(
                validate_runtime_execution_environment(
                    identity["runtime_execution_environment"]
                ).normalized()
            ),
            implementation_bundle_sha256=identity["implementation_bundle_sha256"],
            huggingface_closure_sha256=identity["huggingface_closure_sha256"],
            compile_affecting_launch_sha256=identity["compile_affecting_launch_sha256"],
            observed_gpu=observed_gpu,
        )
    except (KeyError, RunpodLocalError) as error:
        raise RunpodLocalError(
            "compiled-cache contract identity is malformed",
            code="compile_cache_operation_failed",
        ) from error
    if expected != contract or contract["schema_version"] != COMPILE_CACHE_SCHEMA:
        fail("compiled-cache contract is not its canonical derived identity")
    return contract


def prerequisite_receipt_path(contract: dict[str, Any]) -> pathlib.PurePosixPath:
    return pathlib.PurePosixPath(f"{contract['local_root']}.prerequisite.json")


def compile_cache_receipt_path(
    contract: dict[str, Any],
) -> pathlib.PurePosixPath:
    return pathlib.PurePosixPath(f"{contract['local_root']}.stage.json")


def localized_paths(
    *,
    contract: dict[str, Any],
    layout: RuntimeLayout,
) -> dict[str, pathlib.Path]:
    return {
        "persistent_root": layout.localize(contract["persistent_root"]),
        "local_root": layout.localize(contract["local_root"]),
        "stage_receipt": layout.localize(compile_cache_receipt_path(contract)),
        "prerequisite": layout.localize(prerequisite_receipt_path(contract)),
    }


def load_preparation(
    *,
    contract: dict[str, Any],
    path: pathlib.Path,
    boot_id: str,
    expected_mode: Literal["ephemeral", "author"],
) -> tuple[dict[str, Any], bytes]:
    receipt, payload = read_exact_json(
        path,
        mode=0o600,
        maximum_bytes=MAX_MEASUREMENT_BYTES,
    )
    fields = frozenset(
        {
            "schema_version",
            "mode",
            "cache_id",
            "contract",
            "boot_id",
            "local_root",
            "directory_stat",
            "inventory_sha256",
            "file_count",
            "total_bytes",
        }
    )
    root_stat = receipt.get("directory_stat")
    if (
        set(receipt) != fields
        or receipt["schema_version"] != COMPILE_CACHE_PREPARATION_SCHEMA
        or receipt["mode"] != expected_mode
        or receipt["cache_id"] != contract["cache_id"]
        or receipt["contract"] != contract
        or receipt["boot_id"] != boot_id
        or receipt["local_root"] != contract["local_root"]
        or not isinstance(root_stat, dict)
        or set(root_stat) != DIRECTORY_STAT_FIELDS
        or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in root_stat.values()
        )
        or root_stat["mode"] != 0o700
        or not is_sha256(receipt["inventory_sha256"])
        or receipt["file_count"] != 0
        or receipt["total_bytes"] != 0
    ):
        fail("author preparation receipt is malformed or mismatched")
    return receipt, payload


def _validate_evidence_paths(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, list):
        fail(f"{label} must be a sorted path list")
    paths = [safe_relative(item, label=f"{label} entry").as_posix() for item in value]
    if paths != sorted(set(paths)):
        fail(f"{label} must be sorted and unique")
    return paths


def validate_cache_evidence(
    value: Any,
    *,
    driver: str,
    cache_root: str,
    mode: CompileCacheMode,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _CACHE_EVIDENCE_FIELDS:
        fail("compiled-cache launch evidence fields are malformed")
    if (
        value["schema_version"] != VLLM_CACHE_EVIDENCE_SCHEMA
        or value["driver"] != driver
        or value["mode"] != mode
        or value["cache_root"] != cache_root
        or not isinstance(value["cold_compile_observed"], bool)
    ):
        fail("compiled-cache launch evidence is mismatched")
    produced = _validate_evidence_paths(
        value["produced_artifacts"],
        label="produced cache artifacts",
    )
    loaded = _validate_evidence_paths(
        value["loaded_artifacts"],
        label="loaded cache artifacts",
    )
    unexpected = _validate_evidence_paths(
        value["unexpected_cache_paths"],
        label="unexpected cache paths",
    )
    if unexpected:
        fail("compiled-cache launch accessed paths outside its expected cache")
    if mode == "author":
        if not produced or loaded or not value["cold_compile_observed"]:
            fail("author launch did not prove cold cache production")
    elif mode in {"candidate-proof", "accepted"} and (
        produced or not loaded or value["cold_compile_observed"]
    ):
        fail("require launch did not prove cache-only reuse")
    elif mode == "ephemeral" and loaded:
        fail("ephemeral launch unexpectedly loaded a prepared cache")
    return value


def validate_measurement_document(
    measurement: Any,
    *,
    contract: dict[str, Any],
    mode: CompileCacheMode,
) -> dict[str, Any]:
    try:
        execution_environment = validate_runtime_execution_environment(
            measurement.get("runtime_execution_environment")
            if isinstance(measurement, dict)
            else None
        )
    except RunpodLocalError as error:
        raise RunpodLocalError(
            "compiled-cache launch measurement has an invalid runtime "
            "execution environment",
            code="compile_cache_operation_failed",
        ) from error
    if (
        not isinstance(measurement, dict)
        or set(measurement) != _MEASUREMENT_FIELDS
        or measurement["schema_version"] != COMPILE_CACHE_LAUNCH_MEASUREMENT_SCHEMA
        or measurement["mode"] != mode
        or measurement["cache_id"] != contract["cache_id"]
        or measurement["contract"] != contract
        or not isinstance(measurement["boot_id"], str)
        or not measurement["boot_id"]
        or not is_sha256(measurement["service_manifest_sha256"])
        or not is_sha256(measurement["prerequisite_receipt_sha256"])
        or measurement["runtime_execution_environment_sha256"]
        != execution_environment.sha256
        or measurement["runtime_execution_environment"]
        != contract["identity"]["runtime_execution_environment"]
        or measurement["ready"] is not True
        or measurement["process_stopped"] is not True
        or not is_sha256(measurement["pre_inventory_sha256"])
        or not is_sha256(measurement["post_inventory_sha256"])
    ):
        fail("compiled-cache launch measurement is malformed or mismatched")
    times = [
        measurement["started_monotonic_ns"],
        measurement["ready_monotonic_ns"],
        measurement["stopped_monotonic_ns"],
    ]
    if (
        any(isinstance(item, bool) or not isinstance(item, int) for item in times)
        or not 0 < times[0] <= times[1] <= times[2]
    ):
        fail("compiled-cache launch measurement times are malformed")
    if (
        mode == "candidate-proof"
        and startup_duration_ns(measurement) > MAX_CANDIDATE_READY_DURATION_NS
    ):
        fail("candidate cache process-start-to-first-ready exceeded five minutes")
    validate_cache_evidence(
        measurement["cache_evidence"],
        driver=contract["identity"]["driver"],
        cache_root=contract["local_root"],
        mode=mode,
    )
    return measurement


def startup_duration_ns(measurement: dict[str, Any]) -> int:
    """Return the measured process-start-to-first-ready duration."""

    return measurement["ready_monotonic_ns"] - measurement["started_monotonic_ns"]


def candidate_startup_proof(
    *,
    author_measurement: dict[str, Any],
    candidate_measurement: dict[str, Any],
) -> dict[str, Any]:
    """Require a bounded candidate launch that improves on cold authoring."""

    author_duration = startup_duration_ns(author_measurement)
    candidate_duration = startup_duration_ns(candidate_measurement)
    if candidate_duration > MAX_CANDIDATE_READY_DURATION_NS:
        fail("candidate cache process-start-to-first-ready exceeded five minutes")
    if candidate_duration >= author_duration:
        fail(
            "candidate cache process-start-to-first-ready did not improve "
            "over authoring"
        )
    return {
        "schema_version": COMPILE_CACHE_STARTUP_PROOF_SCHEMA,
        "metric": "process-start-to-first-ready-monotonic-ns",
        "maximum_ns": MAX_CANDIDATE_READY_DURATION_NS,
        "author_duration_ns": author_duration,
        "candidate_duration_ns": candidate_duration,
        "improvement_ns": author_duration - candidate_duration,
    }


def load_measurement(
    *,
    path: pathlib.Path,
    contract: dict[str, Any],
    mode: CompileCacheMode,
) -> tuple[dict[str, Any], bytes]:
    measurement, payload = read_exact_json(
        path,
        mode=0o600,
        maximum_bytes=MAX_MEASUREMENT_BYTES,
    )
    validate_measurement_document(
        measurement,
        contract=contract,
        mode=mode,
    )
    return measurement, payload


def artifact_records(
    inventory: dict[str, Any],
    paths: list[str],
) -> list[dict[str, Any]]:
    by_path = {record["path"]: record for record in inventory["files"]}
    try:
        return [
            {
                "path": path,
                "bytes": by_path[path]["bytes"],
                "sha256": by_path[path]["sha256"],
            }
            for path in paths
        ]
    except KeyError as error:
        raise RunpodLocalError(
            "cache evidence names an artifact outside the exact inventory",
            code="compile_cache_operation_failed",
        ) from error


def validate_descriptor(
    value: Any,
    *,
    expected_name: str,
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"name", "bytes", "sha256"}
        or value["name"] != expected_name
        or isinstance(value["bytes"], bool)
        or not isinstance(value["bytes"], int)
        or value["bytes"] <= 0
        or not is_sha256(value["sha256"])
    ):
        fail(f"persistent compiled-cache {expected_name} descriptor is malformed")
    return value
