"""Public bounded-checkpoint API for isolated model-session state.

The codec owns no paths. Callers retain and pass directory descriptors for the
two logical roots (``work`` and ``sessions``) plus a regular pack descriptor.
All tree operations are descriptor-relative and never follow symlinks.
"""

from __future__ import annotations

from .checkpoint_format import (
    CHECKPOINT_SCHEMA,
    DEFAULT_CHECKPOINT_LIMITS,
    CheckpointLimits,
    CheckpointSummary,
    maximum_encoded_bytes,
    validate_pack as _validate_pack,
)
from .checkpoint_tree import (
    hydrate_tree_checkpoint as _hydrate_tree_checkpoint,
    write_tree_checkpoint as _write_tree_checkpoint,
)


__all__ = [
    "CHECKPOINT_SCHEMA",
    "DEFAULT_CHECKPOINT_LIMITS",
    "CheckpointLimits",
    "CheckpointSummary",
    "hydrate_checkpoint",
    "maximum_encoded_bytes",
    "validate_checkpoint",
    "write_checkpoint",
]


def write_checkpoint(
    work_descriptor: int,
    sessions_descriptor: int,
    output_descriptor: int,
    *,
    limits: CheckpointLimits = DEFAULT_CHECKPOINT_LIMITS,
) -> CheckpointSummary:
    """Write one deterministic uncompressed checkpoint to an empty file."""

    return _write_tree_checkpoint(
        work_descriptor,
        sessions_descriptor,
        output_descriptor,
        limits=limits,
    )


def validate_checkpoint(
    input_descriptor: int,
    *,
    limits: CheckpointLimits = DEFAULT_CHECKPOINT_LIMITS,
) -> CheckpointSummary:
    """Validate the complete pack without mutating either filesystem root."""

    return _validate_pack(input_descriptor, limits=limits).summary


def hydrate_checkpoint(
    input_descriptor: int,
    work_descriptor: int,
    sessions_descriptor: int,
    *,
    limits: CheckpointLimits = DEFAULT_CHECKPOINT_LIMITS,
) -> CheckpointSummary:
    """Validate completely, then hydrate two empty retained directory roots."""

    return _hydrate_tree_checkpoint(
        input_descriptor,
        work_descriptor,
        sessions_descriptor,
        limits=limits,
    )
