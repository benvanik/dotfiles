"""Typed failures surfaced by the Runpod command suite."""

from __future__ import annotations


class RunpodLocalError(Exception):
    """An expected operator-facing failure."""

    def __init__(self, message: str, *, code: str = "runpod_error") -> None:
        super().__init__(message)
        self.code = code


class HttpRequestError(RunpodLocalError):
    """A sanitized HTTP transport or service failure."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str = "http_error",
        provider_error: str | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.status = status
        self.provider_error = provider_error
