"""Validated reusable Runpod launch profiles."""

from __future__ import annotations

import base64
import binascii
import hashlib
import math
import os
import pathlib
import re
import stat
import subprocess
from typing import Any

from .errors import RunpodLocalError
from .huggingface_credentials import REMOTE_HF_TOKEN_PATH
from .placement import load_hardware_catalog, select_hardware
from .state import StateStore, validate_record_name
from .timeutil import parse_utc_timestamp, utc_timestamp


PROFILE_SCHEMA = "runpod.profile.v1"
PROVIDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,191}$")
IMAGE_DIGEST_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9._:/-]*[a-z0-9])?"
    r"@sha256:[0-9a-f]{64}$"
)
ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SECRET_REFERENCE_PATTERN = re.compile(
    r"\{\{\s*RUNPOD_SECRET_[A-Za-z0-9_]+\s*\}\}"
)
SENSITIVE_ENVIRONMENT_PATTERN = re.compile(
    r"(?:^|_)(?:TOKEN|KEY|SECRET|PASSWORD|CREDENTIALS?)(?:_|$)",
    re.IGNORECASE,
)
NONSECRET_SENSITIVE_ENVIRONMENT_NAMES = {
    "HF_TOKEN_PATH",
    "SSH_PUBLIC_KEY",
}
FORBIDDEN_HUGGING_FACE_CREDENTIAL_ENVIRONMENT_NAMES = {
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
}
FORBIDDEN_REMOTE_SHELL_ENVIRONMENT_NAMES = {
    "BASHOPTS",
    "BASH_ENV",
    "ENV",
    "HOME",
    "PS4",
    "SHELL",
    "SHELLOPTS",
    "ZDOTDIR",
}
FORBIDDEN_DYNAMIC_LOADER_ENVIRONMENT_NAMES = {
    "GCONV_PATH",
    "GLIBC_TUNABLES",
    "LOCPATH",
}
UNSAFE_RUNPOD_ENVIRONMENT_VALUE_CHARACTERS = frozenset('"$\\`')
DEFAULT_CACHE_ENVIRONMENT = {
    "HF_ASSETS_CACHE": "/workspace/.cache/huggingface/assets",
    "HF_HOME": "/workspace/.cache/huggingface",
    "HF_HUB_CACHE": "/workspace/.cache/huggingface/hub",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HF_HUB_DISABLE_UPDATE_CHECK": "1",
    "HF_TOKEN_PATH": REMOTE_HF_TOKEN_PATH,
    "HF_XET_CACHE": "/workspace/.cache/huggingface/xet",
    "HF_XET_HIGH_PERFORMANCE": "1",
    "TORCH_HOME": "/workspace/.cache/torch",
    "VLLM_CACHE_ROOT": "/workspace/.cache/vllm",
    "XDG_CACHE_HOME": "/workspace/.cache",
}
SSH_PUBLIC_KEY_TYPES = {
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
}
MAX_SSH_PUBLIC_KEY_BYTES = 16 * 1024


def _provider_id(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not PROVIDER_ID_PATTERN.fullmatch(value):
        raise RunpodLocalError(
            f"invalid Runpod {label}: {value!r}",
            code="invalid_profile",
        )
    return value


def validate_image_digest(value: Any) -> str:
    """Require one immutable OCI image reference."""

    if (
        not isinstance(value, str)
        or not IMAGE_DIGEST_PATTERN.fullmatch(value)
        or any(ord(character) < 32 for character in value)
    ):
        raise RunpodLocalError(
            "profile image must be an immutable NAME@sha256:DIGEST reference",
            code="invalid_profile",
        )
    return value


def _printable_path(value: Any, *, label: str) -> str:
    try:
        encoded = value.encode("utf-8") if isinstance(value, str) else b""
    except UnicodeEncodeError as error:
        raise RunpodLocalError(
            f"profile {label} path is not valid UTF-8",
            code="invalid_profile",
        ) from error
    if (
        not isinstance(value, str)
        or not encoded
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in value
        )
    ):
        raise RunpodLocalError(
            f"profile {label} path is invalid",
            code="invalid_profile",
        )
    return value


