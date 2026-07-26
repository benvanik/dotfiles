from __future__ import annotations

import unittest

from runpod_local.api import RunpodApi
from runpod_local.auth import ApiCredential
from runpod_local.errors import RunpodLocalError


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
    ):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "payload": payload,
                "expected_statuses": expected_statuses,
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
        self.assertEqual(result, response)
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
