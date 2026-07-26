"""Runpod stock selection and post-create allocation verification."""

from __future__ import annotations

import math
from typing import Any

from .api import AVAILABLE_STOCK_STATUSES, gpu_stock_is_available
from .errors import RunpodLocalError
from .profile import validate_profile


def _finite_positive(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _finite_nonnegative(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _availability_by_gpu(center: dict[str, Any]) -> dict[str, str]:
    availability = center.get("gpu_availability")
    if not isinstance(availability, list):
        raise RunpodLocalError(
            "normalized data center has no GPU availability list",
            code="invalid_provider_response",
        )
    result: dict[str, str] = {}
    for entry in availability:
        if not isinstance(entry, dict) or not isinstance(
            entry.get("gpu_id"), str
        ):
            raise RunpodLocalError(
                "normalized data center contains invalid GPU availability",
                code="invalid_provider_response",
            )
        gpu_id = entry["gpu_id"]
        if gpu_id in result:
            raise RunpodLocalError(
                f"Runpod data center repeats GPU type {gpu_id}",
                code="invalid_provider_response",
            )
        result[gpu_id] = entry.get("stock_status", "None")
    return result


def select_launch_placement(
    profile: dict[str, Any],
    stock: dict[str, Any],
    *,
    data_center_id: str | None,
    allowed_gpu_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Apply profile order, stock, datacenter, and total price constraints."""
    profile = validate_profile(profile)
    raw_gpus = stock.get("gpus")
    if not isinstance(raw_gpus, list):
        raise RunpodLocalError(
            "stock report has no GPU list",
            code="invalid_stock_report",
        )
    gpus: dict[str, dict[str, Any]] = {}
    for gpu in raw_gpus:
        if not isinstance(gpu, dict) or not isinstance(gpu.get("gpu_id"), str):
            raise RunpodLocalError(
                "stock report contains an invalid GPU",
                code="invalid_stock_report",
            )
        gpu_id = gpu["gpu_id"]
        if gpu_id in gpus:
            raise RunpodLocalError(
                f"stock report repeats GPU type {gpu_id}",
                code="invalid_stock_report",
            )
        gpus[gpu_id] = gpu

    center_stock: dict[str, str] | None = None
    if data_center_id is not None:
        centers = stock.get("data_centers")
        if not isinstance(centers, list):
            raise RunpodLocalError(
                "stock report has no per-datacenter results",
                code="invalid_stock_report",
            )
        matches = [
            center
            for center in centers
            if isinstance(center, dict)
            and center.get("data_center_id") == data_center_id
        ]
        if len(matches) != 1:
            raise RunpodLocalError(
                f"stock report has {len(matches)} entries for {data_center_id}",
                code="invalid_stock_report",
            )
        center_stock = _availability_by_gpu(matches[0])

    pod = profile["pod"]
    gpu_count = pod["gpu_count"]
    price_cap = profile["limits"]["max_hourly_usd"]
    evaluations = []
    selected = None
    for gpu_id in pod["gpu_type_ids"]:
        reasons = []
        gpu = gpus.get(gpu_id)
        if allowed_gpu_ids is not None and gpu_id not in allowed_gpu_ids:
            reasons.append("model placement policy did not admit this GPU")
        if gpu is None:
            reasons.append("GPU is absent from the live stock response")
            price = None
            total_price = None
            stock_status = "None"
        else:
            price = gpu.get("on_demand_price_per_gpu_hour")
            total_price = price * gpu_count if _finite_positive(price) else None
            stock_status = gpu.get("stock_status", "None")
            if gpu.get("secure_cloud") is not True:
                reasons.append("GPU is not declared available in Secure Cloud")
            if not gpu_stock_is_available(gpu, gpu_count=gpu_count):
                reasons.append("global stock is unavailable")
            if total_price is None:
                reasons.append("on-demand price is unavailable")
            elif total_price > price_cap:
                reasons.append(
                    f"quoted total ${total_price:.3f}/h exceeds "
                    f"${price_cap:.3f}/h cap"
                )
        data_center_status = None
        if center_stock is not None:
            data_center_status = center_stock.get(gpu_id, "None")
            if data_center_status not in AVAILABLE_STOCK_STATUSES:
                reasons.append(
                    f"GPU is unavailable in data center {data_center_id}"
                )
        eligible = not reasons
        evaluation = {
            "gpu_id": gpu_id,
            "eligible": eligible,
            "global_stock_status": stock_status,
            "data_center_stock_status": data_center_status,
            "price_per_gpu_hour": price,
            "total_price_per_hour": total_price,
            "reasons": reasons,
        }
        evaluations.append(evaluation)
        if selected is None and eligible:
            selected = evaluation
    return {
        "selected": selected,
        "evaluations": evaluations,
        "data_center_id": data_center_id,
        "gpu_count": gpu_count,
        "max_hourly_usd": price_cap,
        "data_center_stock_is_count_specific": False,
    }


def verify_allocated_pod(
    record: dict[str, Any], pod: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Separate contradictory allocation facts from not-yet-populated facts."""
    expected = record["expected"]
    payload = record["pod_payload"]
    violations: list[str] = []
    pending: list[str] = []

    def exact(field: str, wanted: Any, *, missing_is_pending: bool = True) -> None:
        actual = pod.get(field)
        if actual is None and missing_is_pending:
            pending.append(field)
        elif actual != wanted:
            violations.append(f"{field}: expected {wanted!r}, got {actual!r}")

    exact("id", record["pod_id"], missing_is_pending=False)
    exact("name", record["remote_name"], missing_is_pending=False)
    exact("gpu_id", expected["gpu_id"])
    exact("gpu_count", expected["gpu_count"])
    if expected["data_center_id"] is not None:
        exact("data_center_id", expected["data_center_id"])
    exact(
        "network_volume_id",
        expected["network_volume_id"],
        missing_is_pending=expected["network_volume_id"] is not None,
    )
    exact("secure_cloud", True)
    exact("interruptible", False)
    exact("locked", False)
    if "templateId" in payload:
        exact("template_id", payload["templateId"])
    else:
        exact("image", payload["imageName"])

    status = pod.get("desired_status")
    if status is None:
        pending.append("desired_status")
    elif status != "RUNNING":
        violations.append(
            f"desired_status: expected 'RUNNING', got {status!r}"
        )
    ports = pod.get("ports")
    if not ports:
        pending.append("ports")
    elif not isinstance(ports, list) or sorted(ports) != ["22/tcp"]:
        violations.append(f"ports: expected only ['22/tcp'], got {ports!r}")
    cost = pod.get("cost_per_hour")
    if not _finite_nonnegative(cost):
        pending.append("cost_per_hour")
    elif cost > expected["max_hourly_usd"]:
        violations.append(
            f"cost_per_hour: ${cost:.3f}/h exceeds "
            f"${expected['max_hourly_usd']:.3f}/h cap"
        )
    return violations, pending
