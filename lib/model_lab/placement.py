"""Versioned static GPU placement policy."""

from __future__ import annotations

import json
import math
import pathlib
from typing import Any

from .errors import ModelLabError
from .huggingface_model import GIB, bytes_to_gib, utc_now


PLACEMENT_POLICY = "runpod-static-v1"
DEFAULT_GPU_MEMORY_UTILIZATION = 0.90
DEFAULT_WEIGHT_SLACK = 1.03
DEFAULT_FRAMEWORK_RESERVE_GIB = 4.0


def load_hardware_catalog(
    path: pathlib.Path | None = None,
) -> dict[str, Any]:
    catalog_path = (
        path
        or pathlib.Path(__file__).resolve().parents[2]
        / "runpod"
        / "hardware.json"
    )
    try:
        with catalog_path.open("r", encoding="utf-8") as catalog_file:
            catalog = json.load(catalog_file)
    except (OSError, json.JSONDecodeError) as error:
        raise ModelLabError(
            f"cannot read Runpod hardware catalog {catalog_path}: {error}",
            code="hardware_catalog_error",
        ) from error
    if not isinstance(catalog, dict) or not isinstance(catalog.get("gpus"), list):
        raise ModelLabError(
            f"Runpod hardware catalog {catalog_path} has no GPU list",
            code="hardware_catalog_error",
        )
    identifiers: set[str] = set()
    aliases: set[str] = set()
    for gpu in catalog["gpus"]:
        if not isinstance(gpu, dict):
            raise ModelLabError(
                f"Runpod hardware catalog {catalog_path} contains a non-object GPU",
                code="hardware_catalog_error",
            )
        identifier = gpu.get("id")
        memory = gpu.get("provider_memory_gb")
        if (
            not isinstance(identifier, str)
            or not identifier
            or not isinstance(memory, (int, float))
            or memory <= 0
        ):
            raise ModelLabError(
                f"Runpod hardware catalog {catalog_path} contains an invalid GPU",
                code="hardware_catalog_error",
            )
        if identifier in identifiers:
            raise ModelLabError(
                f"Runpod hardware catalog repeats GPU ID {identifier}",
                code="hardware_catalog_error",
            )
        identifiers.add(identifier)
        for alias in gpu.get("aliases", []):
            if not isinstance(alias, str) or not alias:
                raise ModelLabError(
                    f"Runpod hardware catalog has an invalid alias for {identifier}",
                    code="hardware_catalog_error",
                )
            normalized = alias.casefold()
            if normalized in aliases:
                raise ModelLabError(
                    f"Runpod hardware catalog repeats alias {alias}",
                    code="hardware_catalog_error",
                )
            aliases.add(normalized)
    return catalog


def select_hardware(
    catalog: dict[str, Any], requested: list[str] | None
) -> list[dict[str, Any]]:
    gpus = catalog["gpus"]
    if not requested:
        return list(gpus)
    lookup: dict[str, dict[str, Any]] = {}
    for gpu in gpus:
        lookup[gpu["id"].casefold()] = gpu
        lookup[gpu["display_name"].casefold()] = gpu
        for alias in gpu.get("aliases", []):
            lookup[alias.casefold()] = gpu
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name in requested:
        gpu = lookup.get(name.casefold())
        if gpu is None:
            raise ModelLabError(
                f"unknown GPU {name!r}; use model-lab place --list-gpus",
                code="unknown_gpu",
            )
        if gpu["id"] not in seen:
            selected.append(gpu)
            seen.add(gpu["id"])
    return selected


def _format_capability(weight_format: str) -> str:
    if weight_format == "native":
        return "native"
    if weight_format == "q8":
        return "int8"
    return weight_format


