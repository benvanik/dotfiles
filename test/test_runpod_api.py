from __future__ import annotations

import io
import math
import urllib.error
import unittest

from runpod_local.api import RunpodApi, gpu_stock_is_available, normalize_pod
from runpod_local.auth import ApiCredential
from runpod_local.errors import HttpRequestError, RunpodLocalError
from runpod_local.http import JsonHttpTransport
from runpod_local.provider_cli import standard_volume_monthly_usd


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def request_json(
        self,
        method,
        url,
        *,
        headers=None,
        payload=None,
        expected_statuses=(200,),
        allowed_error_responses=frozenset(),
    ):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "payload": payload,
                "expected_statuses": expected_statuses,
                "allowed_error_responses": allowed_error_responses,
            }
        )
        if not self.responses:
            raise AssertionError("no fake response remains")
        return self.responses.pop(0)


def api_with_responses(*responses):
    transport = FakeTransport(*responses)
    api = RunpodApi(
        ApiCredential("fixture-runpod-token", source="test"),
        transport=transport,
        rest_base="https://rest.example.invalid/v1",
        graphql_url="https://graphql.example.invalid/query",
    )
    return api, transport


class RunpodApiTest(unittest.TestCase):
    def test_standard_volume_monthly_estimate_uses_documented_tiers(self):
        self.assertEqual(standard_volume_monthly_usd(250), 17.50)
        self.assertEqual(standard_volume_monthly_usd(1000), 70.00)
        self.assertEqual(standard_volume_monthly_usd(1250), 82.50)

    def test_pod_list_is_normalized_and_drops_remote_environment(self):
        raw_pod = {
            "id": "pod123",
            "name": "fixture",
            "desiredStatus": "RUNNING",
            "image": "example/image:tag",
            "adjustedCostPerHr": 1.99,
            "env": {"SENSITIVE_VALUE": "must-not-escape"},
            "gpu": {
                "id": "NVIDIA RTX PRO 6000 Blackwell Server Edition",
                "count": 1,
            },
            "machine": {"dataCenterId": "US-KS-2"},
            "networkVolume": {"id": "volume123"},
            "publicIp": "192.0.2.10",
            "portMappings": {"22": 22022},
        }
        api, transport = api_with_responses([raw_pod])
        pods = api.list_pods()

        self.assertEqual(pods[0]["id"], "pod123")
        self.assertEqual(pods[0]["cost_per_hour"], 1.99)
        self.assertEqual(pods[0]["network_volume_id"], "volume123")
        self.assertNotIn("raw", pods[0])
        self.assertNotIn("must-not-escape", repr(pods))
        request = transport.requests[0]
        self.assertEqual(
            request["headers"]["Authorization"], "Bearer fixture-runpod-token"
        )
        self.assertNotIn("fixture-runpod-token", request["url"])
        self.assertIn("includeNetworkVolume=true", request["url"])

    def test_pod_cost_falls_back_and_rejects_nonfinite_values(self):
        fallback = normalize_pod(
            {"adjustedCostPerHr": None, "costPerHr": "1.99"}
        )
        nonfinite = normalize_pod(
            {"adjustedCostPerHr": math.inf, "costPerHr": "nan"}
        )
        negative = normalize_pod(
            {"adjustedCostPerHr": -1, "costPerHr": "-2"}
        )

        self.assertEqual(fallback["cost_per_hour"], 1.99)
        self.assertIsNone(nonfinite["cost_per_hour"])
        self.assertIsNone(negative["cost_per_hour"])

    def test_create_volume_uses_exact_rest_contract(self):
        response = {
            "id": "volume123",
            "name": "model-cache",
            "size": 500,
            "dataCenterId": "US-KS-2",
        }
        api, transport = api_with_responses(response)
        result = api.create_network_volume(
            name="model-cache", size_gb=500, data_center_id="US-KS-2"
        )
        self.assertEqual(
            result,
            {
                "id": "volume123",
                "name": "model-cache",
                "size_gb": 500,
                "data_center_id": "US-KS-2",
            },
        )
        request = transport.requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["expected_statuses"], (201,))
        self.assertEqual(
            request["payload"],
            {
                "name": "model-cache",
                "size": 500,
                "dataCenterId": "US-KS-2",
            },
        )

    def test_create_pod_allowlists_only_definitive_capacity_error(self):
        api, transport = api_with_responses(
            {
                "id": "pod123",
                "name": "fixture",
                "gpu": {"id": "NVIDIA H200", "count": 1},
            }
        )

        pod = api.create_pod({"name": "fixture"})

        self.assertEqual(pod["id"], "pod123")
        request = transport.requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["expected_statuses"], (201,))
        self.assertEqual(
            request["allowed_error_responses"],
            frozenset(
                {
                    (
                        500,
                        "create pod: There are no instances currently available",
                    )
                }
            ),
        )

    def test_create_pod_accepts_exact_live_capacity_response(self):
        safe_message = (
            "create pod: There are no instances currently available"
        )

        def failing_open(request, *, timeout):
            raise urllib.error.HTTPError(
                request.full_url,
                500,
                "Internal Server Error",
                {},
                io.BytesIO(
                    (
                        '{"error":"'
                        + safe_message
                        + '","status":500}'
                    ).encode("utf-8")
                ),
            )

        api = RunpodApi(
            ApiCredential("fixture-runpod-token", source="test"),
            transport=JsonHttpTransport(opener=failing_open),
            rest_base="https://rest.example.invalid/v1",
        )

        with self.assertRaises(HttpRequestError) as caught:
            api.create_pod({"name": "fixture"})

        self.assertEqual(caught.exception.status, 500)
        self.assertEqual(caught.exception.provider_error, safe_message)

    def test_stock_uses_header_authenticated_graphql_without_query_key(self):
        response = {
            "data": {
                "gpuTypes": [
                    {
                        "id": "NVIDIA B300 SXM6 AC",
                        "displayName": "B300",
                        "memoryInGb": 288,
                        "secureCloud": True,
                        "communityCloud": False,
                        "lowestPrice": {
                            "stockStatus": "Low",
                            "uninterruptablePrice": 7.39,
                            "availableGpuCounts": [1, 2],
                        },
                    }
                ]
            }
        }
        api, transport = api_with_responses(response)
        result = api.stock(gpu_count=1)

        gpu = result["gpus"][0]
        self.assertEqual(gpu["gpu_id"], "NVIDIA B300 SXM6 AC")
        self.assertEqual(gpu["on_demand_price_per_gpu_hour"], 7.39)
        request = transport.requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(
            request["url"], "https://graphql.example.invalid/query"
        )
        self.assertNotIn("api_key", request["url"])
        self.assertEqual(
            request["headers"]["Authorization"], "Bearer fixture-runpod-token"
        )
        self.assertIn("gpuCount: 1", request["payload"]["query"])

    def test_data_center_stock_is_normalized(self):
        gpu_response = {"data": {"gpuTypes": []}}
        center_response = {
            "data": {
                "dataCenters": [
                    {
                        "id": "US-NC-2",
                        "name": "North Carolina",
                        "location": "US",
                        "gpuAvailability": [
                            {
                                "gpuTypeId": "NVIDIA B200",
                                "displayName": "B200",
                                "stockStatus": None,
                            }
                        ],
                    }
                ]
            }
        }
        api, _ = api_with_responses(gpu_response, center_response)

        centers = api.stock(include_data_centers=True)["data_centers"]

        self.assertEqual(centers[0]["data_center_id"], "US-NC-2")
        self.assertEqual(
            centers[0]["gpu_availability"][0]["stock_status"], "None"
        )

    def test_stock_status_remains_usable_when_count_hints_are_empty(self):
        api, _ = api_with_responses(
            {
                "data": {
                    "gpuTypes": [
                        {
                            "id": "NVIDIA H200",
                            "displayName": "H200",
                            "memoryInGb": 141,
                            "secureCloud": True,
                            "communityCloud": False,
                            "lowestPrice": {
                                "stockStatus": "High",
                                "uninterruptablePrice": 4.39,
                                "availableGpuCounts": [],
                            },
                        },
                        {
                            "id": "NVIDIA H200 NVL",
                            "displayName": "H200 NVL",
                            "memoryInGb": 143,
                            "secureCloud": True,
                            "communityCloud": False,
                            "lowestPrice": {
                                "stockStatus": None,
                                "uninterruptablePrice": None,
                                "availableGpuCounts": [],
                            },
                        },
                    ]
                }
            }
        )
        gpus = api.stock(gpu_count=1)["gpus"]

        self.assertTrue(gpu_stock_is_available(gpus[0], gpu_count=1))
        self.assertEqual(gpus[1]["stock_status"], "None")
        self.assertFalse(gpu_stock_is_available(gpus[1], gpu_count=1))

    def test_graphql_errors_fail_loud(self):
        api, _ = api_with_responses(
            {"errors": [{"message": "fixture failure"}], "data": None}
        )
        with self.assertRaises(RunpodLocalError) as caught:
            api.stock()
        self.assertEqual(caught.exception.code, "provider_graphql_error")

    def test_delete_pod_requires_204(self):
        api, transport = api_with_responses(None)
        api.delete_pod("pod123")
        self.assertEqual(transport.requests[0]["method"], "DELETE")
        self.assertEqual(transport.requests[0]["expected_statuses"], (204,))


if __name__ == "__main__":
    unittest.main()
