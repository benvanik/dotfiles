"""Model-lab admission policy for files copied into a vLLM snapshot.

The selected checkpoint is handled separately.  Every other admitted file
must be a bounded member of a loader-facing metadata, tokenizer, processor, or
quantization asset class.  This is an allowlist: documentation, alternate
exports, datasets, archives, and media never enter the runnable closure merely
because they happen to share a Hugging Face repository with the checkpoint.
"""

from __future__ import annotations

import pathlib
import re
from collections.abc import Iterable


MAX_HUGGINGFACE_LOADER_ASSET_BYTES = 256 * 1024 * 1024
MAX_HUGGINGFACE_LOADER_ASSET_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_HUGGINGFACE_LOADER_ASSET_COUNT = 512

_CHECKPOINT_INDEX_SUFFIXES = (
    ".safetensors.index.json",
    ".bin.index.json",
)
_EXACT_LOADER_ASSET_NAMES = frozenset(
    {
        "added_tokens.json",
        "chat_template.json",
        "chat_template.jinja",
        "config.json",
        "config_sentence_transformers.json",
        "feature_extractor_config.json",
        "generation_config.json",
        "merges.txt",
        "modules.json",
        "pooling_config.json",
        "preprocessor_config.json",
        "processor_config.json",
        "sentence_bert_config.json",
        "sentencepiece.bpe.model",
        "special_tokens_map.json",
        "spiece.model",
        "tekken.json",
        "tokenizer.json",
        "tokenizer.model",
        "tokenizer.tiktoken",
        "tokenizer_config.json",
        "vocab.json",
        "vocab.txt",
    }
)
_PROCESSOR_CONFIG_NAME = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}_(?:pre)?processor_config\.json$"
)
_QUANTIZATION_CONFIG_NAME = re.compile(
    r"^(?:quant_config|quantization_config|quantize_config|"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}_(?:quant|quantization)_config)\.json$"
)
_CHAT_TEMPLATE_NAME = re.compile(
    r"^chat_template(?:\.[A-Za-z0-9][A-Za-z0-9_.-]{0,95})?\.jinja$"
)


class HuggingFaceSnapshotPolicyError(ValueError):
    """A generated closure violates the shared vLLM snapshot policy."""


def _normalized_path(path: str) -> pathlib.PurePosixPath | None:
    if not isinstance(path, str) or not path or "\\" in path:
        return None
    candidate = pathlib.PurePosixPath(path)
    if (
        candidate.is_absolute()
        or str(candidate) != path
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        return None
    return candidate


def is_huggingface_checkpoint_index(path: str) -> bool:
    """Return whether ``path`` names a supported checkpoint index."""

    return _normalized_path(path) is not None and path.endswith(
        _CHECKPOINT_INDEX_SUFFIXES
    )


def is_huggingface_loader_asset(path: str) -> bool:
    """Return whether ``path`` belongs to an admitted non-weight asset class."""

    normalized = _normalized_path(path)
    if normalized is None:
        return False
    name = normalized.name
    return (
        name in _EXACT_LOADER_ASSET_NAMES
        or _PROCESSOR_CONFIG_NAME.fullmatch(name) is not None
        or _QUANTIZATION_CONFIG_NAME.fullmatch(name) is not None
        or _CHAT_TEMPLATE_NAME.fullmatch(name) is not None
    )


def validate_huggingface_nonweight_assets(
    assets: Iterable[tuple[str, int]],
) -> int:
    """Validate and return aggregate bytes for closure members other than weights.

    A selected checkpoint index participates in the same byte and member-count
    budgets as loader assets.  Weight shards do not: their exact selection and
    sizes are governed by the checkpoint closure itself.
    """

    count = 0
    total_bytes = 0
    for path, byte_count in assets:
        if not (
            is_huggingface_checkpoint_index(path) or is_huggingface_loader_asset(path)
        ):
            raise HuggingFaceSnapshotPolicyError(
                f"file is not an admitted vLLM loader asset: {path}"
            )
        if (
            isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count < 0
        ):
            raise HuggingFaceSnapshotPolicyError(
                f"loader asset has an invalid byte count: {path}"
            )
        if byte_count > MAX_HUGGINGFACE_LOADER_ASSET_BYTES:
            raise HuggingFaceSnapshotPolicyError(
                "loader asset exceeds the "
                f"{MAX_HUGGINGFACE_LOADER_ASSET_BYTES}-byte member limit: {path}"
            )
        count += 1
        if count > MAX_HUGGINGFACE_LOADER_ASSET_COUNT:
            raise HuggingFaceSnapshotPolicyError(
                "loader assets exceed the "
                f"{MAX_HUGGINGFACE_LOADER_ASSET_COUNT}-member limit"
            )
        total_bytes += byte_count
        if total_bytes > MAX_HUGGINGFACE_LOADER_ASSET_TOTAL_BYTES:
            raise HuggingFaceSnapshotPolicyError(
                "loader assets exceed the "
                f"{MAX_HUGGINGFACE_LOADER_ASSET_TOTAL_BYTES}-byte aggregate limit"
            )
    return total_bytes
