"""Pure vLLM deployment planning for one validated inference service."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
from typing import Any, Protocol

from .errors import RunpodLocalError

DRIVER_ID = "vllm-openai.v1"
DEFAULT_REMOTE_PORT = 8000
LOCAL_API_KEY = "model-session-local-no-secret"
REMOTE_SESSION_ROOT = pathlib.PurePosixPath("/root/runpod-session")
REMOTE_SERVICES_ROOT = REMOTE_SESSION_ROOT / "services"
SHARED_SNAPSHOT_ROOT_TEMPLATE = (
    "/root/runpod-session/model-snapshots/{generated_huggingface_closure_sha256}"
)
PERSISTENT_COMPILE_ROOT_TEMPLATE = (
    "/workspace/.cache/compiled/vllm-openai/"
    "{runtime_manifest_sha256}/"
    "{generated_huggingface_closure_sha256}/"
    "{compile_affecting_launch_sha256}/"
    "{gpu_compute_capability}/"
    "{gpu_identity_sha256}/"
    "{driver_version}"
)
MODEL_SNAPSHOT_ARGUMENT = SHARED_SNAPSHOT_ROOT_TEMPLATE


class InferenceServiceDefinition(Protocol):
    """The schema-owned surface consumed by this runtime adapter."""

    @property
    def plan_sha256(self) -> str: ...

    def normalized_plan(self) -> dict[str, Any]: ...


def _remote_port(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
        raise RunpodLocalError(
            "service remote port must be an integer from 1 through 65535",
            code="invalid_service_remote_port",
        )
    return value


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def build_vllm_argv(
    definition: InferenceServiceDefinition,
    *,
    model_path: str,
    remote_port: int,
) -> tuple[str, ...]:
    """Render one shell-free argv from the schema-owned typed plan."""

    service = definition.normalized_plan()
    if (
        not isinstance(model_path, str)
        or not pathlib.PurePosixPath(model_path).is_absolute()
        or model_path != os.path.normpath(model_path)
        or "\x00" in model_path
    ):
        raise RunpodLocalError(
            "staged model path must be an absolute normalized path",
            code="invalid_service_model_path",
        )
    port = _remote_port(remote_port)
    configuration = service["vllm"]
    arguments = [
        "/usr/local/bin/vllm",
        "serve",
        model_path,
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
            "127.0.0.1",
            "--port",
            str(port),
        ]
    )
    return tuple(arguments)


def build_vllm_deployment_plan(
    definition: InferenceServiceDefinition,
    *,
    runtime: dict[str, Any],
    remote_port: int = DEFAULT_REMOTE_PORT,
) -> dict[str, Any]:
    """Resolve deployment-owned paths without staging or starting anything."""

    service = definition.normalized_plan()
    if service.get("driver") != DRIVER_ID:
        raise RunpodLocalError(
            f"unsupported inference-service driver: {service.get('driver')!r}",
            code="unsupported_service_driver",
        )
    service_id = service["service_id"]
    model = service["model"]
    port = _remote_port(remote_port)
    service_root = REMOTE_SERVICES_ROOT / service_id
    definition_path = service_root / "service.toml"
    snapshot_root = SHARED_SNAPSHOT_ROOT_TEMPLATE
    arguments = list(
        build_vllm_argv(
            definition,
            model_path=MODEL_SNAPSHOT_ARGUMENT,
            remote_port=port,
        )
    )
    if len(arguments) < 3 or arguments[2] != MODEL_SNAPSHOT_ARGUMENT:
        raise RunpodLocalError(
            "vLLM adapter did not preserve the typed snapshot argument",
            code="invalid_service_launch_plan",
        )
    compile_affecting_launch_sha256 = _canonical_sha256(service["vllm"])
    return {
        "schema_version": "runpod.vllm-openai-deployment-plan.v1",
        "driver": DRIVER_ID,
        "runtime": runtime,
        "remote_port": port,
        "service_root": str(service_root),
        "definition_path": str(definition_path),
        "process": {
            "state_path": str(service_root / "process.json"),
            "log_path": str(service_root / "service.log"),
            "lifecycle_lock_path": str(service_root / "lifecycle.lock"),
            "serving_lock_path": str(service_root / "serving.lock"),
        },
        "model_snapshot": {
            "source": model["source"],
            "repository": model["repository"],
            "revision": model["revision"],
            "checkpoint_selector": model["checkpoint"],
            "generated_closure_sha256": None,
            "shared_root_template": snapshot_root,
            "vllm_model_argument": MODEL_SNAPSHOT_ARGUMENT,
        },
        "launch": {
            "argv_template": arguments,
            "snapshot_argument_index": 2,
            "compile_affecting_sha256": compile_affecting_launch_sha256,
            "host": "127.0.0.1",
            "port": port,
            "api_key_source": "controller-owned-local-nonsecret",
        },
        "compile_cache_identity_inputs": {
            "semantic_service_plan_sha256": definition.plan_sha256,
            "generated_huggingface_closure_sha256": None,
            "exact_runtime": runtime,
            "compile_affecting_launch_sha256": (compile_affecting_launch_sha256),
            "observed_driver_version": None,
            "observed_compute_capability": None,
            "observed_gpu_identity_sha256": None,
            "persistent_root_template": PERSISTENT_COMPILE_ROOT_TEMPLATE,
        },
    }
