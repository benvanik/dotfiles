from __future__ import annotations

import datetime
import pathlib
import tempfile
import unittest
import uuid

from runpod_local.allocation import (
    select_launch_placement,
    verify_allocated_pod,
)
from runpod_local.errors import HttpRequestError, RunpodLocalError
from runpod_local.instances import InstanceStore, lease_expiry_reasons
from runpod_local.lifecycle import LifecycleManager
from runpod_local.profile import create_profile
from runpod_local.state import StateStore
from runpod_local.timeutil import parse_utc_timestamp


GPU_ID = "NVIDIA RTX PRO 6000 Blackwell Server Edition"
NOW = datetime.datetime(2026, 7, 26, 20, 0, tzinfo=datetime.timezone.utc)
OPERATION_ID = uuid.UUID("12345678-1234-4234-8234-123456789abc")
SSH_PUBLIC_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f "
    "fixture@example"
)


def profile(**overrides):
    arguments = {
        "name": "pro-dev",
        "gpu_names": ["pro6000", "h200"],
        "max_hourly_usd": 3.0,
        "default_ttl_seconds": 3600,
        "image_name": "runpod/pytorch:fixture",
        "network_volume_id": "volume123",
        "ssh_public_key": SSH_PUBLIC_KEY,
    }
    arguments.update(overrides)
    return create_profile(**arguments)


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

    def get_network_volume(self, volume_id):
        return {
            "id": volume_id,
            "name": "model-cache",
            "size_gb": 500,
            "data_center_id": "US-NC-2",
        }

    def stock(self, **_):
        return stock()

    def list_pods(self):
        return [dict(pod) for pod in self.pods]

    def create_pod(self, payload):
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
        return {
            "id": "pod123",
            "name": payload["name"],
            "desired_status": "RUNNING",
            "image": payload.get("imageName"),
            "template_id": payload.get("templateId"),
            "interruptible": False,
            "locked": False,
            "gpu_id": payload["gpuTypeIds"][0],
            "gpu_count": payload["gpuCount"],
            "cost_per_hour": self.created_cost,
            "data_center_id": "US-NC-2",
            "secure_cloud": True,
            "machine_id": "machine123",
            "network_volume_id": payload.get("networkVolumeId"),
            "network_volume": {
                "id": payload.get("networkVolumeId"),
                "name": "model-cache",
                "size_gb": 500,
                "data_center_id": "US-NC-2",
            },
            "public_ip": None,
            "port_mappings": {},
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

        def observe_intent():
            record = store.load("compiler")
            self.assertEqual(record["phase"], "submitting")
            self.assertIsNone(record["pod_id"])
            self.clock.now = NOW + datetime.timedelta(seconds=60)

        self.api.before_create = observe_intent
        record = self.launch()

        self.assertEqual(record["phase"], "active")
        self.assertEqual(
            parse_utc_timestamp(record["lease"]["expires_at"]),
            NOW + datetime.timedelta(seconds=3600),
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

    def test_duplicate_reconciliation_names_enter_conflict(self):
        self.api.create_error = HttpRequestError("fixture timeout")
        with self.assertRaises(HttpRequestError):
            self.launch()
        record = InstanceStore(self.state).load("compiler")
        payload = record["pod_payload"]
        first = self.api.pod_for_payload(payload)
        second = dict(first)
        second["id"] = "pod456"
        self.api.pods = [first, second]

        with self.assertRaises(RunpodLocalError) as caught:
            self.launch()

        self.assertEqual(caught.exception.code, "duplicate_remote_name")
        self.assertEqual(
            InstanceStore(self.state).load("compiler")["phase"], "conflict"
        )

    def test_over_cap_allocation_is_deleted_and_recorded(self):
        self.api.created_cost = 3.01

        with self.assertRaises(RunpodLocalError) as caught:
            self.launch()

        self.assertEqual(caught.exception.code, "allocation_rejected")
        self.assertEqual(self.api.delete_calls, ["pod123"])
        self.assertEqual(
            InstanceStore(self.state).load("compiler")["phase"], "rolled_back"
        )

    def test_allocation_verification_rejects_every_known_policy_mismatch(self):
        record = self.launch()
        baseline = dict(record["provider"])
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
        pod = dict(record["provider"])
        for field in (
            "gpu_id",
            "gpu_count",
            "data_center_id",
            "network_volume_id",
            "secure_cloud",
            "interruptible",
            "locked",
            "image",
            "desired_status",
            "cost_per_hour",
        ):
            pod[field] = None
        pod["ports"] = []

        violations, pending = verify_allocated_pod(record, pod)

        self.assertEqual(violations, [])
        self.assertIn("network_volume_id", pending)
        self.assertIn("ports", pending)

    def test_ephemeral_allocation_accepts_absent_network_volume(self):
        record = self.launch()
        record["expected"]["network_volume_id"] = None
        pod = dict(record["provider"])
        pod["network_volume_id"] = None

        violations, pending = verify_allocated_pod(record, pod)

        self.assertEqual(violations, [])
        self.assertNotIn("network_volume_id", pending)

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
        self.api.pods.append(dict(terminal["provider"]))

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

    def test_hard_ttl_set_remains_anchored_to_submission(self):
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


if __name__ == "__main__":
    unittest.main()
