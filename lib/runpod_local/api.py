"""Direct Runpod REST and GraphQL provider client."""

from __future__ import annotations

import math
import urllib.parse
from typing import Any

from .auth import ApiCredential
from .errors import RunpodLocalError
from .http import JsonHttpTransport
from .profile import validate_ssh_public_key
from .template import (
    docker_arguments_summary,
    environment_summary,
    normalize_template,
    string_summary,
    template_create_payload,
)
from .timeutil import parse_utc_timestamp, utc_timestamp

REST_BASE = "https://rest.runpod.io/v1"
GRAPHQL_URL = "https://api.runpod.io/graphql"
AVAILABLE_STOCK_STATUSES = frozenset({"High", "Medium", "Low"})
GPU_TYPES_QUERY = """
query {
  gpuTypes {
    id
    displayName
    memoryInGb
    secureCloud
    communityCloud
    lowestPrice(input: {gpuCount: %d, secureCloud: %s}) {
      stockStatus
      uninterruptablePrice
      availableGpuCounts
    }
  }
}
"""
DATA_CENTERS_QUERY = """
query {
  dataCenters {
    id
    name
    location
    gpuAvailability {
      gpuTypeId
      displayName
      stockStatus
    }
  }
}
"""
ACCOUNT_SSH_KEY_QUERY = """
query {
  myself {
    pubKey
  }
}
"""
CREATE_POD_MUTATION = """
mutation CreatePod($input: PodFindAndDeployOnDemandInput!) {
  podFindAndDeployOnDemand(input: $input) {
    id
    name
  }
}
"""
POD_POLICY_QUERY = """
query {
  pod(input: {podId: "%s"}) {
    id
    gpuCount
    locked
    podType
  }
}
"""
POD_TYPE_INTERRUPTIBLE = {
    "RESERVED": False,
    "INTERRUPTABLE": True,
    "BID": True,
    "BACKGROUND": True,
}
PROVIDER_POD_SNAPSHOT_FIELDS = frozenset(
    {
        "id_matches_expected",
        "name_matches_expected",
        "desired_status_matches_expected",
        "template_id_matches_expected",
        "container_disk_gb",
        "volume_in_gb",
        "volume_mount_path_matches_expected",
        "environment_status",
        "environment_name_count",
        "environment_names_match_expected",
        "environment_sha256",
        "environment_sha256_matches_expected",
        "registry_auth_status",
        "has_registry_auth",
        "interruptible",
        "locked",
        "gpu_status",
        "gpu_id_matches_expected",
        "gpu_count",
        "cost_status",
        "cost_per_hour",
        "machine_status",
        "data_center_id_matches_expected",
        "secure_cloud",
        "network_volume_status",
        "network_volume_id_matches_expected",
        "network_volume_present",
        "network_volume_size_gb",
        "network_volume_data_center_id_matches_expected",
        "port_count",
        "ports_status",
        "ports_match_expected",
        "port_mappings_status",
        "port_mapping_count",
        "image_matches_expected",
        "image_summary",
        "docker_entrypoint_status",
        "docker_entrypoint_matches_expected",
        "docker_entrypoint_summary",
        "docker_start_cmd_status",
        "docker_start_cmd_matches_expected",
        "docker_start_cmd_summary",
    }
)
PROVIDER_POD_SNAPSHOT_EXPECTED_FIELDS = frozenset(
    {
        "id",
        "name",
        "desired_status",
        "template_id",
        "volume_mount_path",
        "environment_names",
        "environment_sha256",
        "gpu_id",
        "data_center_id",
        "network_volume_id",
        "network_volume_data_center_id",
        "ports",
        "image",
        "docker_entrypoint",
        "docker_start_cmd",
    }
)
PROVIDER_STATUS_VALUES = frozenset({"valid", "invalid", "missing"})
HEX_DIGITS = frozenset("0123456789abcdef")
MAX_PROVIDER_INTEGER = (1 << 63) - 1


def _ssh_public_key_identity(value: str) -> tuple[str, str]:
    fields = validate_ssh_public_key(value).split(maxsplit=2)
    return fields[0], fields[1]


class _AccountSshKeyAttestation:
    """One-use proof that this API client observed an account key."""

    __slots__ = ("authority", "consumed", "key_identity")

    def __init__(
        self,
        *,
        authority: object,
        key_identity: tuple[str, str],
    ) -> None:
        self.authority = authority
        self.consumed = False
        self.key_identity = key_identity


def _provider_id(value: str, *, label: str) -> str:
    if (
        not value
        or len(value) > 191
        or any(
            character
            not in (
                "abcdefghijklmnopqrstuvwxyz"
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                "0123456789_-"
            )
            for character in value
        )
    ):
        raise RunpodLocalError(
            f"invalid Runpod {label}: {value!r}",
            code="invalid_provider_id",
        )
    return value


def _numeric(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            result = float(value)
        except (OverflowError, ValueError):
            return None
        return result if math.isfinite(result) and result >= 0 else None
    if isinstance(value, str):
        try:
            result = float(value)
        except ValueError:
            return None
        return result if math.isfinite(result) and result >= 0 else None
    return None


def valid_ssh_port_mappings(value: Any) -> bool:
    return isinstance(value, dict) and (
        not value
        or (
            set(value) == {"22"}
            and isinstance(value["22"], int)
            and not isinstance(value["22"], bool)
            and 0 < value["22"] <= 65535
        )
    )


def _pod_payload_error(message: str) -> RunpodLocalError:
    return RunpodLocalError(message, code="invalid_pod_payload")


def _pod_payload_string(
    payload: dict[str, Any],
    field: str,
) -> str:
    value = payload.get(field)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or not value.isprintable()
    ):
        raise _pod_payload_error(
            f"Pod creation payload has invalid {field}"
        )
    return value


def _pod_payload_positive_integer(
    payload: dict[str, Any],
    field: str,
) -> int:
    value = payload.get(field)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
    ):
        raise _pod_payload_error(
            f"Pod creation payload has invalid {field}"
        )
    return value


