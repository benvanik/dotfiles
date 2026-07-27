from __future__ import annotations

import copy
import datetime
import pathlib
import tempfile
import unittest
import uuid

from runpod_local.allocation import (
    select_launch_placement,
    verify_allocated_pod,
)
from runpod_local.api import normalize_pod
from runpod_local.errors import HttpRequestError, RunpodLocalError
from runpod_local.instances import (
    InstanceStore,
    json_document_hash,
    lease_expiry_reasons,
    validate_instance_record,
)
from runpod_local.lifecycle import LifecycleManager
from runpod_local.profile import (
    create_profile,
    provider_effective_environment_summary,
)
from runpod_local.runtime_catalog import load_runtime
from runpod_local.state import StateStore
from runpod_local.template import environment_summary
from runpod_local.timeutil import parse_utc_timestamp

GPU_ID = "NVIDIA RTX PRO 6000 Blackwell Server Edition"
NOW = datetime.datetime(2026, 7, 26, 20, 0, tzinfo=datetime.timezone.utc)
OPERATION_ID = uuid.UUID("12345678-1234-4234-8234-123456789abc")
SSH_PUBLIC_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f "
    "fixture@example"
)
IMAGE = (
    "runpod/pytorch@sha256:"
    "1111111111111111111111111111111111111111111111111111111111111111"
)
RUNTIME_ID = "vllm-cu129-v0.25.1"
RUNTIME = load_runtime(RUNTIME_ID)


def profile(**overrides):
    arguments = {
        "name": "pro-dev",
        "gpu_names": ["pro6000", "h200"],
        "max_hourly_usd": 3.0,
        "default_ttl_seconds": 3600,
        "image_name": IMAGE,
        "network_volume_id": "volume123",
        "ssh_public_key": SSH_PUBLIC_KEY,
    }
    arguments.update(overrides)
    return create_profile(**arguments)


def template_profile(**overrides):
    contract = RUNTIME.template_contract(
        name="upstream-runtime",
        template_id="template123",
    )
    arguments = {
        "image_name": RUNTIME.image,
        "template_id": "template123",
        "template_contract": contract,
        "runtime_id": RUNTIME_ID,
    }
    arguments.update(overrides)
    return profile(**arguments)


def stock():
    return {
        "schema_version": "runpod.stock.v1",
        "gpus": [
            {
                "gpu_id": GPU_ID,
                "display_name": "RTX PRO 6000 Server",
                "memory_gb": 96,
                "secure_cloud": True,
                "community_cloud": False,
                "stock_status": "Low",
                "on_demand_price_per_gpu_hour": 1.99,
                "available_gpu_counts": [],
            },
            {
                "gpu_id": "NVIDIA H200",
                "display_name": "H200",
                "memory_gb": 141,
                "secure_cloud": True,
                "community_cloud": False,
                "stock_status": "High",
                "on_demand_price_per_gpu_hour": 4.39,
                "available_gpu_counts": [],
            },
        ],
        "data_centers": [
            {
                "data_center_id": "US-NC-2",
                "name": "fixture",
                "location": "US",
                "gpu_availability": [
                    {
                        "gpu_id": GPU_ID,
                        "display_name": "RTX PRO 6000 Server",
                        "stock_status": "Low",
                    },
                    {
                        "gpu_id": "NVIDIA H200",
                        "display_name": "H200",
                        "stock_status": "None",
                    },
                ],
            }
        ],
    }


