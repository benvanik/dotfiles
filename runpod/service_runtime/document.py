"""Strict parser for one generated inference-service deployment manifest."""

from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import re
import stat
from dataclasses import dataclass
from typing import Any

from runpod_local.errors import RunpodLocalError
from runpod_local.service_huggingface_policy import (
    HuggingFaceSnapshotPolicyError,
    validate_huggingface_nonweight_assets,
)

from . import (
    DEPLOYMENT_IDENTITY_SCHEMA,
    DEPLOYMENT_MANIFEST_SCHEMA,
    IMPLEMENTATION_ID,
)
from .layout import (
    REMOTE_IMPLEMENTATIONS_ROOT,
    REMOTE_RUNTIME_CONTROL_ROOT,
    canonical_service_paths,
)
from .vllm import (
    DRIVER_ID,
    LOOPBACK_HOST,
    build_vllm_argv,
    compile_affecting_sha256,
)


MAX_DEPLOYMENT_MANIFEST_BYTES = 16 * 1024 * 1024
HUGGINGFACE_CLOSURE_SCHEMA = "runpod.huggingface-closure.v1"
SERVICE_PLAN_SCHEMA = "runpod.inference-service-plan.v1"
COMPILE_CACHE_SCHEMA = "runpod.vllm-compile-cache.v1"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SERVICE_ID = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_RUNTIME_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._+-][a-z0-9]+)*$")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"
)
_TOKEN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_IMAGE = re.compile(
    r"^[a-z0-9](?:[a-z0-9._:/-]*[a-z0-9])?"
    r"@sha256:[0-9a-f]{64}$"
)
_IMPLEMENTATION_ROOT = re.compile(r"^[0-9a-f]{64}$")


def _fail(message: str, *, code: str = "invalid_service_deployment_manifest") -> None:
    raise RunpodLocalError(message, code=code)