def _pod_graphql_input(payload: dict[str, Any]) -> dict[str, Any]:
    """Translate the durable REST-shaped launch intent to GraphQL input."""

    if not isinstance(payload, dict):
        raise _pod_payload_error("Pod creation payload is not an object")
    known_fields = {
        "allowedCudaVersions",
        "cloudType",
        "computeType",
        "containerDiskInGb",
        "dataCenterId",
        "env",
        "gpuCount",
        "gpuTypeIds",
        "gpuTypePriority",
        "imageName",
        "interruptible",
        "locked",
        "minRAMPerGPU",
        "minVCPUPerGPU",
        "name",
        "networkVolumeId",
        "ports",
        "templateId",
        "terminateAfter",
        "volumeInGb",
        "volumeMountPath",
    }
    unknown_fields = sorted(set(payload) - known_fields)
    if unknown_fields:
        raise _pod_payload_error(
            "Pod creation payload has unsupported fields: "
            + ", ".join(unknown_fields)
        )
    if payload.get("cloudType") != "SECURE":
        raise _pod_payload_error(
            "Pod creation payload must use secure cloud"
        )
    if payload.get("computeType") != "GPU":
        raise _pod_payload_error(
            "Pod creation payload must use GPU compute"
        )
    if payload.get("gpuTypePriority") != "custom":
        raise _pod_payload_error(
            "Pod creation payload must use custom GPU priority"
        )
    if payload.get("interruptible") is not False:
        raise _pod_payload_error(
            "Pod creation payload must be non-interruptible"
        )
    if payload.get("locked") is not False:
        raise _pod_payload_error(
            "Pod creation payload must be unlocked"
        )

    gpu_type_ids = payload.get("gpuTypeIds")
    if (
        not isinstance(gpu_type_ids, list)
        or len(gpu_type_ids) != 1
        or not isinstance(gpu_type_ids[0], str)
        or not gpu_type_ids[0]
        or len(gpu_type_ids[0]) > 4096
        or not gpu_type_ids[0].isprintable()
    ):
        raise _pod_payload_error(
            "Pod creation payload must select exactly one valid GPU type"
        )
    gpu_count = _pod_payload_positive_integer(payload, "gpuCount")

    ports = payload.get("ports")
    if (
        not isinstance(ports, list)
        or not ports
        or not all(
            isinstance(port, str)
            and port
            and "," not in port
            and port.isprintable()
            for port in ports
        )
    ):
        raise _pod_payload_error(
            "Pod creation payload has invalid ports"
        )

    environment = payload.get("env")
    if (
        not isinstance(environment, dict)
        or not all(
            isinstance(key, str)
            and key
            and key.isprintable()
            and isinstance(value, str)
            for key, value in environment.items()
        )
    ):
        raise _pod_payload_error(
            "Pod creation payload has invalid environment"
        )

    terminate_after = payload.get("terminateAfter")
    try:
        parse_utc_timestamp(terminate_after)
    except RunpodLocalError as error:
        raise _pod_payload_error(
            "Pod creation payload requires an absolute UTC terminateAfter"
        ) from error

    has_image = "imageName" in payload
    has_template = "templateId" in payload
    if has_image == has_template:
        raise _pod_payload_error(
            "Pod creation payload requires exactly one image or template"
        )
    has_network_volume = "networkVolumeId" in payload
    has_ephemeral_volume = "volumeInGb" in payload
    if has_network_volume == has_ephemeral_volume:
        raise _pod_payload_error(
            "Pod creation payload requires exactly one volume source"
        )

    graphql_input: dict[str, Any] = {
        "name": _pod_payload_string(payload, "name"),
        "cloudType": "SECURE",
        "computeType": "GPU",
        "gpuTypeId": gpu_type_ids[0],
        "gpuCount": gpu_count,
        "containerDiskInGb": _pod_payload_positive_integer(
            payload, "containerDiskInGb"
        ),
        "volumeMountPath": _pod_payload_string(
            payload, "volumeMountPath"
        ),
        "ports": ",".join(ports),
        "env": [
            {"key": key, "value": environment[key]}
            for key in sorted(environment)
        ],
        "startSsh": True,
        "minVcpuCount": (
            _pod_payload_positive_integer(payload, "minVCPUPerGPU")
            * gpu_count
        ),
        "minMemoryInGb": (
            _pod_payload_positive_integer(payload, "minRAMPerGPU")
            * gpu_count
        ),
        "terminateAfter": terminate_after,
    }
    if has_image:
        graphql_input["imageName"] = _pod_payload_string(
            payload, "imageName"
        )
    else:
        graphql_input["templateId"] = _pod_payload_string(
            payload, "templateId"
        )
    if has_network_volume:
        graphql_input["networkVolumeId"] = _pod_payload_string(
            payload, "networkVolumeId"
        )
    else:
        graphql_input["volumeInGb"] = _pod_payload_positive_integer(
            payload, "volumeInGb"
        )
    if "dataCenterId" in payload:
        graphql_input["dataCenterId"] = _pod_payload_string(
            payload, "dataCenterId"
        )
    if "allowedCudaVersions" in payload:
        cuda_versions = payload["allowedCudaVersions"]
        if not isinstance(cuda_versions, list) or not all(
            isinstance(version, str) and version
            for version in cuda_versions
        ):
            raise _pod_payload_error(
                "Pod creation payload has invalid allowedCudaVersions"
            )
        graphql_input["allowedCudaVersions"] = list(cuda_versions)
    return graphql_input


def gpu_stock_is_available(gpu: dict[str, Any], *, gpu_count: int) -> bool:
    """Interpret Runpod's global stock signal without requiring count hints.

    The live GraphQL service can report High/Medium/Low stock while returning
    an empty availableGpuCounts list. Treat the stock status as authoritative
    in that case; a non-empty count list remains an additional constraint.
    """
    if gpu_count <= 0:
        raise RunpodLocalError(
            "GPU count must be positive",
            code="invalid_gpu_count",
        )
    if gpu.get("stock_status") not in AVAILABLE_STOCK_STATUSES:
        return False
    counts = gpu.get("available_gpu_counts")
    return not counts or gpu_count in counts


