"""Strict administrator-owned benchmark host policy document."""

from __future__ import annotations

import json
import pathlib
import re
from collections.abc import Mapping
from typing import NoReturn

from .errors import BenchmarkLockError
from .policy import AmdGpuIdentity, FixedHostPolicyConfig


CONFIG_PATH = pathlib.Path("/etc/benchmarkd/config.json")
CONFIG_SCHEMA = "benchmarkd.config.v1"
MAX_CONFIG_BYTES = 64 * 1024

_POLICY_IDENTITY_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,62}")
_CONFIG_FIELDS = frozenset({"schema", "policy_identity", "gpus"})
_GPU_FIELDS = frozenset(
    {
        "bdf",
        "vendor",
        "device",
        "subsystem_vendor",
        "subsystem_device",
        "revision",
        "unique_id",
        "device_class",
    }
)


def _configuration_error(message: str) -> BenchmarkLockError:
    return BenchmarkLockError(
        message,
        code="invalid_benchmark_policy_configuration",
    )


def strict_json_document(
    payload: bytes,
    *,
    description: str,
    maximum: int,
) -> object:
    """Decode bounded ASCII JSON while rejecting duplicate keys and NaN."""

    if not payload or len(payload) > maximum:
        raise _configuration_error(f"{description} is empty or exceeds {maximum} bytes")
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as error:
        raise _configuration_error(f"{description} is not ASCII JSON") from error

    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate object key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> NoReturn:
        raise ValueError(f"non-finite number {value!r}")

    try:
        return json.loads(
            text,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except ValueError as error:
        raise _configuration_error(
            f"{description} is not strict JSON: {error}"
        ) from error


def require_exact_fields(
    value: Mapping[str, object],
    expected: frozenset[str],
    *,
    description: str,
) -> None:
    """Require one exact object shape and identify every mismatch."""

    observed = frozenset(value)
    if observed == expected:
        return
    missing = sorted(expected - observed)
    unknown = sorted(observed - expected)
    details: list[str] = []
    if missing:
        details.append("missing " + ", ".join(missing))
    if unknown:
        details.append("unknown " + ", ".join(unknown))
    raise _configuration_error(
        f"{description} has invalid fields ({'; '.join(details)})"
    )


def parse_policy_configuration(payload: bytes) -> FixedHostPolicyConfig:
    """Parse the one fail-closed administrator policy schema."""

    document = strict_json_document(
        payload,
        description="benchmarkd policy configuration",
        maximum=MAX_CONFIG_BYTES,
    )
    if not isinstance(document, dict):
        raise _configuration_error("benchmarkd policy configuration is not an object")
    require_exact_fields(
        document,
        _CONFIG_FIELDS,
        description="benchmarkd policy configuration",
    )
    if document["schema"] != CONFIG_SCHEMA:
        raise _configuration_error(
            "benchmarkd policy configuration schema is unsupported"
        )
    policy_identity = document["policy_identity"]
    if not isinstance(policy_identity, str) or not _POLICY_IDENTITY_PATTERN.fullmatch(
        policy_identity
    ):
        raise _configuration_error("benchmarkd policy identity is not canonical")
    raw_gpus = document["gpus"]
    if not isinstance(raw_gpus, list) or not raw_gpus or len(raw_gpus) > 64:
        raise _configuration_error(
            "benchmarkd policy must select between one and 64 GPUs"
        )
    gpus: list[AmdGpuIdentity] = []
    for index, raw_gpu in enumerate(raw_gpus):
        if not isinstance(raw_gpu, dict):
            raise _configuration_error(f"benchmarkd GPU {index} is not an object")
        require_exact_fields(
            raw_gpu,
            _GPU_FIELDS,
            description=f"benchmarkd GPU {index}",
        )
        if not all(isinstance(raw_gpu[field], str) for field in _GPU_FIELDS):
            raise _configuration_error(
                f"benchmarkd GPU {index} identity fields must be strings"
            )
        try:
            gpus.append(
                AmdGpuIdentity(
                    bdf=raw_gpu["bdf"],
                    vendor=raw_gpu["vendor"],
                    device=raw_gpu["device"],
                    subsystem_vendor=raw_gpu["subsystem_vendor"],
                    subsystem_device=raw_gpu["subsystem_device"],
                    revision=raw_gpu["revision"],
                    unique_id=raw_gpu["unique_id"],
                    device_class=raw_gpu["device_class"],
                )
            )
        except ValueError as error:
            raise _configuration_error(
                f"benchmarkd GPU {index} identity is invalid: {error}"
            ) from error
    try:
        return FixedHostPolicyConfig(
            gpus=tuple(gpus),
            policy_identity=policy_identity,
        )
    except ValueError as error:
        raise _configuration_error(
            f"benchmarkd policy configuration is invalid: {error}"
        ) from error


def canonical_policy_configuration(config: FixedHostPolicyConfig) -> bytes:
    """Encode one validated policy as its installed canonical form."""

    document = {
        "gpus": [
            {
                "bdf": gpu.bdf,
                "device": gpu.device,
                "device_class": gpu.device_class,
                "revision": gpu.revision,
                "subsystem_device": gpu.subsystem_device,
                "subsystem_vendor": gpu.subsystem_vendor,
                "unique_id": gpu.unique_id,
                "vendor": gpu.vendor,
            }
            for gpu in config.gpus
        ],
        "policy_identity": config.policy_identity,
        "schema": CONFIG_SCHEMA,
    }
    return (
        json.dumps(
            document,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")
