"""Strict, model-lab-owned definition of one pinned inference service."""

from __future__ import annotations

import dataclasses
import hashlib
import math
import os
import pathlib
import re
import tomllib
from typing import Any

from .documents import canonical_sha256, read_owned_regular_file
from .errors import ModelLabError

SERVICE_DEFINITION_SCHEMA = "model-lab.service.v1"
SERVICE_PLAN_SCHEMA = "model-lab.service-plan.v1"
VLLM_DRIVER = "vllm-openai.v1"

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_RUNTIME_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[._+-][a-z0-9]+)*$")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"
)
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_TOKEN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_COMPUTE_CAPABILITY = re.compile(r"^(?P<major>[0-9]{1,2})[.](?P<minor>[0-9])$")

_TOP_FIELDS = frozenset(
    {
        "schema",
        "service_id",
        "driver",
        "runtime_id",
        "model",
        "endpoint",
        "compatibility",
        "resources",
        "vllm",
    }
)
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


def _fail(message: str) -> None:
    raise ModelLabError(message, code="invalid_service_definition")


def _table(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be a TOML table")
    return value


def _fields(
    value: dict[str, Any],
    label: str,
    required: set[str] | frozenset[str],
    optional: set[str] | frozenset[str] = frozenset(),
) -> None:
    unknown = sorted(set(value).difference(required, optional))
    missing = sorted(set(required).difference(value))
    if unknown:
        _fail(f"{label} has unknown fields: {', '.join(unknown)}")
    if missing:
        _fail(f"{label} is missing fields: {', '.join(missing)}")


def _string(value: Any, label: str, maximum_bytes: int = 1024) -> str:
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


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{label} must be a boolean")
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        _fail(f"{label} must be an integer from {minimum} through {maximum}")
    return value


def _choice(value: Any, label: str, choices: set[str] | frozenset[str]) -> str:
    result = _string(value, label)
    if result not in choices:
        _fail(f"{label} must be one of: {', '.join(sorted(choices))}")
    return result


def _token(value: Any, label: str) -> str:
    result = _string(value, label, 64)
    if not _TOKEN.fullmatch(result):
        _fail(f"{label} is not a bounded vLLM selector")
    return result


@dataclasses.dataclass(frozen=True)
class HuggingFaceModelDefinition:
    repository: str
    revision: str
    checkpoint: str | None
    weight_format: str

    def normalized(self) -> dict[str, Any]:
        return {
            "source": "huggingface",
            "repository": self.repository,
            "revision": self.revision,
            "checkpoint": self.checkpoint,
            "weight_format": self.weight_format,
        }


@dataclasses.dataclass(frozen=True)
class EndpointDefinition:
    input_modalities: tuple[str, ...]
    reasoning: bool
    max_output_tokens: int

    def normalized(self) -> dict[str, Any]:
        return {
            "input_modalities": list(self.input_modalities),
            "reasoning": self.reasoning,
            "max_output_tokens": self.max_output_tokens,
        }


@dataclasses.dataclass(frozen=True)
class CompatibilityDefinition:
    minimum_compute_capability: tuple[int, int]

    def normalized(self) -> dict[str, Any]:
        return {"minimum_compute_capability": list(self.minimum_compute_capability)}


@dataclasses.dataclass(frozen=True)
class ResourceDefinition:
    """Consumer-owned admission facts passed opaquely to RunPod claims."""

    gpu_count: int
    gpu_memory_gib: int
    cpu_count: int
    memory_gib: int
    ephemeral_disk_gib: int
    claim_mode: str

    def normalized(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class VllmLaunchPlan:
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
        speculative = None
        if self.speculative_method != "none":
            speculative = {
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
            "speculative_config": speculative,
            "auto_tool_choice": self.tool_call_parser != "none",
            "tool_call_parser": (
                None if self.tool_call_parser == "none" else self.tool_call_parser
            ),
            "generation_config": self.generation_config,
            "seed": 0,
        }


@dataclasses.dataclass(frozen=True)
class ServiceDefinition:
    service_id: str
    driver: str
    runtime_id: str
    model: HuggingFaceModelDefinition
    endpoint: EndpointDefinition
    compatibility: CompatibilityDefinition
    resources: ResourceDefinition
    vllm: VllmLaunchPlan
    source_bytes: bytes = dataclasses.field(repr=False, compare=False)
    source_label: str = dataclasses.field(repr=False, compare=False)

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
            "resources": self.resources.normalized(),
            "vllm": self.vllm.normalized(),
        }

    @property
    def workload_sha256(self) -> str:
        """Identity required by a resumed agent, independent of deployment."""
        from model_session.service_endpoint import service_workload_identity

        return service_workload_identity(self.service_workload())

    def service_workload(self):
        """Return model-session's sole canonical semantic workload."""
        from model_session.attachment import ServiceWorkload

        kv_dtype = {
            "bfloat16": "bf16",
            "float16": "fp16",
            "fp8": "fp8",
        }.get(self.vllm.kv_cache_dtype)
        if kv_dtype is None:
            _fail("vllm.kv_cache_dtype must be explicit for endpoint publication")
        return ServiceWorkload(
            repository=self.model.repository,
            revision=self.model.revision,
            provider="runpod-vllm",
            model_id=self.service_id,
            context_tokens=self.vllm.max_model_len,
            max_output_tokens=self.endpoint.max_output_tokens,
            weight_format=self.model.weight_format,
            kv_cache_dtype=kv_dtype,
            runtime_compatibility=self.runtime_id,
            reasoning=self.endpoint.reasoning,
        )

    @property
    def service_sha256(self) -> str:
        """Identity of all normalized service semantics."""
        return canonical_sha256(self.normalized_plan())

    @property
    def plan_sha256(self) -> str:
        """Identity consumed by generated deployment manifests."""

        return self.service_sha256


def _parse_model(value: Any) -> HuggingFaceModelDefinition:
    table = _table(value, "model")
    _fields(
        table,
        "model",
        {"source", "repository", "revision", "weight_format"},
        {"checkpoint"},
    )
    if _string(table["source"], "model.source") != "huggingface":
        _fail("model.source must be exactly huggingface")
    repository = _string(table["repository"], "model.repository", 193)
    if not _REPOSITORY.fullmatch(repository):
        _fail("model.repository is not an exact namespace/name identifier")
    revision = _string(table["revision"], "model.revision", 40)
    if not _REVISION.fullmatch(revision):
        _fail("model.revision must be an exact lowercase 40-hex commit")
    checkpoint_value = table.get("checkpoint")
    checkpoint = (
        None
        if checkpoint_value is None
        else _string(checkpoint_value, "model.checkpoint", 4096)
    )
    if checkpoint is not None and (
        "/" in checkpoint
        or "\\" in checkpoint
        or not checkpoint.endswith(
            (
                ".safetensors",
                ".safetensors.index.json",
                ".bin",
                ".bin.index.json",
            )
        )
    ):
        _fail("model.checkpoint must name a supported root-level checkpoint")
    weight_format = _choice(
        table["weight_format"],
        "model.weight_format",
        {"native", "bf16", "fp8", "int8", "q8"},
    )
    return HuggingFaceModelDefinition(
        repository,
        revision,
        checkpoint,
        weight_format,
    )


def _parse_endpoint(value: Any) -> EndpointDefinition:
    table = _table(value, "endpoint")
    _fields(
        table,
        "endpoint",
        {"input_modalities", "reasoning", "max_output_tokens"},
    )
    raw = table["input_modalities"]
    if not isinstance(raw, list) or not raw:
        _fail("endpoint.input_modalities must be a non-empty array")
    modalities: list[str] = []
    for item in raw:
        modality = _choice(
            item,
            "endpoint.input_modalities entry",
            {"text", "image"},
        )
        if modality in modalities:
            _fail("endpoint.input_modalities contains a duplicate")
        modalities.append(modality)
    if "text" not in modalities:
        _fail("OpenAI-compatible services must advertise text input")
    return EndpointDefinition(
        tuple(sorted(modalities)),
        _boolean(table["reasoning"], "endpoint.reasoning"),
        _integer(
            table["max_output_tokens"],
            "endpoint.max_output_tokens",
            1,
            2**24,
        ),
    )


def _parse_compatibility(value: Any) -> CompatibilityDefinition:
    table = _table(value, "compatibility")
    _fields(table, "compatibility", {"minimum_compute_capability"})
    text = _string(
        table["minimum_compute_capability"],
        "compatibility.minimum_compute_capability",
        4,
    )
    match = _COMPUTE_CAPABILITY.fullmatch(text)
    if match is None:
        _fail("compatibility.minimum_compute_capability must have MAJOR.MINOR form")
    return CompatibilityDefinition(
        (int(match.group("major")), int(match.group("minor")))
    )


def _parse_resources(value: Any) -> ResourceDefinition:
    table = _table(value, "resources")
    _fields(
        table,
        "resources",
        {
            "gpu_count",
            "gpu_memory_gib",
            "cpu_count",
            "memory_gib",
            "ephemeral_disk_gib",
            "claim_mode",
        },
    )
    return ResourceDefinition(
        gpu_count=_integer(table["gpu_count"], "resources.gpu_count", 1, 16),
        gpu_memory_gib=_integer(
            table["gpu_memory_gib"], "resources.gpu_memory_gib", 1, 4096
        ),
        cpu_count=_integer(table["cpu_count"], "resources.cpu_count", 1, 1024),
        memory_gib=_integer(table["memory_gib"], "resources.memory_gib", 1, 16384),
        ephemeral_disk_gib=_integer(
            table["ephemeral_disk_gib"],
            "resources.ephemeral_disk_gib",
            1,
            65536,
        ),
        claim_mode=_choice(
            table["claim_mode"],
            "resources.claim_mode",
            {"shared", "gpu-exclusive", "host-exclusive"},
        ),
    )


def _parse_vllm(value: Any, endpoint: EndpointDefinition) -> VllmLaunchPlan:
    table = _table(value, "vllm")
    _fields(table, "vllm", _VLLM_FIELDS)
    utilization = table["gpu_memory_utilization"]
    if (
        isinstance(utilization, bool)
        or not isinstance(utilization, float)
        or not math.isfinite(utilization)
        or not 0.0 < utilization <= 1.0
    ):
        _fail("vllm.gpu_memory_utilization must be a finite float in (0, 1]")
    sequences = _integer(table["max_num_sequences"], "vllm.max_num_sequences", 1, 4096)
    batched = _integer(
        table["max_num_batched_tokens"],
        "vllm.max_num_batched_tokens",
        1,
        2**30,
    )
    if batched < sequences:
        _fail(
            "vllm.max_num_batched_tokens cannot be smaller than vllm.max_num_sequences"
        )
    reasoning_parser = _token(table["reasoning_parser"], "vllm.reasoning_parser")
    if endpoint.reasoning != (reasoning_parser != "none"):
        _fail(
            "endpoint.reasoning must agree with whether "
            "vllm.reasoning_parser is enabled"
        )
    speculative_method = _choice(
        table["speculative_method"],
        "vllm.speculative_method",
        {"none", "mtp"},
    )
    speculative_tokens = _integer(
        table["speculative_tokens"], "vllm.speculative_tokens", 0, 8
    )
    if (speculative_method == "none") != (speculative_tokens == 0):
        _fail(
            "vllm.speculative_tokens must be zero exactly when "
            "vllm.speculative_method is none"
        )
    language_model_only = _boolean(
        table["language_model_only"], "vllm.language_model_only"
    )
    if language_model_only and endpoint.input_modalities != ("text",):
        _fail("vllm.language_model_only cannot advertise non-text input")
    return VllmLaunchPlan(
        model_implementation=_choice(
            table["model_implementation"],
            "vllm.model_implementation",
            {"auto", "vllm", "transformers"},
        ),
        dtype=_choice(table["dtype"], "vllm.dtype", {"auto", "bfloat16", "float16"}),
        quantization=_token(table["quantization"], "vllm.quantization"),
        tensor_parallel_size=_integer(
            table["tensor_parallel_size"],
            "vllm.tensor_parallel_size",
            1,
            16,
        ),
        max_model_len=_integer(table["max_model_len"], "vllm.max_model_len", 1, 2**24),
        max_num_sequences=sequences,
        max_num_batched_tokens=batched,
        kv_cache_dtype=_choice(
            table["kv_cache_dtype"],
            "vllm.kv_cache_dtype",
            {"auto", "bfloat16", "float16", "fp8"},
        ),
        gpu_memory_utilization=utilization,
        chunked_prefill=_boolean(table["chunked_prefill"], "vllm.chunked_prefill"),
        load_format=_choice(
            table["load_format"], "vllm.load_format", {"auto", "safetensors"}
        ),
        safetensors_load_strategy=_choice(
            table["safetensors_load_strategy"],
            "vllm.safetensors_load_strategy",
            {"lazy", "eager"},
        ),
        language_model_only=language_model_only,
        mamba_cache_mode=_choice(
            table["mamba_cache_mode"],
            "vllm.mamba_cache_mode",
            {"none", "all"},
        ),
        prefix_caching=_boolean(table["prefix_caching"], "vllm.prefix_caching"),
        reasoning_parser=reasoning_parser,
        tool_call_parser=_token(table["tool_call_parser"], "vllm.tool_call_parser"),
        speculative_method=speculative_method,
        speculative_tokens=speculative_tokens,
        generation_config=_choice(
            table["generation_config"],
            "vllm.generation_config",
            {"auto", "vllm"},
        ),
    )


def parse_service_toml(
    payload: bytes,
    *,
    source: str = "<memory>",
) -> ServiceDefinition:
    if not isinstance(payload, bytes) or not payload:
        _fail("service definition bytes are absent")
    try:
        document = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ModelLabError(
            f"service definition {source} is not valid TOML: {error}",
            code="invalid_service_definition",
        ) from error
    _fields(document, "service definition", _TOP_FIELDS)
    schema = _string(document["schema"], "schema")
    if schema != SERVICE_DEFINITION_SCHEMA:
        _fail(f"unsupported service schema: {schema}")
    service_id = _string(document["service_id"], "service_id", 63)
    if not _IDENTIFIER.fullmatch(service_id):
        _fail("service_id is not a valid lowercase identifier")
    driver = _string(document["driver"], "driver", 64)
    if driver != VLLM_DRIVER:
        _fail(f"unsupported service driver: {driver}")
    runtime_id = _string(document["runtime_id"], "runtime_id", 128)
    if not _RUNTIME_IDENTIFIER.fullmatch(runtime_id):
        _fail("runtime_id is not a valid runtime catalog identifier")
    model = _parse_model(document["model"])
    endpoint = _parse_endpoint(document["endpoint"])
    vllm = _parse_vllm(document["vllm"], endpoint)
    if endpoint.max_output_tokens > vllm.max_model_len:
        _fail("endpoint.max_output_tokens cannot exceed vllm.max_model_len")
    if (
        model.checkpoint is not None
        and model.checkpoint.endswith((".bin", ".bin.index.json"))
        and vllm.load_format == "safetensors"
    ):
        _fail("a PyTorch bin checkpoint requires vllm.load_format = auto")
    return ServiceDefinition(
        service_id=service_id,
        driver=driver,
        runtime_id=runtime_id,
        model=model,
        endpoint=endpoint,
        compatibility=_parse_compatibility(document["compatibility"]),
        resources=_parse_resources(document["resources"]),
        vllm=vllm,
        source_bytes=payload,
        source_label=source,
    )


def load_service(path: os.PathLike[str] | str) -> ServiceDefinition:
    payload = read_owned_regular_file(path, label="service definition")
    return parse_service_toml(payload, source=str(pathlib.Path(path)))