class MutableClock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class FakeApi:
    def __init__(self):
        self.pods = []
        self.create_calls = 0
        self.delete_calls = []
        self.volume_delete_calls = []
        self.create_error = None
        self.delete_error = None
        self.created_cost = 1.99
        self.before_create = None
        self.before_get = None
        self.before_attest = None
        self.account_ssh_attestation_calls = []
        self.account_ssh_attestation_error = None
        self.account_ssh_attestation = object()
        self.templates = {}
        self.template_get_calls = 0
        self.before_get_template = None

    def get_network_volume(self, volume_id):
        return {
            "id": volume_id,
            "name": "model-cache",
            "size_gb": 500,
            "data_center_id": "US-NC-2",
        }

    def stock(self, **_):
        return stock()

    def get_template(self, template_id):
        self.template_get_calls += 1
        if self.before_get_template is not None:
            self.before_get_template(self.template_get_calls)
        return dict(self.templates[template_id])

    def list_pods(self):
        return [dict(pod) for pod in self.pods]

    def attest_account_ssh_key(self, public_key):
        self.account_ssh_attestation_calls.append(public_key)
        if self.before_attest is not None:
            self.before_attest()
        if self.account_ssh_attestation_error is not None:
            raise self.account_ssh_attestation_error
        return self.account_ssh_attestation

    def create_pod(self, payload, *, account_ssh_attestation):
        if account_ssh_attestation is not self.account_ssh_attestation:
            raise AssertionError("wrong account SSH-key attestation")
        self.create_calls += 1
        if self.before_create is not None:
            self.before_create()
        pod = self.pod_for_payload(payload)
        if self.create_error is not None:
            if self.create_error == "after_remote":
                self.pods.append(pod)
                raise HttpRequestError("fixture timeout")
            raise self.create_error
        self.pods.append(pod)
        return dict(pod)

    def pod_for_payload(self, payload):
        template = self.templates.get(payload.get("templateId"))
        normalized_environment = provider_effective_environment_summary(
            payload["env"]
        )
        if normalized_environment is None:
            raise AssertionError("fixture Pod environment is invalid")
        return {
            "id": "pod123",
            "name": payload["name"],
            "desired_status": "RUNNING",
            "image": (
                template["image"]
                if template is not None
                else payload.get("imageName")
            ),
            "template_id": payload.get("templateId"),
            "docker_entrypoint_status": (
                "valid" if template is not None else "missing"
            ),
            "docker_entrypoint": (
                template["docker_entrypoint"]
                if template is not None
                else None
            ),
            "docker_start_cmd_status": (
                "valid" if template is not None else "missing"
            ),
            "docker_start_cmd": (
                template["docker_start_cmd"]
                if template is not None
                else None
            ),
            "container_disk_gb": payload["containerDiskInGb"],
            "volume_in_gb": payload.get("volumeInGb", 0),
            "volume_mount_path": payload["volumeMountPath"],
            "environment_status": "valid",
            **normalized_environment,
            "registry_auth_status": "valid",
            "has_registry_auth": False,
            "interruptible": False,
            "locked": False,
            "gpu_status": "valid",
            "gpu_id": payload["gpuTypeIds"][0],
            "gpu_count": payload["gpuCount"],
            "cost_status": "valid",
            "cost_per_hour": self.created_cost,
            "machine_status": "valid",
            "data_center_id": "US-NC-2",
            "secure_cloud": True,
            "machine_id": "machine123",
            "network_volume_status": (
                "valid"
                if payload.get("networkVolumeId") is not None
                else "missing"
            ),
            "network_volume_id": payload.get("networkVolumeId"),
            "network_volume_data_center_id": (
                "US-NC-2"
                if payload.get("networkVolumeId") is not None
                else None
            ),
            "network_volume": {
                "id": payload.get("networkVolumeId"),
                "name": "model-cache",
                "size_gb": 500,
                "data_center_id": "US-NC-2",
            },
            "public_ip": None,
            "port_mappings_status": "valid",
            "port_mappings": {},
            "ports_status": "valid",
            "ports": ["22/tcp"],
        }

    def get_pod(self, pod_id):
        if self.before_get is not None:
            self.before_get()
        for pod in self.pods:
            if pod["id"] == pod_id:
                return dict(pod)
        raise HttpRequestError("fixture missing", status=404)

    def delete_pod(self, pod_id):
        self.delete_calls.append(pod_id)
        if self.delete_error is not None:
            raise self.delete_error
        before = len(self.pods)
        self.pods = [pod for pod in self.pods if pod["id"] != pod_id]
        if len(self.pods) == before:
            raise HttpRequestError("fixture missing", status=404)


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = StateStore(
            pathlib.Path(self.temporary.name) / "runpod-state"
        )
        self.identity = pathlib.Path(self.temporary.name) / "id_ed25519"
        self.identity.write_text("fixture private key")
        self.identity.chmod(0o600)
        self.launch_profile = profile(identity_file=str(self.identity))
        self.api = FakeApi()
        self.clock = MutableClock()
        self.manager = LifecycleManager(
            self.api,
            self.state,
            clock=self.clock,
            uuid_factory=lambda: OPERATION_ID,
            profile_ssh_validator=lambda _: None,
            key_pair_validator=lambda _identity, _public: None,
        )

    def launch(self, **overrides):
        arguments = {
            "name": "compiler",
            "profile": self.launch_profile,
            "ttl_seconds": 3600,
            "idle_timeout_seconds": 900,
        }
        arguments.update(overrides)
        return self.manager.launch(**arguments)

    def enter_conflict(self):
        self.api.create_error = HttpRequestError("fixture timeout")
        with self.assertRaises(HttpRequestError):
            self.launch()
        record = InstanceStore(self.state).load("compiler")
        first = self.api.pod_for_payload(record["pod_payload"])
        second = dict(first)
        second["id"] = "pod456"
        self.api.pods = [first, second]
        with self.assertRaises(RunpodLocalError) as caught:
            self.launch()
        self.assertEqual(caught.exception.code, "duplicate_remote_name")
        self.api.create_error = None
        return InstanceStore(self.state).load("compiler")

    def test_selection_uses_profile_order_empty_count_hint_and_dc_veto(self):
        report = select_launch_placement(
            profile(),
            stock(),
            data_center_id="US-NC-2",
        )

        self.assertEqual(report["selected"]["gpu_id"], GPU_ID)
        self.assertTrue(report["evaluations"][0]["eligible"])
        self.assertIn(
            "unavailable in data center",
            report["evaluations"][1]["reasons"][1],
        )

    def test_dry_plan_writes_no_state_and_performs_no_mutation(self):
        result = self.manager.plan_launch(
            "compiler",
            profile(),
            ttl_seconds=3600,
            idle_timeout_seconds=900,
        )

        self.assertTrue(result["ready"])
        self.assertEqual(self.api.create_calls, 0)
        self.assertIsNone(
            InstanceStore(self.state).load("compiler", required=False)
        )

    def test_intent_is_durable_before_submit_and_ttl_counts_provisioning(self):
        store = InstanceStore(self.state)

        def delay_account_attestation():
            self.clock.now = NOW + datetime.timedelta(seconds=60)

        def observe_intent():
            record = store.load("compiler")
            self.assertEqual(record["phase"], "submitting")
            self.assertIsNone(record["pod_id"])
            self.assertEqual(
                record["provider_termination_at"],
                "2026-07-26T21:00:00Z",
            )
            self.assertEqual(
                record["pod_payload"]["terminateAfter"],
                record["provider_termination_at"],
            )
            self.assertEqual(
                record["pod_payload"]["dataCenterId"],
                "US-NC-2",
            )

        self.api.before_attest = delay_account_attestation
        self.api.before_create = observe_intent
        record = self.launch()

        self.assertEqual(record["phase"], "active")
        self.assertEqual(
            parse_utc_timestamp(record["lease"]["expires_at"]),
            NOW + datetime.timedelta(seconds=3600),
        )
        self.assertEqual(
            parse_utc_timestamp(record["lease"]["activated_at"]),
            NOW,
        )
        self.assertEqual(
            parse_utc_timestamp(record["lease"]["last_activity_at"]),
            NOW + datetime.timedelta(seconds=60),
        )

    def test_ambiguous_submission_is_reconciled_without_second_post(self):
        self.api.create_error = "after_remote"
        with self.assertRaises(HttpRequestError):
            self.launch()
        self.assertEqual(
            InstanceStore(self.state).load("compiler")["phase"], "submitting"
        )

        self.api.create_error = None
        record = self.launch()

        self.assertEqual(record["phase"], "active")
        self.assertEqual(self.api.create_calls, 1)
        self.assertEqual(record["pod_id"], "pod123")

    def test_ambiguous_submission_with_no_visible_pod_never_reposts(self):
        self.api.create_error = HttpRequestError("fixture timeout")
        with self.assertRaises(HttpRequestError):
            self.launch()
        self.api.create_error = None

        with self.assertRaises(RunpodLocalError) as caught:
            self.launch()

        self.assertEqual(caught.exception.code, "submission_ambiguous")
        self.assertEqual(self.api.create_calls, 1)

    def test_initial_list_failure_keeps_intent_retryable_before_post(self):
        list_pods = self.api.list_pods

        def fail_list():
            raise HttpRequestError("fixture list failure", status=503)

        self.api.list_pods = fail_list
        with self.assertRaises(HttpRequestError):
            self.launch()

        failed = InstanceStore(self.state).load("compiler")
        self.assertEqual(failed["phase"], "intent")
        self.assertEqual(self.api.create_calls, 0)

        self.api.list_pods = list_pods
        resumed = self.launch()

        self.assertEqual(resumed["phase"], "active")
        self.assertEqual(self.api.create_calls, 1)

    def test_unsubmitted_name_collision_is_never_adopted_or_managed(self):
        collision = {
            "id": "foreign123",
            "name": "rp-compiler-123456781234",
        }
        self.api.pods = [collision]

        with self.assertRaises(RunpodLocalError) as caught:
            self.launch()

        self.assertEqual(caught.exception.code, "remote_name_collision")
        receipt = InstanceStore(self.state).load("compiler")
        self.assertEqual(receipt["phase"], "aborted")
        self.assertIsNone(receipt["pod_id"])
        self.assertEqual(self.api.create_calls, 0)
        self.assertEqual(self.api.delete_calls, [])
        status = self.manager.status("compiler", live=True)
        self.assertEqual(
            [pod["id"] for pod in status["unmanaged_pods"]],
            ["foreign123"],
        )

        self.api.pods[0]["name"] = "temporarily-renamed"
        self.manager.uuid_factory = lambda: uuid.UUID(
            "87654321-4321-4321-8321-ba9876543210"
        )
        launched = self.launch()
        self.assertEqual(launched["phase"], "active")
        self.assertEqual(self.api.create_calls, 1)
        self.api.pods[0]["name"] = receipt["remote_name"]
        self.assertEqual(
            [pod["id"] for pod in self.manager.status(live=True)[
                "unmanaged_pods"
            ]],
            ["foreign123"],
        )
        self.manager.terminate(
            "compiler",
            execute=True,
            reason="operator",
        )
        self.assertEqual(self.api.delete_calls, ["pod123"])
        self.assertEqual(self.api.pods, [collision])

    def test_terminal_reuse_rejects_repeated_operation_id(self):
        self.launch()
        self.manager.terminate(
            "compiler",
            execute=True,
            reason="operator",
        )

        with self.assertRaises(RunpodLocalError) as caught:
            self.launch()

        self.assertEqual(
            caught.exception.code,
            "operation_identity_collision",
        )
        self.assertEqual(self.api.create_calls, 1)
        self.assertEqual(self.api.pods, [])

    def test_terminal_reuse_rejects_distinct_id_with_retained_name_prefix(self):
        self.launch()
        self.manager.terminate(
            "compiler",
            execute=True,
            reason="operator",
        )
        self.manager.uuid_factory = lambda: uuid.UUID(
            "12345678-1234-4abc-8abc-fedcba987654"
        )

        with self.assertRaises(RunpodLocalError) as caught:
            self.launch()

        self.assertEqual(
            caught.exception.code,
            "operation_identity_collision",
        )
        self.assertEqual(self.api.create_calls, 1)
        self.assertEqual(self.api.pods, [])

    def test_deadline_expiring_during_reconciliation_sends_no_create(self):
        list_pods = self.api.list_pods

        def expire_before_create():
            self.clock.now = NOW + datetime.timedelta(seconds=3600)
            return list_pods()

        self.api.list_pods = expire_before_create

        with self.assertRaises(RunpodLocalError) as caught:
            self.launch()

        self.assertEqual(caught.exception.code, "launch_expired")
        self.assertEqual(self.api.create_calls, 0)
        self.assertEqual(
            InstanceStore(self.state).load("compiler")["phase"],
            "intent",
        )

    def test_graphql_capacity_error_is_ambiguous_and_never_reposts(self):
        self.api.create_error = RunpodLocalError(
            "fixture no capacity",
            code="provider_graphql_error",
        )

        with self.assertRaises(RunpodLocalError) as submission_error:
            self.launch()

        self.assertEqual(
            submission_error.exception.code,
            "provider_graphql_error",
        )
        first = InstanceStore(self.state).load("compiler")
        self.assertEqual(first["phase"], "submitting")
        self.assertEqual(
            first["events"][-1]["event"],
            "submission_result_unknown",
        )

        self.api.create_error = None
        with self.assertRaises(RunpodLocalError) as caught:
            self.launch()

        self.assertEqual(caught.exception.code, "submission_ambiguous")
        self.assertEqual(self.api.create_calls, 1)
        self.assertEqual(
            InstanceStore(self.state).load("compiler")["phase"],
            "submitting",
        )

    def test_duplicate_reconciliation_names_enter_conflict(self):
        self.enter_conflict()
        self.assertEqual(
            InstanceStore(self.state).load("compiler")["phase"], "conflict"
        )

        plan = self.manager.terminate(
            "compiler", execute=False, reason="operator"
        )
        self.assertEqual(plan["action"], "delete_conflicted_pods")
        self.assertEqual(plan["pod_ids"], ["pod123", "pod456"])
        self.assertEqual(self.api.delete_calls, [])

        result = self.manager.terminate(
            "compiler", execute=True, reason="operator"
        )
        self.assertEqual(result["action"], "delete_conflicted_pods")
        self.assertEqual(self.api.delete_calls, ["pod123", "pod456"])
        self.assertEqual(self.api.pods, [])
        self.assertEqual(
            InstanceStore(self.state).load("compiler")["phase"], "terminated"
        )

    def test_down_captures_late_duplicate_submission_before_delete(self):
        self.api.create_error = HttpRequestError("fixture timeout")
        with self.assertRaises(HttpRequestError):
            self.launch()
        record = InstanceStore(self.state).load("compiler")
        first = self.api.pod_for_payload(record["pod_payload"])
        second = dict(first)
        second["id"] = "pod456"
        self.api.pods = [second, first]
        self.api.create_error = None

        plan = self.manager.terminate(
            "compiler", execute=False, reason="operator"
        )
        unchanged = InstanceStore(self.state).load("compiler")

        self.assertEqual(plan["action"], "delete_conflicted_pods")
        self.assertEqual(plan["pod_ids"], ["pod123", "pod456"])
        self.assertEqual(unchanged["phase"], "submitting")
        self.assertNotIn("conflict_pod_ids", unchanged)
        self.assertEqual(self.api.delete_calls, [])

        delete_pod = self.api.delete_pod

        def verify_durable_identity(pod_id):
            captured = InstanceStore(self.state).load("compiler")
            self.assertEqual(captured["phase"], "conflict")
            self.assertEqual(
                captured["conflict_pod_ids"],
                ["pod123", "pod456"],
            )
            delete_pod(pod_id)

        self.api.delete_pod = verify_durable_identity
        result = self.manager.terminate(
            "compiler", execute=True, reason="operator"
        )

        self.assertEqual(result["pod_ids"], ["pod123", "pod456"])
        self.assertEqual(self.api.delete_calls, ["pod123", "pod456"])
        self.assertEqual(
            InstanceStore(self.state).load("compiler")["phase"],
            "terminated",
        )

    def test_conflict_cleanup_expands_unrecorded_name_match_without_delete(self):
        record = self.enter_conflict()
        unrecorded = self.api.pod_for_payload(record["pod_payload"])
        unrecorded["id"] = "pod789"
        self.api.pods.append(unrecorded)

        with self.assertRaises(RunpodLocalError) as caught:
            self.manager.terminate(
                "compiler", execute=True, reason="operator"
            )

        self.assertEqual(caught.exception.code, "conflict_identity_expanded")
        self.assertEqual(self.api.delete_calls, [])
        self.assertEqual(len(self.api.pods), 3)
        expanded = InstanceStore(self.state).load("compiler")
        self.assertEqual(
            expanded["conflict_pod_ids"],
            ["pod123", "pod456", "pod789"],
        )
        self.assertNotIn("conflict_cleanup_requested_at", expanded)

    def test_conflict_expansion_revokes_watcher_cleanup_authorization(self):
        record = self.enter_conflict()
        self.api.delete_error = HttpRequestError(
            "fixture delete failure",
            status=503,
        )
        with self.assertRaises(HttpRequestError):
            self.manager.terminate(
                "compiler", execute=True, reason="operator"
            )
        authorized = InstanceStore(self.state).load("compiler")
        self.assertIn("conflict_cleanup_requested_at", authorized)

        unrecorded = self.api.pod_for_payload(record["pod_payload"])
        unrecorded["id"] = "pod789"
        self.api.pods.append(unrecorded)
        self.api.delete_error = None

        expanded_result = self.manager.enforce_ttl(execute=True)

        self.assertEqual(
            expanded_result["actions"][0]["error"]["code"],
            "conflict_identity_expanded",
        )
        expanded = InstanceStore(self.state).load("compiler")
        self.assertEqual(
            expanded["conflict_pod_ids"],
            ["pod123", "pod456", "pod789"],
        )
        self.assertNotIn("conflict_cleanup_requested_at", expanded)
        self.assertEqual(self.api.delete_calls, ["pod123"])

        self.api.pods[2]["name"] = "renamed-by-another-controller"
        retry = self.manager.enforce_ttl(execute=True)

        self.assertEqual(retry["actions"], [])
        self.assertEqual(self.api.delete_calls, ["pod123"])
        blocked = self.launch()
        self.assertEqual(blocked["phase"], "conflict")
        self.assertEqual(self.api.create_calls, 1)

    def test_conflict_phase_requires_durable_pod_identities(self):
        record = self.enter_conflict()
        missing_identities = copy.deepcopy(record)
        missing_identities.pop("conflict_pod_ids")

        with self.assertRaises(RunpodLocalError) as caught:
            validate_instance_record(missing_identities)

        self.assertEqual(caught.exception.code, "invalid_instance_record")

        no_disposition = copy.deepcopy(record)
        no_disposition.pop("conflict_review_required_at")
        with self.assertRaises(RunpodLocalError) as no_disposition_error:
            validate_instance_record(no_disposition)
        self.assertEqual(
            no_disposition_error.exception.code,
            "invalid_instance_record",
        )

        contradictory = copy.deepcopy(record)
        contradictory["conflict_cleanup_requested_at"] = (
            contradictory["conflict_review_required_at"]
        )
        with self.assertRaises(RunpodLocalError) as contradictory_error:
            validate_instance_record(contradictory)
        self.assertEqual(
            contradictory_error.exception.code,
            "invalid_instance_record",
        )

    def test_conflict_cleanup_rejects_a_recorded_id_under_another_name(self):
        self.enter_conflict()
        self.api.pods[1]["name"] = "another-operation"

        with self.assertRaises(RunpodLocalError) as caught:
            self.manager.terminate(
                "compiler", execute=True, reason="operator"
            )

        self.assertEqual(caught.exception.code, "conflict_identity_changed")
        self.assertEqual(self.api.delete_calls, [])

    def test_conflict_cleanup_failure_is_retried_by_ttl_watcher(self):
        self.enter_conflict()
        delete_pod = self.api.delete_pod

        def fail_second_delete(pod_id):
            if pod_id == "pod456":
                self.api.delete_calls.append(pod_id)
                raise HttpRequestError("fixture delete failure", status=503)
            delete_pod(pod_id)

        self.api.delete_pod = fail_second_delete
        with self.assertRaises(HttpRequestError):
            self.manager.terminate(
                "compiler", execute=True, reason="operator"
            )

        failed = InstanceStore(self.state).load("compiler")
        self.assertEqual(failed["phase"], "conflict")
        self.assertEqual(
            failed["conflict_pod_ids"],
            ["pod123", "pod456"],
        )
        self.assertIn("conflict_cleanup_requested_at", failed)
        self.assertEqual([pod["id"] for pod in self.api.pods], ["pod456"])

        self.api.delete_pod = delete_pod
        result = self.manager.enforce_ttl(execute=True)

        self.assertEqual(
            result["actions"][0]["reasons"],
            ["conflict_cleanup_retry"],
        )
        self.assertEqual(
            result["actions"][0]["termination"]["pod_ids"],
            ["pod456"],
        )
        self.assertTrue(result["actions"][0]["executed"])
        self.assertEqual(
            self.api.delete_calls,
            ["pod123", "pod456", "pod456"],
        )
        completed = InstanceStore(self.state).load("compiler")
        self.assertEqual(completed["phase"], "terminated")
        self.assertIn("conflict_cleanup_requested_at", completed)

    def test_terminal_conflict_id_change_is_managed_and_blocks_reuse(self):
        self.enter_conflict()
        self.manager.terminate(
            "compiler", execute=True, reason="operator"
        )
        terminal = InstanceStore(self.state).load("compiler")
        renamed = self.api.pod_for_payload(terminal["pod_payload"])
        renamed["id"] = "pod456"
        renamed["name"] = "renamed-by-another-controller"
        self.api.pods = [renamed]

        status = self.manager.status("compiler", live=True)

        self.assertEqual(status["unmanaged_pods"], [])
        self.assertIn(
            "pod_identity_conflict",
            status["instances"][0]["drift"],
        )
        with self.assertRaises(RunpodLocalError) as caught:
            self.manager.plan_launch(
                "compiler",
                self.launch_profile,
                ttl_seconds=1800,
                idle_timeout_seconds=900,
            )
        self.assertEqual(caught.exception.code, "pod_identity_conflict")
        self.assertEqual(self.api.create_calls, 1)

    def test_terminal_reuse_persists_new_exact_id_before_rename(self):
        record = self.launch()
        self.manager.terminate(
            "compiler",
            execute=True,
            reason="operator",
        )
        replacement = self.api.pod_for_payload(record["pod_payload"])
        replacement["id"] = "pod456"
        self.api.pods = [replacement]

        with self.assertRaises(RunpodLocalError) as caught:
            self.launch()

        self.assertEqual(caught.exception.code, "pod_identity_conflict")
        durable = InstanceStore(self.state).load("compiler")
        self.assertEqual(durable["phase"], "terminated")
        self.assertEqual(
            durable["conflict_pod_ids"],
            ["pod123", "pod456"],
        )
        self.assertIn("conflict_review_required_at", durable)
        self.assertNotIn("conflict_cleanup_requested_at", durable)

        self.api.pods[0]["name"] = "renamed-by-another-controller"
        with self.assertRaises(RunpodLocalError) as blocked:
            self.launch()

        self.assertEqual(
            blocked.exception.code,
            "conflict_review_required",
        )
        self.assertEqual(self.api.create_calls, 1)
        status = self.manager.status("compiler", live=True)
        self.assertEqual(status["unmanaged_pods"], [])

    def test_new_exact_name_id_enters_conflict_before_delete(self):
        record = self.launch()
        replacement = self.api.pod_for_payload(record["pod_payload"])
        replacement["id"] = "pod456"
        self.api.pods = [replacement]
        self.api.delete_error = HttpRequestError(
            "fixture delete failure",
            status=503,
        )

        with self.assertRaises(HttpRequestError):
            self.manager.terminate(
                "compiler", execute=True, reason="operator"
            )

        conflicted = InstanceStore(self.state).load("compiler")
        self.assertEqual(conflicted["phase"], "conflict")
        self.assertEqual(
            conflicted["conflict_pod_ids"],
            ["pod123", "pod456"],
        )
        self.assertIn("conflict_cleanup_requested_at", conflicted)

        self.api.delete_error = None
        self.api.pods[0]["name"] = "renamed-by-another-controller"
        status = self.manager.status("compiler", live=True)
        self.assertEqual(status["unmanaged_pods"], [])
        self.assertIn(
            "pod_identity_conflict",
            status["instances"][0]["drift"],
        )
        retried = self.manager.enforce_ttl(execute=True)
        self.assertEqual(
            retried["actions"][0]["error"]["code"],
            "conflict_identity_changed",
        )
        self.assertEqual(self.api.delete_calls, ["pod456"])
        blocked = self.launch()
        self.assertEqual(blocked["phase"], "conflict")
        self.assertEqual(self.api.create_calls, 1)

    def test_changed_durable_id_cannot_hide_a_new_exact_name_id(self):
        record = self.launch()
        renamed = self.api.pod_for_payload(record["pod_payload"])
        renamed["name"] = "renamed-by-another-controller"
        replacement = self.api.pod_for_payload(record["pod_payload"])
        replacement["id"] = "pod456"
        self.api.pods = [renamed, replacement]

        with self.assertRaises(RunpodLocalError) as caught:
            self.manager.terminate(
                "compiler",
                execute=True,
                reason="operator",
            )

        self.assertEqual(caught.exception.code, "conflict_identity_changed")
        conflicted = InstanceStore(self.state).load("compiler")
        self.assertEqual(conflicted["phase"], "conflict")
        self.assertEqual(
            conflicted["conflict_pod_ids"],
            ["pod123", "pod456"],
        )
        self.assertIn("conflict_cleanup_requested_at", conflicted)
        self.assertEqual(self.api.delete_calls, [])

        self.api.pods = [
            {
                **replacement,
                "name": "also-renamed-by-another-controller",
            }
        ]
        status = self.manager.status("compiler", live=True)
        self.assertEqual(status["unmanaged_pods"], [])
        resumed = self.launch()
        self.assertEqual(resumed["phase"], "conflict")
        self.assertEqual(self.api.create_calls, 1)

    def test_over_cap_allocation_is_deleted_and_recorded(self):
        self.api.created_cost = 3.01

        with self.assertRaises(RunpodLocalError) as caught:
            self.launch()

        self.assertEqual(caught.exception.code, "allocation_rejected")
        self.assertEqual(self.api.delete_calls, ["pod123"])
        self.assertEqual(
            InstanceStore(self.state).load("compiler")["phase"], "rolled_back"
        )

    def test_template_launch_attests_provider_before_billable_create(self):
        self.launch_profile = template_profile(
            identity_file=str(self.identity)
        )
        contract = self.launch_profile["pod"]["template_contract"]
        self.api.templates["template123"] = dict(contract)

        record = self.launch()

        self.assertEqual(record["phase"], "active")
        self.assertEqual(
            record["expected"]["template_contract"], contract
        )
        self.assertEqual(record["expected"]["image"], RUNTIME.image)
        self.assertEqual(
            record["expected"]["runtime"]["runtime_id"],
            RUNTIME_ID,
        )
        self.assertEqual(record["expected"]["volume_in_gb"], 0)
        self.assertEqual(record["provider"]["volume_in_gb"], 0)
        self.assertNotIn("docker_entrypoint", record["provider"])
        self.assertTrue(
            record["provider"]["docker_entrypoint_summary"][
                "valid_string_array"
            ]
        )
        self.assertEqual(
            record["provider"]["environment_sha256"],
            record["expected"]["environment_sha256"],
        )
        self.assertIs(record["provider"]["has_registry_auth"], False)
        self.assertEqual(
            record["pod_payload"]["terminateAfter"],
            record["provider_termination_at"],
        )

    def test_provider_runtime_mutation_rolls_back_without_persisting_secrets(
        self,
    ):
        self.launch_profile = template_profile(
            identity_file=str(self.identity)
        )
        contract = self.launch_profile["pod"]["template_contract"]
        self.api.templates["template123"] = dict(contract)
        secret = "PROVIDER_SECRET=must-not-persist"
        expected_environment = dict(
            self.launch_profile["pod"]["environment"]
        )
        mutated_environment = dict(expected_environment)
        mutated_environment["SSH_PUBLIC_KEY"] = secret
        mutated_summary = environment_summary(mutated_environment)
        self.assertIsNotNone(mutated_summary)
        self.assertEqual(
            mutated_summary["environment_names"],
            sorted(expected_environment),
        )

        def mutate_live_pod():
            self.api.pods[0].update(
                {
                    "docker_start_cmd": [secret],
                    "environment_status": "valid",
                    **mutated_summary,
                    "registry_auth_status": "valid",
                    "has_registry_auth": True,
                }
            )

        self.api.before_get = mutate_live_pod

        with self.assertRaises(RunpodLocalError) as caught:
            self.launch()

        self.assertEqual(caught.exception.code, "allocation_rejected")
        self.assertEqual(self.api.delete_calls, ["pod123"])
        receipt = InstanceStore(self.state).load("compiler")
        self.assertEqual(receipt["phase"], "rolled_back")
        self.assertNotEqual(
            receipt["provider"]["environment_sha256"],
            receipt["expected"]["environment_sha256"],
        )
        self.assertTrue(
            receipt["provider"]["docker_start_cmd_summary"][
                "valid_string_array"
            ]
        )
        self.assertIs(receipt["provider"]["has_registry_auth"], True)
        self.assertNotIn("docker_start_cmd", receipt["provider"])
        self.assertNotIn("image", receipt["provider"])
        serialized = self.state.record_path(
            "instances", "compiler"
        ).read_text()
        self.assertNotIn(secret, serialized)
        self.assertNotIn(secret, repr(receipt))
        self.assertNotIn(secret, str(caught.exception))

    def test_malformed_provider_evidence_cannot_block_safe_rollback(self):
        self.launch_profile = template_profile(
            identity_file=str(self.identity)
        )
        contract = self.launch_profile["pod"]["template_contract"]
        self.api.templates["template123"] = dict(contract)
        secret = "MALFORMED_PROVIDER_SECRET"
        oversized = secret + "x" * 5000

        def corrupt_live_pod():
            self.api.pods[0].update(
                {
                    "name": secret,
                    "desired_status": secret,
                    "docker_start_cmd": [{"secret": secret}],
                    "volume_mount_path": secret,
                    "environment_status": "valid",
                    "environment_names": [oversized],
                    "environment_sha256": "0" * 64,
                    "gpu_id": secret,
                    "data_center_id": secret,
                    "network_volume_id": secret,
                    "cost_status": "invalid",
                    "cost_per_hour": None,
                    "machine_id": oversized,
                    "network_volume": {
                        "id": "volume123",
                        "name": oversized,
                        "size_gb": 500,
                        "data_center_id": secret,
                    },
                    "port_mappings": {secret: "invalid"},
                    "ports": [22, secret],
                    "public_ip": secret + "\n",
                }
            )

        self.api.before_get = corrupt_live_pod

        with self.assertRaises(RunpodLocalError) as caught:
            self.launch()

        self.assertEqual(caught.exception.code, "allocation_rejected")
        self.assertEqual(self.api.delete_calls, ["pod123"])
        receipt = InstanceStore(self.state).load("compiler")
        self.assertEqual(receipt["phase"], "rolled_back")
        self.assertEqual(
            receipt["provider"]["environment_status"],
            "invalid",
        )
        self.assertIsNone(
            receipt["provider"]["environment_name_count"]
        )
        self.assertFalse(
            receipt["provider"]["environment_names_match_expected"]
        )
        self.assertFalse(
            receipt["provider"]["desired_status_matches_expected"]
        )
        self.assertFalse(
            receipt["provider"]["volume_mount_path_matches_expected"]
        )
        self.assertFalse(receipt["provider"]["gpu_id_matches_expected"])
        self.assertFalse(
            receipt["provider"]["data_center_id_matches_expected"]
        )
        self.assertFalse(
            receipt["provider"]["network_volume_id_matches_expected"]
        )
        self.assertEqual(receipt["provider"]["cost_status"], "invalid")
        self.assertIsNone(receipt["provider"]["cost_per_hour"])
        self.assertEqual(receipt["provider"]["ports_status"], "invalid")
        self.assertIsNone(receipt["provider"]["port_count"])
        self.assertFalse(receipt["provider"]["ports_match_expected"])
        self.assertEqual(
            receipt["provider"]["port_mappings_status"],
            "invalid",
        )
        self.assertIsNone(receipt["provider"]["port_mapping_count"])
        self.assertFalse(
            receipt["provider"]["docker_start_cmd_summary"][
                "valid_string_array"
            ]
        )
        serialized = self.state.record_path(
            "instances", "compiler"
        ).read_text()
        self.assertNotIn(secret, serialized)
        self.assertNotIn(secret, repr(receipt))
        self.assertNotIn(secret, str(caught.exception))

    def test_instance_receipt_rejects_raw_provider_runtime_fields(self):
        record = self.launch()
        secret = "PROVIDER_SECRET=must-not-load"

        for field, value in (
            ("image", "private/secret-image"),
            ("docker_entrypoint", ["/bin/bash", "-c"]),
            ("docker_start_cmd", [secret]),
            ("env", {"SSH_PUBLIC_KEY": secret}),
            ("containerRegistryAuthId", "secret-registry-id"),
        ):
            with self.subTest(field=field):
                tampered = copy.deepcopy(record)
                tampered["provider"][field] = value
                with self.assertRaises(RunpodLocalError) as caught:
                    validate_instance_record(tampered)
                self.assertEqual(
                    caught.exception.code,
                    "invalid_instance_record",
                )

    def test_receipt_environment_identity_is_canonical_to_pod_payload(self):
        record = self.launch()

        changed_hash = copy.deepcopy(record)
        changed_hash["expected"]["environment_sha256"] = "0" * 64
        with self.assertRaises(RunpodLocalError) as caught_hash:
            validate_instance_record(changed_hash)
        self.assertEqual(
            caught_hash.exception.code,
            "invalid_instance_record",
        )

        changed_auth = copy.deepcopy(record)
        changed_auth["expected"]["has_registry_auth"] = True
        with self.assertRaises(RunpodLocalError) as caught_auth:
            validate_instance_record(changed_auth)
        self.assertEqual(
            caught_auth.exception.code,
            "invalid_instance_record",
        )

    def test_template_drift_fails_before_account_or_create_request(self):
        self.launch_profile = template_profile(
            identity_file=str(self.identity)
        )
        drifted = dict(self.launch_profile["pod"]["template_contract"])
        drifted["docker_start_cmd"] = ["different\n"]
        self.api.templates["template123"] = drifted

        with self.assertRaises(RunpodLocalError) as caught:
            self.launch()

        self.assertEqual(caught.exception.code, "template_contract_drift")
        self.assertEqual(self.api.account_ssh_attestation_calls, [])
        self.assertEqual(self.api.create_calls, 0)
        self.assertEqual(
            InstanceStore(self.state).load("compiler")["phase"],
            "intent",
        )

    def test_template_drift_during_preflight_fails_before_create_request(self):
        self.launch_profile = template_profile(
            identity_file=str(self.identity)
        )
        contract = self.launch_profile["pod"]["template_contract"]
        self.api.templates["template123"] = dict(contract)

        def drift_after_initial_attestation(call_count):
            if call_count == 2:
                self.api.templates["template123"][
                    "docker_start_cmd"
                ] = ["provider-secret=must-not-escape\n"]

        self.api.before_get_template = drift_after_initial_attestation
        with self.assertRaises(RunpodLocalError) as caught:
            self.launch()

        self.assertEqual(caught.exception.code, "template_contract_drift")
        self.assertNotIn("must-not-escape", str(caught.exception))
        self.assertEqual(len(self.api.account_ssh_attestation_calls), 1)
        self.assertEqual(self.api.create_calls, 0)
        self.assertEqual(
            InstanceStore(self.state).load("compiler")["phase"],
            "intent",
        )

    def test_template_allocation_attests_resolved_image_and_commands(self):
        self.launch_profile = template_profile(
            identity_file=str(self.identity)
        )
        contract = self.launch_profile["pod"]["template_contract"]
        self.api.templates["template123"] = dict(contract)
        record = self.launch()
        pod = dict(self.api.pods[0])

        for field, replacement in (
            (
                "image",
                "private/secret-registry-material@sha256:" + "2" * 64,
            ),
            ("docker_entrypoint", ["/bin/sh", "-c"]),
            (
                "docker_start_cmd",
                ["PROVIDER_SECRET=must-not-escape\n"],
            ),
        ):
            with self.subTest(field=field):
                drifted = dict(pod)
                drifted[field] = replacement
                violations, _ = verify_allocated_pod(record, drifted)
                self.assertTrue(
                    any(
                        violation.startswith(f"{field}:")
                        for violation in violations
                    ),
                    violations,
                )
                self.assertNotIn("must-not-escape", repr(violations))
                self.assertNotIn(
                    "secret-registry-material",
                    repr(violations),
                )

    def test_live_allocation_rejects_boolean_integer_type_aliases(self):
        record = self.launch()
        baseline = dict(self.api.pods[0])
        aliases = (
            ("gpu_count", True),
            ("secure_cloud", 1),
            ("interruptible", 0),
            ("locked", 0),
            ("volume_in_gb", False),
            ("volume_in_gb", 0.0),
            ("has_registry_auth", 0),
        )

        for field, value in aliases:
            with self.subTest(field=field, value=value):
                pod = dict(baseline)
                pod[field] = value
                violations, _ = verify_allocated_pod(record, pod)
                self.assertTrue(
                    any(
                        violation.startswith(f"{field}:")
                        for violation in violations
                    ),
                    violations,
                )

    def test_rest_normalization_rejects_present_malformed_launch_facts(self):
        self.launch_profile = template_profile(
            identity_file=str(self.identity)
        )
        contract = self.launch_profile["pod"]["template_contract"]
        self.api.templates["template123"] = dict(contract)
        record = self.launch()
        raw = {
            "id": record["pod_id"],
            "name": record["remote_name"],
            "desiredStatus": "RUNNING",
            "image": contract["image"],
            "templateId": "template123",
            "dockerEntrypoint": contract["docker_entrypoint"],
            "dockerStartCmd": contract["docker_start_cmd"],
            "containerDiskInGb": 50,
            "volumeInGb": 0,
            "volumeMountPath": "/workspace",
            "env": dict(record["pod_payload"]["env"]),
            "containerRegistryAuthId": None,
            "interruptible": False,
            "locked": False,
            "gpu": {"id": GPU_ID, "count": 1},
            "gpuCount": 1,
            "adjustedCostPerHr": 1.99,
            "machine": {
                "dataCenterId": "US-NC-2",
                "secureCloud": True,
                "gpuTypeId": GPU_ID,
            },
            "networkVolume": {
                "id": "volume123",
                "name": "model-cache",
                "size": 500,
                "dataCenterId": "US-NC-2",
            },
            "ports": ["22/tcp"],
            "portMappings": {},
        }
        secret = "NORMALIZED_PROVIDER_SECRET"
        malformed_cases = (
            ("dockerEntrypoint", {"secret": secret}, "docker_entrypoint"),
            ("dockerStartCmd", [{"secret": secret}], "docker_start_cmd"),
            ("ports", {"secret": secret}, "ports"),
            ("portMappings", secret, "port_mappings"),
            ("portMappings", {"22": secret}, "port_mappings"),
            ("networkVolume", secret, "network_volume"),
            ("machine", secret, "machine"),
            ("gpu", secret, "gpu"),
            ("adjustedCostPerHr", 10**4000, "cost_per_hour"),
        )

        for raw_field, malformed, violation_field in malformed_cases:
            with self.subTest(field=raw_field):
                candidate = dict(raw)
                candidate[raw_field] = malformed
                normalized = normalize_pod(candidate)
                violations, _ = verify_allocated_pod(record, normalized)
                self.assertTrue(
                    any(
                        violation.startswith(f"{violation_field}:")
                        for violation in violations
                    ),
                    violations,
                )
                self.assertNotIn(secret, repr(normalized))
                self.assertNotIn(secret, repr(violations))

        wrong_volume_dc = copy.deepcopy(raw)
        wrong_volume_dc["networkVolume"]["dataCenterId"] = "OTHER-DC"
        violations, _ = verify_allocated_pod(
            record,
            normalize_pod(wrong_volume_dc),
        )
        self.assertTrue(
            any(
                violation.startswith(
                    "network_volume_data_center_id:"
                )
                for violation in violations
            ),
            violations,
        )

    def test_template_receipt_cannot_change_zero_local_volume(self):
        self.launch_profile = template_profile(
            identity_file=str(self.identity)
        )
        contract = self.launch_profile["pod"]["template_contract"]
        self.api.templates["template123"] = dict(contract)
        record = self.launch()
        record["expected"]["volume_in_gb"] = 20

        with self.assertRaises(RunpodLocalError) as caught:
            validate_instance_record(record)

        self.assertEqual(caught.exception.code, "invalid_instance_record")

    def test_allocation_verification_rejects_every_known_policy_mismatch(self):
        record = self.launch()
        baseline = dict(self.api.pods[0])
        mismatches = {
            "id": "other-pod",
            "name": "other-name",
            "gpu_id": "NVIDIA H200",
            "gpu_count": 2,
            "data_center_id": "OTHER-DC",
            "network_volume_id": "other-volume",
            "secure_cloud": False,
            "interruptible": True,
            "locked": True,
            "image": "other/image:tag",
            "container_disk_gb": 51,
            "volume_in_gb": 1,
            "volume_mount_path": "/other",
            "environment_names": ["INJECTED"],
            "environment_sha256": "0" * 64,
            "has_registry_auth": True,
            "desired_status": "EXITED",
            "ports": ["22/tcp", "8000/http"],
            "cost_per_hour": 3.01,
        }

        for field, value in mismatches.items():
            with self.subTest(field=field):
                pod = dict(baseline)
                pod[field] = value
                violations, _ = verify_allocated_pod(record, pod)
                self.assertTrue(
                    any(violation.startswith(f"{field}:") for violation in violations),
                    violations,
                )

    def test_unknown_allocation_fields_remain_provisioning_not_mismatch(self):
        record = self.launch()
        pod = dict(self.api.pods[0])
        for field in (
            "gpu_id",
            "gpu_count",
            "data_center_id",
            "network_volume_id",
            "secure_cloud",
            "interruptible",
            "locked",
            "image",
            "container_disk_gb",
            "volume_in_gb",
            "volume_mount_path",
            "desired_status",
            "cost_per_hour",
        ):
            pod[field] = None
        pod["gpu_status"] = "missing"
        pod["machine_status"] = "missing"
        pod["network_volume_status"] = "missing"
        pod["cost_status"] = "missing"
        pod["environment_status"] = "missing"
        pod["environment_names"] = None
        pod["environment_sha256"] = None
        pod["registry_auth_status"] = "missing"
        pod["has_registry_auth"] = None
        pod["ports_status"] = "missing"
        pod["ports"] = []

        violations, pending = verify_allocated_pod(record, pod)

        self.assertEqual(violations, [])
        self.assertIn("network_volume", pending)
        self.assertIn("environment", pending)
        self.assertIn("registry_auth", pending)
        self.assertIn("ports", pending)

    def test_ephemeral_allocation_accepts_absent_network_volume(self):
        record = self.launch()
        record["expected"]["network_volume_id"] = None
        pod = dict(self.api.pods[0])
        pod["network_volume_status"] = "missing"
        pod["network_volume_id"] = None
        pod["network_volume_data_center_id"] = None
        pod["network_volume"] = None

        violations, pending = verify_allocated_pod(record, pod)

        self.assertEqual(violations, [])
        self.assertNotIn("network_volume_id", pending)

        unexpected = dict(pod)
        unexpected["network_volume_status"] = "valid"
        unexpected["network_volume"] = {}
        violations, _ = verify_allocated_pod(record, unexpected)
        self.assertIn("network_volume: unexpected", violations)

    def test_failed_rollback_keeps_exact_pod_id_for_retry(self):
        self.api.created_cost = 3.01
        self.api.delete_error = HttpRequestError(
            "fixture service failure", status=503
        )

        with self.assertRaises(RunpodLocalError) as caught:
            self.launch()

        record = InstanceStore(self.state).load("compiler")
        self.assertEqual(caught.exception.code, "rollback_required")
        self.assertEqual(record["phase"], "rollback_required")
        self.assertEqual(record["pod_id"], "pod123")

    def test_intent_key_failure_prevents_first_billable_request(self):
        def reject_key_pair(_identity, _public_key):
            raise RunpodLocalError(
                "fixture key mismatch",
                code="ssh_key_mismatch",
            )

        self.manager.key_pair_validator = reject_key_pair
        with self.assertRaises(RunpodLocalError) as caught:
            self.launch()

        record = InstanceStore(self.state).load("compiler")
        self.assertEqual(caught.exception.code, "ssh_key_mismatch")
        self.assertEqual(record["phase"], "intent")
        self.assertEqual(self.api.create_calls, 0)

        self.manager.key_pair_validator = lambda _identity, _public: None
        resumed = self.launch()
        self.assertEqual(resumed["phase"], "active")
        self.assertEqual(self.api.create_calls, 1)

    def test_account_key_failure_leaves_retryable_intent_before_post(self):
        self.api.account_ssh_attestation_error = RunpodLocalError(
            "fixture account key is missing",
            code="account_ssh_key_not_authorized",
        )

        with self.assertRaises(RunpodLocalError) as caught:
            self.launch()

        record = InstanceStore(self.state).load("compiler")
        self.assertEqual(
            caught.exception.code, "account_ssh_key_not_authorized"
        )
        self.assertEqual(record["phase"], "intent")
        self.assertIsNone(record["lease"])
        self.assertNotIn("submission_started_at", record)
        self.assertEqual(self.api.create_calls, 0)
        self.assertEqual(
            self.api.account_ssh_attestation_calls, [SSH_PUBLIC_KEY]
        )

        self.api.account_ssh_attestation_error = None
        resumed = self.launch()

        self.assertEqual(resumed["phase"], "active")
        self.assertEqual(self.api.create_calls, 1)

    def test_provider_deadline_closes_absent_ambiguous_submission(self):
        self.api.create_error = HttpRequestError("fixture timeout")
        with self.assertRaises(HttpRequestError):
            self.launch()
        record = InstanceStore(self.state).load("compiler")
        self.api.create_error = None

        with self.assertRaises(RunpodLocalError) as early:
            self.manager.terminate(
                "compiler",
                execute=True,
                reason="operator",
            )
        self.assertEqual(early.exception.code, "submission_ambiguous")

        self.clock.now = parse_utc_timestamp(
            record["provider_termination_at"]
        )
        plan = self.manager.terminate(
            "compiler",
            execute=False,
            reason="operator",
        )
        self.assertEqual(plan["action"], "close_expired_submission")
        self.assertEqual(
            InstanceStore(self.state).load("compiler")["phase"],
            "submitting",
        )

        closed = self.manager.enforce_ttl(execute=True)

        self.assertTrue(closed["actions"][0]["executed"])
        self.assertEqual(
            closed["actions"][0]["termination"]["action"],
            "close_expired_submission",
        )
        self.assertEqual(
            InstanceStore(self.state).load("compiler")["phase"],
            "aborted",
        )

        late = self.api.pod_for_payload(record["pod_payload"])
        self.api.pods.append(late)
        status = self.manager.status("compiler", live=True)
        self.assertEqual(status["unmanaged_pods"], [])
        self.assertIn(
            "terminal_receipt_has_live_pod",
            status["instances"][0]["drift"],
        )
        recovered = self.manager.enforce_ttl(execute=True)
        self.assertEqual(
            recovered["actions"][0]["termination"]["action"],
            "delete_terminal_pod_leak",
        )
        self.assertEqual(self.api.delete_calls, ["pod123"])

    def test_expired_terminal_receipt_captures_late_duplicate_leak(self):
        self.api.create_error = HttpRequestError("fixture timeout")
        with self.assertRaises(HttpRequestError):
            self.launch()
        record = InstanceStore(self.state).load("compiler")
        self.clock.now = parse_utc_timestamp(
            record["provider_termination_at"]
        )
        self.manager.terminate(
            "compiler",
            execute=True,
            reason="provider_deadline",
        )
        terminal = InstanceStore(self.state).load("compiler")
        first = self.api.pod_for_payload(terminal["pod_payload"])
        second = dict(first)
        second["id"] = "pod456"
        self.api.pods = [first, second]

        plan = self.manager.terminate(
            "compiler",
            execute=False,
            reason="terminal_leak_recovery",
        )
        unchanged = InstanceStore(self.state).load("compiler")

        self.assertEqual(plan["action"], "delete_terminal_conflicted_pods")
        self.assertEqual(unchanged["phase"], "aborted")
        self.assertNotIn("conflict_pod_ids", unchanged)

        result = self.manager.terminate(
            "compiler",
            execute=True,
            reason="terminal_leak_recovery",
        )

        self.assertEqual(
            result["action"],
            "delete_terminal_conflicted_pods",
        )
        self.assertEqual(self.api.delete_calls, ["pod123", "pod456"])
        cleaned = InstanceStore(self.state).load("compiler")
        self.assertEqual(cleaned["phase"], "aborted")
        self.assertEqual(
            cleaned["conflict_pod_ids"],
            ["pod123", "pod456"],
        )

    def test_malformed_late_duplicate_identity_never_mutates_or_deletes(self):
        self.api.create_error = HttpRequestError("fixture timeout")
        with self.assertRaises(HttpRequestError):
            self.launch()
        record = InstanceStore(self.state).load("compiler")
        first = self.api.pod_for_payload(record["pod_payload"])
        second = dict(first)
        self.api.pods = [first, second]
        before = copy.deepcopy(record)

        with self.assertRaises(RunpodLocalError) as caught:
            self.manager.terminate(
                "compiler",
                execute=True,
                reason="operator",
            )

        self.assertEqual(caught.exception.code, "invalid_provider_response")
        self.assertEqual(
            InstanceStore(self.state).load("compiler"),
            before,
        )
        self.assertEqual(self.api.delete_calls, [])

    def test_down_is_plan_only_then_deletes_pod_but_never_volume(self):
        self.launch()

        plan = self.manager.terminate(
            "compiler", execute=False, reason="operator"
        )
        self.assertEqual(plan["volume_action"], "preserve")
        self.assertEqual(self.api.delete_calls, [])

        self.manager.terminate("compiler", execute=True, reason="operator")

        self.assertEqual(self.api.delete_calls, ["pod123"])
        self.assertEqual(self.api.volume_delete_calls, [])
        self.assertEqual(
            InstanceStore(self.state).load("compiler")["phase"], "terminated"
        )

    def test_terminal_exact_identity_leak_can_be_cleaned_up(self):
        self.launch()
        self.manager.terminate("compiler", execute=True, reason="operator")
        terminal = InstanceStore(self.state).load("compiler")
        self.api.pods.append(
            self.api.pod_for_payload(terminal["pod_payload"])
        )

        plan = self.manager.terminate(
            "compiler", execute=False, reason="terminal_leak_recovery"
        )
        self.assertEqual(plan["action"], "delete_terminal_pod_leak")

        self.manager.terminate(
            "compiler", execute=True, reason="terminal_leak_recovery"
        )
        self.assertEqual(self.api.pods, [])
        self.assertEqual(
            InstanceStore(self.state).load("compiler")["phase"],
            "terminated",
        )

    def test_terminal_leak_identity_survives_delete_failure_and_rename(self):
        self.api.create_error = HttpRequestError("fixture timeout")
        with self.assertRaises(HttpRequestError):
            self.launch()
        record = InstanceStore(self.state).load("compiler")
        self.clock.now = parse_utc_timestamp(
            record["provider_termination_at"]
        )
        self.manager.terminate(
            "compiler",
            execute=True,
            reason="provider_deadline",
        )
        late = self.api.pod_for_payload(record["pod_payload"])
        self.api.pods = [late]
        self.api.delete_error = HttpRequestError(
            "fixture delete failure",
            status=503,
        )

        with self.assertRaises(HttpRequestError):
            self.manager.terminate(
                "compiler",
                execute=True,
                reason="terminal_leak_recovery",
            )

        durable = InstanceStore(self.state).load("compiler")
        self.assertEqual(durable["phase"], "aborted")
        self.assertEqual(durable["pod_id"], "pod123")
        self.api.delete_error = None
        self.api.pods[0]["name"] = "renamed-by-another-controller"

        status = self.manager.status("compiler", live=True)
        self.assertEqual(status["unmanaged_pods"], [])
        self.assertIn(
            "pod_identity_conflict",
            status["instances"][0]["drift"],
        )
        with self.assertRaises(RunpodLocalError) as caught:
            self.manager.plan_launch(
                "compiler",
                self.launch_profile,
                ttl_seconds=1800,
                idle_timeout_seconds=900,
            )
        self.assertEqual(caught.exception.code, "pod_identity_conflict")
        self.assertEqual(self.api.create_calls, 1)

    def test_ttl_scan_carries_terminal_identity_across_rename(self):
        record = self.launch()
        self.manager.terminate(
            "compiler",
            execute=True,
            reason="operator",
        )
        replacement = self.api.pod_for_payload(record["pod_payload"])
        replacement["id"] = "pod456"
        self.api.pods = [replacement]
        list_pods = self.api.list_pods
        calls = 0

        def observe_then_rename():
            nonlocal calls
            calls += 1
            result = list_pods()
            if calls == 1:
                self.api.pods[0]["name"] = (
                    "renamed-by-another-controller"
                )
            return result

        self.api.list_pods = observe_then_rename
        first = self.manager.enforce_ttl(execute=True)

        self.assertEqual(
            first["actions"][0]["error"]["code"],
            "conflict_identity_expanded",
        )
        durable = InstanceStore(self.state).load("compiler")
        self.assertEqual(
            durable["conflict_pod_ids"],
            ["pod123", "pod456"],
        )
        self.assertIn("conflict_review_required_at", durable)
        self.assertNotIn("conflict_cleanup_requested_at", durable)
        self.assertEqual(self.api.delete_calls, ["pod123"])

        second = self.manager.enforce_ttl(execute=True)
        self.assertEqual(
            second["actions"][0]["error"]["code"],
            "conflict_identity_changed",
        )
        self.assertEqual(self.api.delete_calls, ["pod123"])
        status = self.manager.status("compiler", live=True)
        self.assertEqual(status["unmanaged_pods"], [])
        with self.assertRaises(RunpodLocalError) as blocked:
            self.launch()
        self.assertEqual(
            blocked.exception.code,
            "conflict_review_required",
        )
        self.assertEqual(self.api.create_calls, 1)

    def test_terminal_conflict_expansion_needs_new_explicit_cleanup(self):
        self.api.create_error = HttpRequestError("fixture timeout")
        with self.assertRaises(HttpRequestError):
            self.launch()
        record = InstanceStore(self.state).load("compiler")
        self.clock.now = parse_utc_timestamp(
            record["provider_termination_at"]
        )
        self.manager.terminate(
            "compiler",
            execute=True,
            reason="provider_deadline",
        )
        first = self.api.pod_for_payload(record["pod_payload"])
        second = dict(first)
        second["id"] = "pod456"
        self.api.pods = [first, second]
        self.manager.terminate(
            "compiler",
            execute=True,
            reason="operator",
        )
        terminal = InstanceStore(self.state).load("compiler")
        self.assertIn("conflict_cleanup_requested_at", terminal)

        unreviewed = self.api.pod_for_payload(record["pod_payload"])
        unreviewed["id"] = "pod789"
        self.api.pods = [unreviewed]
        first_watch = self.manager.enforce_ttl(execute=True)

        self.assertEqual(
            first_watch["actions"][0]["error"]["code"],
            "conflict_identity_expanded",
        )
        expanded = InstanceStore(self.state).load("compiler")
        self.assertEqual(
            expanded["conflict_pod_ids"],
            ["pod123", "pod456", "pod789"],
        )
        self.assertIn("conflict_review_required_at", expanded)
        self.assertNotIn("conflict_cleanup_requested_at", expanded)
        deletes_before_second_watch = list(self.api.delete_calls)

        second_watch = self.manager.enforce_ttl(execute=True)

        self.assertEqual(
            second_watch["actions"][0]["error"]["code"],
            "conflict_review_required",
        )
        self.assertEqual(
            self.api.delete_calls,
            deletes_before_second_watch,
        )
        reviewed = self.manager.terminate(
            "compiler",
            execute=True,
            reason="operator_reviewed_expanded_set",
        )
        self.assertEqual(
            reviewed["action"],
            "delete_terminal_conflicted_pods",
        )
        self.assertEqual(self.api.delete_calls[-1], "pod789")
        authorized = InstanceStore(self.state).load("compiler")
        self.assertIn("conflict_cleanup_requested_at", authorized)
        self.assertNotIn("conflict_review_required_at", authorized)

    def test_stale_operation_identity_cannot_delete_reused_local_name(self):
        first = self.launch()
        self.manager.terminate("compiler", execute=True, reason="operator")
        self.manager.uuid_factory = lambda: uuid.UUID(
            "87654321-4321-4321-8321-ba9876543210"
        )
        second = self.launch()

        with self.assertRaises(RunpodLocalError) as caught:
            self.manager.terminate(
                "compiler",
                execute=True,
                reason="stale_watcher",
                expected_operation_id=first["operation_id"],
                require_expired=True,
            )

        self.assertEqual(caught.exception.code, "instance_identity_changed")
        self.assertEqual(second["phase"], "active")
        self.assertEqual(self.api.delete_calls, ["pod123"])

    def test_expired_lease_cannot_be_touched_or_extended(self):
        record = self.launch()
        store = InstanceStore(self.state)
        self.clock.now = parse_utc_timestamp(record["lease"]["expires_at"])

        self.assertEqual(
            lease_expiry_reasons(record, now=self.clock.now),
            ["hard_ttl", "explicit_heartbeat_idle_timeout"],
        )
        with self.assertRaises(RunpodLocalError) as touch_error:
            store.touch(
                "compiler", now=self.clock.now, source="fixture_activity"
            )
        with self.assertRaises(RunpodLocalError) as extend_error:
            store.extend_ttl(
                "compiler", extension_seconds=60, now=self.clock.now
            )
        self.assertEqual(touch_error.exception.code, "lease_expired")
        self.assertEqual(extend_error.exception.code, "lease_expired")

    def test_ttl_enforcement_is_plan_only_then_terminates(self):
        record = self.launch()
        self.clock.now = parse_utc_timestamp(record["lease"]["expires_at"])

        plan = self.manager.enforce_ttl(execute=False)
        self.assertEqual(
            plan["actions"][0]["reasons"],
            ["hard_ttl", "explicit_heartbeat_idle_timeout"],
        )
        self.assertEqual(self.api.delete_calls, [])

        result = self.manager.enforce_ttl(execute=True)

        self.assertTrue(result["actions"][0]["executed"])
        self.assertEqual(self.api.delete_calls, ["pod123"])

    def test_failed_ttl_delete_is_retried_until_receipt_is_terminal(self):
        record = self.launch()
        self.clock.now = parse_utc_timestamp(record["lease"]["expires_at"])
        self.api.delete_error = HttpRequestError(
            "fixture transient delete failure", status=503
        )

        first = self.manager.enforce_ttl(execute=True)

        self.assertIn("error", first["actions"][0])
        self.assertEqual(
            InstanceStore(self.state).load("compiler")["phase"],
            "termination_pending",
        )

        self.api.delete_error = None
        second = self.manager.enforce_ttl(execute=True)

        self.assertEqual(
            second["actions"][0]["reasons"], ["termination_retry"]
        )
        self.assertTrue(second["actions"][0]["executed"])
        self.assertEqual(
            InstanceStore(self.state).load("compiler")["phase"],
            "terminated",
        )

    def test_stale_ttl_scan_cannot_delete_a_freshly_heartbeated_lease(self):
        record = self.launch()
        old_deadline = parse_utc_timestamp(
            record["lease"]["last_activity_at"]
        ) + datetime.timedelta(
            seconds=record["lease"]["idle_timeout_seconds"]
        )
        self.clock.now = old_deadline + datetime.timedelta(seconds=1)
        self.assertIn(
            "explicit_heartbeat_idle_timeout",
            lease_expiry_reasons(record, now=self.clock.now),
        )
        InstanceStore(self.state).touch(
            "compiler",
            now=old_deadline - datetime.timedelta(seconds=1),
            source="racing_heartbeat",
            expected_operation_id=record["operation_id"],
            expected_pod_id=record["pod_id"],
        )

        result = self.manager.terminate(
            "compiler",
            execute=True,
            reason="stale_idle_scan",
            expected_operation_id=record["operation_id"],
            require_expired=True,
        )

        self.assertEqual(result["action"], "lease_no_longer_expired")
        self.assertFalse(result["executed"])
        self.assertEqual(self.api.delete_calls, [])

    def test_local_ttl_cannot_move_past_immutable_provider_deadline(self):
        record = self.launch()
        activated_at = parse_utc_timestamp(record["lease"]["activated_at"])
        self.clock.now = activated_at + datetime.timedelta(seconds=300)

        updated = InstanceStore(self.state).set_ttl(
            "compiler",
            ttl_seconds=1800,
            now=self.clock.now,
        )

        self.assertEqual(
            parse_utc_timestamp(updated["lease"]["expires_at"]),
            activated_at + datetime.timedelta(seconds=1800),
        )
        restored = InstanceStore(self.state).extend_ttl(
            "compiler",
            extension_seconds=1800,
            now=self.clock.now,
        )
        self.assertEqual(
            restored["lease"]["expires_at"],
            record["provider_termination_at"],
        )

        with self.assertRaises(RunpodLocalError) as extend_error:
            InstanceStore(self.state).extend_ttl(
                "compiler",
                extension_seconds=1,
                now=self.clock.now,
            )
        with self.assertRaises(RunpodLocalError) as set_error:
            InstanceStore(self.state).set_ttl(
                "compiler",
                ttl_seconds=3601,
                now=self.clock.now,
            )

        self.assertEqual(
            extend_error.exception.code,
            "provider_deadline_exceeded",
        )
        self.assertEqual(
            set_error.exception.code,
            "provider_deadline_exceeded",
        )

    def test_provider_deadline_and_lease_invariants_reject_tampering(self):
        record = self.launch()
        cases = {}

        mismatched_payload = copy.deepcopy(record)
        mismatched_payload["pod_payload"]["terminateAfter"] = (
            "2026-07-26T21:00:01Z"
        )
        cases["record_payload_mismatch"] = mismatched_payload

        mismatched_intent_clock = copy.deepcopy(record)
        mismatched_intent_clock["provider_termination_at"] = (
            "2026-07-26T21:00:01Z"
        )
        mismatched_intent_clock["pod_payload"]["terminateAfter"] = (
            "2026-07-26T21:00:01Z"
        )
        cases["intent_clock_mismatch"] = mismatched_intent_clock

        lease_past_provider = copy.deepcopy(record)
        lease_past_provider["lease"]["ttl_seconds"] = 3601
        lease_past_provider["lease"]["expires_at"] = (
            "2026-07-26T21:00:01Z"
        )
        cases["lease_past_provider"] = lease_past_provider

        bad_extension_history = copy.deepcopy(record)
        bad_extension_history["lease"]["extensions_total_seconds"] = 1
        cases["bad_extension_history"] = bad_extension_history

        missing_provider_deadline = copy.deepcopy(record)
        missing_provider_deadline.pop("provider_termination_at")
        missing_provider_deadline["pod_payload"].pop("terminateAfter")
        missing_provider_deadline["pod_payload_sha256"] = json_document_hash(
            missing_provider_deadline["pod_payload"]
        )
        cases["missing_provider_deadline"] = missing_provider_deadline

        unsupported_schema = copy.deepcopy(record)
        unsupported_schema["schema_version"] = "runpod.instance.v1"
        cases["unsupported_schema"] = unsupported_schema

        for label, candidate in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(RunpodLocalError) as caught:
                    validate_instance_record(candidate)
                self.assertEqual(
                    caught.exception.code,
                    "invalid_instance_record",
                )

if __name__ == "__main__":
    unittest.main()
