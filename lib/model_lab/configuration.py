"""Authored model-lab policy, separate from RunPod host configuration."""

from __future__ import annotations

import dataclasses
import os
import pathlib
import re
import tomllib
from typing import Any

from .documents import canonical_sha256, read_owned_regular_file
from .errors import ModelLabError

LAB_SCHEMA = "model-lab.v1"
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


def _fail(message: str) -> None:
    raise ModelLabError(message, code="invalid_lab_configuration")


def _exact_fields(
    value: Any,
    *,
    label: str,
    required: set[str],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be a TOML table")
    unknown = sorted(set(value).difference(required))
    missing = sorted(required.difference(value))
    if unknown:
        _fail(f"{label} has unknown fields: {', '.join(unknown)}")
    if missing:
        _fail(f"{label} is missing fields: {', '.join(missing)}")
    return value


def _seconds(value: Any, *, label: str, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        _fail(f"{label} must be an integer from {minimum} through {maximum}")
    return value


@dataclasses.dataclass(frozen=True)
class LeasePolicy:
    hard_ttl_seconds: int
    service_idle_ttl_seconds: int
    renewal_ttl_seconds: int
    minimum_useful_seconds: int
    startup_timeout_seconds: int

    def normalized(self) -> dict[str, int]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class LabConfiguration:
    allowed_runpod_profiles: tuple[str, ...]
    lease: LeasePolicy
    source_bytes: bytes = dataclasses.field(repr=False, compare=False)
    source_label: str = dataclasses.field(repr=False, compare=False)

    def normalized(self) -> dict[str, Any]:
        return {
            "schema": LAB_SCHEMA,
            "allowed_runpod_profiles": list(self.allowed_runpod_profiles),
            "lease": self.lease.normalized(),
        }

    @property
    def configuration_sha256(self) -> str:
        return canonical_sha256(self.normalized())


def parse_lab_toml(
    payload: bytes,
    *,
    source: str = "<memory>",
) -> LabConfiguration:
    try:
        document = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ModelLabError(
            f"lab configuration {source} is not valid TOML: {error}",
            code="invalid_lab_configuration",
        ) from error
    table = _exact_fields(
        document,
        label="lab configuration",
        required={"schema", "allowed_runpod_profiles", "lease"},
    )
    if table["schema"] != LAB_SCHEMA:
        _fail(f"unsupported lab configuration schema: {table['schema']}")
    raw_profiles = table["allowed_runpod_profiles"]
    if not isinstance(raw_profiles, list) or not raw_profiles:
        _fail("allowed_runpod_profiles must be a non-empty array")
    profiles: list[str] = []
    for value in raw_profiles:
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            _fail("allowed_runpod_profiles contains an invalid identifier")
        if value in profiles:
            _fail("allowed_runpod_profiles contains a duplicate")
        profiles.append(value)
    lease = _exact_fields(
        table["lease"],
        label="lease",
        required={
            "hard_ttl_seconds",
            "service_idle_ttl_seconds",
            "renewal_ttl_seconds",
            "minimum_useful_seconds",
            "startup_timeout_seconds",
        },
    )
    parsed_lease = LeasePolicy(
        hard_ttl_seconds=_seconds(
            lease["hard_ttl_seconds"],
            label="lease.hard_ttl_seconds",
            minimum=300,
            maximum=86400,
        ),
        service_idle_ttl_seconds=_seconds(
            lease["service_idle_ttl_seconds"],
            label="lease.service_idle_ttl_seconds",
            minimum=60,
            maximum=86400,
        ),
        renewal_ttl_seconds=_seconds(
            lease["renewal_ttl_seconds"],
            label="lease.renewal_ttl_seconds",
            minimum=30,
            maximum=3600,
        ),
        minimum_useful_seconds=_seconds(
            lease["minimum_useful_seconds"],
            label="lease.minimum_useful_seconds",
            minimum=60,
            maximum=86400,
        ),
        startup_timeout_seconds=_seconds(
            lease["startup_timeout_seconds"],
            label="lease.startup_timeout_seconds",
            minimum=60,
            maximum=300,
        ),
    )
    if parsed_lease.renewal_ttl_seconds >= parsed_lease.hard_ttl_seconds:
        _fail("lease.renewal_ttl_seconds must be shorter than the hard TTL")
    if (
        parsed_lease.startup_timeout_seconds
        + parsed_lease.minimum_useful_seconds
        > parsed_lease.hard_ttl_seconds
    ):
        _fail(
            "lease startup timeout plus minimum useful lifetime must not "
            "exceed the hard TTL"
        )
    return LabConfiguration(
        allowed_runpod_profiles=tuple(profiles),
        lease=parsed_lease,
        source_bytes=payload,
        source_label=source,
    )


def load_lab_configuration(path: os.PathLike[str] | str) -> LabConfiguration:
    return parse_lab_toml(
        read_owned_regular_file(path, label="lab configuration"),
        source=str(pathlib.Path(path)),
    )
