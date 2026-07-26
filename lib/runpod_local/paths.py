"""Filesystem locations shared by the Runpod tools."""

from __future__ import annotations

import os
import pathlib


def dotfiles_root() -> pathlib.Path:
    override = os.environ.get("RUNPOD_DOTFILES_ROOT")
    if override:
        return pathlib.Path(override).expanduser().resolve()
    return pathlib.Path(__file__).resolve().parents[2]


def state_root(override: str | pathlib.Path | None = None) -> pathlib.Path:
    if override is not None:
        return pathlib.Path(override).expanduser().resolve()
    configured = os.environ.get("RUNPOD_HOME")
    if configured:
        return pathlib.Path(configured).expanduser().resolve()
    return pathlib.Path.home() / ".local" / "runpod"


def ensure_private_directory(path: pathlib.Path) -> pathlib.Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path
