"""Validated reusable Runpod launch profiles."""

from __future__ import annotations

import math
import re
from typing import Any

from .errors import RunpodLocalError
from .placement import load_hardware_catalog, select_hardware
from .state import StateStore, validate_record_name
from .timeutil import utc_timestamp


PROFILE_SCHEMA = "runpod.profile.v1"
PROVIDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,191}$")
ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SECRET_REFERENCE_PATTERN = re.compile(
    r"^\{\{\s*RUNPOD_SECRET_[A-Za-z0-9_]+\s*\}\}$"
)
SENSITIVE_ENVIRONMENT_PATTERN = re.compile(
    r"(?:^|_)(?:TOKEN|KEY|SECRET|PASSWORD|CREDENTIALS?)(?:_|$)",
    re.IGNORECASE,
)
DEFAULT_CACHE_ENVIRONMENT = {
    "HF_HOME": "/workspace/.cache/huggingface",
    "HF_HUB_CACHE": "/workspace/.cache/huggingface/hub",
    "TORCH_HOME": "/workspace/.cache/torch",
    "VLLM_CACHE_ROOT": "/workspace/.cache/vllm",
    "XDG_CACHE_HOME": "/workspace/.cache",
}


def _provider_id(value: str, *, label: str) -> str:
    if not PROVIDER_ID_PATTERN.fullmatch(value):
        raise RunpodLocalError(
            f"invalid Runpod {label}: {value!r}",
            code="invalid_profile",
        )
    return value


def validate_environment(environment: dict[str, str]) -> dict[str, str]:
    result = {}
    for name, value in sorted(environment.items()):
        if not ENVIRONMENT_NAME_PATTERN.fullmatch(name):
            raise RunpodLocalError(
                f"invalid environment variable name: {name!r}",
                code="invalid_profile_environment",
            )
        if not isinstance(value, str) or "\x00" in value:
            raise RunpodLocalError(
                f"invalid environment value for {name}",
                code="invalid_profile_environment",
            )
        if SENSITIVE_ENVIRONMENT_PATTERN.search(name) and not (
            SECRET_REFERENCE_PATTERN.fullmatch(value)
        ):
            raise RunpodLocalError(
                f"{name} looks sensitive and must use a Runpod secret reference "
                "such as {{ RUNPOD_SECRET_name }}",
                code="literal_secret_rejected",
            )
        result[name] = value
    return result


