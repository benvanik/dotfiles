"""Typed container execution environment shared by verification and launch."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
from dataclasses import dataclass
from typing import Any, Mapping

from runpod_local.errors import RunpodLocalError


RUNTIME_EXECUTION_ENVIRONMENT_SCHEMA = "runpod.runtime-execution-environment.v1"
_FIXED_ENVIRONMENT = {
    "DO_NOT_TRACK": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HF_HUB_DISABLE_UPDATE_CHECK": "1",
    "HOME": "/root",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "LOGNAME": "root",
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "PYTHONDONTWRITEBYTECODE": "1",
    "TZ": "UTC",
    "USER": "root",
}
_INHERITED_NAMES = (
    "CUDA_VISIBLE_DEVICES",
    "LD_LIBRARY_PATH",
    "NVIDIA_DRIVER_CAPABILITIES",
    "NVIDIA_VISIBLE_DEVICES",
)
_DOCUMENT_FIELDS = frozenset({"schema_version", "values", "sha256"})
_MAX_VALUE_BYTES = 16 * 1024


def _fail(message: str) -> None:
    raise RunpodLocalError(
        message,
        code="invalid_runtime_execution_environment",
    )


def _canonical_values(values: Mapping[str, str]) -> bytes:
    return (
        json.dumps(
            dict(values),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("ascii")


def _validate_plain_value(name: str, value: str) -> None:
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as error:
        raise RunpodLocalError(
            f"runtime environment {name} is not ASCII",
            code="invalid_runtime_execution_environment",
        ) from error
    if (
        not encoded
        or len(encoded) > _MAX_VALUE_BYTES
        or any(byte < 0x21 or byte > 0x7E for byte in encoded)
    ):
        _fail(f"runtime environment {name} is empty, unbounded, or unsafe")


def _validate_library_path(value: str) -> None:
    _validate_plain_value("LD_LIBRARY_PATH", value)
    components = value.split(":")
    if len(components) != len(set(components)):
        _fail("LD_LIBRARY_PATH contains duplicate components")
    for component in components:
        path = pathlib.PurePosixPath(component)
        if (
            not component
            or not path.is_absolute()
            or str(path) != os.path.normpath(component)
            or component in {".", ".."}
        ):
            _fail("LD_LIBRARY_PATH contains a non-absolute component")


def _validate_values(values: Any) -> dict[str, str]:
    allowed = {*_FIXED_ENVIRONMENT, *_INHERITED_NAMES}
    if (
        not isinstance(values, dict)
        or not all(isinstance(name, str) for name in values)
        or set(values) - allowed
        or any(not isinstance(value, str) for value in values.values())
        or any(values.get(name) != value for name, value in _FIXED_ENVIRONMENT.items())
    ):
        _fail("runtime execution environment fields are malformed")
    normalized = {name: values[name] for name in sorted(values)}
    for name in _INHERITED_NAMES:
        if name not in normalized:
            continue
        if name == "LD_LIBRARY_PATH":
            _validate_library_path(normalized[name])
        else:
            _validate_plain_value(name, normalized[name])
    return normalized


@dataclass(frozen=True)
class RuntimeExecutionEnvironment:
    """One exact bounded environment used for both verifier and vLLM."""

    values: dict[str, str]
    sha256: str

    def normalized(self) -> dict[str, Any]:
        return {
            "schema_version": RUNTIME_EXECUTION_ENVIRONMENT_SCHEMA,
            "values": dict(self.values),
            "sha256": self.sha256,
        }


def runtime_execution_environment(
    source: Mapping[str, str] | None = None,
) -> RuntimeExecutionEnvironment:
    inherited = os.environ if source is None else source
    values = dict(_FIXED_ENVIRONMENT)
    for name in _INHERITED_NAMES:
        if name in inherited:
            values[name] = inherited[name]
    normalized = _validate_values(values)
    return RuntimeExecutionEnvironment(
        values=normalized,
        sha256=hashlib.sha256(_canonical_values(normalized)).hexdigest(),
    )


def validate_runtime_execution_environment(
    value: Any,
) -> RuntimeExecutionEnvironment:
    if not isinstance(value, dict) or set(value) != _DOCUMENT_FIELDS:
        _fail("runtime execution environment document is malformed")
    if value["schema_version"] != RUNTIME_EXECUTION_ENVIRONMENT_SCHEMA:
        _fail("runtime execution environment schema is unsupported")
    normalized = _validate_values(value["values"])
    digest = hashlib.sha256(_canonical_values(normalized)).hexdigest()
    if value["sha256"] != digest:
        _fail("runtime execution environment digest is mismatched")
    return RuntimeExecutionEnvironment(
        values=normalized,
        sha256=digest,
    )


__all__ = [
    "RUNTIME_EXECUTION_ENVIRONMENT_SCHEMA",
    "RuntimeExecutionEnvironment",
    "runtime_execution_environment",
    "validate_runtime_execution_environment",
]