def validate_ssh_identity_file(value: Any) -> pathlib.Path:
    if not isinstance(value, str) or not value:
        raise RunpodLocalError(
            "SSH identity path is missing",
            code="invalid_ssh_identity",
        )
    path = pathlib.Path(value).expanduser()
    if not path.is_absolute():
        raise RunpodLocalError(
            "SSH identity path must be absolute or start with ~/",
            code="invalid_ssh_identity",
        )
    try:
        metadata = path.lstat()
    except (OSError, ValueError) as error:
        raise RunpodLocalError(
            f"cannot inspect SSH identity file {path}: {error}",
            code="invalid_ssh_identity",
        ) from error
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise RunpodLocalError(
            f"SSH identity is not a regular non-symlink file: {path}",
            code="invalid_ssh_identity",
        )
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise RunpodLocalError(
            f"SSH identity is not owned by the current user: {path}",
            code="invalid_ssh_identity",
        )
    if metadata.st_mode & 0o077:
        raise RunpodLocalError(
            f"SSH identity permissions are broader than 0600: {path}",
            code="invalid_ssh_identity",
        )
    return path


def validate_environment(environment: dict[str, str]) -> dict[str, str]:
    if not isinstance(environment, dict):
        raise RunpodLocalError(
            "profile environment must be an object",
            code="invalid_profile_environment",
        )
    result = {}
    for name, value in environment.items():
        if (
            not isinstance(name, str)
            or not ENVIRONMENT_NAME_PATTERN.fullmatch(name)
        ):
            raise RunpodLocalError(
                f"invalid environment variable name: {name!r}",
                code="invalid_profile_environment",
            )
        if (
            not isinstance(value, str)
            or any(
                ord(character) < 32
                or ord(character) == 127
                or character in UNSAFE_RUNPOD_ENVIRONMENT_VALUE_CHARACTERS
                for character in value
            )
        ):
            raise RunpodLocalError(
                f"environment value for {name} cannot be represented safely "
                "by the Runpod image startup contract",
                code="invalid_profile_environment",
            )
        if SECRET_REFERENCE_PATTERN.search(value):
            raise RunpodLocalError(
                "Runpod-secret environment references are unsupported because "
                "the expanded value cannot be validated before image startup",
                code="invalid_profile_environment",
            )
        if name in FORBIDDEN_HUGGING_FACE_CREDENTIAL_ENVIRONMENT_NAMES:
            raise RunpodLocalError(
                f"{name} is reserved; Hugging Face credentials must use the "
                "ephemeral SSH lease",
                code="invalid_profile_environment",
            )
        if (
            name in FORBIDDEN_REMOTE_SHELL_ENVIRONMENT_NAMES
            or name.startswith("LD_")
            or name in FORBIDDEN_DYNAMIC_LOADER_ENVIRONMENT_NAMES
        ):
            raise RunpodLocalError(
                f"{name} is reserved by the reconciled SSH/runtime control "
                "plane",
                code="invalid_profile_environment",
            )
        if (
            name not in NONSECRET_SENSITIVE_ENVIRONMENT_NAMES
            and SENSITIVE_ENVIRONMENT_PATTERN.search(name)
        ):
            raise RunpodLocalError(
                f"{name} looks sensitive and cannot be stored in a launch "
                "profile",
                code="literal_secret_rejected",
            )
        if name == "HF_TOKEN_PATH" and value != REMOTE_HF_TOKEN_PATH:
            raise RunpodLocalError(
                f"HF_TOKEN_PATH must use ephemeral {REMOTE_HF_TOKEN_PATH}",
                code="invalid_profile_environment",
            )
        result[name] = value
    return {name: result[name] for name in sorted(result)}


def _ssh_string(
    blob: bytes, offset: int, *, label: str
) -> tuple[bytes, int]:
    if len(blob) - offset < 4:
        raise RunpodLocalError(
            f"SSH public key has a truncated {label} length",
            code="invalid_ssh_public_key",
        )
    length = int.from_bytes(blob[offset : offset + 4], "big")
    start = offset + 4
    end = start + length
    if end > len(blob):
        raise RunpodLocalError(
            f"SSH public key has a truncated {label}",
            code="invalid_ssh_public_key",
        )
    return blob[start:end], end


