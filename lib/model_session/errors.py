"""Typed failures from the provider-neutral model-session foundation."""

from __future__ import annotations


class ModelSessionError(Exception):
    """An expected operator-facing profile or session-state failure."""

    def __init__(self, message: str, *, code: str = "model_session_error") -> None:
        super().__init__(message)
        self.code = code
