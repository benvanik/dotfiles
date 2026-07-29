"""Exact Hugging Face checkpoint inspection and bounded runtime estimates."""

from __future__ import annotations

import datetime
import math
import os
import pathlib
import re
import urllib.parse
from typing import Any

from .cache import JsonCache
from .errors import HttpRequestError, ModelLabError
from .http import JsonHttpTransport


GIB = 1024**3
HUGGING_FACE_BASE = "https://huggingface.co"
MODEL_INFO_CACHE_SECONDS = 3600
REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$"
)
INDEX_CANDIDATES = (
    "model.safetensors.index.json",
    "pytorch_model.bin.index.json",
)
SINGLE_FILE_CANDIDATES = (
    "model.safetensors",
    "pytorch_model.bin",
)
DTYPE_BYTES = {
    "F64": 8.0,
    "F32": 4.0,
    "F16": 2.0,
    "BF16": 2.0,
    "I64": 8.0,
    "U64": 8.0,
    "I32": 4.0,
    "U32": 4.0,
    "I16": 2.0,
    "U16": 2.0,
    "I8": 1.0,
    "U8": 1.0,
    "F8_E4M3": 1.0,
    "F8_E5M2": 1.0,
    "F8_E4M3FN": 1.0,
    "F8_E5M2FNUZ": 1.0,
    "BOOL": 1.0,
    "F4": 0.5,
}
KV_DTYPE_BYTES = {
    "bf16": 2,
    "fp16": 2,
    "fp8": 1,
}
WEIGHT_FORMAT_BYTES = {
    "bf16": 2,
    "fp8": 1,
    "int8": 1,
    "q8": 1,
}


def utc_now() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def bytes_to_gib(value: int | None) -> float | None:
    if value is None:
        return None
    return round(value / GIB, 3)


def validate_repository_id(repository: str) -> str:
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise ModelLabError(
            "Hugging Face repository must be an exact namespace/name identifier",
            code="invalid_repository",
        )
    return repository


def validate_repository_path(path: str, *, label: str = "repository path") -> str:
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(part in ("", ".", "..") for part in path.split("/"))
        or any(ord(character) < 32 for character in path)
    ):
        raise ModelLabError(
            f"invalid {label}: {path!r}",
            code="invalid_repository_path",
        )
    return path


def _quoted_repository(repository: str) -> str:
    return urllib.parse.quote(repository, safe="/")


def _quoted_revision(revision: str) -> str:
    if not revision or any(ord(character) < 32 for character in revision):
        raise ModelLabError(
            "Hugging Face revision must be non-empty and contain no control characters",
            code="invalid_revision",
        )
    return urllib.parse.quote(revision, safe="")


def _quoted_file(path: str) -> str:
    validate_repository_path(path)
    return "/".join(urllib.parse.quote(part, safe="") for part in path.split("/"))


