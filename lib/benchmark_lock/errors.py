"""Typed operator-facing failures for benchmark admission."""

from __future__ import annotations


class BenchmarkLockError(Exception):
    """A benchmark-lock failure with one stable machine-readable code."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "benchmark_lock_error",
    ) -> None:
        super().__init__(message)
        self.code = code
