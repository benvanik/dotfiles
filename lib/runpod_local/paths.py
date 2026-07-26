"""Filesystem locations shared by the Runpod tools."""

from __future__ import annotations

import os
import pathlib
import stat

from .errors import RunpodLocalError


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


def config_root(override: str | pathlib.Path | None = None) -> pathlib.Path:
    if override is not None:
        return pathlib.Path(override).expanduser().absolute()
    configured = os.environ.get("RUNPOD_CONFIG_HOME")
    if configured:
        return pathlib.Path(configured).expanduser().absolute()
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    base = pathlib.Path(xdg_config).expanduser() if xdg_config else pathlib.Path.home() / ".config"
    return base / "runpod-local"


def credentials_file(override: str | pathlib.Path | None = None) -> pathlib.Path:
    if override is not None:
        return pathlib.Path(override).expanduser().absolute()
    configured = os.environ.get("RUNPOD_CREDENTIALS_FILE")
    if configured:
        return pathlib.Path(configured).expanduser().absolute()
    return config_root() / "api-key"


def _validate_private_directory(path: pathlib.Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise RunpodLocalError(
            f"private directory does not exist: {path}",
            code="unsafe_private_directory",
        )
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise RunpodLocalError(
            f"private state path is not a real directory: {path}",
            code="unsafe_private_directory",
        )
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise RunpodLocalError(
            f"private state directory is not owned by the current user: {path}",
            code="unsafe_private_directory",
        )
    if metadata.st_mode & 0o077:
        raise RunpodLocalError(
            f"private directory permissions are broader than 0700: {path}",
            code="unsafe_private_permissions",
        )


def ensure_private_directory(path: pathlib.Path) -> pathlib.Path:
    missing = []
    cursor = path
    while True:
        try:
            cursor.lstat()
            break
        except FileNotFoundError:
            missing.append(cursor)
            parent = cursor.parent
            if parent == cursor:
                raise RunpodLocalError(
                    f"cannot find an existing parent for private directory {path}",
                    code="unsafe_private_directory",
                )
            cursor = parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        _validate_private_directory(directory)
    _validate_private_directory(path)
    return path