class HuggingFaceClient:
    """Resolves one revision and fetches only metadata, never checkpoint blobs."""

    def __init__(
        self,
        *,
        cache: JsonCache,
        transport: JsonHttpTransport | None = None,
        token: str | None = None,
        offline: bool = False,
        refresh: bool = False,
    ) -> None:
        if offline and refresh:
            raise ModelLabError(
                "--offline and --refresh are mutually exclusive",
                code="conflicting_cache_options",
            )
        self.cache = cache
        self.transport = transport or JsonHttpTransport()
        self.token = token if token is not None else os.environ.get("HF_TOKEN")
        self.offline = offline
        self.refresh = refresh

    def _headers(self) -> dict[str, str]:
        if not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}

    def _cached_json(
        self,
        url: str,
        *,
        maximum_age_seconds: float | None,
        optional: bool = False,
    ) -> Any | None:
        maximum_age = None if self.offline else maximum_age_seconds
        if not self.refresh:
            cached = self.cache.get(url, maximum_age_seconds=maximum_age)
            if cached is not None:
                return cached
        if self.offline:
            raise ModelLabError(
                f"offline metadata cache miss for {urllib.parse.urlsplit(url).path}",
                code="offline_cache_miss",
            )
        try:
            value = self.transport.request_json(
                "GET", url, headers=self._headers()
            )
        except HttpRequestError as error:
            if optional and error.status == 404:
                return None
            raise
        self.cache.put(url, value)
        return value

    def model_info(self, repository: str, revision: str) -> dict[str, Any]:
        repository = validate_repository_id(repository)
        url = (
            f"{HUGGING_FACE_BASE}/api/models/{_quoted_repository(repository)}"
            f"/revision/{_quoted_revision(revision)}?blobs=true"
        )
        value = self._cached_json(
            url, maximum_age_seconds=MODEL_INFO_CACHE_SECONDS
        )
        if not isinstance(value, dict):
            raise ModelLabError(
                "Hugging Face model metadata was not a JSON object",
                code="invalid_model_metadata",
            )
        return value

    def json_file(
        self,
        repository: str,
        resolved_revision: str,
        path: str,
        *,
        optional: bool = False,
    ) -> dict[str, Any] | None:
        url = (
            f"{HUGGING_FACE_BASE}/{_quoted_repository(repository)}/resolve/"
            f"{_quoted_revision(resolved_revision)}/{_quoted_file(path)}"
        )
        value = self._cached_json(
            url, maximum_age_seconds=None, optional=optional
        )
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ModelLabError(
                f"Hugging Face file {path} was not a JSON object",
                code="invalid_repository_json",
            )
        return value

    def file_size(
        self, repository: str, resolved_revision: str, path: str
    ) -> int:
        if self.offline:
            raise ModelLabError(
                f"offline metadata did not include the size of {path}",
                code="offline_size_miss",
            )
        url = (
            f"{HUGGING_FACE_BASE}/{_quoted_repository(repository)}/resolve/"
            f"{_quoted_revision(resolved_revision)}/{_quoted_file(path)}"
        )
        return self.transport.content_length(url, headers=self._headers())


def _sibling_map(model_info: dict[str, Any]) -> dict[str, dict[str, Any]]:
    siblings = model_info.get("siblings")
    if not isinstance(siblings, list):
        raise ModelLabError(
            "Hugging Face metadata has no sibling file list",
            code="missing_siblings",
        )
    result: dict[str, dict[str, Any]] = {}
    for sibling in siblings:
        if not isinstance(sibling, dict):
            raise ModelLabError(
                "Hugging Face sibling metadata contains a non-object entry",
                code="invalid_sibling",
            )
        path = sibling.get("rfilename")
        if not isinstance(path, str):
            raise ModelLabError(
                "Hugging Face sibling metadata contains a file without a name",
                code="invalid_sibling",
            )
        validate_repository_path(path, label="sibling path")
        if path in result:
            raise ModelLabError(
                f"Hugging Face metadata contains duplicate sibling {path}",
                code="duplicate_sibling",
            )
        result[path] = sibling
    return result


def _sibling_size(sibling: dict[str, Any]) -> int | None:
    size = sibling.get("size")
    lfs = sibling.get("lfs")
    lfs_size = lfs.get("size") if isinstance(lfs, dict) else None
    valid_size = size if isinstance(size, int) and size >= 0 else None
    valid_lfs_size = (
        lfs_size if isinstance(lfs_size, int) and lfs_size >= 0 else None
    )
    if (
        valid_size is not None
        and valid_lfs_size is not None
        and valid_size != valid_lfs_size
    ):
        path = sibling.get("rfilename", "<unknown>")
        raise ModelLabError(
            f"Hugging Face reports conflicting sizes for {path}: "
            f"{valid_size} versus {valid_lfs_size}",
            code="conflicting_sibling_size",
        )
    if valid_size is not None:
        return valid_size
    if valid_lfs_size is not None:
        return valid_lfs_size
    return None


