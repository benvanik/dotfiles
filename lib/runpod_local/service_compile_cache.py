"""Content identity for reusable vLLM compilation caches.

The cache contract is generated deployment state. It combines the model
closure, typed compile-affecting launch plan, exact runtime, observed NVIDIA
driver, and reusable GPU product identity. No model definition owns a cache
path or cache-management implementation.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from dataclasses import dataclass
from typing import Any

from .errors import RunpodLocalError


COMPILE_CACHE_SCHEMA = "runpod.vllm-compile-cache.v1"
VLLM_DRIVER = "vllm-openai.v1"
PERSISTENT_COMPILE_ROOT = pathlib.PurePosixPath(
    "/workspace/.cache/compiled/vllm-openai/v1"
)
LOCAL_COMPILE_ROOT = pathlib.PurePosixPath(
    "/root/runpod-session/cache/compiled/vllm-openai/v1"
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DRIVER_VERSION_PATTERN = re.compile(r"^[0-9]+(?:[.][0-9]+)+$")
_IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*@sha256:[0-9a-f]{64}$")
_RUNTIME_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._+-][a-z0-9]+)*$")


def _fail(message: str) -> None:
    raise RunpodLocalError(message, code="invalid_compile_cache_identity")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _required_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        _fail(f"{label} must be a lowercase SHA-256 identity")
    return value


def _required_string(
    value: Any,
    *,
    label: str,
    maximum_bytes: int,
) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail(f"{label} must be a nonempty string without surrounding whitespace")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        _fail(f"{label} must be valid UTF-8")
    if len(encoded) > maximum_bytes or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        _fail(f"{label} contains unsupported text")
    return value


def _compute_capability(value: Any) -> tuple[int, int]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
    ):
        _fail("observed GPU compute capability must be [MAJOR, MINOR]")
    major, minor = value
    if not 1 <= major <= 99 or not 0 <= minor <= 9:
        _fail("observed GPU compute capability is outside the supported range")
    return major, minor


def _positive_integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _fail(f"{label} must be a positive integer")
    return value


@dataclass(frozen=True)
class ObservedNvidiaGpu:
    """Reusable product identity plus the exact host driver."""

    name: str
    compute_capability: tuple[int, int]
    memory_mib: int
    driver_version: str

    @classmethod
    def from_mapping(cls, value: Any) -> ObservedNvidiaGpu:
        if not isinstance(value, dict) or set(value) != {
            "name",
            "compute_capability",
            "memory_mib",
            "driver_version",
        }:
            _fail(
                "observed GPU must contain exactly name, compute_capability, "
                "memory_mib, and driver_version"
            )
        driver_version = _required_string(
            value["driver_version"],
            label="observed GPU driver version",
            maximum_bytes=64,
        )
        if not _DRIVER_VERSION_PATTERN.fullmatch(driver_version):
            _fail("observed GPU driver version is malformed")
        return cls(
            name=_required_string(
                value["name"],
                label="observed GPU name",
                maximum_bytes=256,
            ),
            compute_capability=_compute_capability(value["compute_capability"]),
            memory_mib=_positive_integer(
                value["memory_mib"],
                label="observed GPU memory MiB",
            ),
            driver_version=driver_version,
        )

    def product_identity(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "compute_capability": list(self.compute_capability),
            "memory_mib": self.memory_mib,
        }

    @property
    def product_sha256(self) -> str:
        return _sha256(self.product_identity())

    @property
    def architecture(self) -> str:
        major, minor = self.compute_capability
        return f"sm{major}{minor}"

    def normalized(self) -> dict[str, Any]:
        return {
            **self.product_identity(),
            "product_sha256": self.product_sha256,
            "driver_version": self.driver_version,
        }


def _runtime_identity(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        _fail("exact runtime identity must be an object")
    runtime_id = _required_string(
        value.get("runtime_id"),
        label="runtime ID",
        maximum_bytes=128,
    )
    if not _RUNTIME_ID_PATTERN.fullmatch(runtime_id):
        _fail("runtime ID is malformed")
    image = _required_string(
        value.get("image"),
        label="runtime image",
        maximum_bytes=512,
    )
    if not _IMAGE_PATTERN.fullmatch(image):
        _fail("runtime image must use an immutable sha256 digest")
    manifest = value.get("manifest")
    if not isinstance(manifest, dict):
        _fail("runtime manifest identity must be an object")
    return {
        "runtime_id": runtime_id,
        "image": image,
        "manifest_sha256": _required_sha256(
            manifest.get("sha256"),
            label="runtime manifest",
        ),
    }


def build_compile_cache_contract(
    *,
    driver: str,
    runtime: dict[str, Any],
    huggingface_closure_sha256: str,
    compile_affecting_launch_sha256: str,
    observed_gpu: dict[str, Any],
) -> dict[str, Any]:
    """Build one exact reusable cache identity and its generated roots."""

    if driver != VLLM_DRIVER:
        _fail(f"unsupported compile-cache driver: {driver!r}")
    gpu = ObservedNvidiaGpu.from_mapping(observed_gpu)
    identity = {
        "schema_version": COMPILE_CACHE_SCHEMA,
        "driver": driver,
        "runtime": _runtime_identity(runtime),
        "huggingface_closure_sha256": _required_sha256(
            huggingface_closure_sha256,
            label="Hugging Face closure",
        ),
        "compile_affecting_launch_sha256": _required_sha256(
            compile_affecting_launch_sha256,
            label="compile-affecting launch plan",
        ),
        "gpu": gpu.normalized(),
    }
    cache_id = _sha256(identity)
    relative_root = (
        pathlib.PurePosixPath(f"driver-{gpu.driver_version}")
        / gpu.architecture
        / gpu.product_sha256
        / cache_id
    )
    return {
        "schema_version": COMPILE_CACHE_SCHEMA,
        "cache_id": cache_id,
        "identity": identity,
        "persistent_root": str(PERSISTENT_COMPILE_ROOT / relative_root),
        "local_root": str(LOCAL_COMPILE_ROOT / relative_root),
    }
