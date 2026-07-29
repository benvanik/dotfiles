from __future__ import annotations

import math
import unittest

from runpod_local.api import (
    GRAPHQL_NO_CAPACITY_ERROR_CODE,
    NO_INSTANCES_AVAILABLE_GRAPHQL_ERROR,
    RunpodApi,
    gpu_stock_is_available,
    normalize_pod,
    provider_pod_snapshot,
)
from runpod_local.auth import ApiCredential
from runpod_local.errors import RunpodLocalError
from runpod_local.provider_cli import standard_volume_monthly_usd
from runpod_local.template import (
    build_private_template_contract,
    environment_summary,
    template_contract_violations,
    validate_private_template_contract,
)

SSH_PUBLIC_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f "
    "fixture@example"
)
OTHER_SSH_PUBLIC_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIEJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJC "
    "other@example"
)
IMAGE = "vllm/vllm-openai@sha256:" + "1" * 64


def raw_template(**overrides):
    value = {
        "id": "template123",
        "name": "upstream-vllm",
        "imageName": IMAGE,
        "category": "NVIDIA",
        "containerDiskInGb": 50,
        "containerRegistryAuthId": None,
        "dockerEntrypoint": ["/bin/bash", "-c"],
        "dockerStartCmd": ["exec /usr/sbin/sshd -D -e\n"],
        "env": {},
        "isPublic": False,
        "isServerless": False,
        "ports": ["22/tcp"],
        "volumeInGb": 0,
        "volumeMountPath": "/workspace",
    }
    value.update(overrides)
    return value


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


def account_ssh_key_response(public_keys=SSH_PUBLIC_KEY):
    return {"data": {"myself": {"pubKey": public_keys}}}


def pod_create_payload(public_key=SSH_PUBLIC_KEY, **overrides):
    payload = {
        "name": "fixture",
        "cloudType": "SECURE",
        "computeType": "GPU",
        "gpuTypeIds": ["NVIDIA H200"],
        "gpuTypePriority": "custom",
        "gpuCount": 2,
        "containerDiskInGb": 50,
        "volumeMountPath": "/workspace",
        "ports": ["22/tcp"],
        "env": {
            "SSH_PUBLIC_KEY": public_key,
            "HF_HOME": "/workspace/.cache/huggingface",
        },
        "interruptible": False,
        "locked": False,
        "minVCPUPerGPU": 8,
        "minRAMPerGPU": 32,
        "allowedCudaVersions": ["12.8"],
        "imageName": "runpod/pytorch:fixture",
        "networkVolumeId": "volume123",
        "dataCenterId": "US-NC-2",
        "terminateAfter": "2026-07-28T03:30:00Z",
    }
    payload.update(overrides)
    return payload