def _select_checkpoint(
    siblings: dict[str, dict[str, Any]], requested_index: str | None
) -> tuple[str | None, list[str], str]:
    if requested_index:
        requested_index = validate_repository_path(
            requested_index, label="index path"
        )
        if requested_index not in siblings:
            raise ModelLabError(
                f"requested checkpoint index does not exist: {requested_index}",
                code="missing_checkpoint_index",
            )
        return requested_index, [], "indexed"

    root_indices = [path for path in INDEX_CANDIDATES if path in siblings]
    if len(root_indices) == 1:
        return root_indices[0], [], "indexed"
    if len(root_indices) > 1:
        raise ModelLabError(
            "repository has both safetensors and PyTorch root checkpoint indexes; "
            "select one with --index-file",
            code="ambiguous_checkpoint",
        )

    exact_single_files = [path for path in SINGLE_FILE_CANDIDATES if path in siblings]
    if len(exact_single_files) == 1:
        return None, exact_single_files, "single_file"
    if len(exact_single_files) > 1:
        raise ModelLabError(
            "repository has both root model.safetensors and pytorch_model.bin; "
            "select an explicit index-backed checkpoint",
            code="ambiguous_checkpoint",
        )

    root_weight_files = sorted(
        path
        for path in siblings
        if "/" not in path
        and (
            path.endswith(".safetensors")
            or path.endswith(".bin")
            or path.endswith(".pth")
        )
    )
    if len(root_weight_files) == 1:
        return None, root_weight_files, "single_file"
    if not root_weight_files:
        raise ModelLabError(
            "repository has no unambiguous root checkpoint; pass --index-file",
            code="checkpoint_not_found",
        )
    raise ModelLabError(
        "repository has multiple root weight files without a recognized index; "
        "an exact checkpoint cannot be inferred",
        code="ambiguous_checkpoint",
    )


def _index_shards(
    index: dict[str, Any],
    index_path: str,
    siblings: dict[str, dict[str, Any]],
) -> list[str]:
    weight_map = index.get("weight_map")
    if not isinstance(weight_map, dict) or not weight_map:
        raise ModelLabError(
            f"checkpoint index {index_path} has no non-empty weight_map",
            code="invalid_checkpoint_index",
        )
    index_directory = pathlib.PurePosixPath(index_path).parent
    shards: set[str] = set()
    for tensor_name, shard_name in weight_map.items():
        if not isinstance(tensor_name, str) or not isinstance(shard_name, str):
            raise ModelLabError(
                f"checkpoint index {index_path} has a non-string weight mapping",
                code="invalid_checkpoint_index",
            )
        validate_repository_path(shard_name, label="checkpoint shard path")
        candidates = {shard_name}
        if str(index_directory) != ".":
            candidates.add(str(index_directory / shard_name))
        available = sorted(candidate for candidate in candidates if candidate in siblings)
        if len(available) != 1:
            if not available:
                raise ModelLabError(
                    f"checkpoint index {index_path} references missing shard "
                    f"{shard_name}",
                    code="missing_checkpoint_shard",
                )
            raise ModelLabError(
                f"checkpoint index {index_path} maps {shard_name} ambiguously to "
                f"{', '.join(available)}",
                code="ambiguous_checkpoint_shard",
            )
        validate_repository_path(
            available[0], label="resolved checkpoint shard path"
        )
        shards.add(available[0])
    return sorted(shards)


def _tensor_summary(model_info: dict[str, Any]) -> tuple[int | None, dict[str, int]]:
    safetensors = model_info.get("safetensors")
    if not isinstance(safetensors, dict):
        return None, {}
    total = safetensors.get("total")
    parameter_count = total if isinstance(total, int) and total >= 0 else None
    parameters = safetensors.get("parameters")
    if not isinstance(parameters, dict):
        return parameter_count, {}
    dtypes: dict[str, int] = {}
    for dtype, count in parameters.items():
        if isinstance(dtype, str) and isinstance(count, int) and count >= 0:
            dtypes[dtype] = count
    return parameter_count, dict(sorted(dtypes.items()))


