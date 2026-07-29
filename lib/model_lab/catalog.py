"""Resolution of authored service IDs and model-session profile routes."""

from __future__ import annotations

import dataclasses
import pathlib
import re
from typing import Protocol, runtime_checkable

from .errors import ModelLabError
from .paths import profile_path, profiles_root, service_path, services_root
from .service_definition import ServiceDefinition, load_service

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


@runtime_checkable
class ProfileRoute(Protocol):
    profile_id: str
    project_id: str
    service_id: str
    required_input_modalities: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class ResolvedProfileRoute:
    profile_root: pathlib.Path
    profile_id: str
    project_id: str
    service_id: str
    required_input_modalities: tuple[str, ...]


def require_identifier(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ModelLabError(
            f"{label} is not a valid lowercase identifier",
            code="invalid_identifier",
        )
    return value


def load_service_id(
    service_id: str,
    *,
    root: pathlib.Path,
) -> ServiceDefinition:
    service_id = require_identifier(service_id, label="service ID")
    path = service_path(service_id, root)
    try:
        definition = load_service(path)
    except ModelLabError as error:
        if error.code == "unsafe_authored_document" and not path.exists():
            raise ModelLabError(
                f"service does not exist: {service_id}",
                code="service_not_found",
            ) from error
        raise
    if definition.service_id != service_id:
        raise ModelLabError(
            f"service file name and service_id disagree: {path}",
            code="service_identity_mismatch",
        )
    return definition


def list_service_ids(*, root: pathlib.Path) -> tuple[str, ...]:
    directory = services_root(root)
    try:
        entries = list(directory.iterdir())
    except FileNotFoundError:
        return ()
    services: list[str] = []
    for path in entries:
        if path.name.startswith(".") or path.suffix != ".toml":
            continue
        service_id = path.stem
        require_identifier(service_id, label="service file name")
        load_service_id(service_id, root=root)
        services.append(service_id)
    return tuple(sorted(services))


def load_profile_route(profile_id: str, *, root: pathlib.Path) -> ProfileRoute:
    """Delegates profile authority to model-session's v3 route loader."""
    profile_id = require_identifier(profile_id, label="profile ID")
    path = profile_path(profile_id, root)
    try:
        from model_session.profile import (
            load_profile,
            load_profile_route as route_loader,
        )
    except ImportError as error:
        raise ModelLabError(
            "model-session does not expose its v3 profile route loader",
            code="model_session_integration_unavailable",
        ) from error
    try:
        route = route_loader(path.parent)
    except Exception as error:
        if getattr(error, "code", None) is not None:
            raise ModelLabError(
                str(error),
                code=getattr(error, "code"),
            ) from error
        raise
    missing = [
        name
        for name in (
            "profile_id",
            "project_id",
            "service_id",
            "required_input_modalities",
        )
        if not hasattr(route, name)
    ]
    if missing:
        try:
            profile = load_profile(path.parent)
        except Exception as error:
            if getattr(error, "code", None) is not None:
                raise ModelLabError(
                    str(error),
                    code=getattr(error, "code"),
                ) from error
            raise
        contract = profile.contract
        if contract.service_id is None or contract.endpoint is None:
            raise ModelLabError(
                "model-session profile is not a service-referencing v3 profile",
                code="profile_service_required",
            )
        route = ResolvedProfileRoute(
            profile_root=contract.profile_root,
            profile_id=contract.profile_id,
            project_id=contract.project_id,
            service_id=contract.service_id,
            required_input_modalities=(contract.endpoint.required_input_modalities),
        )
    if route.profile_id != profile_id:
        raise ModelLabError(
            f"profile directory and profile_id disagree: {path.parent}",
            code="profile_identity_mismatch",
        )
    return route


def list_profile_ids(*, root: pathlib.Path) -> tuple[str, ...]:
    directory = profiles_root(root)
    try:
        entries = list(directory.iterdir())
    except FileNotFoundError:
        return ()
    profiles: list[str] = []
    for path in entries:
        if path.name.startswith(".") or not path.is_dir() or path.is_symlink():
            continue
        require_identifier(path.name, label="profile directory")
        load_profile_route(path.name, root=root)
        profiles.append(path.name)
    return tuple(sorted(profiles))
