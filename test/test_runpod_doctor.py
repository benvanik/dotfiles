from __future__ import annotations

import pathlib
import tempfile
import unittest

from runpod_local.auth import CredentialStore
from runpod_local.doctor import (
    CheckCollector,
    _check_live,
    run_doctor,
)
from runpod_local.state import StateStore


class FakeReadOnlyApi:
    def __init__(self):
        self.calls = []

    def list_pods(self):
        self.calls.append("list_pods")
        return [
            {
                "id": "unmanaged123",
                "name": "external-controller",
            }
        ]

    def list_network_volumes(self):
        self.calls.append("list_network_volumes")
        return []

    def stock(self, **_):
        self.calls.append("stock")
        return {
            "gpus": [
                {
                    "gpu_id": "NVIDIA H200",
                    "on_demand_price_per_gpu_hour": 4.39,
                }
            ]
        }


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.state = StateStore(self.root / "state")
        self.credential_path = self.root / "config" / "api-key"

    def test_missing_credential_is_reported_without_creating_state(self):
        result = run_doctor(
            state=self.state,
            credential_store=CredentialStore(self.credential_path),
            live=False,
        )

        credential = next(
            check for check in result["checks"] if check["id"] == "credential"
        )
        self.assertEqual(credential["status"], "error")
        self.assertFalse(self.state.root.exists())

    def test_live_probe_uses_only_read_methods_and_reports_unmanaged_pod(self):
        api = FakeReadOnlyApi()
        collector = CheckCollector()

        _check_live(
            api=api,
            state=self.state,
            instances=[],
            collector=collector,
        )
        result = collector.result()

        self.assertEqual(
            api.calls, ["list_pods", "list_network_volumes", "stock"]
        )
        unmanaged = next(
            check
            for check in result["checks"]
            if check["id"] == "unmanaged_pods"
        )
        self.assertEqual(unmanaged["status"], "warning")
        self.assertNotIn("env", repr(result))

    def test_dangling_known_hosts_directory_symlink_is_an_error(self):
        self.state.root.mkdir(mode=0o700)
        ssh_directory = self.state.root / "ssh"
        ssh_directory.mkdir(mode=0o700)
        (ssh_directory / "known-hosts").symlink_to(
            self.root / "missing-known-hosts"
        )

        result = run_doctor(
            state=self.state,
            credential_store=CredentialStore(self.credential_path),
            live=False,
        )

        known_hosts = next(
            check
            for check in result["checks"]
            if check["id"] == "known_hosts"
        )
        self.assertEqual(known_hosts["status"], "error")

    def test_submitting_receipt_owns_its_unique_name_match(self):
        api = FakeReadOnlyApi()
        api.list_pods = lambda: [
            {
                "id": "pod123",
                "name": "rp-compiler-123456781234",
            }
        ]
        collector = CheckCollector()
        _check_live(
            api=api,
            state=self.state,
            instances=[
                {
                    "name": "compiler",
                    "remote_name": "rp-compiler-123456781234",
                    "phase": "submitting",
                    "pod_id": None,
                    "expected": {"network_volume_id": None},
                }
            ],
            collector=collector,
        )
        result = collector.result()

        submitting = next(
            check
            for check in result["checks"]
            if check["id"] == "submitting_pod_compiler"
        )
        unmanaged = next(
            check
            for check in result["checks"]
            if check["id"] == "unmanaged_pods"
        )
        self.assertEqual(submitting["status"], "warning")
        self.assertEqual(unmanaged["status"], "ok")

    def test_unsubmitted_intent_name_collision_remains_unmanaged(self):
        api = FakeReadOnlyApi()
        api.list_pods = lambda: [
            {
                "id": "foreign123",
                "name": "rp-compiler-123456781234",
            }
        ]
        collector = CheckCollector()
        _check_live(
            api=api,
            state=self.state,
            instances=[
                {
                    "name": "compiler",
                    "remote_name": "rp-compiler-123456781234",
                    "phase": "intent",
                    "pod_id": None,
                    "expected": {"network_volume_id": None},
                }
            ],
            collector=collector,
        )
        result = collector.result()

        collision = next(
            check
            for check in result["checks"]
            if check["id"] == "intent_pod_compiler"
        )
        unmanaged = next(
            check
            for check in result["checks"]
            if check["id"] == "unmanaged_pods"
        )
        self.assertEqual(collision["status"], "error")
        self.assertEqual(unmanaged["status"], "warning")
        self.assertEqual(unmanaged["details"]["pod_ids"], ["foreign123"])

    def test_unsubmitted_aborted_name_collision_remains_unmanaged(self):
        api = FakeReadOnlyApi()
        api.list_pods = lambda: [
            {
                "id": "foreign123",
                "name": "rp-compiler-123456781234",
            }
        ]
        collector = CheckCollector()
        _check_live(
            api=api,
            state=self.state,
            instances=[
                {
                    "name": "compiler",
                    "remote_name": "rp-compiler-123456781234",
                    "phase": "aborted",
                    "pod_id": None,
                    "expected": {"network_volume_id": None},
                }
            ],
            collector=collector,
        )
        result = collector.result()

        collision = next(
            check
            for check in result["checks"]
            if check["id"] == "unsubmitted_collision_compiler"
        )
        unmanaged = next(
            check
            for check in result["checks"]
            if check["id"] == "unmanaged_pods"
        )
        self.assertEqual(collision["status"], "error")
        self.assertEqual(unmanaged["status"], "warning")
        self.assertEqual(unmanaged["details"]["pod_ids"], ["foreign123"])

    def test_live_cleanup_pending_receipt_is_an_error(self):
        api = FakeReadOnlyApi()
        collector = CheckCollector()
        _check_live(
            api=api,
            state=self.state,
            instances=[
                {
                    "name": "compiler",
                    "remote_name": "external-controller",
                    "phase": "termination_pending",
                    "pod_id": "unmanaged123",
                    "expected": {"network_volume_id": None},
                }
            ],
            collector=collector,
        )
        result = collector.result()

        cleanup = next(
            check
            for check in result["checks"]
            if check["id"] == "cleanup_pod_compiler"
        )
        self.assertEqual(cleanup["status"], "error")

    def test_terminal_receipt_owns_a_late_unique_name_match(self):
        api = FakeReadOnlyApi()
        api.list_pods = lambda: [
            {
                "id": "pod123",
                "name": "rp-compiler-123456781234",
            }
        ]
        collector = CheckCollector()
        _check_live(
            api=api,
            state=self.state,
            instances=[
                {
                    "name": "compiler",
                    "remote_name": "rp-compiler-123456781234",
                    "phase": "aborted",
                    "pod_id": None,
                    "submission_started_at": "2026-07-26T20:00:00Z",
                    "expected": {"network_volume_id": None},
                }
            ],
            collector=collector,
        )
        result = collector.result()

        terminal = next(
            check
            for check in result["checks"]
            if check["id"] == "terminal_pod_compiler"
        )
        unmanaged = next(
            check
            for check in result["checks"]
            if check["id"] == "unmanaged_pods"
        )
        self.assertEqual(terminal["status"], "error")
        self.assertEqual(unmanaged["status"], "ok")

    def test_changed_conflict_id_is_managed_and_reported(self):
        api = FakeReadOnlyApi()
        api.list_pods = lambda: [
            {
                "id": "conflict456",
                "name": "renamed-by-another-controller",
            },
        ]
        collector = CheckCollector()
        _check_live(
            api=api,
            state=self.state,
            instances=[
                {
                    "name": "compiler",
                    "remote_name": "rp-compiler-operation",
                    "phase": "terminated",
                    "pod_id": None,
                    "expected": {"network_volume_id": None},
                    "conflict_pod_ids": ["conflict123", "conflict456"],
                }
            ],
            collector=collector,
        )
        result = collector.result()

        identity = next(
            check
            for check in result["checks"]
            if check["id"] == "conflict_identity_compiler"
        )
        unmanaged = next(
            check
            for check in result["checks"]
            if check["id"] == "unmanaged_pods"
        )
        self.assertEqual(identity["status"], "error")
        self.assertEqual(identity["details"]["pod_ids"], ["conflict456"])
        self.assertEqual(unmanaged["status"], "ok")


if __name__ == "__main__":
    unittest.main()