def normalize_pod(pod: dict[str, Any]) -> dict[str, Any]:
    raw_gpu = pod.get("gpu")
    if "gpu" not in pod or raw_gpu is None:
        gpu = {}
        gpu_status = "missing"
    elif isinstance(raw_gpu, dict):
        gpu = raw_gpu
        gpu_status = "valid"
    else:
        gpu = {}
        gpu_status = "invalid"
    gpu_count = gpu.get("count")
    if gpu_count is None:
        gpu_count = pod.get("gpuCount")
    raw_machine = pod.get("machine")
    if "machine" not in pod or raw_machine is None:
        machine = {}
        machine_status = "missing"
    elif isinstance(raw_machine, dict):
        machine = raw_machine
        machine_status = "valid"
    else:
        machine = {}
        machine_status = "invalid"
    raw_network_volume = pod.get("networkVolume")
    if "networkVolume" not in pod or raw_network_volume is None:
        network_volume = None
        network_volume_status = "missing"
    elif isinstance(raw_network_volume, dict):
        network_volume = raw_network_volume
        network_volume_status = "valid"
    else:
        network_volume = None
        network_volume_status = "invalid"
    raw_port_mappings = pod.get("portMappings")
    if "portMappings" not in pod or raw_port_mappings is None:
        port_mappings = {}
        port_mappings_status = "missing"
    elif isinstance(raw_port_mappings, dict):
        if valid_ssh_port_mappings(raw_port_mappings):
            port_mappings = dict(raw_port_mappings)
            port_mappings_status = "valid"
        else:
            port_mappings = {}
            port_mappings_status = "invalid"
    else:
        port_mappings = {}
        port_mappings_status = "invalid"
    cost_per_hour = None
    cost_status = "missing"
    for cost_field in ("adjustedCostPerHr", "costPerHr"):
        if cost_field not in pod or pod[cost_field] is None:
            continue
        cost_per_hour = _numeric(pod[cost_field])
        cost_status = "valid" if cost_per_hour is not None else "invalid"
        break
    if "env" not in pod:
        normalized_environment = None
        environment_status = "missing"
    else:
        normalized_environment = environment_summary(pod.get("env"))
        environment_status = (
            "valid" if normalized_environment is not None else "invalid"
        )
    registry_auth = pod.get("containerRegistryAuthId")
    if (
        "containerRegistryAuthId" not in pod
        or registry_auth in (None, "")
    ):
        has_registry_auth = False
        registry_auth_status = "valid"
    elif isinstance(registry_auth, str):
        has_registry_auth = True
        registry_auth_status = "valid"
    else:
        has_registry_auth = None
        registry_auth_status = "invalid"

    def string_array(field: str) -> tuple[list[str] | None, str]:
        if field not in pod or pod[field] is None:
            return None, "missing"
        value = pod.get(field)
        if not isinstance(value, list) or not all(
            isinstance(argument, str) for argument in value
        ):
            return None, "invalid"
        return list(value), "valid"

    docker_entrypoint, docker_entrypoint_status = string_array(
        "dockerEntrypoint"
    )
    docker_start_cmd, docker_start_cmd_status = string_array(
        "dockerStartCmd"
    )
    raw_ports = pod.get("ports")
    if "ports" not in pod or raw_ports is None:
        ports = []
        ports_status = "missing"
    elif isinstance(raw_ports, list) and all(
        isinstance(port, str) for port in raw_ports
    ):
        ports = list(raw_ports)
        ports_status = "valid"
    else:
        ports = []
        ports_status = "invalid"

    return {
        "id": pod.get("id"),
        "name": pod.get("name"),
        "desired_status": pod.get("desiredStatus"),
        "image": pod.get("image", pod.get("imageName")),
        "template_id": pod.get("templateId"),
        "docker_entrypoint_status": docker_entrypoint_status,
        "docker_entrypoint": docker_entrypoint,
        "docker_start_cmd_status": docker_start_cmd_status,
        "docker_start_cmd": docker_start_cmd,
        "container_disk_gb": pod.get("containerDiskInGb"),
        "volume_in_gb": pod.get("volumeInGb"),
        "volume_mount_path": pod.get("volumeMountPath"),
        "environment_status": environment_status,
        "environment_names": (
            normalized_environment["environment_names"]
            if normalized_environment is not None
            else None
        ),
        "environment_sha256": (
            normalized_environment["environment_sha256"]
            if normalized_environment is not None
            else None
        ),
        "registry_auth_status": registry_auth_status,
        "has_registry_auth": has_registry_auth,
        "interruptible": pod.get("interruptible"),
        "locked": pod.get("locked"),
        "gpu_status": gpu_status,
        "gpu_id": gpu.get("id", machine.get("gpuTypeId")),
        "gpu_count": gpu_count,
        "cost_status": cost_status,
        "cost_per_hour": cost_per_hour,
        "machine_status": machine_status,
        "data_center_id": machine.get("dataCenterId"),
        "secure_cloud": machine.get("secureCloud"),
        "machine_id": pod.get("machineId"),
        "network_volume_status": network_volume_status,
        "network_volume_id": (
            network_volume.get("id") if network_volume is not None else None
        ),
        "network_volume_data_center_id": (
            network_volume.get("dataCenterId")
            if network_volume is not None
            else None
        ),
        "network_volume": (
            {
                "id": network_volume.get("id"),
                "name": network_volume.get("name"),
                "size_gb": network_volume.get("size"),
                "data_center_id": network_volume.get("dataCenterId"),
            }
            if network_volume is not None
            else None
        ),
        "public_ip": pod.get("publicIp"),
        "port_mappings_status": port_mappings_status,
        "port_mappings": port_mappings,
        "ports_status": ports_status,
        "ports": ports,
    }


def _safe_provider_integer(value: Any) -> int | None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value > MAX_PROVIDER_INTEGER
    ):
        return None
    return value


def _safe_provider_number(value: Any) -> int | float | None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or _numeric(value) is None
    ):
        return None
    return value


def _safe_provider_boolean(value: Any) -> bool | None:
    return value if type(value) is bool else None


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in HEX_DIGITS for character in value)
    )


def _valid_environment_names(value: Any) -> bool:
    return (
        isinstance(value, list)
        and all(
            isinstance(name, str)
            and bool(name)
            and len(name) <= 4096
            and name.isprintable()
            for name in value
        )
        and value == sorted(set(value))
    )


def _valid_string_summary(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "valid_string",
        "utf8_bytes",
        "sha256",
    }:
        return False
    valid = value.get("valid_string")
    byte_count = value.get("utf8_bytes")
    digest = value.get("sha256")
    if type(valid) is not bool:
        return False
    if valid:
        return (
            isinstance(byte_count, int)
            and not isinstance(byte_count, bool)
            and byte_count >= 0
            and _valid_sha256(digest)
        )
    return byte_count is None and digest is None


