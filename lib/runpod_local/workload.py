"""Provider-read-only Hugging Face inspection and static GPU admission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .cache import JsonCache
from .errors import RunpodLocalError
from .http import JsonHttpTransport
from .model import HuggingFaceClient, ModelInspector
from .placement import (
    DEFAULT_FRAMEWORK_RESERVE_GIB,
    DEFAULT_GPU_MEMORY_UTILIZATION,
    DEFAULT_WEIGHT_SLACK,
    load_hardware_catalog,
    place_model,
    select_hardware,
)


@dataclass(frozen=True)
class HuggingFaceWorkload:
    """One exact checkpoint inspection request."""

    repository: str
    revision: str = "main"
    index_file: str | None = None
    context_tokens: int = 32768
    sequences: int = 1
    kv_dtype: str = "bf16"
    weight_format: str = "native"
    offline: bool = False
    refresh: bool = False


@dataclass(frozen=True)
class WorkloadPlacementRequest:
    """Static hardware admission constraints, independent of provider stock."""

    allowed_gpu_ids: tuple[str, ...]
    gpu_count: int
    requested_gpus: tuple[str, ...] = ()
    model: HuggingFaceWorkload | None = None
    allow_indeterminate_fit: bool = False
    gpu_memory_utilization: float = DEFAULT_GPU_MEMORY_UTILIZATION
    weight_slack: float = DEFAULT_WEIGHT_SLACK
    framework_reserve_gib: float = DEFAULT_FRAMEWORK_RESERVE_GIB


@dataclass(frozen=True)
class WorkloadPlacement:
    """The GPU admission set and model facts carried into launch."""

    admitted_gpu_ids: set[str] | None
    model_summary: dict[str, Any] | None


def plan_workload(
    request: WorkloadPlacementRequest,
    *,
    cache: JsonCache,
    catalog: dict[str, Any] | None = None,
    transport: JsonHttpTransport | None = None,
) -> WorkloadPlacement:
    """Inspects metadata and applies static placement without provider access."""
    if not request.allowed_gpu_ids:
        raise RunpodLocalError(
            "workload placement requires at least one allowed GPU ID",
            code="invalid_workload_placement",
        )

    resolved_catalog = catalog
    if request.requested_gpus or request.model is not None:
        if resolved_catalog is None:
            resolved_catalog = load_hardware_catalog()

    requested_ids: set[str] | None = None
    if request.requested_gpus:
        if resolved_catalog is None:
            raise AssertionError("hardware catalog unexpectedly absent")
        requested = select_hardware(
            resolved_catalog, list(request.requested_gpus)
        )
        requested_ids = {gpu["id"] for gpu in requested}
        outside = requested_ids.difference(request.allowed_gpu_ids)
        if outside:
            raise RunpodLocalError(
                "requested GPU is not allowed by the profile: "
                + ", ".join(sorted(outside)),
                code="gpu_not_allowed",
            )

    if request.model is None:
        return WorkloadPlacement(
            admitted_gpu_ids=requested_ids,
            model_summary=None,
        )

    if resolved_catalog is None:
        raise AssertionError("hardware catalog unexpectedly absent")
    model_request = request.model
    model = ModelInspector(
        HuggingFaceClient(
            cache=cache,
            transport=(
                transport
                if transport is not None
                else JsonHttpTransport()
            ),
            offline=model_request.offline,
            refresh=model_request.refresh,
        )
    ).inspect(
        model_request.repository,
        revision=model_request.revision,
        index_file=model_request.index_file,
        context_tokens=model_request.context_tokens,
        sequences=model_request.sequences,
        kv_dtype=model_request.kv_dtype,
        weight_format=model_request.weight_format,
    )
    placement = place_model(
        model,
        catalog=resolved_catalog,
        requested_gpus=list(request.allowed_gpu_ids),
        gpu_count=request.gpu_count,
        gpu_memory_utilization=request.gpu_memory_utilization,
        weight_slack=request.weight_slack,
        framework_reserve_gib=request.framework_reserve_gib,
    )
    admitted_statuses = {"candidate"}
    if request.allow_indeterminate_fit:
        admitted_statuses.add("indeterminate")
    admitted_ids = {
        candidate["gpu_id"]
        for candidate in placement["placements"]
        if candidate["status"] in admitted_statuses
    }
    if requested_ids is not None:
        admitted_ids.intersection_update(requested_ids)
    summary = {
        "repository": placement["model"]["repository"],
        "requested_revision": placement["model"]["requested_revision"],
        "resolved_revision": placement["model"]["resolved_revision"],
        "checkpoint": placement["model"]["checkpoint"],
        "weight_format": placement["model"]["weight_format"],
        "weight_bytes": placement["model"]["weight_bytes"],
        "kv_cache": placement["model"]["kv_cache"],
        "placement_policy": placement["policy"],
        "placements": placement["placements"],
        "admitted_statuses": sorted(admitted_statuses),
        "admitted_gpu_ids": sorted(admitted_ids),
    }
    return WorkloadPlacement(
        admitted_gpu_ids=admitted_ids,
        model_summary=summary,
    )
