"""Direct Runpod REST and GraphQL provider client."""

from __future__ import annotations

import math
import urllib.parse
from typing import Any

from .auth import ApiCredential
from .errors import RunpodLocalError
from .http import JsonHttpTransport
from .timeutil import utc_timestamp


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


def _provider_id(value: str, *, label: str) -> str:
    if (
        not value
        or len(value) > 191
        or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in value)
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
        "gpu_count": gpu.get("count"),
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
        )

    def _graphql(self, query: str) -> dict[str, Any]:
        value = self.transport.request_json(
            "POST",
            self.graphql_url,
            headers=self._headers(),
            payload={"query": query},
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
        return normalize_pod(value)

    def create_pod(self, payload: dict[str, Any]) -> dict[str, Any]:
        value = self._rest(
            "POST", "pods", payload=payload, expected_statuses=(201,)
        )
        if not isinstance(value, dict):
            raise RunpodLocalError(
                "Runpod Pod creation response was not an object",
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