def _valid_docker_arguments_summary(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "valid_string_array",
        "argument_count",
        "utf8_bytes",
        "sha256",
    }:
        return False
    valid = value.get("valid_string_array")
    argument_count = value.get("argument_count")
    byte_count = value.get("utf8_bytes")
    digest = value.get("sha256")
    if (
        type(valid) is not bool
        or (
            argument_count is not None
            and (
                not isinstance(argument_count, int)
                or isinstance(argument_count, bool)
                or argument_count < 0
            )
        )
    ):
        return False
    if valid:
        return (
            argument_count is not None
            and isinstance(byte_count, int)
            and not isinstance(byte_count, bool)
            and byte_count >= 0
            and _valid_sha256(digest)
        )
    return byte_count is None and digest is None


def _strict_match(actual: Any, expected: Any) -> bool:
    return type(actual) is type(expected) and actual == expected


def _safe_provider_status(value: Any) -> str:
    return (
        value
        if isinstance(value, str) and value in PROVIDER_STATUS_VALUES
        else "invalid"
    )


def validate_provider_pod_snapshot(value: Any) -> dict[str, Any]:
    """Require the exact secret-safe Pod shape accepted by durable receipts."""

    if (
        not isinstance(value, dict)
        or set(value) != PROVIDER_POD_SNAPSHOT_FIELDS
    ):
        raise RunpodLocalError(
            "durable provider Pod snapshot has unsupported or missing fields",
            code="invalid_provider_snapshot",
        )
    required_match_fields = (
        "id_matches_expected",
        "name_matches_expected",
        "desired_status_matches_expected",
        "template_id_matches_expected",
        "volume_mount_path_matches_expected",
        "environment_names_match_expected",
        "environment_sha256_matches_expected",
        "gpu_id_matches_expected",
        "network_volume_id_matches_expected",
        "ports_match_expected",
        "image_matches_expected",
        "docker_entrypoint_matches_expected",
        "docker_start_cmd_matches_expected",
    )
    optional_match_fields = (
        "data_center_id_matches_expected",
        "network_volume_data_center_id_matches_expected",
    )
    if any(type(value[field]) is not bool for field in required_match_fields):
        raise RunpodLocalError(
            "durable provider Pod snapshot has an invalid match result",
            code="invalid_provider_snapshot",
        )
    if any(
        value[field] is not None and type(value[field]) is not bool
        for field in optional_match_fields
    ):
        raise RunpodLocalError(
            "durable provider Pod snapshot has an invalid match result",
            code="invalid_provider_snapshot",
        )
    integer_fields = (
        "container_disk_gb",
        "volume_in_gb",
        "environment_name_count",
        "gpu_count",
        "network_volume_size_gb",
        "port_count",
        "port_mapping_count",
    )
    if any(
        value[field] is not None
        and _safe_provider_integer(value[field]) is None
        for field in integer_fields
    ):
        raise RunpodLocalError(
            "durable provider Pod snapshot has an invalid numeric fact",
            code="invalid_provider_snapshot",
        )
    boolean_fields = (
        "has_registry_auth",
        "interruptible",
        "locked",
        "secure_cloud",
        "network_volume_present",
    )
    if any(
        value[field] is not None
        and _safe_provider_boolean(value[field]) is None
        for field in boolean_fields
    ):
        raise RunpodLocalError(
            "durable provider Pod snapshot has an invalid boolean fact",
            code="invalid_provider_snapshot",
        )
    cost_status = value["cost_status"]
    cost_per_hour = value["cost_per_hour"]
    if (
        _safe_provider_status(cost_status) != cost_status
        or (
            cost_status == "valid"
            and _safe_provider_number(cost_per_hour) is None
        )
        or (
            cost_status != "valid"
            and cost_per_hour is not None
        )
    ):
        raise RunpodLocalError(
            "durable provider Pod snapshot has invalid price evidence",
            code="invalid_provider_snapshot",
        )
    status_fields = (
        "gpu_status",
        "machine_status",
        "network_volume_status",
        "ports_status",
        "port_mappings_status",
        "docker_entrypoint_status",
        "docker_start_cmd_status",
    )
    if any(
        _safe_provider_status(value[field]) != value[field]
        for field in status_fields
    ):
        raise RunpodLocalError(
            "durable provider Pod snapshot has an invalid fact status",
            code="invalid_provider_snapshot",
        )
    if (
        (
            value["network_volume_status"] == "valid"
            and value["network_volume_present"] is not True
        )
        or (
            value["network_volume_status"] != "valid"
            and value["network_volume_present"] is not False
        )
        or (
            value["ports_status"] == "valid"
            and value["port_count"] is None
        )
        or (
            value["ports_status"] != "valid"
            and value["port_count"] is not None
        )
        or (
            value["port_mappings_status"] == "valid"
            and value["port_mapping_count"] is None
        )
        or (
            value["port_mappings_status"] != "valid"
            and value["port_mapping_count"] is not None
        )
    ):
        raise RunpodLocalError(
            "durable provider Pod snapshot has inconsistent fact status",
            code="invalid_provider_snapshot",
        )
    environment_status = value["environment_status"]
    environment_sha256 = value["environment_sha256"]
    if (
        _safe_provider_status(environment_status) != environment_status
        or (
            environment_status == "valid"
            and (
                value["environment_name_count"] is None
                or not _valid_sha256(environment_sha256)
            )
        )
        or (
            environment_status != "valid"
            and (
                value["environment_name_count"] is not None
                or environment_sha256 is not None
            )
        )
    ):
        raise RunpodLocalError(
            "durable provider Pod snapshot has an invalid environment summary",
            code="invalid_provider_snapshot",
        )
    registry_auth_status = value["registry_auth_status"]
    has_registry_auth = value["has_registry_auth"]
    if (
        _safe_provider_status(registry_auth_status)
        != registry_auth_status
        or (
            registry_auth_status == "valid"
            and type(has_registry_auth) is not bool
        )
        or (
            registry_auth_status != "valid"
            and has_registry_auth is not None
        )
    ):
        raise RunpodLocalError(
            "durable provider Pod snapshot has invalid registry-auth evidence",
            code="invalid_provider_snapshot",
        )
    if (
        not _valid_string_summary(value["image_summary"])
        or not _valid_docker_arguments_summary(
            value["docker_entrypoint_summary"]
        )
        or not _valid_docker_arguments_summary(
            value["docker_start_cmd_summary"]
        )
    ):
        raise RunpodLocalError(
            "durable provider Pod snapshot has invalid byte summaries",
            code="invalid_provider_snapshot",
        )
    for status_field, summary_field in (
        ("docker_entrypoint_status", "docker_entrypoint_summary"),
        ("docker_start_cmd_status", "docker_start_cmd_summary"),
    ):
        summary_is_valid = value[summary_field]["valid_string_array"]
        if (value[status_field] == "valid") is not summary_is_valid:
            raise RunpodLocalError(
                "durable provider Pod snapshot has inconsistent Docker status",
                code="invalid_provider_snapshot",
            )
    return value