def place_model(
    model_estimate: dict[str, Any],
    *,
    catalog: dict[str, Any] | None = None,
    requested_gpus: list[str] | None = None,
    gpu_count: int = 1,
    gpu_memory_utilization: float = DEFAULT_GPU_MEMORY_UTILIZATION,
    weight_slack: float = DEFAULT_WEIGHT_SLACK,
    framework_reserve_gib: float = DEFAULT_FRAMEWORK_RESERVE_GIB,
) -> dict[str, Any]:
    if gpu_count <= 0:
        raise ModelLabError(
            "GPU count must be positive",
            code="invalid_gpu_count",
        )
    if not 0 < gpu_memory_utilization <= 1:
        raise ModelLabError(
            "GPU memory utilization must be greater than zero and at most one",
            code="invalid_placement_policy",
        )
    if weight_slack < 1:
        raise ModelLabError(
            "weight slack must be at least one",
            code="invalid_placement_policy",
        )
    if framework_reserve_gib < 0:
        raise ModelLabError(
            "framework reserve must not be negative",
            code="invalid_placement_policy",
        )
    runtime = model_estimate.get("runtime_estimate")
    if not isinstance(runtime, dict):
        raise ModelLabError(
            "model estimate has no runtime_estimate object",
            code="invalid_model_estimate",
        )
    weight_bytes = runtime.get("weight_bytes")
    if not isinstance(weight_bytes, int) or weight_bytes <= 0:
        raise ModelLabError(
            "model estimate has no positive weight byte count",
            code="invalid_model_estimate",
        )
    kv_cache = runtime.get("kv_cache")
    if not isinstance(kv_cache, dict):
        raise ModelLabError(
            "model estimate has no KV cache result",
            code="invalid_model_estimate",
        )
    kv_available = kv_cache.get("available") is True
    kv_bytes = kv_cache.get("bytes") if kv_available else None
    if kv_available and (not isinstance(kv_bytes, int) or kv_bytes < 0):
        raise ModelLabError(
            "model estimate has an invalid KV cache byte count",
            code="invalid_model_estimate",
        )

    catalog = catalog or load_hardware_catalog()
    hardware = select_hardware(catalog, requested_gpus)
    per_gpu_weight_bytes = math.ceil(weight_bytes / gpu_count)
    weight_envelope_bytes = math.ceil(per_gpu_weight_bytes * weight_slack)
    per_gpu_kv_bytes = math.ceil(kv_bytes / gpu_count) if kv_bytes is not None else None
    framework_reserve_bytes = math.ceil(framework_reserve_gib * GIB)
    weight_format = runtime.get("weight_format")
    format_capability = (
        _format_capability(weight_format)
        if isinstance(weight_format, str)
        else "native"
    )
    placements = []
    for gpu in hardware:
        physical_bytes = math.floor(float(gpu["provider_memory_gb"]) * GIB)
        allocatable_bytes = math.floor(
            physical_bytes * gpu_memory_utilization
        )
        capabilities = set(gpu.get("capabilities", []))
        format_supported = (
            format_capability == "native" or format_capability in capabilities
        )
        required_bytes = (
            weight_envelope_bytes
            + framework_reserve_bytes
            + (per_gpu_kv_bytes or 0)
        )

        reasons: list[str] = []
        if per_gpu_weight_bytes > physical_bytes:
            status = "impossible"
            reasons.append(
                "the serialized tensor residency basis alone exceeds physical VRAM"
            )
        elif not format_supported:
            status = "indeterminate"
            reasons.append(
                f"the catalog does not declare native {format_capability} support"
            )
        elif gpu_count > 1:
            status = "indeterminate"
            reasons.append(
                "multi-GPU partition balance, replicated tensors, and communication "
                "allocations are not modeled"
            )
        elif not kv_available:
            status = "indeterminate"
            reasons.append(
                f"KV cache is unmodeled: {kv_cache.get('reason', 'unknown reason')}"
            )
        elif required_bytes <= allocatable_bytes:
            status = "candidate"
            reasons.append(
                "the modeled workload fits the policy allocation envelope"
            )
        else:
            status = "tight"
            reasons.append(
                "weights fit physical VRAM, but the requested workload exceeds "
                "the policy allocation envelope"
            )

        headroom_bytes = allocatable_bytes - required_bytes
        placements.append(
            {
                "gpu_id": gpu["id"],
                "display_name": gpu["display_name"],
                "architecture": gpu.get("architecture"),
                "gpu_count": gpu_count,
                "provider_memory_gb": gpu["provider_memory_gb"],
                "physical_bytes_per_gpu": physical_bytes,
                "allocatable_bytes_per_gpu": allocatable_bytes,
                "weight_bytes_per_gpu": per_gpu_weight_bytes,
                "weight_envelope_bytes_per_gpu": weight_envelope_bytes,
                "kv_cache_bytes_per_gpu": per_gpu_kv_bytes,
                "framework_reserve_bytes_per_gpu": framework_reserve_bytes,
                "required_bytes_per_gpu": required_bytes,
                "required_gib_per_gpu": bytes_to_gib(required_bytes),
                "headroom_bytes_per_gpu": headroom_bytes,
                "headroom_gib_per_gpu": round(headroom_bytes / GIB, 3),
                "format_supported": format_supported,
                "status": status,
                "reasons": reasons,
            }
        )

    status_order = {
        "candidate": 0,
        "tight": 1,
        "indeterminate": 2,
        "impossible": 3,
    }
    placements.sort(
        key=lambda placement: (
            status_order[placement["status"]],
            placement["provider_memory_gb"],
            placement["gpu_id"],
        )
    )
    repository = model_estimate.get("repository", {})
    checkpoint = model_estimate.get("checkpoint")
    return {
        "schema_version": "model-lab.placement.v1",
        "generated_at": utc_now(),
        "model": {
            "repository": repository.get("id"),
            "requested_revision": repository.get("requested_revision"),
            "resolved_revision": repository.get("resolved_revision"),
            "checkpoint": checkpoint if isinstance(checkpoint, dict) else None,
            "weight_format": weight_format,
            "weight_bytes": weight_bytes,
            "kv_cache": kv_cache,
        },
        "policy": {
            "id": PLACEMENT_POLICY,
            "gpu_memory_utilization": gpu_memory_utilization,
            "weight_slack": weight_slack,
            "framework_reserve_gib_per_gpu": framework_reserve_gib,
            "provider_memory_unit_assumption": "reported GB treated as GiB",
            "verified_status_requires_measurement": True,
        },
        "catalog": {
            "schema_version": catalog.get("schema_version"),
            "as_of": catalog.get("catalog_as_of"),
            "source": catalog.get("source"),
        },
        "placements": placements,
    }
