"""Canonical private Runpod Pod-template contracts."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .errors import RunpodLocalError

IMAGE_DIGEST_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9._:/-]*[a-z0-9])?"
    r"@sha256:[0-9a-f]{64}$"
)
PROVIDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,191}$")
TEMPLATE_CONTRACT_FIELDS = frozenset(
    {
        "id",
        "name",
        "image",
        "category",
        "container_disk_gb",
        "docker_entrypoint",
        "docker_start_cmd",
        "ports",
        "is_public",
        "is_serverless",
        "volume_in_gb",
        "volume_mount_path",
        "environment_names",
        "has_registry_auth",
    }
)
MAX_DOCKER_ARGUMENTS = 64
MAX_DOCKER_ARGUMENT_BYTES = 128 * 1024
MAX_DOCKER_ARGUMENT_TOTAL_BYTES = 256 * 1024


def environment_summary(value: Any) -> dict[str, Any] | None:
    """Fingerprint one exact string environment without retaining its values."""

    if not isinstance(value, dict) or not all(
        isinstance(name, str)
        and bool(name)
        and name.isprintable()
        and isinstance(item, str)
        for name, item in value.items()
    ):
        return None
    try:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (UnicodeEncodeError, ValueError):
        return None
    return {
        "environment_names": sorted(value),
        "environment_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def string_summary(value: Any) -> dict[str, Any]:
    """Describe a provider string without retaining its bytes."""

    try:
        encoded = value.encode("utf-8") if isinstance(value, str) else None
    except UnicodeEncodeError:
        encoded = None
    return {
        "valid_string": encoded is not None,
        "utf8_bytes": len(encoded) if encoded is not None else None,
        "sha256": (
            hashlib.sha256(encoded).hexdigest()
            if encoded is not None
            else None
        ),
    }


def docker_arguments_summary(value: Any) -> dict[str, Any]:
    """Describe an argument vector without disclosing any argument bytes."""

    argument_count = len(value) if isinstance(value, list) else None
    if not isinstance(value, list) or not all(
        isinstance(argument, str) for argument in value
    ):
        return {
            "valid_string_array": False,
            "argument_count": argument_count,
            "utf8_bytes": None,
            "sha256": None,
        }
    try:
        encoded_arguments = [
            argument.encode("utf-8") for argument in value
        ]
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (UnicodeEncodeError, ValueError):
        return {
            "valid_string_array": False,
            "argument_count": argument_count,
            "utf8_bytes": None,
            "sha256": None,
        }
    return {
        "valid_string_array": True,
        "argument_count": argument_count,
        "utf8_bytes": sum(len(argument) for argument in encoded_arguments),
        "sha256": hashlib.sha256(canonical).hexdigest(),
    }


def redact_docker_arguments(value: Any) -> Any:
    """Copy a public result while replacing every Docker argv with metadata."""

    if isinstance(value, dict):
        return {
            key: (
                docker_arguments_summary(item)
                if key
                in {
                    "docker_entrypoint",
                    "docker_start_cmd",
                    "dockerEntrypoint",
                    "dockerStartCmd",
                }
                else redact_docker_arguments(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_docker_arguments(item) for item in value]
    return value


def validate_image_digest(value: Any) -> str:
    """Require one immutable OCI image reference."""

    if (
        not isinstance(value, str)
        or not IMAGE_DIGEST_PATTERN.fullmatch(value)
        or any(ord(character) < 32 for character in value)
    ):
        raise RunpodLocalError(
            "image must be an immutable NAME@sha256:DIGEST reference",
            code="invalid_image_digest",
        )
    return value


def validate_docker_arguments(value: Any, *, label: str) -> list[str]:
    """Validate one bounded, exact OCI argument vector."""

    if (
        not isinstance(value, list)
        or not value
        or len(value) > MAX_DOCKER_ARGUMENTS
    ):
        raise RunpodLocalError(
            f"{label} must be a non-empty Docker argument array",
            code="invalid_template_contract",
        )
    result: list[str] = []
    total_bytes = 0
    for argument in value:
        try:
            encoded = (
                argument.encode("utf-8") if isinstance(argument, str) else b""
            )
        except UnicodeEncodeError as error:
            raise RunpodLocalError(
                f"{label} contains an argument that is not valid UTF-8",
                code="invalid_template_contract",
            ) from error
        if (
            not encoded
            or len(encoded) > MAX_DOCKER_ARGUMENT_BYTES
            or any(
                ord(character) < 32 and character not in "\n\t"
                or ord(character) == 127
                for character in argument
            )
        ):
            raise RunpodLocalError(
                f"{label} contains an invalid Docker argument",
                code="invalid_template_contract",
            )
        total_bytes += len(encoded)
        result.append(argument)
    if total_bytes > MAX_DOCKER_ARGUMENT_TOTAL_BYTES:
        raise RunpodLocalError(
            f"{label} exceeds the bounded Docker argument size",
            code="invalid_template_contract",
        )
    return result


def _template_name(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 191
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise RunpodLocalError(
            "template name must be 1-191 printable characters",
            code="invalid_template_contract",
        )
    return value


def _positive_integer(value: Any, *, label: str, minimum: int = 1) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
    ):
        raise RunpodLocalError(
            f"{label} must be an integer of at least {minimum}",
            code="invalid_template_contract",
        )
    return value


def _mount_path(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise RunpodLocalError(
            "template volume mount path must be absolute and printable",
            code="invalid_template_contract",
        )
    return value


def build_private_template_contract(
    *,
    name: str,
    image: str,
    docker_entrypoint: list[str],
    docker_start_cmd: list[str],
    container_disk_gb: int = 50,
    volume_in_gb: int = 0,
    volume_mount_path: str = "/workspace",
    template_id: str | None = None,
) -> dict[str, Any]:
    """Build the only template shape accepted by the local controller."""

    if template_id is not None and (
        not isinstance(template_id, str)
        or not PROVIDER_ID_PATTERN.fullmatch(template_id)
    ):
        raise RunpodLocalError(
            "invalid Runpod template ID",
            code="invalid_template_contract",
        )
    if (
        not isinstance(volume_in_gb, int)
        or isinstance(volume_in_gb, bool)
        or volume_in_gb != 0
    ):
        raise RunpodLocalError(
            "private Pod-template overlay volume must be exactly 0 GB",
            code="invalid_template_contract",
        )
    return {
        "id": template_id,
        "name": _template_name(name),
        "image": validate_image_digest(image),
        "category": "NVIDIA",
        "container_disk_gb": _positive_integer(
            container_disk_gb,
            label="template container disk",
            minimum=20,
        ),
        "docker_entrypoint": validate_docker_arguments(
            docker_entrypoint,
            label="template Docker entrypoint",
        ),
        "docker_start_cmd": validate_docker_arguments(
            docker_start_cmd,
            label="template Docker start command",
        ),
        "ports": ["22/tcp"],
        "is_public": False,
        "is_serverless": False,
        "volume_in_gb": 0,
        "volume_mount_path": _mount_path(volume_mount_path),
        "environment_names": [],
        "has_registry_auth": False,
    }


def normalize_template(template: dict[str, Any]) -> dict[str, Any]:
    """Normalize a provider template without retaining environment values."""

    environment = template.get("env")
    if "env" not in template:
        environment_names: list[str] | None = []
    elif isinstance(environment, dict) and all(
        isinstance(name, str) for name in environment
    ):
        environment_names = sorted(environment)
    else:
        environment_names = None
    registry_auth = template.get("containerRegistryAuthId")
    if registry_auth in (None, ""):
        has_registry_auth: bool | None = False
    elif isinstance(registry_auth, str):
        has_registry_auth = True
    else:
        has_registry_auth = None

    def string_array(field: str) -> list[str] | None:
        value = template.get(field)
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            return None
        return list(value)

    return {
        "id": template.get("id"),
        "name": template.get("name"),
        "image": template.get("imageName", template.get("image")),
        "category": template.get("category"),
        "container_disk_gb": template.get("containerDiskInGb"),
        "docker_entrypoint": string_array("dockerEntrypoint"),
        "docker_start_cmd": string_array("dockerStartCmd"),
        "ports": string_array("ports"),
        # Runpod's live REST implementation omits zero-value fields even
        # though its published response schema shows them as explicit.
        # Nonempty env, true booleans, and positive volume sizes remain
        # observable; omission is therefore the exact empty/false/zero state.
        "is_public": template.get("isPublic", False),
        "is_serverless": template.get("isServerless", False),
        "volume_in_gb": template.get("volumeInGb", 0),
        "volume_mount_path": template.get("volumeMountPath"),
        "environment_names": environment_names,
        "has_registry_auth": has_registry_auth,
    }


def validate_private_template_contract(
    contract: Any,
    *,
    require_id: bool,
) -> dict[str, Any]:
    """Require an exact normalized private Pod-template contract."""

    if (
        not isinstance(contract, dict)
        or set(contract) != TEMPLATE_CONTRACT_FIELDS
    ):
        raise RunpodLocalError(
            "template contract has unsupported or missing fields",
            code="invalid_template_contract",
        )
    template_id = contract.get("id")
    if require_id and template_id is None:
        raise RunpodLocalError(
            "template contract has no durable provider ID",
            code="invalid_template_contract",
        )
    reconstructed = build_private_template_contract(
        name=contract.get("name"),
        image=contract.get("image"),
        docker_entrypoint=contract.get("docker_entrypoint"),
        docker_start_cmd=contract.get("docker_start_cmd"),
        container_disk_gb=contract.get("container_disk_gb"),
        volume_in_gb=contract.get("volume_in_gb"),
        volume_mount_path=contract.get("volume_mount_path"),
        template_id=template_id,
    )
    if any(
        type(contract[field]) is not type(reconstructed[field])
        or contract[field] != reconstructed[field]
        for field in TEMPLATE_CONTRACT_FIELDS
    ):
        raise RunpodLocalError(
            "template must be private, non-serverless, SSH-only, "
            "environment-free, and registry-auth-free",
            code="invalid_template_contract",
        )
    return reconstructed


def template_create_payload(contract: dict[str, Any]) -> dict[str, Any]:
    contract = validate_private_template_contract(
        contract,
        require_id=False,
    )
    if contract["id"] is not None:
        raise RunpodLocalError(
            "new template request already has a provider ID",
            code="invalid_template_contract",
        )
    return {
        "imageName": contract["image"],
        "name": contract["name"],
        "category": contract["category"],
        "containerDiskInGb": contract["container_disk_gb"],
        "dockerEntrypoint": contract["docker_entrypoint"],
        "dockerStartCmd": contract["docker_start_cmd"],
        "env": {},
        "isPublic": False,
        "isServerless": False,
        "ports": contract["ports"],
        "readme": "",
        "volumeInGb": contract["volume_in_gb"],
        "volumeMountPath": contract["volume_mount_path"],
    }


def template_contract_violations(
    observed: dict[str, Any],
    expected: dict[str, Any],
) -> list[str]:
    """Compare provider state to one durable expected template contract."""

    expected = validate_private_template_contract(
        expected,
        require_id=expected.get("id") is not None,
    )
    violations: list[str] = []
    observed_id = observed.get("id")
    if expected["id"] is None:
        if (
            not isinstance(observed_id, str)
            or not PROVIDER_ID_PATTERN.fullmatch(observed_id)
        ):
            violations.append("missing_or_invalid_template_id")
    elif type(observed_id) is not str or observed_id != expected["id"]:
        violations.append("id: mismatch")
    for field in sorted(TEMPLATE_CONTRACT_FIELDS - {"id"}):
        actual = observed.get(field)
        wanted = expected[field]
        if type(actual) is not type(wanted) or actual != wanted:
            violations.append(f"{field}: mismatch")
    return violations
