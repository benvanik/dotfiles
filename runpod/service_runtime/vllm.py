"""Fixed vLLM argv and environment rendering from a normalized service."""

from __future__ import annotations

import hashlib
import json
import pathlib
from typing import Any

from runpod_local.errors import RunpodLocalError


DRIVER_ID = "vllm-openai.v1"
VLLM_EXECUTABLE = "/usr/local/bin/vllm"
LOOPBACK_HOST = "127.0.0.1"
LOCAL_API_KEY = "model-session-local-no-secret"
FORBIDDEN_INHERITED_ENVIRONMENT = frozenset(
    {
        "CONDA_PREFIX",
        "HF_TOKEN",
        "HF_TOKEN_PATH",
        "HUGGINGFACE_HUB_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
        "PYTHONHOME",
        "PYTHONPATH",
        "VIRTUAL_ENV",
    }
)


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def compile_affecting_sha256(service: dict[str, Any]) -> str:
    return canonical_sha256(service["vllm"])


def build_vllm_argv(
    service: dict[str, Any],
    *,
    snapshot_root: pathlib.PurePosixPath,
    port: int,
) -> tuple[str, ...]:
    """Render the only executable launch shape accepted by this driver."""

    if service.get("driver") != DRIVER_ID:
        raise RunpodLocalError(
            f"unsupported service driver: {service.get('driver')!r}",
            code="unsupported_service_runtime_driver",
        )
    configuration = service["vllm"]
    arguments = [
        VLLM_EXECUTABLE,
        "serve",
        str(snapshot_root),
        "--served-model-name",
        service["service_id"],
        "--model-impl",
        configuration["model_implementation"],
        "--dtype",
        configuration["dtype"],
    ]
    if configuration["quantization"] is not None:
        arguments.extend(["--quantization", configuration["quantization"]])
    arguments.extend(
        [
            "--tensor-parallel-size",
            str(configuration["tensor_parallel_size"]),
            "--max-model-len",
            str(configuration["max_model_len"]),
            "--max-num-seqs",
            str(configuration["max_num_sequences"]),
            (
                "--enable-chunked-prefill"
                if configuration["chunked_prefill"]
                else "--no-enable-chunked-prefill"
            ),
            "--max-num-batched-tokens",
            str(configuration["max_num_batched_tokens"]),
            "--kv-cache-dtype",
            configuration["kv_cache_dtype"],
            "--gpu-memory-utilization",
            str(configuration["gpu_memory_utilization"]),
            "--load-format",
            configuration["load_format"],
            "--safetensors-load-strategy",
            configuration["safetensors_load_strategy"],
            (
                "--language-model-only"
                if configuration["language_model_only"]
                else "--no-language-model-only"
            ),
            "--mamba-cache-mode",
            configuration["mamba_cache_mode"],
            (
                "--enable-prefix-caching"
                if configuration["prefix_caching"]
                else "--no-enable-prefix-caching"
            ),
        ]
    )
    if configuration["reasoning_parser"] is not None:
        arguments.extend(["--reasoning-parser", configuration["reasoning_parser"]])
    if configuration["speculative_config"] is not None:
        arguments.extend(
            [
                "--speculative-config",
                json.dumps(
                    configuration["speculative_config"],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ]
        )
    if configuration["tool_call_parser"] is not None:
        arguments.extend(
            [
                "--enable-auto-tool-choice",
                "--tool-call-parser",
                configuration["tool_call_parser"],
            ]
        )
    arguments.extend(
        [
            "--generation-config",
            configuration["generation_config"],
            "--seed",
            str(configuration["seed"]),
            "--fail-on-environ-validation",
            "--api-key",
            LOCAL_API_KEY,
            "--no-enable-log-requests",
            "--no-enable-log-outputs",
            "--disable-uvicorn-access-log",
            "--host",
            LOOPBACK_HOST,
            "--port",
            str(port),
        ]
    )
    return tuple(arguments)


def build_vllm_environment(
    *,
    session_root: pathlib.PurePosixPath,
    compile_root: pathlib.PurePosixPath,
    service_id: str,
    process_nonce: str,
    manifest_sha256: str,
) -> dict[str, str]:
    """Return fixed additions to a sanitized inherited environment."""

    hf_root = session_root / "cache" / "hf-runtime"
    return {
        "CUDA_CACHE_DISABLE": "0",
        "CUDA_CACHE_MAXSIZE": "4294967296",
        "CUDA_CACHE_PATH": str(compile_root / "cuda"),
        "DO_NOT_TRACK": "1",
        "FLASHINFER_WORKSPACE_BASE": str(compile_root / "flashinfer"),
        "HF_ASSETS_CACHE": str(hf_root / "assets"),
        "HF_HOME": str(hf_root),
        "HF_HUB_CACHE": str(hf_root / "hub"),
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "HF_HUB_DISABLE_UPDATE_CHECK": "1",
        "HF_HUB_OFFLINE": "1",
        "HF_XET_CACHE": str(hf_root / "xet"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "RUNPOD_SERVICE_ID": service_id,
        "RUNPOD_SERVICE_MANIFEST_SHA256": manifest_sha256,
        "RUNPOD_SERVICE_PROCESS_NONCE": process_nonce,
        "TORCHINDUCTOR_CACHE_DIR": str(compile_root / "torchinductor"),
        "TORCH_HOME": str(compile_root / "torch"),
        "TRANSFORMERS_OFFLINE": "1",
        "TRITON_CACHE_DIR": str(compile_root / "triton"),
        "VLLM_CACHE_ROOT": str(compile_root / "vllm"),
        "VLLM_DISABLE_COMPILE_CACHE": "0",
        "VLLM_FORCE_AOT_LOAD": "1",
        "VLLM_NO_USAGE_STATS": "1",
        "VLLM_USE_AOT_COMPILE": "1",
        "XDG_CACHE_HOME": str(compile_root / "xdg"),
    }