def _tensor_bytes_from_dtypes(dtypes: dict[str, int]) -> int | None:
    if not dtypes or any(dtype not in DTYPE_BYTES for dtype in dtypes):
        return None
    return math.ceil(
        sum(count * DTYPE_BYTES[dtype] for dtype, count in dtypes.items())
    )


def _first_integer(config: dict[str, Any], *names: str) -> int | None:
    for name in names:
        value = config.get(name)
        if isinstance(value, int) and value > 0:
            return value
    return None


def _declared_positive_integer(
    config: dict[str, Any], name: str
) -> tuple[int | None, str | None]:
    if name not in config or config[name] is None:
        return None, None
    value = config[name]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None, f"configuration field {name} is not a positive integer"
    return value, None


def _text_config(config: dict[str, Any]) -> tuple[dict[str, Any], str]:
    for key in ("text_config", "language_config", "llm_config"):
        nested = config.get(key)
        if isinstance(nested, dict):
            return nested, key
    return config, "root"


def _kv_cache_estimate(
    config: dict[str, Any],
    *,
    context_tokens: int,
    sequences: int,
    kv_dtype: str,
) -> dict[str, Any]:
    if context_tokens <= 0 or sequences <= 0:
        raise ModelLabError(
            "context tokens and sequence count must both be positive",
            code="invalid_runtime_shape",
        )
    if kv_dtype not in KV_DTYPE_BYTES:
        raise ModelLabError(
            f"unsupported KV cache dtype: {kv_dtype}",
            code="invalid_kv_dtype",
        )
    text_config, config_source = _text_config(config)
    layers = _first_integer(text_config, "num_hidden_layers", "n_layer")
    dtype_bytes = KV_DTYPE_BYTES[kv_dtype]
    common = {
        "dtype": kv_dtype,
        "dtype_bytes": dtype_bytes,
        "context_tokens": context_tokens,
        "sequences": sequences,
        "config_source": config_source,
    }
    if layers is None:
        return {
            **common,
            "available": False,
            "reason": "configuration has no positive hidden-layer count",
        }

    kv_lora_rank = _first_integer(text_config, "kv_lora_rank")
    rope_head_dim = _first_integer(text_config, "qk_rope_head_dim")
    if kv_lora_rank is not None or rope_head_dim is not None:
        if kv_lora_rank is None or rope_head_dim is None:
            return {
                **common,
                "available": False,
                "reason": "configuration has an incomplete MLA cache shape",
            }
        bytes_per_token = layers * (kv_lora_rank + rope_head_dim) * dtype_bytes
        total_bytes = bytes_per_token * context_tokens * sequences
        return {
            **common,
            "available": True,
            "method": "mla_latent_cache",
            "confidence": "architectural_estimate",
            "layer_count": layers,
            "kv_lora_rank": kv_lora_rank,
            "qk_rope_head_dim": rope_head_dim,
            "bytes_per_token_per_sequence": bytes_per_token,
            "bytes": total_bytes,
            "gib": bytes_to_gib(total_bytes),
        }

    attention_heads = _first_integer(
        text_config, "num_attention_heads", "n_head"
    )
    key_value_heads = _first_integer(
        text_config, "num_key_value_heads", "n_head_kv"
    )
    hidden_size = _first_integer(text_config, "hidden_size", "n_embd")
    head_dimension = _first_integer(text_config, "head_dim", "head_size")
    if attention_heads is None:
        return {
            **common,
            "available": False,
            "reason": "configuration has no positive attention-head count",
        }
    if key_value_heads is None:
        key_value_heads = attention_heads
    if head_dimension is None:
        if hidden_size is None or hidden_size % attention_heads != 0:
            return {
                **common,
                "available": False,
                "reason": "configuration does not define a derivable attention head size",
            }
        head_dimension = hidden_size // attention_heads

    layer_types = text_config.get("layer_types")
    sliding_window = _first_integer(text_config, "sliding_window")
    global_key_value_heads, invalid_reason = _declared_positive_integer(
        text_config, "num_global_key_value_heads"
    )
    if invalid_reason is not None:
        return {
            **common,
            "available": False,
            "reason": invalid_reason,
        }
    global_head_dimension, invalid_reason = _declared_positive_integer(
        text_config, "global_head_dim"
    )
    if invalid_reason is not None:
        return {
            **common,
            "available": False,
            "reason": invalid_reason,
        }
    has_global_geometry = (
        global_key_value_heads is not None or global_head_dimension is not None
    )
    if layer_types is None and has_global_geometry:
        return {
            **common,
            "available": False,
            "reason": (
                "configuration declares global attention cache geometry "
                "without layer_types"
            ),
        }

    method = "full_attention_upper_bound"
    confidence = "conservative_architectural_estimate"
    full_attention_layers = layers
    sliding_attention_layers = 0
    if layer_types is not None:
        if not isinstance(layer_types, list) or len(layer_types) != layers:
            return {
                **common,
                "available": False,
                "reason": "layer_types does not match the hidden-layer count",
            }
        full_attention_layers = 0
        for layer_type in layer_types:
            if layer_type in ("full_attention", "global_attention"):
                full_attention_layers += 1
            elif layer_type in ("sliding_attention", "local_attention"):
                if sliding_window is None:
                    return {
                        **common,
                        "available": False,
                        "reason": "a sliding-attention layer has no sliding_window",
                    }
                sliding_attention_layers += 1
            else:
                return {
                    **common,
                    "available": False,
                    "reason": f"unsupported cache-bearing layer type: {layer_type!r}",
                }
        method = "declared_attention_layer_types"
        confidence = "architectural_estimate"

    full_key_value_heads = global_key_value_heads or key_value_heads
    full_head_dimension = global_head_dimension or head_dimension
    # Shared K/V projection weights still produce separate runtime cache tensors.
    full_bytes_per_token_per_layer = (
        2 * full_key_value_heads * full_head_dimension * dtype_bytes
    )
    sliding_bytes_per_token_per_layer = (
        2 * key_value_heads * head_dimension * dtype_bytes
    )
    sliding_cache_tokens = (
        min(context_tokens, sliding_window)
        if sliding_window is not None
        else 0
    )
    full_bytes_per_sequence = (
        full_attention_layers
        * context_tokens
        * full_bytes_per_token_per_layer
    )
    sliding_bytes_per_sequence = (
        sliding_attention_layers
        * sliding_cache_tokens
        * sliding_bytes_per_token_per_layer
    )
    bytes_per_sequence = (
        full_bytes_per_sequence + sliding_bytes_per_sequence
    )
    bytes_per_token = (
        full_attention_layers * full_bytes_per_token_per_layer
        + sliding_attention_layers * sliding_bytes_per_token_per_layer
    )
    layer_geometries: dict[str, dict[str, int]] = {}
    if full_attention_layers:
        layer_geometries["full_attention"] = {
            "layer_count": full_attention_layers,
            "cache_tokens_per_layer": context_tokens,
            "key_value_heads": full_key_value_heads,
            "head_dimension": full_head_dimension,
            "bytes_per_token_per_layer": full_bytes_per_token_per_layer,
            "bytes_per_sequence": full_bytes_per_sequence,
        }
    if sliding_attention_layers:
        layer_geometries["sliding_attention"] = {
            "layer_count": sliding_attention_layers,
            "cache_tokens_per_layer": sliding_cache_tokens,
            "key_value_heads": key_value_heads,
            "head_dimension": head_dimension,
            "bytes_per_token_per_layer": (
                sliding_bytes_per_token_per_layer
            ),
            "bytes_per_sequence": sliding_bytes_per_sequence,
        }
    total_bytes = bytes_per_sequence * sequences
    return {
        **common,
        "available": True,
        "method": method,
        "confidence": confidence,
        "layer_count": layers,
        "attention_heads": attention_heads,
        "key_value_heads": key_value_heads,
        "head_dimension": head_dimension,
        "bytes_per_token_per_sequence_full_attention": bytes_per_token,
        "attention_layer_geometries": layer_geometries,
        "bytes": total_bytes,
        "gib": bytes_to_gib(total_bytes),
    }