def provider_pod_snapshot(
    pod: dict[str, Any],
    *,
    expected: dict[str, Any],
) -> dict[str, Any]:
    """Build the only normalized Pod shape permitted in a durable receipt."""

    if (
        not isinstance(expected, dict)
        or set(expected) != PROVIDER_POD_SNAPSHOT_EXPECTED_FIELDS
    ):
        raise RunpodLocalError(
            "provider Pod snapshot expectations are incomplete",
            code="invalid_provider_snapshot",
        )
    snapshot: dict[str, Any] = {
        "id_matches_expected": _strict_match(
            pod.get("id"), expected["id"]
        ),
        "name_matches_expected": _strict_match(
            pod.get("name"), expected["name"]
        ),
        "desired_status_matches_expected": _strict_match(
            pod.get("desired_status"), expected["desired_status"]
        ),
        "template_id_matches_expected": _strict_match(
            pod.get("template_id"), expected["template_id"]
        ),
        "container_disk_gb": _safe_provider_integer(
            pod.get("container_disk_gb")
        ),
        "volume_in_gb": _safe_provider_integer(pod.get("volume_in_gb")),
        "volume_mount_path_matches_expected": _strict_match(
            pod.get("volume_mount_path"),
            expected["volume_mount_path"],
        ),
        "interruptible": _safe_provider_boolean(
            pod.get("interruptible")
        ),
        "locked": _safe_provider_boolean(pod.get("locked")),
        "gpu_status": _safe_provider_status(pod.get("gpu_status")),
        "gpu_id_matches_expected": _strict_match(
            pod.get("gpu_id"), expected["gpu_id"]
        ),
        "gpu_count": _safe_provider_integer(pod.get("gpu_count")),
        "data_center_id_matches_expected": (
            None
            if expected["data_center_id"] is None
            else _strict_match(
                pod.get("data_center_id"),
                expected["data_center_id"],
            )
        ),
        "machine_status": _safe_provider_status(
            pod.get("machine_status")
        ),
        "secure_cloud": _safe_provider_boolean(
            pod.get("secure_cloud")
        ),
        "network_volume_id_matches_expected": _strict_match(
            pod.get("network_volume_id"),
            expected["network_volume_id"],
        ),
    }
    observed_cost = _safe_provider_number(pod.get("cost_per_hour"))
    observed_cost_status = _safe_provider_status(pod.get("cost_status"))
    if observed_cost_status == "valid" and observed_cost is not None:
        snapshot["cost_status"] = "valid"
        snapshot["cost_per_hour"] = observed_cost
    else:
        snapshot["cost_status"] = (
            "invalid"
            if observed_cost_status == "valid"
            else observed_cost_status
        )
        snapshot["cost_per_hour"] = None

    environment_status = _safe_provider_status(
        pod.get("environment_status")
    )
    environment_names = pod.get("environment_names")
    environment_sha256 = pod.get("environment_sha256")
    if (
        environment_status == "valid"
        and _valid_environment_names(environment_names)
        and _valid_sha256(environment_sha256)
    ):
        snapshot["environment_status"] = "valid"
        snapshot["environment_name_count"] = len(environment_names)
        snapshot["environment_names_match_expected"] = _strict_match(
            environment_names,
            expected["environment_names"],
        )
        snapshot["environment_sha256"] = environment_sha256
        snapshot["environment_sha256_matches_expected"] = _strict_match(
            environment_sha256,
            expected["environment_sha256"],
        )
    else:
        snapshot["environment_status"] = (
            "invalid"
            if environment_status == "valid"
            else environment_status
        )
        snapshot["environment_name_count"] = None
        snapshot["environment_names_match_expected"] = False
        snapshot["environment_sha256"] = None
        snapshot["environment_sha256_matches_expected"] = False

    registry_auth_status = _safe_provider_status(
        pod.get("registry_auth_status")
    )
    has_registry_auth = pod.get("has_registry_auth")
    if (
        registry_auth_status == "valid"
        and type(has_registry_auth) is bool
    ):
        snapshot["registry_auth_status"] = "valid"
        snapshot["has_registry_auth"] = has_registry_auth
    else:
        snapshot["registry_auth_status"] = (
            "invalid"
            if registry_auth_status == "valid"
            else registry_auth_status
        )
        snapshot["has_registry_auth"] = None

    ports = pod.get("ports")
    valid_ports = isinstance(ports, list) and all(
        isinstance(port, str) for port in ports
    )
    observed_ports_status = _safe_provider_status(
        pod.get("ports_status")
    )
    if observed_ports_status == "valid" and valid_ports:
        snapshot["ports_status"] = "valid"
        snapshot["port_count"] = len(ports)
    else:
        snapshot["ports_status"] = (
            "invalid"
            if observed_ports_status == "valid"
            else observed_ports_status
        )
        snapshot["port_count"] = None
    snapshot["ports_match_expected"] = bool(
        snapshot["ports_status"] == "valid"
        and sorted(ports) == sorted(expected["ports"])
    )
    port_mappings = pod.get("port_mappings")
    observed_port_mappings_status = _safe_provider_status(
        pod.get("port_mappings_status")
    )
    if (
        observed_port_mappings_status == "valid"
        and valid_ssh_port_mappings(port_mappings)
    ):
        snapshot["port_mappings_status"] = "valid"
        snapshot["port_mapping_count"] = len(port_mappings)
    else:
        snapshot["port_mappings_status"] = (
            "invalid"
            if observed_port_mappings_status == "valid"
            else observed_port_mappings_status
        )
        snapshot["port_mapping_count"] = None
    network_volume = pod.get("network_volume")
    observed_volume_status = _safe_provider_status(
        pod.get("network_volume_status")
    )
    if (
        observed_volume_status == "valid"
        and isinstance(network_volume, dict)
    ):
        snapshot["network_volume_status"] = "valid"
        snapshot["network_volume_present"] = True
    else:
        snapshot["network_volume_status"] = (
            "invalid"
            if observed_volume_status == "valid"
            else observed_volume_status
        )
        snapshot["network_volume_present"] = False
    snapshot["network_volume_size_gb"] = (
        _safe_provider_integer(network_volume.get("size_gb"))
        if snapshot["network_volume_status"] == "valid"
        else None
    )
    expected_volume_data_center = expected[
        "network_volume_data_center_id"
    ]
    snapshot["network_volume_data_center_id_matches_expected"] = (
        None
        if expected_volume_data_center is None
        else bool(
            snapshot["network_volume_status"] == "valid"
            and _strict_match(
                network_volume.get("data_center_id"),
                expected_volume_data_center,
            )
        )
    )
    snapshot["image_summary"] = string_summary(pod.get("image"))
    snapshot["image_matches_expected"] = _strict_match(
        pod.get("image"), expected["image"]
    )
    docker_entrypoint = pod.get("docker_entrypoint")
    entrypoint_summary = docker_arguments_summary(docker_entrypoint)
    observed_entrypoint_status = _safe_provider_status(
        pod.get("docker_entrypoint_status")
    )
    snapshot["docker_entrypoint_status"] = (
        "valid"
        if (
            observed_entrypoint_status == "valid"
            and entrypoint_summary["valid_string_array"]
        )
        else (
            "invalid"
            if observed_entrypoint_status == "valid"
            else observed_entrypoint_status
        )
    )
    snapshot["docker_entrypoint_summary"] = (
        entrypoint_summary
        if snapshot["docker_entrypoint_status"] == "valid"
        else docker_arguments_summary(None)
    )
    snapshot["docker_entrypoint_matches_expected"] = _strict_match(
        docker_entrypoint,
        expected["docker_entrypoint"],
    )
    docker_start_cmd = pod.get("docker_start_cmd")
    start_cmd_summary = docker_arguments_summary(docker_start_cmd)
    observed_start_cmd_status = _safe_provider_status(
        pod.get("docker_start_cmd_status")
    )
    snapshot["docker_start_cmd_status"] = (
        "valid"
        if (
            observed_start_cmd_status == "valid"
            and start_cmd_summary["valid_string_array"]
        )
        else (
            "invalid"
            if observed_start_cmd_status == "valid"
            else observed_start_cmd_status
        )
    )
    snapshot["docker_start_cmd_summary"] = (
        start_cmd_summary
        if snapshot["docker_start_cmd_status"] == "valid"
        else docker_arguments_summary(None)
    )
    snapshot["docker_start_cmd_matches_expected"] = _strict_match(
        docker_start_cmd,
        expected["docker_start_cmd"],
    )
    return validate_provider_pod_snapshot(snapshot)


