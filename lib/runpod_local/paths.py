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


def runpod_root(
    override: str | pathlib.Path | None = None,
) -> pathlib.Path:
    """Portable authored Runpod configuration and evidence."""

    if override is not None:
        return pathlib.Path(override).expanduser().absolute()
    configured = os.environ.get("RUNPOD_ROOT")
    if configured:
        return pathlib.Path(configured).expanduser().absolute()
    return pathlib.Path("/mnt/dev/runpod")


def runpod_config_file(
    override: str | pathlib.Path | None = None,
) -> pathlib.Path:
    return runpod_root(override) / "runpod.toml"


def profile_root(
    override: str | pathlib.Path | None = None,
) -> pathlib.Path:
    return runpod_root(override) / "profiles"


def volume_root(
    override: str | pathlib.Path | None = None,
) -> pathlib.Path:
    return runpod_root(override) / "volumes"


def state_root(override: str | pathlib.Path | None = None) -> pathlib.Path:
    """Machine-local receipts and locks."""

    if override is not None:
        return pathlib.Path(override).expanduser().absolute()
    configured = os.environ.get("RUNPOD_STATE_HOME")
    if configured:
        return pathlib.Path(configured).expanduser().absolute()
    xdg_state = os.environ.get("XDG_STATE_HOME")
    base = (
        pathlib.Path(xdg_state).expanduser()
        if xdg_state
        else pathlib.Path.home() / ".local" / "state"
    )
    return base / "runpod"


def runtime_root() -> pathlib.Path:
    """Boot-local sockets, endpoint receipts, and transient coordination."""

    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not xdg_runtime:
        raise RunpodLocalError(
            "XDG_RUNTIME_DIR is required for Runpod boot-local state",
            code="runtime_directory_unavailable",
        )
    return pathlib.Path(xdg_runtime).expanduser().absolute() / "runpod"


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