def _architecture_summary(config: dict[str, Any]) -> dict[str, Any]:
    text_config, source = _text_config(config)
    architectures = config.get("architectures")
    if not isinstance(architectures, list):
        architectures = []
    expert_count = _first_integer(
        text_config,
        "num_experts",
        "n_routed_experts",
        "num_local_experts",
        "moe_num_experts",
    )
    experts_per_token = _first_integer(
        text_config,
        "num_experts_per_tok",
        "num_experts_per_token",
        "moe_top_k",
        "num_selected_experts",
    )
    maximum_context = _first_integer(
        text_config, "max_position_embeddings", "n_positions", "seq_length"
    )
    return {
        "model_type": text_config.get("model_type", config.get("model_type")),
        "architectures": [
            value for value in architectures if isinstance(value, str)
        ],
        "config_source": source,
        "declared_dtype": text_config.get(
            "dtype",
            text_config.get(
                "torch_dtype", config.get("dtype", config.get("torch_dtype"))
            ),
        ),
        "hidden_size": _first_integer(text_config, "hidden_size", "n_embd"),
        "layer_count": _first_integer(
            text_config, "num_hidden_layers", "n_layer"
        ),
        "attention_heads": _first_integer(
            text_config, "num_attention_heads", "n_head"
        ),
        "key_value_heads": _first_integer(
            text_config, "num_key_value_heads", "n_head_kv"
        ),
        "head_dimension": _first_integer(text_config, "head_dim", "head_size"),
        "maximum_context_tokens": maximum_context,
        "expert_count": expert_count,
        "experts_per_token": experts_per_token,
        "is_moe": expert_count is not None,
    }