def normalize_volume(volume: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": volume.get("id"),
        "name": volume.get("name"),
        "size_gb": volume.get("size"),
        "data_center_id": volume.get("dataCenterId"),
    }


def normalize_data_center(center: dict[str, Any]) -> dict[str, Any]:
    availability = center.get("gpuAvailability")
    if not isinstance(availability, list):
        availability = []
    normalized_availability = []
    for gpu in availability:
        if not isinstance(gpu, dict):
            raise RunpodLocalError(
                "Runpod data-center availability contains a non-object",
                code="invalid_provider_response",
            )
        normalized_availability.append(
            {
                "gpu_id": gpu.get("gpuTypeId"),
                "display_name": gpu.get("displayName"),
                "stock_status": gpu.get("stockStatus") or "None",
            }
        )
    normalized_availability.sort(key=lambda gpu: gpu["gpu_id"] or "")
    return {
        "data_center_id": center.get("id"),
        "name": center.get("name"),
        "location": center.get("location"),
        "gpu_availability": normalized_availability,
    }


class RunpodApi:
    def __init__(
        self,
        credential: ApiCredential,
        *,
        transport: JsonHttpTransport | None = None,
        rest_base: str = REST_BASE,
        graphql_url: str = GRAPHQL_URL,
    ) -> None:
        self.credential = credential
        self.transport = transport or JsonHttpTransport()
        self.rest_base = rest_base.rstrip("/")
        self.graphql_url = graphql_url
        self._account_ssh_attestation_authority = object()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.credential.token}"}

    def _rest(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        payload: Any | None = None,
        expected_statuses: tuple[int, ...] = (200,),
        allowed_error_responses: frozenset[tuple[int, str]] = frozenset(),
    ) -> Any:
        url = f"{self.rest_base}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        return self.transport.request_json(
            method,
            url,
            headers=self._headers(),
            payload=payload,
            expected_statuses=expected_statuses,
            allowed_error_responses=allowed_error_responses,
        )

    def _graphql(
        self,
        query: str,
        *,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query}
        if variables is not None:
            payload["variables"] = variables
        value = self.transport.request_json(
            "POST",
            self.graphql_url,
            headers=self._headers(),
            payload=payload,
        )
        if not isinstance(value, dict):
            raise RunpodLocalError(
                "Runpod GraphQL response was not an object",
                code="invalid_provider_response",
            )
        if value.get("errors"):
            raise RunpodLocalError(
                "Runpod GraphQL returned one or more errors",
                code="provider_graphql_error",
            )
        data = value.get("data")
        if not isinstance(data, dict):
            raise RunpodLocalError(
                "Runpod GraphQL response has no data object",
                code="invalid_provider_response",
            )
        return data

    def list_pods(self) -> list[dict[str, Any]]:
        value = self._rest(
            "GET",
            "pods",
            query={"includeMachine": "true", "includeNetworkVolume": "true"},
        )
        if not isinstance(value, list) or not all(
            isinstance(pod, dict) for pod in value
        ):
            raise RunpodLocalError(
                "Runpod Pod list response was not an object list",
                code="invalid_provider_response",
            )
        return [normalize_pod(pod) for pod in value]

    def attest_account_ssh_key(
        self, public_key: str
    ) -> _AccountSshKeyAttestation:
        """Require the profile key among the account's startup SSH keys."""

        expected_identity = _ssh_public_key_identity(public_key)
        data = self._graphql(ACCOUNT_SSH_KEY_QUERY)
        myself = data.get("myself")
        if not isinstance(myself, dict):
            raise RunpodLocalError(
                "Runpod GraphQL account response has no myself object",
                code="invalid_provider_response",
            )
        account_public_keys = myself.get("pubKey")
        if account_public_keys is not None and not isinstance(
            account_public_keys, str
        ):
            raise RunpodLocalError(
                "Runpod GraphQL account response has an invalid pubKey",
                code="invalid_provider_response",
            )
        for line in (
            account_public_keys.splitlines()
            if isinstance(account_public_keys, str)
            else ()
        ):
            if not line:
                continue
            try:
                account_identity = _ssh_public_key_identity(line)
            except RunpodLocalError:
                continue
            if account_identity == expected_identity:
                return _AccountSshKeyAttestation(
                    authority=self._account_ssh_attestation_authority,
                    key_identity=expected_identity,
                )
        raise RunpodLocalError(
            "Runpod account SSH Public Keys do not include the profile's "
            "dedicated key. Add the configured .pub key to the SSH Public "
            "Keys field in Runpod account settings before retrying; no Pod "
            "create request was sent.",
            code="account_ssh_key_not_authorized",
        )

    def _consume_account_ssh_key_attestation(
        self,
        payload: dict[str, Any],
        attestation: Any,
    ) -> None:
        if (
            not isinstance(attestation, _AccountSshKeyAttestation)
            or attestation.authority
            is not self._account_ssh_attestation_authority
            or attestation.consumed
        ):
            raise RunpodLocalError(
                "Pod creation requires a fresh Runpod account SSH-key "
                "attestation",
                code="account_ssh_attestation_required",
            )
        environment = payload.get("env") if isinstance(payload, dict) else None
        public_key = (
            environment.get("SSH_PUBLIC_KEY")
            if isinstance(environment, dict)
            else None
        )
        try:
            payload_identity = _ssh_public_key_identity(public_key)
        except RunpodLocalError as error:
            raise RunpodLocalError(
                "Pod creation payload has no valid SSH_PUBLIC_KEY identity",
                code="invalid_pod_payload",
            ) from error
        if payload_identity != attestation.key_identity:
            raise RunpodLocalError(
                "Pod SSH_PUBLIC_KEY identity does not match its Runpod "
                "account attestation",
                code="account_ssh_attestation_mismatch",
            )
        attestation.consumed = True

    def _get_pod_policy_attestation(self, pod_id: str) -> dict[str, Any]:
        data = self._graphql(POD_POLICY_QUERY % pod_id)
        value = data.get("pod")
        if not isinstance(value, dict):
            raise RunpodLocalError(
                "Runpod GraphQL Pod policy response has no Pod object",
                code="invalid_provider_response",
            )
        if value.get("id") != pod_id:
            raise RunpodLocalError(
                "Runpod GraphQL Pod policy response ID did not match the "
                "requested Pod",
                code="invalid_provider_response",
            )
        gpu_count = value.get("gpuCount")
        if (
            not isinstance(gpu_count, int)
            or isinstance(gpu_count, bool)
            or gpu_count <= 0
        ):
            raise RunpodLocalError(
                "Runpod GraphQL Pod policy response has invalid gpuCount",
                code="invalid_provider_response",
            )
        locked = value.get("locked")
        if not isinstance(locked, bool):
            raise RunpodLocalError(
                "Runpod GraphQL Pod policy response has invalid locked",
                code="invalid_provider_response",
            )
        pod_type = value.get("podType")
        if (
            not isinstance(pod_type, str)
            or pod_type not in POD_TYPE_INTERRUPTIBLE
        ):
            raise RunpodLocalError(
                "Runpod GraphQL Pod policy response has unsupported podType",
                code="invalid_provider_response",
            )
        return {
            "gpuCount": gpu_count,
            "locked": locked,
            "interruptible": POD_TYPE_INTERRUPTIBLE[pod_type],
        }

    def get_pod(self, pod_id: str) -> dict[str, Any]:
        pod_id = _provider_id(pod_id, label="Pod ID")
        value = self._rest(
            "GET",
            f"pods/{urllib.parse.quote(pod_id, safe='')}",
            query={"includeMachine": "true", "includeNetworkVolume": "true"},
        )
        if not isinstance(value, dict):
            raise RunpodLocalError(
                "Runpod Pod response was not an object",
                code="invalid_provider_response",
            )
        if value.get("id") != pod_id:
            raise RunpodLocalError(
                "Runpod REST Pod response ID did not match the requested Pod",
                code="invalid_provider_response",
            )
        policy = self._get_pod_policy_attestation(pod_id)
        rest_gpu_count = normalize_pod(value)["gpu_count"]
        if rest_gpu_count is not None and (
            not isinstance(rest_gpu_count, int)
            or isinstance(rest_gpu_count, bool)
            or rest_gpu_count != policy["gpuCount"]
        ):
            raise RunpodLocalError(
                "Runpod REST and GraphQL Pod GPU counts did not match",
                code="invalid_provider_response",
            )
        for field in ("locked", "interruptible"):
            rest_policy = value.get(field)
            if rest_policy is not None and (
                not isinstance(rest_policy, bool)
                or rest_policy is not policy[field]
            ):
                raise RunpodLocalError(
                    "Runpod REST and GraphQL Pod policy did not match",
                    code="invalid_provider_response",
                )
        merged = dict(value)
        merged.update(policy)
        return normalize_pod(merged)

    def create_pod(
        self,
        payload: dict[str, Any],
        *,
        account_ssh_attestation: Any = None,
    ) -> dict[str, Any]:
        graphql_input = _pod_graphql_input(payload)
        self._consume_account_ssh_key_attestation(
            payload, account_ssh_attestation
        )
        data = self._graphql(
            CREATE_POD_MUTATION,
            variables={"input": graphql_input},
        )
        value = data.get("podFindAndDeployOnDemand")
        if not isinstance(value, dict):
            raise RunpodLocalError(
                "Runpod GraphQL Pod creation response has no Pod object",
                code="invalid_provider_response",
            )
        pod_id = value.get("id")
        if not isinstance(pod_id, str):
            raise RunpodLocalError(
                "Runpod GraphQL Pod creation response has no Pod ID",
                code="invalid_provider_response",
            )
        try:
            _provider_id(pod_id, label="Pod ID")
        except RunpodLocalError as error:
            raise RunpodLocalError(
                "Runpod GraphQL Pod creation response has an invalid Pod ID",
                code="invalid_provider_response",
            ) from error
        if value.get("name") != payload["name"]:
            raise RunpodLocalError(
                "Runpod GraphQL Pod creation response name did not match "
                "the requested Pod",
                code="invalid_provider_response",
            )
        return normalize_pod(value)

    def start_pod(self, pod_id: str) -> None:
        pod_id = _provider_id(pod_id, label="Pod ID")
        self._rest(
            "POST",
            f"pods/{urllib.parse.quote(pod_id, safe='')}/start",
            expected_statuses=(200,),
        )

    def stop_pod(self, pod_id: str) -> None:
        pod_id = _provider_id(pod_id, label="Pod ID")
        self._rest(
            "POST",
            f"pods/{urllib.parse.quote(pod_id, safe='')}/stop",
            expected_statuses=(200,),
        )

    def delete_pod(self, pod_id: str) -> None:
        pod_id = _provider_id(pod_id, label="Pod ID")
        self._rest(
            "DELETE",
            f"pods/{urllib.parse.quote(pod_id, safe='')}",
            expected_statuses=(204,),
        )

    def list_network_volumes(self) -> list[dict[str, Any]]:
        value = self._rest("GET", "networkvolumes")
        if not isinstance(value, list) or not all(
            isinstance(volume, dict) for volume in value
        ):
            raise RunpodLocalError(
                "Runpod network-volume response was not an object list",
                code="invalid_provider_response",
            )
        return [normalize_volume(volume) for volume in value]

    def get_network_volume(self, volume_id: str) -> dict[str, Any]:
        volume_id = _provider_id(volume_id, label="network volume ID")
        value = self._rest(
            "GET", f"networkvolumes/{urllib.parse.quote(volume_id, safe='')}"
        )
        if not isinstance(value, dict):
            raise RunpodLocalError(
                "Runpod network-volume response was not an object",
                code="invalid_provider_response",
            )
        return normalize_volume(value)

    def create_network_volume(
        self, *, name: str, size_gb: int, data_center_id: str
    ) -> dict[str, Any]:
        _provider_id(data_center_id, label="data center ID")
        value = self._rest(
            "POST",
            "networkvolumes",
            payload={
                "name": name,
                "size": size_gb,
                "dataCenterId": data_center_id,
            },
            expected_statuses=(201,),
        )
        if not isinstance(value, dict):
            raise RunpodLocalError(
                "Runpod network-volume creation response was not an object",
                code="invalid_provider_response",
            )
        return normalize_volume(value)

    def list_templates(self) -> list[dict[str, Any]]:
        value = self._rest("GET", "templates")
        if not isinstance(value, list) or not all(
            isinstance(template, dict) for template in value
        ):
            raise RunpodLocalError(
                "Runpod template response was not an object list",
                code="invalid_provider_response",
            )
        return [normalize_template(template) for template in value]

    def get_template(self, template_id: str) -> dict[str, Any]:
        template_id = _provider_id(template_id, label="template ID")
        value = self._rest(
            "GET",
            f"templates/{urllib.parse.quote(template_id, safe='')}",
        )
        if not isinstance(value, dict):
            raise RunpodLocalError(
                "Runpod template response was not an object",
                code="invalid_provider_response",
            )
        normalized = normalize_template(value)
        if normalized["id"] != template_id:
            raise RunpodLocalError(
                "Runpod template response ID did not match the request",
                code="invalid_provider_response",
            )
        return normalized

    def create_template(
        self,
        contract: dict[str, Any],
    ) -> dict[str, Any]:
        value = self._rest(
            "POST",
            "templates",
            payload=template_create_payload(contract),
            expected_statuses=(200, 201),
        )
        if not isinstance(value, dict):
            raise RunpodLocalError(
                "Runpod template creation response was not an object",
                code="invalid_provider_response",
            )
        return normalize_template(value)

    def stock(
        self,
        *,
        gpu_count: int = 1,
        secure_cloud: bool = True,
        include_data_centers: bool = False,
    ) -> dict[str, Any]:
        if gpu_count <= 0:
            raise RunpodLocalError(
                "GPU count must be positive",
                code="invalid_gpu_count",
            )
        query = GPU_TYPES_QUERY % (
            gpu_count,
            "true" if secure_cloud else "false",
        )
        data = self._graphql(query)
        raw_gpus = data.get("gpuTypes")
        if not isinstance(raw_gpus, list):
            raise RunpodLocalError(
                "Runpod GraphQL response has no GPU type list",
                code="invalid_provider_response",
            )
        gpus = []
        for gpu in raw_gpus:
            if not isinstance(gpu, dict):
                raise RunpodLocalError(
                    "Runpod GPU type list contains a non-object",
                    code="invalid_provider_response",
                )
            lowest = (
                gpu.get("lowestPrice")
                if isinstance(gpu.get("lowestPrice"), dict)
                else {}
            )
            available_counts = lowest.get("availableGpuCounts")
            if not isinstance(available_counts, list):
                available_counts = []
            gpus.append(
                {
                    "gpu_id": gpu.get("id"),
                    "display_name": gpu.get("displayName"),
                    "memory_gb": gpu.get("memoryInGb"),
                    "secure_cloud": gpu.get("secureCloud"),
                    "community_cloud": gpu.get("communityCloud"),
                    "stock_status": lowest.get("stockStatus") or "None",
                    "on_demand_price_per_gpu_hour": _numeric(
                        lowest.get("uninterruptablePrice")
                    ),
                    "available_gpu_counts": [
                        count
                        for count in available_counts
                        if isinstance(count, int) and count > 0
                    ],
                }
            )
        gpus.sort(
            key=lambda gpu: (
                gpu["memory_gb"]
                if isinstance(gpu["memory_gb"], (int, float))
                else 0,
                gpu["gpu_id"] or "",
            )
        )
        data_centers: list[dict[str, Any]] | None = None
        if include_data_centers:
            center_data = self._graphql(DATA_CENTERS_QUERY)
            raw_centers = center_data.get("dataCenters")
            if not isinstance(raw_centers, list) or not all(
                isinstance(center, dict) for center in raw_centers
            ):
                raise RunpodLocalError(
                    "Runpod GraphQL response has no data-center list",
                    code="invalid_provider_response",
                )
            data_centers = [
                normalize_data_center(center) for center in raw_centers
            ]
            data_centers.sort(
                key=lambda center: center["data_center_id"] or ""
            )
        return {
            "schema_version": "runpod.stock.v1",
            "generated_at": utc_timestamp(),
            "cloud_type": "SECURE" if secure_cloud else "COMMUNITY",
            "gpu_count": gpu_count,
            "gpus": gpus,
            "data_centers": data_centers,
        }