def pod_create_response(*, pod_id="pod123", name="fixture"):
    return {
        "data": {
            "podFindAndDeployOnDemand": {
                "id": pod_id,
                "name": name,
            }
        }
    }


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
            "containerRegistryAuthId": None,
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
        self.assertEqual(pods[0]["cost_status"], "valid")
        self.assertEqual(pods[0]["network_volume_id"], "volume123")
        self.assertEqual(
            pods[0]["environment_names"],
            ["SENSITIVE_VALUE"],
        )
        self.assertEqual(
            pods[0]["environment_sha256"],
            environment_summary(
                {"SENSITIVE_VALUE": "must-not-escape"}
            )["environment_sha256"],
        )
        self.assertEqual(pods[0]["environment_status"], "valid")
        self.assertEqual(pods[0]["registry_auth_status"], "valid")
        self.assertIs(pods[0]["has_registry_auth"], False)
        self.assertNotIn("raw", pods[0])
        self.assertNotIn("must-not-escape", repr(pods))
        request = transport.requests[0]
        self.assertEqual(
            request["headers"]["Authorization"], "Bearer fixture-runpod-token"
        )
        self.assertNotIn("fixture-runpod-token", request["url"])
        self.assertIn("includeNetworkVolume=true", request["url"])
        self.assertEqual(len(transport.requests), 1)

    def test_pod_gpu_count_falls_back_to_top_level_rest_field(self):
        top_level = normalize_pod(
            {
                "gpu": {"id": "NVIDIA H200"},
                "gpuCount": 2,
            }
        )
        nested = normalize_pod(
            {
                "gpu": {"id": "NVIDIA H200", "count": 1},
                "gpuCount": 2,
            }
        )

        self.assertEqual(top_level["gpu_count"], 2)
        self.assertEqual(nested["gpu_count"], 1)

    def test_pod_cost_distinguishes_missing_valid_and_malformed_values(self):
        missing = normalize_pod({})
        fallback = normalize_pod(
            {
                "adjustedCostPerHr": None,
                "costPerHr": "1.99",
            }
        )
        secret = "PROVIDER_COST_SECRET"
        malformed_adjusted = normalize_pod(
            {
                "adjustedCostPerHr": secret,
                "costPerHr": 1.99,
            }
        )
        malformed_base = normalize_pod({"costPerHr": secret})
        huge_integer = normalize_pod(
            {"adjustedCostPerHr": 10**4000}
        )

        self.assertEqual(missing["cost_status"], "missing")
        self.assertIsNone(missing["cost_per_hour"])
        self.assertEqual(fallback["cost_status"], "valid")
        self.assertEqual(fallback["cost_per_hour"], 1.99)
        self.assertEqual(malformed_adjusted["cost_status"], "invalid")
        self.assertIsNone(malformed_adjusted["cost_per_hour"])
        self.assertEqual(malformed_base["cost_status"], "invalid")
        self.assertIsNone(malformed_base["cost_per_hour"])
        self.assertEqual(huge_integer["cost_status"], "invalid")
        self.assertIsNone(huge_integer["cost_per_hour"])
        self.assertNotIn(secret, repr(malformed_adjusted))
        self.assertNotIn(secret, repr(malformed_base))

    def test_pod_normalizes_exact_docker_overrides(self):
        pod = normalize_pod(
            {
                "dockerEntrypoint": ["/bin/bash", "-c"],
                "dockerStartCmd": ["exec /usr/sbin/sshd -D -e\n"],
            }
        )
        self.assertEqual(
            pod["docker_entrypoint"], ["/bin/bash", "-c"]
        )
        self.assertEqual(
            pod["docker_start_cmd"],
            ["exec /usr/sbin/sshd -D -e\n"],
        )

    def test_pod_normalizes_exact_storage_attestation(self):
        pod = normalize_pod(
            {
                "containerDiskInGb": 50,
                "volumeInGb": 0,
                "volumeMountPath": "/workspace",
            }
        )

        self.assertEqual(pod["container_disk_gb"], 50)
        self.assertEqual(pod["volume_in_gb"], 0)
        self.assertEqual(pod["volume_mount_path"], "/workspace")

    def test_pod_environment_hash_detects_same_name_value_mutation(self):
        expected = normalize_pod(
            {
                "env": {"SSH_PUBLIC_KEY": "expected-key"},
                "containerRegistryAuthId": None,
            }
        )
        mutated = normalize_pod(
            {
                "env": {"SSH_PUBLIC_KEY": "mutated-secret-value"},
                "containerRegistryAuthId": None,
            }
        )

        self.assertEqual(
            expected["environment_names"],
            mutated["environment_names"],
        )
        self.assertNotEqual(
            expected["environment_sha256"],
            mutated["environment_sha256"],
        )
        self.assertNotIn("mutated-secret-value", repr(mutated))

    def test_pod_registry_auth_accepts_omitted_null_or_empty_as_no_auth(self):
        absent = normalize_pod({"env": {}})
        none = normalize_pod(
            {"env": {}, "containerRegistryAuthId": None}
        )
        empty = normalize_pod(
            {"env": {}, "containerRegistryAuthId": ""}
        )
        configured = normalize_pod(
            {
                "env": {},
                "containerRegistryAuthId": "registry-secret-id",
            }
        )

        self.assertEqual(absent["registry_auth_status"], "valid")
        self.assertIs(absent["has_registry_auth"], False)
        self.assertEqual(none["registry_auth_status"], "valid")
        self.assertIs(none["has_registry_auth"], False)
        self.assertEqual(empty["registry_auth_status"], "valid")
        self.assertIs(empty["has_registry_auth"], False)
        self.assertEqual(configured["registry_auth_status"], "valid")
        self.assertIs(configured["has_registry_auth"], True)
        self.assertNotIn("registry-secret-id", repr(configured))

    def test_durable_provider_snapshot_never_contains_raw_runtime_bytes(self):
        secret = "PROVIDER_SECRET=must-not-persist"
        normalized = normalize_pod(
            {
                "image": "private/secret-image",
                "dockerEntrypoint": ["/bin/bash", "-c"],
                "dockerStartCmd": [secret],
                "env": {"INJECTED": secret},
                "containerRegistryAuthId": "secret-registry-id",
            }
        )

        snapshot = provider_pod_snapshot(
            normalized,
            expected={
                "id": None,
                "name": None,
                "desired_status": None,
                "template_id": None,
                "volume_mount_path": None,
                "environment_names": normalized["environment_names"],
                "environment_sha256": normalized[
                    "environment_sha256"
                ],
                "gpu_id": None,
                "data_center_id": None,
                "network_volume_id": None,
                "network_volume_data_center_id": None,
                "ports": [],
                "image": "private/secret-image",
                "docker_entrypoint": ["/bin/bash", "-c"],
                "docker_start_cmd": [secret],
            },
        )

        self.assertNotIn("image", snapshot)
        self.assertNotIn("docker_entrypoint", snapshot)
        self.assertNotIn("docker_start_cmd", snapshot)
        self.assertNotIn(secret, repr(snapshot))
        self.assertNotIn("secret-registry-id", repr(snapshot))
        self.assertTrue(snapshot["image_matches_expected"])
        self.assertTrue(snapshot["docker_start_cmd_matches_expected"])
        self.assertTrue(
            snapshot["docker_start_cmd_summary"]["valid_string_array"]
        )
        self.assertIs(snapshot["has_registry_auth"], True)

    def test_get_pod_merges_exact_graphql_policy_attestation(self):
        pod_types = {
            "RESERVED": False,
            "INTERRUPTABLE": True,
            "BID": True,
            "BACKGROUND": True,
        }
        for pod_type, interruptible in pod_types.items():
            with self.subTest(pod_type=pod_type):
                rest_pod = {
                    "id": "pod123",
                    "name": "fixture",
                    "gpu": {"id": "NVIDIA H200"},
                    "gpuCount": 1,
                }
                policy_response = {
                    "data": {
                        "pod": {
                            "id": "pod123",
                            "gpuCount": 1,
                            "locked": False,
                            "podType": pod_type,
                        }
                    }
                }
                api, transport = api_with_responses(
                    rest_pod, policy_response
                )

                pod = api.get_pod("pod123")

                self.assertEqual(pod["gpu_count"], 1)
                self.assertIs(pod["locked"], False)
                self.assertIs(pod["interruptible"], interruptible)
                self.assertEqual(len(transport.requests), 2)
                self.assertEqual(transport.requests[0]["method"], "GET")
                policy_request = transport.requests[1]
                self.assertEqual(policy_request["method"], "POST")
                self.assertEqual(
                    policy_request["url"],
                    "https://graphql.example.invalid/query",
                )
                query = policy_request["payload"]["query"]
                self.assertIn(
                    'pod(input: {podId: "pod123"})',
                    query,
                )
                self.assertIn("gpuCount", query)
                self.assertIn("locked", query)
                self.assertIn("podType", query)

    def test_get_pod_rejects_mismatched_provider_ids(self):
        valid_policy = {
            "data": {
                "pod": {
                    "id": "pod123",
                    "gpuCount": 1,
                    "locked": False,
                    "podType": "RESERVED",
                }
            }
        }
        cases = {
            "rest": (
                {"id": "other", "gpuCount": 1},
                valid_policy,
            ),
            "graphql": (
                {"id": "pod123", "gpuCount": 1},
                {
                    "data": {
                        "pod": {
                            "id": "other",
                            "gpuCount": 1,
                            "locked": False,
                            "podType": "RESERVED",
                        }
                    }
                },
            ),
        }
        for source, responses in cases.items():
            with self.subTest(source=source):
                api, _ = api_with_responses(*responses)

                with self.assertRaises(RunpodLocalError) as caught:
                    api.get_pod("pod123")

                self.assertEqual(
                    caught.exception.code,
                    "invalid_provider_response",
                )

    def test_get_pod_rejects_invalid_graphql_policy_shape(self):
        rest_pod = {"id": "pod123", "gpuCount": 1}
        for name, response in {
            "missing": {"data": {}},
            "null": {"data": {"pod": None}},
            "list": {"data": {"pod": []}},
        }.items():
            with self.subTest(name=name):
                api, _ = api_with_responses(rest_pod, response)

                with self.assertRaises(RunpodLocalError) as caught:
                    api.get_pod("pod123")

                self.assertEqual(
                    caught.exception.code,
                    "invalid_provider_response",
                )

    def test_get_pod_rejects_invalid_graphql_policy_types(self):
        rest_pod = {"id": "pod123", "gpuCount": 1}
        valid_policy = {
            "id": "pod123",
            "gpuCount": 1,
            "locked": False,
            "podType": "RESERVED",
        }
        cases = {
            "boolean_gpu_count": {"gpuCount": True},
            "string_gpu_count": {"gpuCount": "1"},
            "zero_gpu_count": {"gpuCount": 0},
            "integer_locked": {"locked": 0},
            "unknown_pod_type": {"podType": "FIXTURE"},
            "non_string_pod_type": {"podType": 1},
            "unhashable_pod_type": {"podType": []},
        }
        for name, replacement in cases.items():
            with self.subTest(name=name):
                policy = dict(valid_policy)
                policy.update(replacement)
                api, _ = api_with_responses(
                    rest_pod,
                    {"data": {"pod": policy}},
                )

                with self.assertRaises(RunpodLocalError) as caught:
                    api.get_pod("pod123")

                self.assertEqual(
                    caught.exception.code,
                    "invalid_provider_response",
                )

    def test_get_pod_rejects_rest_graphql_gpu_count_mismatch(self):
        api, _ = api_with_responses(
            {"id": "pod123", "gpuCount": 2},
            {
                "data": {
                    "pod": {
                        "id": "pod123",
                        "gpuCount": 1,
                        "locked": False,
                        "podType": "RESERVED",
                    }
                }
            },
        )

        with self.assertRaises(RunpodLocalError) as caught:
            api.get_pod("pod123")

        self.assertEqual(
            caught.exception.code,
            "invalid_provider_response",
        )

    def test_get_pod_rejects_rest_graphql_policy_mismatch(self):
        policy_response = {
            "data": {
                "pod": {
                    "id": "pod123",
                    "gpuCount": 1,
                    "locked": False,
                    "podType": "RESERVED",
                }
            }
        }
        cases = {
            "locked": {"locked": True},
            "interruptible": {"interruptible": True},
            "invalid_locked": {"locked": 0},
            "invalid_interruptible": {"interruptible": 0},
        }
        for name, rest_policy in cases.items():
            with self.subTest(name=name):
                rest_pod = {
                    "id": "pod123",
                    "gpuCount": 1,
                    **rest_policy,
                }
                api, _ = api_with_responses(rest_pod, policy_response)

                with self.assertRaises(RunpodLocalError) as caught:
                    api.get_pod("pod123")

                self.assertEqual(
                    caught.exception.code,
                    "invalid_provider_response",
                )

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

    def test_template_list_and_get_normalize_without_environment_values(self):
        provider = raw_template(
            env={"SENSITIVE_TOKEN": "must-not-escape"}
        )
        api, transport = api_with_responses([provider], provider)

        listed = api.list_templates()
        fetched = api.get_template("template123")

        self.assertEqual(listed, [fetched])
        self.assertEqual(fetched["image"], IMAGE)
        self.assertEqual(
            fetched["environment_names"], ["SENSITIVE_TOKEN"]
        )
        self.assertNotIn("must-not-escape", repr(listed))
        self.assertEqual(
            transport.requests[1]["url"],
            "https://rest.example.invalid/v1/templates/template123",
        )

    def test_template_normalizes_omitted_provider_zero_values(self):
        provider = raw_template()
        for field in ("env", "isPublic", "isServerless", "volumeInGb"):
            del provider[field]
        api, _ = api_with_responses(provider)

        fetched = api.get_template("template123")

        self.assertEqual(fetched["environment_names"], [])
        self.assertIs(fetched["is_public"], False)
        self.assertIs(fetched["is_serverless"], False)
        self.assertEqual(fetched["volume_in_gb"], 0)
        self.assertEqual(
            validate_private_template_contract(fetched, require_id=True),
            fetched,
        )

    def test_create_template_uses_exact_private_pod_contract(self):
        contract = build_private_template_contract(
            name="upstream-vllm",
            image=IMAGE,
            docker_entrypoint=["/bin/bash", "-c"],
            docker_start_cmd=["exec /usr/sbin/sshd -D -e\n"],
        )
        api, transport = api_with_responses(raw_template())

        created = api.create_template(contract)

        self.assertEqual(created["id"], "template123")
        self.assertEqual(
            transport.requests[0]["expected_statuses"],
            (200, 201),
        )
        self.assertEqual(
            transport.requests[0]["payload"],
            {
                "imageName": IMAGE,
                "name": "upstream-vllm",
                "category": "NVIDIA",
                "containerDiskInGb": 50,
                "dockerEntrypoint": ["/bin/bash", "-c"],
                "dockerStartCmd": ["exec /usr/sbin/sshd -D -e\n"],
                "env": {},
                "isPublic": False,
                "isServerless": False,
                "ports": ["22/tcp"],
                "readme": "",
                "volumeInGb": 0,
                "volumeMountPath": "/workspace",
            },
        )

    def test_template_contract_rejects_python_numeric_boolean_aliases(self):
        for value in (False, 0.0):
            with self.subTest(volume_in_gb=value):
                with self.assertRaises(RunpodLocalError) as caught:
                    build_private_template_contract(
                        name="upstream-vllm",
                        image=IMAGE,
                        docker_entrypoint=["/bin/bash", "-c"],
                        docker_start_cmd=["bootstrap\n"],
                        volume_in_gb=value,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "invalid_template_contract",
                )

        contract = build_private_template_contract(
            name="upstream-vllm",
            image=IMAGE,
            docker_entrypoint=["/bin/bash", "-c"],
            docker_start_cmd=["bootstrap\n"],
        )
        for field, value in (
            ("is_public", 0),
            ("is_serverless", 0),
            ("volume_in_gb", False),
        ):
            with self.subTest(field=field):
                drifted = dict(contract)
                drifted[field] = value
                with self.assertRaises(RunpodLocalError) as caught:
                    validate_private_template_contract(
                        drifted,
                        require_id=False,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "invalid_template_contract",
                )

    def test_template_contract_rejects_non_string_id_without_type_error(self):
        with self.assertRaises(RunpodLocalError) as caught:
            build_private_template_contract(
                name="upstream-vllm",
                image=IMAGE,
                docker_entrypoint=["/bin/bash", "-c"],
                docker_start_cmd=["bootstrap\n"],
                template_id=7,
            )
        self.assertEqual(
            caught.exception.code,
            "invalid_template_contract",
        )

    def test_template_drift_diagnostics_never_disclose_docker_arguments(self):
        expected = build_private_template_contract(
            name="upstream-vllm",
            image=IMAGE,
            docker_entrypoint=["/bin/bash", "-c"],
            docker_start_cmd=["approved-bootstrap\n"],
        )
        observed = {
            **expected,
            "id": "template123",
            "docker_start_cmd": ["SECRET=must-not-escape\n"],
        }

        violations = template_contract_violations(observed, expected)

        self.assertIn("docker_start_cmd: mismatch", violations)
        self.assertNotIn("must-not-escape", repr(violations))

    def test_create_pod_uses_exact_graphql_variable_contract(self):
        api, transport = api_with_responses(
            account_ssh_key_response(),
            pod_create_response(),
        )
        attestation = api.attest_account_ssh_key(SSH_PUBLIC_KEY)

        pod = api.create_pod(
            pod_create_payload(),
            account_ssh_attestation=attestation,
        )

        self.assertEqual(pod["id"], "pod123")
        self.assertEqual(pod["name"], "fixture")
        request = transport.requests[1]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(
            request["url"], "https://graphql.example.invalid/query"
        )
        self.assertEqual(request["expected_statuses"], (200,))
        self.assertEqual(
            request["allowed_error_responses"], frozenset()
        )
        query = request["payload"]["query"]
        self.assertIn(
            "podFindAndDeployOnDemand(input: $input)", query
        )
        self.assertNotIn(SSH_PUBLIC_KEY, query)
        self.assertNotIn("NVIDIA H200", query)
        self.assertNotIn("2026-07-28T03:30:00Z", query)
        self.assertEqual(
            request["payload"]["variables"],
            {
                "input": {
                    "name": "fixture",
                    "cloudType": "SECURE",
                    "computeType": "GPU",
                    "gpuTypeId": "NVIDIA H200",
                    "gpuCount": 2,
                    "containerDiskInGb": 50,
                    "volumeMountPath": "/workspace",
                    "ports": "22/tcp",
                    "env": [
                        {
                            "key": "HF_HOME",
                            "value": "/workspace/.cache/huggingface",
                        },
                        {
                            "key": "SSH_PUBLIC_KEY",
                            "value": SSH_PUBLIC_KEY,
                        },
                    ],
                    "startSsh": True,
                    "minVcpuCount": 16,
                    "minMemoryInGb": 64,
                    "terminateAfter": "2026-07-28T03:30:00Z",
                    "imageName": "runpod/pytorch:fixture",
                    "networkVolumeId": "volume123",
                    "dataCenterId": "US-NC-2",
                    "allowedCudaVersions": ["12.8"],
                }
            },
        )

    def test_create_pod_maps_template_and_ephemeral_volume(self):
        payload = pod_create_payload()
        del payload["imageName"]
        del payload["networkVolumeId"]
        payload["templateId"] = "template123"
        payload["volumeInGb"] = 20
        api, transport = api_with_responses(
            account_ssh_key_response(),
            pod_create_response(),
        )
        attestation = api.attest_account_ssh_key(SSH_PUBLIC_KEY)

        api.create_pod(
            payload,
            account_ssh_attestation=attestation,
        )

        graphql_input = transport.requests[1]["payload"]["variables"]["input"]
        self.assertEqual(graphql_input["templateId"], "template123")
        self.assertEqual(graphql_input["volumeInGb"], 20)
        self.assertEqual(
            graphql_input["terminateAfter"], "2026-07-28T03:30:00Z"
        )
        self.assertNotIn("imageName", graphql_input)
        self.assertNotIn("networkVolumeId", graphql_input)

    def test_create_pod_requires_absolute_utc_termination_deadline(self):
        invalid_values = (
            None,
            "30m",
            "2026-07-28T03:30:00-07:00",
            "2026-07-28T03:30:00.000Z",
            "2026-02-30T03:30:00Z",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                api, transport = api_with_responses(
                    account_ssh_key_response()
                )
                attestation = api.attest_account_ssh_key(SSH_PUBLIC_KEY)
                payload = pod_create_payload()
                if value is None:
                    del payload["terminateAfter"]
                else:
                    payload["terminateAfter"] = value

                with self.assertRaises(RunpodLocalError) as caught:
                    api.create_pod(
                        payload,
                        account_ssh_attestation=attestation,
                    )

                self.assertEqual(
                    caught.exception.code, "invalid_pod_payload"
                )
                self.assertEqual(len(transport.requests), 1)

    def test_create_pod_requires_one_selected_gpu_type(self):
        for gpu_type_ids in ([], ["NVIDIA H200", "NVIDIA B200"], [1]):
            with self.subTest(gpu_type_ids=gpu_type_ids):
                api, transport = api_with_responses(
                    account_ssh_key_response()
                )
                attestation = api.attest_account_ssh_key(SSH_PUBLIC_KEY)

                with self.assertRaises(RunpodLocalError) as caught:
                    api.create_pod(
                        pod_create_payload(gpuTypeIds=gpu_type_ids),
                        account_ssh_attestation=attestation,
                    )

                self.assertEqual(
                    caught.exception.code, "invalid_pod_payload"
                )
                self.assertEqual(len(transport.requests), 1)

    def test_create_pod_graphql_no_capacity_is_definitive_and_consumes_attestation(
        self,
    ):
        api, transport = api_with_responses(
            account_ssh_key_response(),
            {
                "errors": [
                    {
                        "message": NO_INSTANCES_AVAILABLE_GRAPHQL_ERROR
                    }
                ],
                "data": None,
            },
        )
        attestation = api.attest_account_ssh_key(SSH_PUBLIC_KEY)

        with self.assertRaises(RunpodLocalError) as caught:
            api.create_pod(
                pod_create_payload(),
                account_ssh_attestation=attestation,
            )

        self.assertEqual(
            caught.exception.code, GRAPHQL_NO_CAPACITY_ERROR_CODE
        )
        self.assertEqual(
            str(caught.exception),
            "Runpod GraphQL podFindAndDeployOnDemand returned one or more "
            "errors (error_count=1; classification=capacity_unavailable; "
            "provider messages withheld)",
        )
        self.assertEqual(
            transport.requests[1]["allowed_error_responses"], frozenset()
        )
        with self.assertRaises(RunpodLocalError) as reused:
            api.create_pod(
                pod_create_payload(),
                account_ssh_attestation=attestation,
            )
        self.assertEqual(
            reused.exception.code, "account_ssh_attestation_required"
        )
        self.assertEqual(len(transport.requests), 2)

    def test_create_pod_mixed_graphql_errors_remain_ambiguous(self):
        api, _ = api_with_responses(
            account_ssh_key_response(),
            {
                "errors": [
                    {
                        "message": NO_INSTANCES_AVAILABLE_GRAPHQL_ERROR,
                    },
                    {"message": "fixture unclassified provider failure"},
                ],
                "data": None,
            },
        )
        attestation = api.attest_account_ssh_key(SSH_PUBLIC_KEY)

        with self.assertRaises(RunpodLocalError) as caught:
            api.create_pod(
                pod_create_payload(),
                account_ssh_attestation=attestation,
            )

        self.assertEqual(caught.exception.code, "provider_graphql_error")
        self.assertIn("error_count=2", str(caught.exception))
        self.assertIn(
            "classification=capacity_unavailable,unclassified",
            str(caught.exception),
        )

    def test_create_pod_capacity_error_with_pod_data_remains_ambiguous(self):
        api, _ = api_with_responses(
            account_ssh_key_response(),
            {
                "errors": [
                    {
                        "message": NO_INSTANCES_AVAILABLE_GRAPHQL_ERROR,
                    }
                ],
                "data": {
                    "podFindAndDeployOnDemand": {
                        "id": "pod123",
                        "name": "fixture",
                    }
                },
            },
        )
        attestation = api.attest_account_ssh_key(SSH_PUBLIC_KEY)

        with self.assertRaises(RunpodLocalError) as caught:
            api.create_pod(
                pod_create_payload(),
                account_ssh_attestation=attestation,
            )

        self.assertEqual(caught.exception.code, "provider_graphql_error")

    def test_create_pod_graphql_diagnostic_never_discloses_credentials_or_environment(
        self,
    ):
        token = "fixture-runpod-token"
        environment_secret = "fixture-environment-secret"
        payload = pod_create_payload()
        payload["env"]["PRIVATE_VALUE"] = environment_secret
        api, _ = api_with_responses(
            account_ssh_key_response(),
            {
                "errors": [
                    {
                        "message": (
                            "No instances available; provider echoed "
                            f"{token} and {environment_secret}"
                        ),
                        "path": [environment_secret],
                        "extensions": {
                            "code": environment_secret,
                            "debug": token,
                        },
                    }
                ],
                "data": None,
            },
        )
        attestation = api.attest_account_ssh_key(SSH_PUBLIC_KEY)

        with self.assertRaises(RunpodLocalError) as caught:
            api.create_pod(
                payload,
                account_ssh_attestation=attestation,
            )

        diagnostic = str(caught.exception)
        self.assertEqual(caught.exception.code, "provider_graphql_error")
        self.assertIn("classification=capacity_unavailable", diagnostic)
        self.assertIn("provider messages withheld", diagnostic)
        self.assertNotIn(token, diagnostic)
        self.assertNotIn(environment_secret, diagnostic)
        self.assertNotIn(SSH_PUBLIC_KEY, diagnostic)
        self.assertNotIn(token, repr(caught.exception.__dict__))
        self.assertNotIn(
            environment_secret,
            repr(caught.exception.__dict__),
        )
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_graphql_diagnostic_rejects_malformed_error_shape_without_echoing_it(
        self,
    ):
        provider_secret = "fixture-provider-secret"
        api, _ = api_with_responses(
            {
                "errors": {
                    "message": provider_secret,
                    "extensions": {"code": provider_secret},
                },
                "data": None,
            }
        )

        with self.assertRaises(RunpodLocalError) as caught:
            api.stock()

        self.assertEqual(
            str(caught.exception),
            "Runpod GraphQL gpuTypes returned one or more errors "
            "(error_shape=invalid; provider messages withheld)",
        )
        self.assertNotIn(provider_secret, str(caught.exception))

    def test_create_pod_rejects_invalid_graphql_response_identity(self):
        invalid_pods = (
            None,
            [],
            {"name": "fixture"},
            {"id": "invalid pod", "name": "fixture"},
            {"id": "pod123", "name": "other"},
        )
        for pod in invalid_pods:
            with self.subTest(pod=pod):
                api, _ = api_with_responses(
                    account_ssh_key_response(),
                    {"data": {"podFindAndDeployOnDemand": pod}},
                )
                attestation = api.attest_account_ssh_key(SSH_PUBLIC_KEY)

                with self.assertRaises(RunpodLocalError) as caught:
                    api.create_pod(
                        pod_create_payload(),
                        account_ssh_attestation=attestation,
                    )

                self.assertEqual(
                    caught.exception.code, "invalid_provider_response"
                )

    def test_account_ssh_key_match_is_comment_insensitive_and_multiline(self):
        account_keys = (
            f"{OTHER_SSH_PUBLIC_KEY}\n"
            + " ".join(SSH_PUBLIC_KEY.split(maxsplit=2)[:2])
            + " account-comment"
        )
        api, transport = api_with_responses(
            account_ssh_key_response(account_keys)
        )

        api.attest_account_ssh_key(SSH_PUBLIC_KEY)

        self.assertEqual(len(transport.requests), 1)
        request = transport.requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(
            request["url"], "https://graphql.example.invalid/query"
        )
        self.assertIn("myself", request["payload"]["query"])
        self.assertIn("pubKey", request["payload"]["query"])

    def test_missing_account_ssh_key_fails_typed_without_create_post(self):
        for account_key in (None, OTHER_SSH_PUBLIC_KEY):
            with self.subTest(account_key=account_key):
                api, transport = api_with_responses(
                    account_ssh_key_response(account_key)
                )

                with self.assertRaises(RunpodLocalError) as caught:
                    api.attest_account_ssh_key(SSH_PUBLIC_KEY)

                self.assertEqual(
                    caught.exception.code,
                    "account_ssh_key_not_authorized",
                )
                self.assertIn(
                    "SSH Public Keys", str(caught.exception)
                )
                self.assertIn(
                    "no Pod create request was sent",
                    str(caught.exception),
                )
                self.assertEqual(len(transport.requests), 1)

    def test_create_pod_requires_matching_one_use_account_attestation(self):
        api, transport = api_with_responses(
            account_ssh_key_response(),
            pod_create_response(),
        )

        with self.assertRaises(RunpodLocalError) as missing:
            api.create_pod(pod_create_payload())

        self.assertEqual(
            missing.exception.code, "account_ssh_attestation_required"
        )
        self.assertEqual(transport.requests, [])

        attestation = api.attest_account_ssh_key(SSH_PUBLIC_KEY)
        with self.assertRaises(RunpodLocalError) as mismatch:
            api.create_pod(
                pod_create_payload(OTHER_SSH_PUBLIC_KEY),
                account_ssh_attestation=attestation,
            )

        self.assertEqual(
            mismatch.exception.code, "account_ssh_attestation_mismatch"
        )
        self.assertEqual(len(transport.requests), 1)

        pod = api.create_pod(
            pod_create_payload(),
            account_ssh_attestation=attestation,
        )
        self.assertEqual(pod["id"], "pod123")
        self.assertEqual(len(transport.requests), 2)

        with self.assertRaises(RunpodLocalError) as reused:
            api.create_pod(
                pod_create_payload(),
                account_ssh_attestation=attestation,
            )
        self.assertEqual(
            reused.exception.code, "account_ssh_attestation_required"
        )
        self.assertEqual(len(transport.requests), 2)

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
        self.assertEqual(
            str(caught.exception),
            "Runpod GraphQL gpuTypes returned one or more errors "
            "(error_count=1; classification=unclassified; provider "
            "messages withheld)",
        )

    def test_delete_pod_requires_204(self):
        api, transport = api_with_responses(None)
        api.delete_pod("pod123")
        self.assertEqual(transport.requests[0]["method"], "DELETE")
        self.assertEqual(transport.requests[0]["expected_statuses"], (204,))


if __name__ == "__main__":
    unittest.main()
