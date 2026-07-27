"""Direct Runpod REST and GraphQL provider client."""

from __future__ import annotations

import math
import urllib.parse
from typing import Any

from .auth import ApiCredential
from .errors import RunpodLocalError
from .http import JsonHttpTransport
from .profile import validate_ssh_public_key
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
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    if isinstance(value, str):
        try:
            result = float(value)
        except ValueError:
            return None
        return result if math.isfinite(result) and result >= 0 else None
    return None


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
    gpu = pod.get("gpu") if isinstance(pod.get("gpu"), dict) else {}
    gpu_count = gpu.get("count")
    if gpu_count is None:
        gpu_count = pod.get("gpuCount")
    machine = (
        pod.get("machine") if isinstance(pod.get("machine"), dict) else {}
    )
    network_volume = (
        pod.get("networkVolume")
        if isinstance(pod.get("networkVolume"), dict)
        else None
    )
    port_mappings = (
        pod.get("portMappings")
        if isinstance(pod.get("portMappings"), dict)
        else {}
    )
    adjusted_cost = _numeric(pod.get("adjustedCostPerHr"))
    cost_per_hour = (
        adjusted_cost
        if adjusted_cost is not None
        else _numeric(pod.get("costPerHr"))
    )
    return {
        "id": pod.get("id"),
        "name": pod.get("name"),
        "desired_status": pod.get("desiredStatus"),
        "image": pod.get("image", pod.get("imageName")),
        "template_id": pod.get("templateId"),
        "interruptible": pod.get("interruptible"),
        "locked": pod.get("locked"),
        "gpu_id": gpu.get("id", machine.get("gpuTypeId")),
        "gpu_count": gpu_count,
        "cost_per_hour": cost_per_hour,
        "data_center_id": machine.get("dataCenterId"),
        "secure_cloud": machine.get("secureCloud"),
        "machine_id": pod.get("machineId"),
        "network_volume_id": (
            network_volume.get("id") if network_volume is not None else None
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
        "port_mappings": port_mappings,
        "ports": pod.get("ports") if isinstance(pod.get("ports"), list) else [],
    }


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
        return value

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