def create_profile(
    *,
    name: str,
    gpu_names: list[str],
    max_hourly_usd: float,
    default_ttl_seconds: int,
    image_name: str | None = None,
    template_id: str | None = None,
    network_volume_id: str | None = None,
    ephemeral: bool = False,
    container_disk_gb: int = 50,
    gpu_count: int = 1,
    allowed_cuda_versions: list[str] | None = None,
    min_vcpu_per_gpu: int = 8,
    min_ram_per_gpu: int = 32,
    identity_file: str = "~/.ssh/id_ed25519",
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    validate_record_name(name)
    if (image_name is None) == (template_id is None):
        raise RunpodLocalError(
            "profile requires exactly one of image_name or template_id",
            code="invalid_profile",
        )
    if image_name is not None and (
        not image_name or any(character.isspace() for character in image_name)
    ):
        raise RunpodLocalError(
            "profile image name must be a non-empty container reference",
            code="invalid_profile",
        )
    if template_id is not None:
        _provider_id(template_id, label="template ID")
    if ephemeral == (network_volume_id is not None):
        raise RunpodLocalError(
            "profile requires either a network volume ID or explicit ephemeral storage",
            code="invalid_profile",
        )
    if network_volume_id is not None:
        _provider_id(network_volume_id, label="network volume ID")
    if (
        not isinstance(gpu_names, list)
        or not gpu_names
        or not all(isinstance(gpu_name, str) for gpu_name in gpu_names)
    ):
        raise RunpodLocalError(
            "profile requires at least one GPU",
            code="invalid_profile",
        )
    selected_gpus = select_hardware(load_hardware_catalog(), gpu_names)
    if not isinstance(gpu_count, int) or isinstance(gpu_count, bool) or gpu_count <= 0:
        raise RunpodLocalError(
            "profile GPU count must be positive",
            code="invalid_profile",
        )
    if (
        not isinstance(max_hourly_usd, (int, float))
        or isinstance(max_hourly_usd, bool)
        or not math.isfinite(max_hourly_usd)
        or max_hourly_usd <= 0
    ):
        raise RunpodLocalError(
            "profile hourly price cap must be positive",
            code="invalid_profile",
        )
    if (
        not isinstance(default_ttl_seconds, int)
        or isinstance(default_ttl_seconds, bool)
        or default_ttl_seconds <= 0
    ):
        raise RunpodLocalError(
            "profile default TTL must be positive",
            code="invalid_profile",
        )
    if (
        not isinstance(container_disk_gb, int)
        or isinstance(container_disk_gb, bool)
        or container_disk_gb < 20
    ):
        raise RunpodLocalError(
            "profile container disk must be at least 20 GB",
            code="invalid_profile",
        )
    if (
        not isinstance(min_vcpu_per_gpu, int)
        or isinstance(min_vcpu_per_gpu, bool)
        or min_vcpu_per_gpu <= 0
        or not isinstance(min_ram_per_gpu, int)
        or isinstance(min_ram_per_gpu, bool)
        or min_ram_per_gpu <= 0
    ):
        raise RunpodLocalError(
            "profile CPU and RAM floors must be positive",
            code="invalid_profile",
        )
    if not identity_file or any(ord(character) < 32 for character in identity_file):
        raise RunpodLocalError(
            "profile SSH identity path is invalid",
            code="invalid_profile",
        )
    if allowed_cuda_versions is not None and not isinstance(
        allowed_cuda_versions, list
    ):
        raise RunpodLocalError(
            "allowed CUDA versions must be a list",
            code="invalid_profile",
        )
    cuda_versions = allowed_cuda_versions or []
    if not all(
        isinstance(version, str) and re.fullmatch(r"[0-9]+\.[0-9]+", version)
        for version in cuda_versions
    ):
        raise RunpodLocalError(
            "allowed CUDA versions must look like 12.8",
            code="invalid_profile",
        )
    if environment is not None and not isinstance(environment, dict):
        raise RunpodLocalError(
            "profile environment must be an object",
            code="invalid_profile",
        )
    merged_environment = dict(DEFAULT_CACHE_ENVIRONMENT)
    if environment:
        merged_environment.update(environment)
    merged_environment = validate_environment(merged_environment)
    return {
        "schema_version": PROFILE_SCHEMA,
        "name": name,
        "created_at": utc_timestamp(),
        "pod": {
            "image_name": image_name,
            "template_id": template_id,
            "cloud_type": "SECURE",
            "gpu_type_ids": [gpu["id"] for gpu in selected_gpus],
            "gpu_count": gpu_count,
            "gpu_type_priority": "custom",
            "network_volume_id": network_volume_id,
            "storage_mode": "ephemeral" if ephemeral else "network_volume",
            "container_disk_gb": container_disk_gb,
            "volume_mount_path": "/workspace",
            "ports": ["22/tcp"],
            "allowed_cuda_versions": cuda_versions,
            "min_vcpu_per_gpu": min_vcpu_per_gpu,
            "min_ram_per_gpu": min_ram_per_gpu,
            "interruptible": False,
            "environment": merged_environment,
        },
        "ssh": {
            "user": "root",
            "identity_file": identity_file,
        },
        "limits": {
            "max_hourly_usd": max_hourly_usd,
        },
        "lease": {
            "default_ttl_seconds": default_ttl_seconds,
            "expiry_action": "terminate",
        },
    }


def validate_profile(profile: dict[str, Any]) -> dict[str, Any]:
    if profile.get("schema_version") != PROFILE_SCHEMA:
        raise RunpodLocalError(
            "profile has an unsupported schema version",
            code="invalid_profile",
        )
    name = profile.get("name")
    if not isinstance(name, str):
        raise RunpodLocalError(
            "profile has no name",
            code="invalid_profile",
        )
    validate_record_name(name)
    pod = profile.get("pod")
    ssh = profile.get("ssh")
    limits = profile.get("limits")
    lease = profile.get("lease")
    if not all(
        isinstance(value, dict) for value in (pod, ssh, limits, lease)
    ):
        raise RunpodLocalError(
            "profile is missing a required object",
            code="invalid_profile",
        )
    reconstructed = create_profile(
        name=name,
        gpu_names=pod.get("gpu_type_ids", []),
        max_hourly_usd=limits.get("max_hourly_usd", 0),
        default_ttl_seconds=lease.get("default_ttl_seconds", 0),
        image_name=pod.get("image_name"),
        template_id=pod.get("template_id"),
        network_volume_id=pod.get("network_volume_id"),
        ephemeral=pod.get("storage_mode") == "ephemeral",
        container_disk_gb=pod.get("container_disk_gb", 0),
        gpu_count=pod.get("gpu_count", 0),
        allowed_cuda_versions=pod.get("allowed_cuda_versions"),
        min_vcpu_per_gpu=pod.get("min_vcpu_per_gpu", 0),
        min_ram_per_gpu=pod.get("min_ram_per_gpu", 0),
        identity_file=ssh.get("identity_file", ""),
        environment=pod.get("environment", {}),
    )
    reconstructed["created_at"] = profile.get(
        "created_at", reconstructed["created_at"]
    )
    return reconstructed


class ProfileStore:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    def save(self, profile: dict[str, Any], *, replace: bool = False) -> None:
        profile = validate_profile(profile)
        name = profile["name"]
        with self.state.locked("profiles"):
            existing = self.state.read("profiles", name)
            if existing is not None and not replace:
                raise RunpodLocalError(
                    f"profile already exists: {name}",
                    code="profile_exists",
                )
            self.state.write("profiles", name, profile)

    def load(self, name: str) -> dict[str, Any]:
        validate_record_name(name)
        profile = self.state.read("profiles", name)
        if profile is None:
            raise RunpodLocalError(
                f"profile does not exist: {name}",
                code="profile_not_found",
            )
        return validate_profile(profile)

    def list(self) -> list[dict[str, Any]]:
        return [validate_profile(profile) for profile in self.state.list("profiles")]
