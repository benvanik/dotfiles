"""Stable JSON and concise human output."""

from __future__ import annotations

import json
import sys
from typing import Any


def print_json(value: Any) -> None:
    json.dump(value, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def format_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    gib = value / (1024**3)
    return f"{gib:.2f} GiB ({value:,} bytes)"


def print_model_human(report: dict[str, Any]) -> None:
    repository = report["repository"]
    checkpoint = report["checkpoint"]
    architecture = report["architecture"]
    runtime = report["runtime_estimate"]
    kv_cache = runtime["kv_cache"]
    print(
        f"{repository['id']} @ {repository['resolved_revision']}",
    )
    print(
        f"  checkpoint: {checkpoint['file_count']} files, "
        f"{format_bytes(checkpoint['download_bytes'])} download"
    )
    print(
        f"  tensors:    {format_bytes(checkpoint['tensor_bytes'])}, "
        f"{checkpoint['parameter_count'] or 'unknown'} parameters"
    )
    family = "MoE" if architecture["is_moe"] else "dense/unspecified"
    print(
        f"  model:      {architecture['model_type'] or 'unknown'} ({family}), "
        f"{architecture['layer_count'] or 'unknown'} layers"
    )
    print(
        f"  weights:    {format_bytes(runtime['weight_bytes'])} "
        f"({runtime['weight_format']}, {runtime['weight_source']})"
    )
    if kv_cache["available"]:
        print(
            f"  KV cache:   {format_bytes(kv_cache['bytes'])} for "
            f"{kv_cache['sequences']} × {kv_cache['context_tokens']:,} tokens "
            f"at {kv_cache['dtype']}"
        )
    else:
        print(f"  KV cache:   unknown ({kv_cache['reason']})")
    for warning in report["warnings"]:
        print(f"  warning:    {warning}")


def print_placement_human(report: dict[str, Any]) -> None:
    model = report["model"]
    print(
        f"{model['repository']} @ {model['resolved_revision']} "
        f"({model['weight_format']})"
    )
    print(
        "STATUS         GPU                              VRAM   REQUIRED  HEADROOM"
    )
    for placement in report["placements"]:
        print(
            f"{placement['status']:<14} "
            f"{placement['display_name'][:31]:<31} "
            f"{placement['provider_memory_gb']:>5g}G "
            f"{placement['required_gib_per_gpu']:>8.2f}G "
            f"{placement['headroom_gib_per_gpu']:>8.2f}G"
        )
        if placement["status"] != "candidate":
            print(f"  {'; '.join(placement['reasons'])}")
