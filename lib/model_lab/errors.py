"""Typed, operator-facing model-lab failures."""

from __future__ import annotations


class ModelLabError(Exception):
    """A failure whose message and stable code may be shown to an operator."""

    def __init__(self, message: str, *, code: str = "model_lab_error") -> None:
        super().__init__(message)
        self.code = code


class HttpRequestError(ModelLabError):
    """A sanitized HTTP transport or Hugging Face service failure."""

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