class ModelInspector:
    def __init__(self, client: HuggingFaceClient) -> None:
        self.client = client

    def inspect(
        self,
        repository: str,
        *,
        revision: str = "main",
        index_file: str | None = None,
        context_tokens: int = 32768,
        sequences: int = 1,
        kv_dtype: str = "bf16",
        weight_format: str = "native",
    ) -> dict[str, Any]:
        repository = validate_repository_id(repository)
        model_info = self.client.model_info(repository, revision)
        resolved_revision = model_info.get("sha")
        if not isinstance(resolved_revision, str) or not resolved_revision:
            raise ModelLabError(
                "Hugging Face metadata did not resolve the requested revision to a commit",
                code="missing_resolved_revision",
            )
        siblings = _sibling_map(model_info)
        selected_index, checkpoint_files, selection_method = _select_checkpoint(
            siblings, index_file
        )

        index_metadata: dict[str, Any] = {}
        if selected_index is not None:
            index = self.client.json_file(
                repository, resolved_revision, selected_index
            )
            if index is None:
                raise ModelLabError(
                    f"checkpoint index disappeared: {selected_index}",
                    code="missing_checkpoint_index",
                )
            checkpoint_files = _index_shards(index, selected_index, siblings)
            raw_metadata = index.get("metadata")
            if isinstance(raw_metadata, dict):
                index_metadata = raw_metadata

        file_records = []
        download_bytes = 0
        for path in checkpoint_files:
            sibling = siblings.get(path)
            if sibling is None:
                raise ModelLabError(
                    f"checkpoint index references a missing sibling: {path}",
                    code="missing_checkpoint_shard",
                )
            size = _sibling_size(sibling)
            if size is None:
                size = self.client.file_size(repository, resolved_revision, path)
            download_bytes += size
            file_records.append({"path": path, "bytes": size})

        parameter_count, stored_dtypes = _tensor_summary(model_info)
        tensor_bytes = index_metadata.get("total_size")
        if not isinstance(tensor_bytes, int) or tensor_bytes < 0:
            tensor_bytes = _tensor_bytes_from_dtypes(stored_dtypes)
        if tensor_bytes is None:
            tensor_bytes = download_bytes
            tensor_bytes_source = "checkpoint_download_upper_bound"
        elif "total_size" in index_metadata:
            tensor_bytes_source = "checkpoint_index_metadata"
        else:
            tensor_bytes_source = "huggingface_tensor_dtype_counts"

        if weight_format == "native":
            runtime_weight_bytes = tensor_bytes
            runtime_weight_source = "exact_checkpoint_tensor_storage"
            runtime_weight_confidence = "exact_storage_not_measured_runtime"
        else:
            normalized_format = "int8" if weight_format == "q8" else weight_format
            bytes_per_parameter = WEIGHT_FORMAT_BYTES.get(normalized_format)
            if bytes_per_parameter is None:
                raise ModelLabError(
                    f"unsupported weight format: {weight_format}",
                    code="invalid_weight_format",
                )
            if parameter_count is None:
                raise ModelLabError(
                    f"cannot project {weight_format} storage without a parameter count",
                    code="missing_parameter_count",
                )
            runtime_weight_bytes = parameter_count * bytes_per_parameter
            runtime_weight_source = "hypothetical_uniform_parameter_storage"
            runtime_weight_confidence = "low_projection"

        config = self.client.json_file(
            repository, resolved_revision, "config.json", optional=True
        )
        if config is None:
            config = {}
        architecture = _architecture_summary(config)
        kv_cache = _kv_cache_estimate(
            config,
            context_tokens=context_tokens,
            sequences=sequences,
            kv_dtype=kv_dtype,
        )
        warnings: list[str] = []
        if tensor_bytes > download_bytes:
            warnings.append(
                "checkpoint index tensor total exceeds the selected files' exact "
                "download bytes; both facts are preserved and placement uses the "
                "larger declared tensor total"
            )
        maximum_context = architecture.get("maximum_context_tokens")
        if isinstance(maximum_context, int) and context_tokens > maximum_context:
            warnings.append(
                f"requested context {context_tokens} exceeds the declared "
                f"maximum {maximum_context}"
            )
        if weight_format != "native":
            warnings.append(
                f"{weight_format} weight storage is a uniform parameter-count "
                "projection, not a runnable checkpoint or measured vLLM allocation"
            )
        quantization_config = config.get("quantization_config")
        if quantization_config is not None and not isinstance(
            quantization_config, dict
        ):
            quantization_config = {"raw_value": quantization_config}

        checkpoint_format = (
            "safetensors"
            if all(path.endswith(".safetensors") for path in checkpoint_files)
            else "pytorch_or_other"
        )
        return {
            "schema_version": "model-lab.model-estimate.v1",
            "generated_at": utc_now(),
            "repository": {
                "id": repository,
                "requested_revision": revision,
                "resolved_revision": resolved_revision,
                "gated": bool(model_info.get("gated", False)),
                "private": bool(model_info.get("private", False)),
            },
            "checkpoint": {
                "selection_method": selection_method,
                "format": checkpoint_format,
                "index_file": selected_index,
                "file_count": len(file_records),
                "files": file_records,
                "download_bytes": download_bytes,
                "download_gib": bytes_to_gib(download_bytes),
                "tensor_bytes": tensor_bytes,
                "tensor_gib": bytes_to_gib(tensor_bytes),
                "tensor_bytes_source": tensor_bytes_source,
                "parameter_count": parameter_count,
                "stored_tensor_dtypes": stored_dtypes,
            },
            "architecture": architecture,
            "quantization_config": quantization_config,
            "runtime_estimate": {
                "weight_format": weight_format,
                "weight_bytes": runtime_weight_bytes,
                "weight_gib": bytes_to_gib(runtime_weight_bytes),
                "weight_source": runtime_weight_source,
                "weight_confidence": runtime_weight_confidence,
                "kv_cache": kv_cache,
            },
            "warnings": warnings,
        }