def _ssh_public_key_blob(key_type: str, blob: bytes) -> None:
    algorithm, offset = _ssh_string(blob, 0, label="algorithm")
    try:
        decoded_algorithm = algorithm.decode("ascii")
    except UnicodeDecodeError as error:
        raise RunpodLocalError(
            "SSH public key algorithm is not ASCII",
            code="invalid_ssh_public_key",
        ) from error
    if decoded_algorithm != key_type:
        raise RunpodLocalError(
            "SSH public key label does not match its wire algorithm",
            code="invalid_ssh_public_key",
        )

    if key_type == "ssh-ed25519":
        public_key, offset = _ssh_string(blob, offset, label="Ed25519 key")
        if len(public_key) != 32:
            raise RunpodLocalError(
                "SSH Ed25519 public key must be 32 bytes",
                code="invalid_ssh_public_key",
            )
    elif key_type == "ssh-rsa":
        exponent, offset = _ssh_string(blob, offset, label="RSA exponent")
        modulus, offset = _ssh_string(blob, offset, label="RSA modulus")
        if (
            not exponent
            or not modulus
            or exponent[0] & 0x80
            or modulus[0] & 0x80
        ):
            raise RunpodLocalError(
                "SSH RSA public key has an invalid positive integer",
                code="invalid_ssh_public_key",
            )
    elif key_type.startswith("ecdsa-sha2-"):
        curve, offset = _ssh_string(blob, offset, label="ECDSA curve")
        point, offset = _ssh_string(blob, offset, label="ECDSA point")
        expected_curve = key_type.removeprefix("ecdsa-sha2-").encode("ascii")
        point_lengths = {
            b"nistp256": 65,
            b"nistp384": 97,
            b"nistp521": 133,
        }
        if (
            curve != expected_curve
            or len(point) != point_lengths.get(curve)
            or not point.startswith(b"\x04")
        ):
            raise RunpodLocalError(
                "SSH ECDSA public key has an invalid curve or point",
                code="invalid_ssh_public_key",
            )
    elif key_type == "sk-ssh-ed25519@openssh.com":
        public_key, offset = _ssh_string(
            blob, offset, label="security-key Ed25519 key"
        )
        application, offset = _ssh_string(
            blob, offset, label="security-key application"
        )
        if len(public_key) != 32 or not application:
            raise RunpodLocalError(
                "SSH security-key Ed25519 data is invalid",
                code="invalid_ssh_public_key",
            )
    elif key_type == "sk-ecdsa-sha2-nistp256@openssh.com":
        curve, offset = _ssh_string(
            blob, offset, label="security-key ECDSA curve"
        )
        point, offset = _ssh_string(
            blob, offset, label="security-key ECDSA point"
        )
        application, offset = _ssh_string(
            blob, offset, label="security-key application"
        )
        if (
            curve != b"nistp256"
            or len(point) != 65
            or not point.startswith(b"\x04")
            or not application
        ):
            raise RunpodLocalError(
                "SSH security-key ECDSA data is invalid",
                code="invalid_ssh_public_key",
            )
    else:
        raise AssertionError(f"unhandled SSH public-key type: {key_type}")

    if offset != len(blob):
        raise RunpodLocalError(
            "SSH public key has trailing wire data",
            code="invalid_ssh_public_key",
        )