def _exact_fields(
    value: Any,
    *,
    label: str,
    fields: frozenset[str],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        _fail(f"{label} must contain exactly: {', '.join(sorted(fields))}")
    return value


def _text(value: Any, *, label: str, maximum_bytes: int = 4096) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail(f"{label} must be a nonempty string without surrounding whitespace")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        _fail(f"{label} must be valid UTF-8")
    if len(encoded) > maximum_bytes or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        _fail(f"{label} contains unsupported text")
    return value


def _sha256(value: Any, *, label: str) -> str:
    text = _text(value, label=label, maximum_bytes=64)
    if not _SHA256.fullmatch(text):
        _fail(f"{label} must be a lowercase SHA-256 identity")
    return text


def _integer(
    value: Any,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        _fail(f"{label} must be an integer from {minimum} through {maximum}")
    return value


def _boolean(value: Any, *, label: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{label} must be a boolean")
    return value


def _choice(
    value: Any,
    *,
    label: str,
    choices: frozenset[str],
) -> str:
    text = _text(value, label=label, maximum_bytes=128)
    if text not in choices:
        _fail(f"{label} must be one of: {', '.join(sorted(choices))}")
    return text


def _token_or_none(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    text = _text(value, label=label, maximum_bytes=64)
    if not _TOKEN.fullmatch(text):
        _fail(f"{label} is not a bounded runtime selector")
    return text


def _relative_path(
    value: Any,
    *,
    label: str,
    suffixes: tuple[str, ...] | None = None,
) -> str:
    text = _text(value, label=label, maximum_bytes=4096)
    path = pathlib.PurePosixPath(text)
    if (
        path.is_absolute()
        or str(path) != text
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in text
    ):
        _fail(f"{label} must be a normalized relative POSIX path")
    if suffixes is not None and not text.endswith(suffixes):
        _fail(f"{label} has an unsupported file type")
    return text


def _root_checkpoint_path(
    value: Any,
    *,
    label: str,
    suffixes: tuple[str, ...],
) -> str:
    path = _relative_path(value, label=label, suffixes=suffixes)
    if len(pathlib.PurePosixPath(path).parts) != 1:
        _fail(f"{label} must name a root-level checkpoint file")
    return path


def _canonical_bytes(value: Any, *, newline: bool) -> bytes:
    suffix = "\n" if newline else ""
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + suffix
    ).encode("ascii")


def _canonical_sha256(value: Any, *, newline: bool = False) -> str:
    return hashlib.sha256(_canonical_bytes(value, newline=newline)).hexdigest()


def _normalized_service(value: Any) -> dict[str, Any]:
    service = _exact_fields(
        value,
        label="definition.service",
        fields=frozenset(
            {
                "schema",
                "service_id",
                "driver",
                "runtime_id",
                "model",
                "endpoint",
                "compatibility",
                "vllm",
            }
        ),
    )
    if service["schema"] != SERVICE_PLAN_SCHEMA:
        _fail("definition.service schema is unsupported")
    service_id = _text(
        service["service_id"],
        label="definition.service.service_id",
        maximum_bytes=63,
    )
    if not _SERVICE_ID.fullmatch(service_id):
        _fail("definition.service.service_id is malformed")
    if service["driver"] != DRIVER_ID:
        _fail("definition.service driver is unsupported")
    runtime_id = _text(
        service["runtime_id"],
        label="definition.service.runtime_id",
        maximum_bytes=128,
    )
    if not _RUNTIME_ID.fullmatch(runtime_id):
        _fail("definition.service.runtime_id is malformed")

    model = _exact_fields(
        service["model"],
        label="definition.service.model",
        fields=frozenset({"source", "repository", "revision", "checkpoint"}),
    )
    if model["source"] != "huggingface":
        _fail("definition.service.model source is unsupported")
    repository = _text(
        model["repository"],
        label="definition.service.model.repository",
        maximum_bytes=193,
    )
    if not _REPOSITORY.fullmatch(repository):
        _fail("definition.service.model.repository is malformed")
    revision = _text(
        model["revision"],
        label="definition.service.model.revision",
        maximum_bytes=40,
    )
    if not _SHA1.fullmatch(revision):
        _fail("definition.service.model.revision is not an exact commit")
    checkpoint = model["checkpoint"]
    if checkpoint is not None:
        _root_checkpoint_path(
            checkpoint,
            label="definition.service.model.checkpoint",
            suffixes=(
                ".safetensors",
                ".safetensors.index.json",
                ".bin",
                ".bin.index.json",
            ),
        )

    endpoint = _exact_fields(
        service["endpoint"],
        label="definition.service.endpoint",
        fields=frozenset({"input_modalities", "reasoning"}),
    )
    modalities = endpoint["input_modalities"]
    if (
        not isinstance(modalities, list)
        or not modalities
        or any(item not in {"text", "image"} for item in modalities)
        or modalities != sorted(set(modalities))
        or "text" not in modalities
    ):
        _fail("definition.service.endpoint.input_modalities is invalid")
    _boolean(endpoint["reasoning"], label="definition.service.endpoint.reasoning")

    compatibility = _exact_fields(
        service["compatibility"],
        label="definition.service.compatibility",
        fields=frozenset({"minimum_compute_capability"}),
    )
    minimum_capability = compatibility["minimum_compute_capability"]
    if not isinstance(minimum_capability, list) or len(minimum_capability) != 2:
        _fail("minimum compute capability must be [MAJOR, MINOR]")
    _integer(
        minimum_capability[0],
        label="minimum compute capability major",
        minimum=1,
        maximum=99,
    )
    _integer(
        minimum_capability[1],
        label="minimum compute capability minor",
        minimum=0,
        maximum=9,
    )

    configuration = _exact_fields(
        service["vllm"],
        label="definition.service.vllm",
        fields=frozenset(
            {
                "auto_tool_choice",
                "chunked_prefill",
                "dtype",
                "generation_config",
                "gpu_memory_utilization",
                "kv_cache_dtype",
                "language_model_only",
                "load_format",
                "mamba_cache_mode",
                "max_model_len",
                "max_num_batched_tokens",
                "max_num_sequences",
                "model_implementation",
                "prefix_caching",
                "quantization",
                "reasoning_parser",
                "safetensors_load_strategy",
                "seed",
                "speculative_config",
                "tensor_parallel_size",
                "tool_call_parser",
            }
        ),
    )
    _choice(
        configuration["model_implementation"],
        label="vLLM model implementation",
        choices=frozenset({"auto", "transformers", "vllm"}),
    )
    _choice(
        configuration["dtype"],
        label="vLLM dtype",
        choices=frozenset({"auto", "bfloat16", "float16"}),
    )
    _token_or_none(configuration["quantization"], label="vLLM quantization")
    _integer(
        configuration["tensor_parallel_size"],
        label="vLLM tensor parallel size",
        minimum=1,
        maximum=16,
    )
    _integer(
        configuration["max_model_len"],
        label="vLLM maximum model length",
        minimum=1,
        maximum=2**24,
    )
    maximum_sequences = _integer(
        configuration["max_num_sequences"],
        label="vLLM maximum sequence count",
        minimum=1,
        maximum=4096,
    )
    maximum_tokens = _integer(
        configuration["max_num_batched_tokens"],
        label="vLLM maximum batched token count",
        minimum=1,
        maximum=2**30,
    )
    if maximum_tokens < maximum_sequences:
        _fail("vLLM maximum batched tokens is smaller than sequence count")
    _choice(
        configuration["kv_cache_dtype"],
        label="vLLM KV-cache dtype",
        choices=frozenset({"auto", "bfloat16", "float16", "fp8"}),
    )
    utilization = configuration["gpu_memory_utilization"]
    if (
        type(utilization) is not float
        or not math.isfinite(utilization)
        or not 0.0 < utilization <= 1.0
    ):
        _fail("vLLM GPU memory utilization is invalid")
    for name in (
        "chunked_prefill",
        "language_model_only",
        "prefix_caching",
        "auto_tool_choice",
    ):
        _boolean(configuration[name], label=f"vLLM {name}")
    _choice(
        configuration["load_format"],
        label="vLLM load format",
        choices=frozenset({"auto", "safetensors"}),
    )
    if (
        checkpoint is not None
        and checkpoint.endswith((".bin", ".bin.index.json"))
        and configuration["load_format"] != "auto"
    ):
        _fail("PyTorch checkpoint selectors require vLLM load_format=auto")
    _choice(
        configuration["safetensors_load_strategy"],
        label="vLLM safetensors load strategy",
        choices=frozenset({"eager", "lazy"}),
    )
    _choice(
        configuration["mamba_cache_mode"],
        label="vLLM mamba cache mode",
        choices=frozenset({"all", "none"}),
    )
    reasoning_parser = _token_or_none(
        configuration["reasoning_parser"],
        label="vLLM reasoning parser",
    )
    if endpoint["reasoning"] != (reasoning_parser is not None):
        _fail("endpoint reasoning and vLLM reasoning parser disagree")
    tool_parser = _token_or_none(
        configuration["tool_call_parser"],
        label="vLLM tool-call parser",
    )
    if configuration["auto_tool_choice"] != (tool_parser is not None):
        _fail("vLLM auto-tool choice and tool-call parser disagree")
    speculative = configuration["speculative_config"]
    if speculative is not None:
        speculative = _exact_fields(
            speculative,
            label="vLLM speculative config",
            fields=frozenset({"method", "num_speculative_tokens"}),
        )
        if speculative["method"] != "mtp":
            _fail("vLLM speculative method is unsupported")
        _integer(
            speculative["num_speculative_tokens"],
            label="vLLM speculative token count",
            minimum=1,
            maximum=8,
        )
    _choice(
        configuration["generation_config"],
        label="vLLM generation config",
        choices=frozenset({"auto", "vllm"}),
    )
    if type(configuration["seed"]) is not int or configuration["seed"] != 0:
        _fail("vLLM seed must be exactly zero")
    if configuration["language_model_only"] and modalities != ["text"]:
        _fail("language-model-only service advertises a non-text modality")
    return service


def _runtime_selection(value: Any, *, expected_runtime_id: str) -> dict[str, Any]:
    runtime = _exact_fields(
        value,
        label="runtime",
        fields=frozenset(
            {
                "schema_version",
                "runtime_id",
                "image",
                "manifest",
                "verifier",
                "launch_overlay",
                "container_disk_gb",
                "volume_in_gb",
                "volume_mount_path",
            }
        ),
    )
    if runtime["schema_version"] != "runpod.runtime-selection.v1":
        _fail("runtime selection schema is unsupported")
    runtime_id = _text(runtime["runtime_id"], label="runtime.runtime_id")
    if runtime_id != expected_runtime_id or not _RUNTIME_ID.fullmatch(runtime_id):
        _fail("runtime selection does not match the service")
    image = _text(runtime["image"], label="runtime.image", maximum_bytes=512)
    if not _IMAGE.fullmatch(image):
        _fail("runtime image is not immutable")
    manifest = _exact_fields(
        runtime["manifest"],
        label="runtime.manifest",
        fields=frozenset({"path", "remote_path", "sha256", "bytes"}),
    )
    _relative_path(manifest["path"], label="runtime.manifest.path")
    _sha256(manifest["sha256"], label="runtime.manifest.sha256")
    _integer(
        manifest["bytes"],
        label="runtime manifest bytes",
        minimum=1,
        maximum=1024 * 1024,
    )
    if manifest["remote_path"] != str(
        REMOTE_RUNTIME_CONTROL_ROOT / "runtime-manifest.json"
    ):
        _fail("runtime manifest remote path is unsupported")
    verifier = _exact_fields(
        runtime["verifier"],
        label="runtime.verifier",
        fields=frozenset({"path", "remote_path", "sha256", "bytes"}),
    )
    _relative_path(verifier["path"], label="runtime.verifier.path")
    _sha256(verifier["sha256"], label="runtime.verifier.sha256")
    _integer(
        verifier["bytes"],
        label="runtime verifier bytes",
        minimum=1,
        maximum=1024 * 1024,
    )
    if verifier["remote_path"] != str(
        REMOTE_RUNTIME_CONTROL_ROOT / "verify-runtime.py"
    ):
        _fail("runtime verifier remote path is unsupported")
    overlay = _exact_fields(
        runtime["launch_overlay"],
        label="runtime.launch_overlay",
        fields=frozenset(
            {
                "bootstrap_id",
                "bootstrap_path",
                "bootstrap_sha256",
                "bootstrap_bytes",
                "docker_entrypoint_summary",
                "docker_start_cmd_summary",
            }
        ),
    )
    _text(overlay["bootstrap_id"], label="runtime bootstrap ID")
    _relative_path(overlay["bootstrap_path"], label="runtime bootstrap path")
    _sha256(overlay["bootstrap_sha256"], label="runtime bootstrap identity")
    _integer(
        overlay["bootstrap_bytes"],
        label="runtime bootstrap bytes",
        minimum=1,
        maximum=1024 * 1024,
    )
    for name in ("docker_entrypoint_summary", "docker_start_cmd_summary"):
        summary = _exact_fields(
            overlay[name],
            label=f"runtime {name}",
            fields=frozenset(
                {
                    "valid_string_array",
                    "argument_count",
                    "utf8_bytes",
                    "sha256",
                }
            ),
        )
        if summary["valid_string_array"] is not True:
            _fail(f"runtime {name} is not a valid argument summary")
        _integer(
            summary["argument_count"],
            label=f"runtime {name} argument count",
            minimum=1,
            maximum=64,
        )
        _integer(
            summary["utf8_bytes"],
            label=f"runtime {name} byte count",
            minimum=1,
            maximum=256 * 1024,
        )
        _sha256(summary["sha256"], label=f"runtime {name} identity")
    _integer(
        runtime["container_disk_gb"],
        label="runtime container disk GiB",
        minimum=1,
        maximum=4096,
    )
    if (
        type(runtime["volume_in_gb"]) is not int
        or runtime["volume_in_gb"] != 0
        or runtime["volume_mount_path"] != "/workspace"
    ):
        _fail("runtime volume contract is unsupported")
    return runtime


def _huggingface_closure(
    value: Any,
    *,
    model: dict[str, Any],
    load_format: str,
) -> dict[str, Any]:
    closure = _exact_fields(
        value,
        label="huggingface_closure",
        fields=frozenset(
            {
                "schema_version",
                "source",
                "checkpoint",
                "files",
                "file_count",
                "total_bytes",
                "closure_sha256",
            }
        ),
    )
    if closure["schema_version"] != HUGGINGFACE_CLOSURE_SCHEMA:
        _fail("Hugging Face closure schema is unsupported")
    source = _exact_fields(
        closure["source"],
        label="huggingface_closure.source",
        fields=frozenset({"kind", "repository", "revision"}),
    )
    if source != {
        "kind": "huggingface",
        "repository": model["repository"],
        "revision": model["revision"],
    }:
        _fail("Hugging Face closure source does not match the service")
    checkpoint = _exact_fields(
        closure["checkpoint"],
        label="huggingface_closure.checkpoint",
        fields=frozenset({"requested_selector", "resolved_index", "weight_files"}),
    )
    if checkpoint["requested_selector"] != model["checkpoint"]:
        _fail("Hugging Face closure checkpoint selector does not match")
    if checkpoint["resolved_index"] is not None:
        _root_checkpoint_path(
            checkpoint["resolved_index"],
            label="Hugging Face resolved checkpoint index",
            suffixes=(".safetensors.index.json", ".bin.index.json"),
        )
    weight_files = checkpoint["weight_files"]
    if (
        not isinstance(weight_files, list)
        or not weight_files
        or weight_files != sorted(set(weight_files))
    ):
        _fail("Hugging Face closure weight file list is invalid")
    resolved_index = checkpoint["resolved_index"]
    requested_selector = checkpoint["requested_selector"]
    if resolved_index is None and len(weight_files) != 1:
        _fail("an unindexed Hugging Face closure must have one weight file")
    if requested_selector is not None:
        selected_checkpoint = (
            resolved_index if resolved_index is not None else weight_files[0]
        )
        if requested_selector != selected_checkpoint:
            _fail(
                "Hugging Face closure selector does not match the resolved checkpoint"
            )
    family_source = resolved_index or requested_selector
    if family_source is not None and family_source.endswith(
        (".bin", ".bin.index.json")
    ):
        weight_suffix = ".bin"
    elif family_source is not None:
        weight_suffix = ".safetensors"
    else:
        weight_suffixes = {
            ".bin" if path.endswith(".bin") else ".safetensors"
            for path in weight_files
            if path.endswith((".bin", ".safetensors"))
        }
        if len(weight_suffixes) != 1:
            _fail("Hugging Face closure mixes checkpoint weight families")
        weight_suffix = weight_suffixes.pop()
    if weight_suffix == ".bin" and load_format != "auto":
        _fail("PyTorch checkpoint closures require vLLM load_format=auto")
    for path in weight_files:
        _root_checkpoint_path(
            path,
            label="Hugging Face checkpoint weight",
            suffixes=(weight_suffix,),
        )
    files = closure["files"]
    if not isinstance(files, list) or not files:
        _fail("Hugging Face closure files must be a nonempty array")
    observed_paths: list[str] = []
    roles: dict[str, str] = {}
    total_bytes = 0
    for index, raw_record in enumerate(files):
        record = _exact_fields(
            raw_record,
            label=f"Hugging Face closure file {index}",
            fields=frozenset({"path", "bytes", "role", "identity"}),
        )
        path = _relative_path(
            record["path"],
            label=f"Hugging Face closure file {index} path",
        )
        observed_paths.append(path)
        byte_count = _integer(
            record["bytes"],
            label=f"Hugging Face closure file {index} bytes",
            minimum=0,
            maximum=2**63 - 1,
        )
        total_bytes += byte_count
        role = _choice(
            record["role"],
            label=f"Hugging Face closure file {index} role",
            choices=frozenset({"checkpoint-index", "checkpoint-weight", "snapshot"}),
        )
        if role == "checkpoint-index" and not path.endswith(
            (".safetensors.index.json", ".bin.index.json")
        ):
            _fail(f"Hugging Face closure file {index} index role is invalid")
        if role == "checkpoint-weight" and not path.endswith((".safetensors", ".bin")):
            _fail(f"Hugging Face closure file {index} weight role is invalid")
        if role == "snapshot" and path.endswith(
            (
                ".safetensors",
                ".bin",
                ".pt",
                ".pth",
                ".ckpt",
                ".gguf",
                ".h5",
                ".msgpack",
                ".safetensors.index.json",
                ".bin.index.json",
            )
        ):
            _fail(f"Hugging Face closure file {index} snapshot role is invalid")
        roles[path] = role
        identity = _exact_fields(
            record["identity"],
            label=f"Hugging Face closure file {index} identity",
            fields=frozenset({"algorithm", "digest"}),
        )
        algorithm = _choice(
            identity["algorithm"],
            label=f"Hugging Face closure file {index} identity algorithm",
            choices=frozenset({"git-blob-sha1", "sha256"}),
        )
        digest = _text(
            identity["digest"],
            label=f"Hugging Face closure file {index} identity digest",
            maximum_bytes=64,
        )
        expected_pattern = _SHA256 if algorithm == "sha256" else _SHA1
        if not expected_pattern.fullmatch(digest):
            _fail(f"Hugging Face closure file {index} identity is malformed")
    if observed_paths != sorted(set(observed_paths)):
        _fail("Hugging Face closure files are not uniquely sorted")
    if set(weight_files) != {
        path for path, role in roles.items() if role == "checkpoint-weight"
    }:
        _fail("Hugging Face closure checkpoint weights disagree with file roles")
    index_members = sorted(
        path for path, role in roles.items() if role == "checkpoint-index"
    )
    if index_members != ([] if resolved_index is None else [resolved_index]):
        _fail("Hugging Face closure resolved index role does not match")
    try:
        validate_huggingface_nonweight_assets(
            (path, files[index]["bytes"])
            for index, path in enumerate(observed_paths)
            if roles[path] != "checkpoint-weight"
        )
    except HuggingFaceSnapshotPolicyError as error:
        _fail(str(error))
    if closure["file_count"] != len(files) or closure["total_bytes"] != total_bytes:
        _fail("Hugging Face closure counts do not match its files")
    closure_sha256 = _sha256(
        closure["closure_sha256"],
        label="Hugging Face closure identity",
    )
    expected_sha256 = _canonical_sha256(
        {
            "schema_version": "runpod.huggingface-closure-identity.v1",
            "source": source,
            "checkpoint": checkpoint,
            "files": files,
        },
        newline=True,
    )
    if closure_sha256 != expected_sha256:
        _fail("Hugging Face closure identity does not match its records")
    return closure


def _duplicate_rejecting_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _fail(f"deployment manifest repeats JSON field {key!r}")
        value[key] = item
    return value


@dataclass(frozen=True)
class DeploymentManifest:
    """Validated immutable deployment input plus its exact byte identity."""

    value: dict[str, Any]
    payload: bytes
    manifest_sha256: str

    @property
    def service(self) -> dict[str, Any]:
        return self.value["definition"]["service"]

    @property
    def service_id(self) -> str:
        return self.service["service_id"]

    @property
    def service_plan_sha256(self) -> str:
        return self.value["definition"]["service_plan_sha256"]

    @property
    def deployment_id(self) -> str:
        return self.value["deployment"]["deployment_id"]

    @property
    def runtime(self) -> dict[str, Any]:
        return self.value["runtime"]

    @property
    def implementation_bundle_sha256(self) -> str:
        return self.value["implementation"]["bundle_sha256"]

    @property
    def closure(self) -> dict[str, Any]:
        return self.value["huggingface_closure"]

    @property
    def closure_sha256(self) -> str:
        return self.closure["closure_sha256"]

    @property
    def port(self) -> int:
        return self.value["deployment"]["launch"]["port"]

    @property
    def compile_affecting_launch_sha256(self) -> str:
        return self.value["deployment"]["launch"]["compile_affecting_sha256"]


def parse_deployment_manifest(payload: bytes) -> DeploymentManifest:
    """Parse and cross-bind one generated deployment document."""

    if (
        not isinstance(payload, bytes)
        or not payload
        or len(payload) > MAX_DEPLOYMENT_MANIFEST_BYTES
    ):
        _fail("deployment manifest is absent or exceeds its size bound")
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_duplicate_rejecting_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunpodLocalError(
            f"deployment manifest is not valid JSON: {error}",
            code="invalid_service_deployment_manifest",
        ) from error
    document = _exact_fields(
        value,
        label="deployment manifest",
        fields=frozenset(
            {
                "schema_version",
                "definition",
                "runtime",
                "huggingface_closure",
                "implementation",
                "deployment",
                "compile_cache",
            }
        ),
    )
    if document["schema_version"] != DEPLOYMENT_MANIFEST_SCHEMA:
        _fail("deployment manifest schema is unsupported")
    definition = _exact_fields(
        document["definition"],
        label="definition",
        fields=frozenset(
            {"source_sha256", "source_bytes", "service_plan_sha256", "service"}
        ),
    )
    _sha256(definition["source_sha256"], label="definition source identity")
    _integer(
        definition["source_bytes"],
        label="definition source bytes",
        minimum=1,
        maximum=256 * 1024,
    )
    service = _normalized_service(definition["service"])
    service_plan_sha256 = _sha256(
        definition["service_plan_sha256"],
        label="semantic service plan identity",
    )
    if service_plan_sha256 != _canonical_sha256(service, newline=True):
        _fail("semantic service plan identity does not match the normalized plan")
    runtime = _runtime_selection(
        document["runtime"],
        expected_runtime_id=service["runtime_id"],
    )
    closure = _huggingface_closure(
        document["huggingface_closure"],
        model=service["model"],
        load_format=service["vllm"]["load_format"],
    )
    deployment = _exact_fields(
        document["deployment"],
        label="deployment",
        fields=frozenset(
            {
                "deployment_id",
                "service_root",
                "manifest_path",
                "process",
                "model_snapshot",
                "launch",
            }
        ),
    )
    deployment_id = _sha256(
        deployment["deployment_id"],
        label="deployment identity",
    )
    paths = canonical_service_paths(
        service_id=service["service_id"],
        deployment_id=deployment_id,
        closure_sha256=closure["closure_sha256"],
    )

    implementation = _exact_fields(
        document["implementation"],
        label="implementation",
        fields=frozenset(
            {
                "implementation_id",
                "bundle_sha256",
                "remote_root",
                "entrypoint",
                "receipt",
            }
        ),
    )
    if implementation["implementation_id"] != IMPLEMENTATION_ID:
        _fail("deployment implementation ID is unsupported")
    bundle_sha256 = _sha256(
        implementation["bundle_sha256"],
        label="deployment implementation bundle",
    )
    if not _IMPLEMENTATION_ROOT.fullmatch(bundle_sha256):
        _fail("deployment implementation bundle identity is malformed")
    expected_implementation_root = REMOTE_IMPLEMENTATIONS_ROOT / bundle_sha256
    if implementation["remote_root"] != str(
        expected_implementation_root
    ) or implementation["entrypoint"] != str(
        expected_implementation_root / "bin" / "runpod-service-runtime"
    ):
        _fail("deployment implementation paths are not content-derived")
    receipt = _exact_fields(
        implementation["receipt"],
        label="implementation.receipt",
        fields=frozenset({"remote_path", "bytes", "sha256"}),
    )
    if receipt["remote_path"] != str(expected_implementation_root / "bundle.json"):
        _fail("deployment implementation receipt path is not content-derived")
    _integer(
        receipt["bytes"],
        label="deployment implementation receipt bytes",
        minimum=1,
        maximum=1024 * 1024,
    )
    _sha256(
        receipt["sha256"],
        label="deployment implementation receipt identity",
    )

    if deployment["service_root"] != str(paths.service_root) or deployment[
        "manifest_path"
    ] != str(paths.manifest):
        _fail("deployment service paths are not service-derived")
    process = _exact_fields(
        deployment["process"],
        label="deployment.process",
        fields=frozenset(
            {
                "state_path",
                "log_path",
                "lifecycle_lock_path",
                "serving_lock_path",
            }
        ),
    )
    expected_process = {
        "state_path": str(paths.process_state),
        "log_path": str(paths.service_log),
        "lifecycle_lock_path": str(paths.lifecycle_lock),
        "serving_lock_path": str(paths.serving_lock),
    }
    if process != expected_process:
        _fail("deployment process paths are not service-derived")
    snapshot = _exact_fields(
        deployment["model_snapshot"],
        label="deployment.model_snapshot",
        fields=frozenset({"root", "closure_sha256"}),
    )
    if snapshot != {
        "root": str(paths.snapshot_root),
        "closure_sha256": closure["closure_sha256"],
    }:
        _fail("deployment snapshot path is not closure-derived")
    launch = _exact_fields(
        deployment["launch"],
        label="deployment.launch",
        fields=frozenset(
            {
                "argv",
                "snapshot_argument_index",
                "compile_affecting_sha256",
                "host",
                "port",
            }
        ),
    )
    port = _integer(
        launch["port"],
        label="deployment loopback port",
        minimum=1,
        maximum=65535,
    )
    expected_launch_hash = compile_affecting_sha256(service)
    if (
        launch["host"] != LOOPBACK_HOST
        or launch["snapshot_argument_index"] != 2
        or launch["compile_affecting_sha256"] != expected_launch_hash
        or launch["argv"]
        != list(
            build_vllm_argv(
                service,
                snapshot_root=paths.snapshot_root,
                port=port,
            )
        )
    ):
        _fail("deployment launch is not the typed vLLM launch")

    compile_cache = _exact_fields(
        document["compile_cache"],
        label="compile_cache",
        fields=frozenset(
            {
                "status",
                "contract_schema_version",
                "inputs",
                "observed_gpu",
            }
        ),
    )
    inputs = _exact_fields(
        compile_cache["inputs"],
        label="compile_cache.inputs",
        fields=frozenset(
            {
                "driver",
                "runtime",
                "runtime_execution_environment",
                "implementation_bundle_sha256",
                "huggingface_closure_sha256",
                "compile_affecting_launch_sha256",
            }
        ),
    )
    if (
        compile_cache["status"]
        != "requires-runtime-execution-environment-and-observed-gpu"
        or compile_cache["contract_schema_version"] != COMPILE_CACHE_SCHEMA
        or compile_cache["observed_gpu"] is not None
        or inputs
        != {
            "driver": DRIVER_ID,
            "runtime": runtime,
            "runtime_execution_environment": None,
            "implementation_bundle_sha256": bundle_sha256,
            "huggingface_closure_sha256": closure["closure_sha256"],
            "compile_affecting_launch_sha256": expected_launch_hash,
        }
    ):
        _fail("compile-cache requirement is not bound to this deployment")
    manifest_core = {
        **document,
        "deployment": {
            key: item
            for key, item in deployment.items()
            if key not in {"deployment_id", "manifest_path"}
        },
    }
    expected_deployment_id = _canonical_sha256(
        {
            "schema_version": DEPLOYMENT_IDENTITY_SCHEMA,
            "manifest": manifest_core,
        },
        newline=True,
    )
    if deployment_id != expected_deployment_id:
        _fail("deployment identity does not match its exact pre-path inputs")
    return DeploymentManifest(
        value=document,
        payload=payload,
        manifest_sha256=hashlib.sha256(payload).hexdigest(),
    )


def load_deployment_manifest(path: pathlib.Path) -> DeploymentManifest:
    """Read one private manifest through one no-follow descriptor."""

    try:
        path_stat = path.lstat()
    except OSError as error:
        raise RunpodLocalError(
            f"cannot inspect deployment manifest: {path}",
            code="unsafe_service_deployment_manifest",
        ) from error
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or stat.S_ISLNK(path_stat.st_mode)
        or path_stat.st_uid != os.getuid()
        or path_stat.st_nlink != 1
        or stat.S_IMODE(path_stat.st_mode) != 0o600
        or not 1 <= path_stat.st_size <= MAX_DEPLOYMENT_MANIFEST_BYTES
    ):
        _fail(
            f"deployment manifest has an unsafe file identity: {path}",
            code="unsafe_service_deployment_manifest",
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RunpodLocalError(
            f"cannot safely open deployment manifest: {path}",
            code="unsafe_service_deployment_manifest",
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != path_stat.st_dev
            or opened.st_ino != path_stat.st_ino
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != path_stat.st_uid
            or opened.st_nlink != path_stat.st_nlink
            or stat.S_IMODE(opened.st_mode) != stat.S_IMODE(path_stat.st_mode)
            or opened.st_size != path_stat.st_size
        ):
            _fail(
                "deployment manifest changed while opening",
                code="unsafe_service_deployment_manifest",
            )
        chunks: list[bytes] = []
        remaining = MAX_DEPLOYMENT_MANIFEST_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        final = os.fstat(descriptor)
        if (
            len(payload) > MAX_DEPLOYMENT_MANIFEST_BYTES
            or final.st_size != opened.st_size
            or final.st_mtime_ns != opened.st_mtime_ns
            or final.st_ctime_ns != opened.st_ctime_ns
            or final.st_uid != opened.st_uid
            or final.st_nlink != opened.st_nlink
            or stat.S_IMODE(final.st_mode) != stat.S_IMODE(opened.st_mode)
        ):
            _fail(
                "deployment manifest changed while reading",
                code="unsafe_service_deployment_manifest",
            )
    finally:
        os.close(descriptor)
    return parse_deployment_manifest(payload)
