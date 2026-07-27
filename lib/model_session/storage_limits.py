"""Shared kernel-representable limits for anonymous model-session storage."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .errors import ModelSessionError


STORAGE_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
MIN_STORAGE_POOL_INODES = 2
MAX_STORAGE_POOL_BYTES = 1 << 50
MAX_STORAGE_POOL_INODES = 1 << 40


def _validate_positive_integer(
    value: int,
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
        raise ModelSessionError(
            f"{label} must be an integer from {minimum} through {maximum}",
            code="invalid_storage_limit",
        )
    return value


@dataclass(frozen=True)
class StoragePoolLimits:
    """Exact tmpfs capacity and inode count for one mutable root."""

    bytes: int
    inodes: int

    def __post_init__(self) -> None:
        _validate_positive_integer(
            self.bytes,
            label="storage pool bytes",
            minimum=STORAGE_PAGE_SIZE,
            maximum=MAX_STORAGE_POOL_BYTES,
        )
        if self.bytes % STORAGE_PAGE_SIZE != 0:
            raise ModelSessionError(
                "storage pool bytes must be a multiple of "
                f"{STORAGE_PAGE_SIZE}",
                code="invalid_storage_limit",
            )
        _validate_positive_integer(
            self.inodes,
            label="storage pool inodes",
            minimum=MIN_STORAGE_POOL_INODES,
            maximum=MAX_STORAGE_POOL_INODES,
        )