def validate_ssh_public_key(value: str) -> str:
    try:
        encoded_value = value.encode("utf-8") if isinstance(value, str) else b""
    except UnicodeEncodeError as error:
        raise RunpodLocalError(
            "SSH public key is not valid UTF-8 text",
            code="invalid_ssh_public_key",
        ) from error
    if (
        not isinstance(value, str)
        or not value
        or len(encoded_value) > MAX_SSH_PUBLIC_KEY_BYTES
        or value != value.strip()
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in value
        )
    ):
        raise RunpodLocalError(
            "SSH public key must be one printable OpenSSH public-key line",
            code="invalid_ssh_public_key",
        )
    fields = value.split(maxsplit=2)
    if len(fields) < 2 or fields[0] not in SSH_PUBLIC_KEY_TYPES:
        raise RunpodLocalError(
            "SSH public key has an unsupported or missing key type",
            code="invalid_ssh_public_key",
        )
    try:
        decoded = base64.b64decode(fields[1], validate=True)
    except (binascii.Error, ValueError) as error:
        raise RunpodLocalError(
            "SSH public key body is not valid base64",
            code="invalid_ssh_public_key",
        ) from error
    _ssh_public_key_blob(fields[0], decoded)
    return value


def load_ssh_public_key_file(value: str) -> tuple[pathlib.Path, str]:
    if not isinstance(value, str) or not value:
        raise RunpodLocalError(
            "SSH public key path is missing",
            code="invalid_ssh_public_key",
        )
    path = pathlib.Path(value).expanduser()
    if not path.is_absolute():
        raise RunpodLocalError(
            "SSH public key path must be absolute or start with ~/",
            code="invalid_ssh_public_key",
        )
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except (OSError, ValueError) as error:
        raise RunpodLocalError(
            f"cannot read SSH public key file {path}: {error}",
            code="invalid_ssh_public_key",
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RunpodLocalError(
                f"SSH public key is not a regular file: {path}",
                code="invalid_ssh_public_key",
            )
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise RunpodLocalError(
                f"SSH public key is not owned by the current user: {path}",
                code="invalid_ssh_public_key",
            )
        if metadata.st_mode & 0o022:
            raise RunpodLocalError(
                f"SSH public key is writable by another user: {path}",
                code="invalid_ssh_public_key",
            )
        if metadata.st_size > MAX_SSH_PUBLIC_KEY_BYTES:
            raise RunpodLocalError(
                f"SSH public key file exceeds {MAX_SSH_PUBLIC_KEY_BYTES} bytes",
                code="invalid_ssh_public_key",
            )
        with os.fdopen(descriptor, "rb", closefd=True) as public_key_file:
            descriptor = -1
            raw = public_key_file.read(MAX_SSH_PUBLIC_KEY_BYTES + 1)
        if len(raw) > MAX_SSH_PUBLIC_KEY_BYTES:
            raise RunpodLocalError(
                f"SSH public key file exceeds {MAX_SSH_PUBLIC_KEY_BYTES} bytes",
                code="invalid_ssh_public_key",
            )
    except RunpodLocalError:
        raise
    except OSError as error:
        raise RunpodLocalError(
            f"cannot read SSH public key file {path}: {error}",
            code="invalid_ssh_public_key",
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        public_key = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RunpodLocalError(
            "SSH public key file is not UTF-8",
            code="invalid_ssh_public_key",
        ) from error
    if public_key.endswith("\n"):
        public_key = public_key[:-1]
    if "\n" in public_key or "\r" in public_key:
        raise RunpodLocalError(
            "SSH public key file must contain exactly one key",
            code="invalid_ssh_public_key",
        )
    return path, validate_ssh_public_key(public_key)


def validate_ssh_key_pair(
    identity_file: str, public_key: str
) -> None:
    expected_fields = validate_ssh_public_key(public_key).split(maxsplit=2)
    identity_path = validate_ssh_identity_file(identity_file)
    try:
        result = subprocess.run(
            [
                "ssh-keygen",
                "-y",
                "-P",
                "",
                "-f",
                str(identity_path),
            ],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RunpodLocalError(
            "cannot derive the public key from the SSH identity",
            code="invalid_ssh_identity",
        ) from error
    if result.returncode != 0:
        raise RunpodLocalError(
            "SSH identity must be a readable non-interactive private key",
            code="invalid_ssh_identity",
        )
    try:
        derived = validate_ssh_public_key(
            result.stdout.decode("utf-8").removesuffix("\n")
        )
    except (UnicodeDecodeError, RunpodLocalError) as error:
        raise RunpodLocalError(
            "ssh-keygen returned an invalid public key for the SSH identity",
            code="invalid_ssh_identity",
        ) from error
    derived_fields = derived.split(maxsplit=2)
    if expected_fields[:2] != derived_fields[:2]:
        raise RunpodLocalError(
            "SSH identity and injected public key do not form a key pair",
            code="ssh_key_mismatch",
        )


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
    identity_file: str = "~/.ssh/id_ed25519_runpod",
    public_key_file: str = "~/.ssh/id_ed25519_runpod.pub",
    ssh_public_key: str | None = None,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    validate_record_name(name)
    if (image_name is None) == (template_id is None):
        raise RunpodLocalError(
            "profile requires exactly one of image_name or template_id",
            code="invalid_profile",
        )
    if image_name is not None:
        validate_image_digest(image_name)
    if template_id is not None:
        _provider_id(template_id, label="template ID")
    if not isinstance(ephemeral, bool):
        raise RunpodLocalError(
            "profile ephemeral storage selector must be boolean",
            code="invalid_profile",
        )
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
    identity_file = _printable_path(identity_file, label="SSH identity")
    public_key_file = _printable_path(
        public_key_file, label="SSH public-key"
    )
    if ssh_public_key is None:
        raise RunpodLocalError(
            "profile requires an explicit SSH public key",
            code="invalid_profile",
        )
    ssh_public_key = validate_ssh_public_key(ssh_public_key)
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
    if "PUBLIC_KEY" in merged_environment:
        raise RunpodLocalError(
            "PUBLIC_KEY is provider-owned; profiles inject only SSH_PUBLIC_KEY",
            code="invalid_profile_environment",
        )
    configured_public_key = merged_environment.get("SSH_PUBLIC_KEY")
    if (
        configured_public_key is not None
        and configured_public_key != ssh_public_key
    ):
        raise RunpodLocalError(
            "SSH_PUBLIC_KEY conflicts with the profile public key",
            code="invalid_profile_environment",
        )
    merged_environment["SSH_PUBLIC_KEY"] = ssh_public_key
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
            "public_key_file": public_key_file,
            "public_key_sha256": hashlib.sha256(
                ssh_public_key.encode("utf-8")
            ).hexdigest(),
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
    if not isinstance(profile, dict):
        raise RunpodLocalError(
            "profile is not an object",
            code="invalid_profile",
        )
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
    environment = pod.get("environment")
    if not isinstance(environment, dict):
        raise RunpodLocalError(
            "profile environment is not an object",
            code="invalid_profile",
        )
    created_at = profile.get("created_at")
    if not isinstance(created_at, str):
        raise RunpodLocalError(
            "profile has no creation timestamp",
            code="invalid_profile",
        )
    parse_utc_timestamp(created_at)
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
        public_key_file=ssh.get("public_key_file", ""),
        ssh_public_key=environment.get("SSH_PUBLIC_KEY"),
        environment=environment,
    )
    reconstructed["created_at"] = created_at
    for section_name in ("pod", "ssh", "limits", "lease"):
        original_section = profile[section_name]
        for key, value in reconstructed[section_name].items():
            if original_section.get(key) != value:
                raise RunpodLocalError(
                    f"profile {section_name}.{key} violates its canonical policy",
                    code="invalid_profile",
                )
    return reconstructed


def validate_profile_ssh_files(
    profile: dict[str, Any],
) -> tuple[pathlib.Path, pathlib.Path]:
    profile = validate_profile(profile)
    identity_path = validate_ssh_identity_file(
        profile["ssh"]["identity_file"]
    )
    public_key_path, public_key = load_ssh_public_key_file(
        profile["ssh"]["public_key_file"]
    )
    injected_public_key = profile["pod"]["environment"]["SSH_PUBLIC_KEY"]
    if public_key != injected_public_key:
        raise RunpodLocalError(
            f"profile {profile['name']} public-key file no longer matches "
            "the key that would be injected",
            code="ssh_key_mismatch",
        )
    validate_ssh_key_pair(str(identity_path), injected_public_key)
    return identity_path, public_key_path


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
