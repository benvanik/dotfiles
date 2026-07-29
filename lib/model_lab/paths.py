"""The non-overlapping authored, durable-state, and boot-local namespaces."""

from __future__ import annotations

import os
import pathlib
import stat

from .errors import ModelLabError


def authored_root(override: str | pathlib.Path | None = None) -> pathlib.Path:
    """Returns the portable, user-authored model-lab root."""
    if override is not None:
        return pathlib.Path(override).expanduser().absolute()
    configured = os.environ.get("MODEL_LAB_ROOT")
    if configured:
        return pathlib.Path(configured).expanduser().absolute()
    return pathlib.Path("/mnt/dev/model-lab")


def state_root(override: str | pathlib.Path | None = None) -> pathlib.Path:
    """Returns machine-local controller state, never portable instantiation."""
    if override is not None:
        return pathlib.Path(override).expanduser().absolute()
    configured = os.environ.get("MODEL_LAB_STATE_HOME")
    if configured:
        return pathlib.Path(configured).expanduser().absolute()
    xdg_state = os.environ.get("XDG_STATE_HOME")
    base = (
        pathlib.Path(xdg_state).expanduser()
        if xdg_state
        else pathlib.Path.home() / ".local" / "state"
    )
    return base / "model-lab"


def runtime_root(override: str | pathlib.Path | None = None) -> pathlib.Path:
    """Returns boot-local sockets and process receipts."""
    if override is not None:
        return pathlib.Path(override).expanduser().absolute()
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not xdg_runtime:
        raise ModelLabError(
            "XDG_RUNTIME_DIR is required for model-lab sockets",
            code="runtime_directory_unavailable",
        )
    return pathlib.Path(xdg_runtime).expanduser().absolute() / "model-lab"


def services_root(root: pathlib.Path | None = None) -> pathlib.Path:
    return (root if root is not None else authored_root()) / "services"


def profiles_root(root: pathlib.Path | None = None) -> pathlib.Path:
    return (root if root is not None else authored_root()) / "profiles"


def projects_root(root: pathlib.Path | None = None) -> pathlib.Path:
    return (root if root is not None else authored_root()) / "projects"


def sessions_root(root: pathlib.Path | None = None) -> pathlib.Path:
    return (root if root is not None else authored_root()) / "sessions"


def service_path(service_id: str, root: pathlib.Path | None = None) -> pathlib.Path:
    return services_root(root) / f"{service_id}.toml"


def profile_path(profile_id: str, root: pathlib.Path | None = None) -> pathlib.Path:
    return profiles_root(root) / profile_id / "profile.toml"


def endpoint_socket_path(
    service_id: str,
    root: pathlib.Path | None = None,
) -> pathlib.Path:
    return (
        (root if root is not None else runtime_root())
        / "services"
        / (f"{service_id}.sock")
    )


def endpoint_receipt_path(
    service_id: str,
    root: pathlib.Path | None = None,
) -> pathlib.Path:
    return (
        (root if root is not None else runtime_root())
        / "services"
        / (f"{service_id}.json")
    )


def validate_private_directory(path: pathlib.Path) -> None:
    """Rejects shared, foreign, symlinked, or non-directory state roots."""
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise ModelLabError(
            f"private directory does not exist: {path}",
            code="unsafe_private_directory",
        ) from error
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise ModelLabError(
            f"private path is not a real directory: {path}",
            code="unsafe_private_directory",
        )
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ModelLabError(
            f"private directory is not owned by the current user: {path}",
            code="unsafe_private_directory",
        )
    if metadata.st_mode & 0o077:
        raise ModelLabError(
            f"private directory permissions are broader than 0700: {path}",
            code="unsafe_private_permissions",
        )


def ensure_private_directory(path: pathlib.Path) -> pathlib.Path:
    """Creates an exact private path while validating each created component."""
    missing: list[pathlib.Path] = []
    cursor = path
    while True:
        try:
            cursor.lstat()
            break
        except FileNotFoundError:
            missing.append(cursor)
            parent = cursor.parent
            if parent == cursor:
                raise ModelLabError(
                    f"cannot find an existing parent for {path}",
                    code="unsafe_private_directory",
                )
            cursor = parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        validate_private_directory(directory)
    validate_private_directory(path)
    return path
