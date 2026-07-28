"""Strict declarative definitions for one pinned inference service.

The service definition is authored instantiation data.  It selects one exact
Hugging Face revision and one reviewed runtime driver, but contains no host,
credential, port, cache-path, project, or lifecycle state.  Model closure
identities are resolved later from the immutable revision and become generated
deployment state.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import re
import stat
from dataclasses import dataclass, field
from typing import Any

import tomllib

from .errors import RunpodLocalError

SERVICE_DEFINITION_SCHEMA = "runpod.inference-service.v1"
SERVICE_PLAN_SCHEMA = "runpod.inference-service-plan.v1"
VLLM_DRIVER = "vllm-openai.v1"
MAX_SERVICE_DEFINITION_BYTES = 256 * 1024

_TOP_LEVEL_FIELDS = frozenset(
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
)
_MODEL_REQUIRED_FIELDS = frozenset(
    {
        "source",
        "repository",
        "revision",
    }
)
_MODEL_OPTIONAL_FIELDS = frozenset({"checkpoint"})
_ENDPOINT_FIELDS = frozenset({"input_modalities", "reasoning"})
_COMPATIBILITY_FIELDS = frozenset({"minimum_compute_capability"})
_VLLM_FIELDS = frozenset(
    {
        "model_implementation",
        "dtype",
        "quantization",
        "tensor_parallel_size",
        "max_model_len",
        "max_num_sequences",
        "max_num_batched_tokens",
        "kv_cache_dtype",
        "gpu_memory_utilization",
        "chunked_prefill",
        "load_format",
        "safetensors_load_strategy",
        "language_model_only",
        "mamba_cache_mode",
        "prefix_caching",
        "reasoning_parser",
        "tool_call_parser",
        "speculative_method",
        "speculative_tokens",
        "generation_config",
    }
)

_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_RUNTIME_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._+-][a-z0-9]+)*$")
_REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"
)
_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_VLLM_TOKEN_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_COMPUTE_CAPABILITY_PATTERN = re.compile(r"^(?P<major>[0-9]{1,2})[.](?P<minor>[0-9])$")

_INPUT_MODALITIES = frozenset({"text", "image"})
_VLLM_DTYPES = frozenset({"auto", "bfloat16", "float16"})
_VLLM_MODEL_IMPLEMENTATIONS = frozenset({"auto", "vllm", "transformers"})
_VLLM_KV_CACHE_DTYPES = frozenset({"auto", "bfloat16", "float16", "fp8"})
_VLLM_LOAD_FORMATS = frozenset({"auto", "safetensors"})
_VLLM_SAFETENSORS_LOAD_STRATEGIES = frozenset({"lazy", "eager"})
_VLLM_MAMBA_CACHE_MODES = frozenset({"none", "all"})
_VLLM_SPECULATIVE_METHODS = frozenset({"none", "mtp"})
_VLLM_GENERATION_CONFIGS = frozenset({"auto", "vllm"})
_CHECKPOINT_SUFFIXES = (
    ".safetensors",
    ".safetensors.index.json",
    ".bin",
    ".bin.index.json",
)


def _fail(message: str, *, code: str = "invalid_service_definition") -> None:
    raise RunpodLocalError(message, code=code)


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("ascii")


def _require_table(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be a TOML table")
    return value


def _require_fields(
    value: dict[str, Any],
    *,
    label: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> None:
    unknown = sorted(set(value).difference(required, optional))
    if unknown:
        _fail(f"{label} has unknown fields: {', '.join(unknown)}")
    missing = sorted(required.difference(value))
    if missing:
        _fail(f"{label} is missing fields: {', '.join(missing)}")


def _require_string(
    value: Any,
    *,
    label: str,
    maximum_bytes: int = 1024,
) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{label} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        _fail(f"{label} must be valid UTF-8")
    if len(encoded) > maximum_bytes or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        _fail(f"{label} contains unsupported text")
    return value


def _require_boolean(value: Any, *, label: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{label} must be a boolean")
    return value


def _require_integer(
    value: Any,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        _fail(f"{label} must be an integer from {minimum} through {maximum}")
    return value


def _require_choice(
    value: Any,
    *,
    label: str,
    choices: frozenset[str],
) -> str:
    text = _require_string(value, label=label)
    if text not in choices:
        _fail(f"{label} must be one of: {', '.join(sorted(choices))}")
    return text


def _require_vllm_token(value: Any, *, label: str) -> str:
    text = _require_string(value, label=label, maximum_bytes=64)
    if not _VLLM_TOKEN_PATTERN.fullmatch(text):
        _fail(f"{label} is not a bounded vLLM selector")
    return text


def _require_checkpoint(value: Any) -> str:
    checkpoint = _require_string(
        value,
        label="model.checkpoint",
        maximum_bytes=4096,
    )
    if (
        checkpoint.startswith("/")
        or "\\" in checkpoint
        or any(component in {"", ".", ".."} for component in checkpoint.split("/"))
        or not checkpoint.endswith(_CHECKPOINT_SUFFIXES)
    ):
        _fail(
            "model.checkpoint must be a relative safetensors or PyTorch bin "
            "file or index"
        )
    return checkpoint


def _validate_model_load_format(
    model: HuggingFaceModelDefinition,
    vllm: VllmLaunchPlan,
) -> None:
    checkpoint = model.checkpoint
    if (
        checkpoint is not None
        and checkpoint.endswith((".bin", ".bin.index.json"))
        and vllm.load_format == "safetensors"
    ):
        _fail("a PyTorch bin checkpoint requires vllm.load_format = auto")


def _require_compute_capability(value: Any) -> tuple[int, int]:
    text = _require_string(
        value,
        label="compatibility.minimum_compute_capability",
        maximum_bytes=4,
    )
    match = _COMPUTE_CAPABILITY_PATTERN.fullmatch(text)
    if match is None:
        _fail("compatibility.minimum_compute_capability must have MAJOR.MINOR form")
    return (int(match.group("major")), int(match.group("minor")))


@dataclass(frozen=True)
class HuggingFaceModelDefinition:
    """One immutable Hugging Face source selection."""

    repository: str
    revision: str
    checkpoint: str | None

    def normalized(self) -> dict[str, Any]:
        return {
            "source": "huggingface",
            "repository": self.repository,
            "revision": self.revision,
            "checkpoint": self.checkpoint,
        }


@dataclass(frozen=True)
class EndpointDefinition:
    """Capabilities advertised to an admitted inference consumer."""

    input_modalities: tuple[str, ...]
    reasoning: bool

    def normalized(self) -> dict[str, Any]:
        return {
            "input_modalities": list(self.input_modalities),
            "reasoning": self.reasoning,
        }


@dataclass(frozen=True)
class CompatibilityDefinition:
    """Minimum runtime compatibility without selecting a concrete host."""

    minimum_compute_capability: tuple[int, int]

    def normalized(self) -> dict[str, Any]:
        return {"minimum_compute_capability": list(self.minimum_compute_capability)}


@dataclass(frozen=True)
class VllmLaunchPlan:
    """Typed vLLM choices used for both argv and plan identity."""

    model_implementation: str
    dtype: str
    quantization: str
    tensor_parallel_size: int
    max_model_len: int
    max_num_sequences: int
    max_num_batched_tokens: int
    kv_cache_dtype: str
    gpu_memory_utilization: float
    chunked_prefill: bool
    load_format: str
    safetensors_load_strategy: str
    language_model_only: bool
    mamba_cache_mode: str
    prefix_caching: bool
    reasoning_parser: str
    tool_call_parser: str
    speculative_method: str
    speculative_tokens: int
    generation_config: str

    def normalized(self) -> dict[str, Any]:
        speculative_configuration = None
        if self.speculative_method != "none":
            speculative_configuration = {
                "method": self.speculative_method,
                "num_speculative_tokens": self.speculative_tokens,
            }
        return {
            "model_implementation": self.model_implementation,
            "dtype": self.dtype,
            "quantization": (
                None if self.quantization == "none" else self.quantization
            ),
            "tensor_parallel_size": self.tensor_parallel_size,
            "max_model_len": self.max_model_len,
            "max_num_sequences": self.max_num_sequences,
            "max_num_batched_tokens": self.max_num_batched_tokens,
            "kv_cache_dtype": self.kv_cache_dtype,
            "gpu_memory_utilization": self.gpu_memory_utilization,
            "chunked_prefill": self.chunked_prefill,
            "load_format": self.load_format,
            "safetensors_load_strategy": self.safetensors_load_strategy,
            "language_model_only": self.language_model_only,
            "mamba_cache_mode": self.mamba_cache_mode,
            "prefix_caching": self.prefix_caching,
            "reasoning_parser": (
                None if self.reasoning_parser == "none" else self.reasoning_parser
            ),
            "speculative_config": speculative_configuration,
            "auto_tool_choice": self.tool_call_parser != "none",
            "tool_call_parser": (
                None if self.tool_call_parser == "none" else self.tool_call_parser
            ),
            "generation_config": self.generation_config,
            "seed": 0,
        }


@dataclass(frozen=True)
class InferenceServiceDefinition:
    """One parsed service recipe plus its exact authored source identity."""

    service_id: str
    driver: str
    runtime_id: str
    model: HuggingFaceModelDefinition
    endpoint: EndpointDefinition
    compatibility: CompatibilityDefinition
    vllm: VllmLaunchPlan
    source_bytes: bytes = field(repr=False, compare=False)
    source_label: str = field(repr=False, compare=False)

    @property
    def source_sha256(self) -> str:
        return hashlib.sha256(self.source_bytes).hexdigest()

    @property
    def source_size(self) -> int:
        return len(self.source_bytes)

    def normalized_plan(self) -> dict[str, Any]:
        return {
            "schema": SERVICE_PLAN_SCHEMA,
            "service_id": self.service_id,
            "driver": self.driver,
            "runtime_id": self.runtime_id,
            "model": self.model.normalized(),
            "endpoint": self.endpoint.normalized(),
            "compatibility": self.compatibility.normalized(),
            "vllm": self.vllm.normalized(),
        }

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(_canonical_json_bytes(self.normalized_plan())).hexdigest()


def _parse_model(value: Any) -> HuggingFaceModelDefinition:
    table = _require_table(value, label="model")
    _require_fields(
        table,
        label="model",
        required=_MODEL_REQUIRED_FIELDS,
        optional=_MODEL_OPTIONAL_FIELDS,
    )
    source = _require_string(table["source"], label="model.source")
    if source != "huggingface":
        _fail("model.source must be exactly huggingface")
    repository = _require_string(
        table["repository"],
        label="model.repository",
        maximum_bytes=193,
    )
    if not _REPOSITORY_PATTERN.fullmatch(repository):
        _fail("model.repository is not an exact namespace/name identifier")
    revision = _require_string(
        table["revision"],
        label="model.revision",
        maximum_bytes=40,
    )
    if not _REVISION_PATTERN.fullmatch(revision):
        _fail("model.revision must be an exact lowercase 40-hex commit")
    checkpoint_value = table.get("checkpoint")
    checkpoint = (
        None if checkpoint_value is None else _require_checkpoint(checkpoint_value)
    )
    return HuggingFaceModelDefinition(
        repository=repository,
        revision=revision,
        checkpoint=checkpoint,
    )


def _parse_endpoint(value: Any) -> EndpointDefinition:
    table = _require_table(value, label="endpoint")
    _require_fields(
        table,
        label="endpoint",
        required=_ENDPOINT_FIELDS,
    )
    raw_modalities = table["input_modalities"]
    if not isinstance(raw_modalities, list) or not raw_modalities:
        _fail("endpoint.input_modalities must be a non-empty array")
    modalities: list[str] = []
    for raw_modality in raw_modalities:
        modality = _require_string(
            raw_modality,
            label="endpoint.input_modalities entry",
            maximum_bytes=16,
        )
        if modality not in _INPUT_MODALITIES:
            _fail("endpoint.input_modalities entries must be text or image")
        if modality in modalities:
            _fail("endpoint.input_modalities contains a duplicate")
        modalities.append(modality)
    if "text" not in modalities:
        _fail("vLLM OpenAI services must advertise text input")
    return EndpointDefinition(
        input_modalities=tuple(sorted(modalities)),
        reasoning=_require_boolean(
            table["reasoning"],
            label="endpoint.reasoning",
        ),
    )


def _parse_compatibility(value: Any) -> CompatibilityDefinition:
    table = _require_table(value, label="compatibility")
    _require_fields(
        table,
        label="compatibility",
        required=_COMPATIBILITY_FIELDS,
    )
    return CompatibilityDefinition(
        minimum_compute_capability=_require_compute_capability(
            table["minimum_compute_capability"]
        )
    )


def _parse_vllm(
    value: Any,
    *,
    endpoint: EndpointDefinition,
) -> VllmLaunchPlan:
    table = _require_table(value, label="vllm")
    _require_fields(table, label="vllm", required=_VLLM_FIELDS)
    gpu_memory_utilization = table["gpu_memory_utilization"]
    if (
        isinstance(gpu_memory_utilization, bool)
        or not isinstance(gpu_memory_utilization, float)
        or not math.isfinite(gpu_memory_utilization)
        or gpu_memory_utilization <= 0.0
        or gpu_memory_utilization > 1.0
    ):
        _fail(
            "vllm.gpu_memory_utilization must be a finite TOML float "
            "greater than zero and at most one"
        )
    max_num_sequences = _require_integer(
        table["max_num_sequences"],
        label="vllm.max_num_sequences",
        minimum=1,
        maximum=4096,
    )
    max_num_batched_tokens = _require_integer(
        table["max_num_batched_tokens"],
        label="vllm.max_num_batched_tokens",
        minimum=1,
        maximum=2**30,
    )
    if max_num_batched_tokens < max_num_sequences:
        _fail(
            "vllm.max_num_batched_tokens cannot be smaller than vllm.max_num_sequences"
        )
    reasoning_parser = _require_vllm_token(
        table["reasoning_parser"],
        label="vllm.reasoning_parser",
    )
    if endpoint.reasoning != (reasoning_parser != "none"):
        _fail(
            "endpoint.reasoning must agree with whether "
            "vllm.reasoning_parser is enabled"
        )
    tool_call_parser = _require_vllm_token(
        table["tool_call_parser"],
        label="vllm.tool_call_parser",
    )
    speculative_method = _require_choice(
        table["speculative_method"],
        label="vllm.speculative_method",
        choices=_VLLM_SPECULATIVE_METHODS,
    )
    speculative_tokens = _require_integer(
        table["speculative_tokens"],
        label="vllm.speculative_tokens",
        minimum=0,
        maximum=8,
    )
    if (speculative_method == "none") != (speculative_tokens == 0):
        _fail(
            "vllm.speculative_tokens must be zero exactly when "
            "vllm.speculative_method is none"
        )
    language_model_only = _require_boolean(
        table["language_model_only"],
        label="vllm.language_model_only",
    )
    if language_model_only and endpoint.input_modalities != ("text",):
        _fail("vllm.language_model_only cannot advertise non-text input")
    return VllmLaunchPlan(
        model_implementation=_require_choice(
            table["model_implementation"],
            label="vllm.model_implementation",
            choices=_VLLM_MODEL_IMPLEMENTATIONS,
        ),
        dtype=_require_choice(
            table["dtype"],
            label="vllm.dtype",
            choices=_VLLM_DTYPES,
        ),
        quantization=_require_vllm_token(
            table["quantization"],
            label="vllm.quantization",
        ),
        tensor_parallel_size=_require_integer(
            table["tensor_parallel_size"],
            label="vllm.tensor_parallel_size",
            minimum=1,
            maximum=16,
        ),
        max_model_len=_require_integer(
            table["max_model_len"],
            label="vllm.max_model_len",
            minimum=1,
            maximum=2**24,
        ),
        max_num_sequences=max_num_sequences,
        max_num_batched_tokens=max_num_batched_tokens,
        kv_cache_dtype=_require_choice(
            table["kv_cache_dtype"],
            label="vllm.kv_cache_dtype",
            choices=_VLLM_KV_CACHE_DTYPES,
        ),
        gpu_memory_utilization=gpu_memory_utilization,
        chunked_prefill=_require_boolean(
            table["chunked_prefill"],
            label="vllm.chunked_prefill",
        ),
        load_format=_require_choice(
            table["load_format"],
            label="vllm.load_format",
            choices=_VLLM_LOAD_FORMATS,
        ),
        safetensors_load_strategy=_require_choice(
            table["safetensors_load_strategy"],
            label="vllm.safetensors_load_strategy",
            choices=_VLLM_SAFETENSORS_LOAD_STRATEGIES,
        ),
        language_model_only=language_model_only,
        mamba_cache_mode=_require_choice(
            table["mamba_cache_mode"],
            label="vllm.mamba_cache_mode",
            choices=_VLLM_MAMBA_CACHE_MODES,
        ),
        prefix_caching=_require_boolean(
            table["prefix_caching"],
            label="vllm.prefix_caching",
        ),
        reasoning_parser=reasoning_parser,
        tool_call_parser=tool_call_parser,
        speculative_method=speculative_method,
        speculative_tokens=speculative_tokens,
        generation_config=_require_choice(
            table["generation_config"],
            label="vllm.generation_config",
            choices=_VLLM_GENERATION_CONFIGS,
        ),
    )


def parse_inference_service_toml(
    payload: bytes,
    *,
    source: str = "<memory>",
) -> InferenceServiceDefinition:
    """Parse exact TOML bytes into one normalized inference service."""

    if (
        not isinstance(payload, bytes)
        or not payload
        or len(payload) > MAX_SERVICE_DEFINITION_BYTES
    ):
        _fail("inference service definition bytes are absent or exceed the size limit")
    try:
        document = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise RunpodLocalError(
            f"inference service definition {source} is not valid TOML: {error}",
            code="invalid_service_definition",
        ) from error
    _require_fields(
        document,
        label="inference service definition",
        required=_TOP_LEVEL_FIELDS,
    )
    schema = _require_string(document["schema"], label="schema")
    if schema != SERVICE_DEFINITION_SCHEMA:
        _fail(f"unsupported inference service schema: {schema}")
    service_id = _require_string(
        document["service_id"],
        label="service_id",
        maximum_bytes=63,
    )
    if not _IDENTIFIER_PATTERN.fullmatch(service_id):
        _fail("service_id is not a valid lowercase service identifier")
    driver = _require_string(document["driver"], label="driver")
    if driver != VLLM_DRIVER:
        _fail(f"unsupported inference service driver: {driver}")
    runtime_id = _require_string(
        document["runtime_id"],
        label="runtime_id",
        maximum_bytes=128,
    )
    if not _RUNTIME_IDENTIFIER_PATTERN.fullmatch(runtime_id):
        _fail("runtime_id is not a valid runtime catalog identifier")
    model = _parse_model(document["model"])
    endpoint = _parse_endpoint(document["endpoint"])
    compatibility = _parse_compatibility(document["compatibility"])
    vllm = _parse_vllm(document["vllm"], endpoint=endpoint)
    _validate_model_load_format(model, vllm)
    return InferenceServiceDefinition(
        service_id=service_id,
        driver=driver,
        runtime_id=runtime_id,
        model=model,
        endpoint=endpoint,
        compatibility=compatibility,
        vllm=vllm,
        source_bytes=payload,
        source_label=source,
    )


def load_inference_service(
    path: os.PathLike[str] | str,
) -> InferenceServiceDefinition:
    """Read and parse one owned regular config without reopening its path."""

    try:
        source_path = pathlib.Path(path)
    except TypeError as error:
        raise RunpodLocalError(
            "inference service definition path is invalid",
            code="unsafe_service_definition",
        ) from error
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source_path, flags)
    except OSError as error:
        raise RunpodLocalError(
            f"cannot safely open inference service definition {source_path}: {error}",
            code="unsafe_service_definition",
        ) from error
    try:
        opened_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or opened_stat.st_nlink != 1
            or opened_stat.st_size < 1
            or opened_stat.st_size > MAX_SERVICE_DEFINITION_BYTES
            or (hasattr(os, "getuid") and opened_stat.st_uid != os.getuid())
            or opened_stat.st_mode & 0o022
        ):
            _fail(
                f"inference service definition has an unsafe identity: {source_path}",
                code="unsafe_service_definition",
            )
        chunks: list[bytes] = []
        remaining = MAX_SERVICE_DEFINITION_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        final_stat = os.fstat(descriptor)
        if (
            len(payload) > MAX_SERVICE_DEFINITION_BYTES
            or final_stat.st_size != opened_stat.st_size
            or final_stat.st_mtime_ns != opened_stat.st_mtime_ns
            or final_stat.st_ctime_ns != opened_stat.st_ctime_ns
        ):
            _fail(
                f"inference service definition changed while reading: {source_path}",
                code="unsafe_service_definition",
            )
    finally:
        os.close(descriptor)
    return parse_inference_service_toml(
        payload,
        source=str(source_path),
    )
