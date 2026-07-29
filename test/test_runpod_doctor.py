from __future__ import annotations

import datetime
import pathlib
import tempfile
import unittest

from runpod_local.auth import CredentialStore
from runpod_local.claim_acquisition import ClaimAcquisitionStore
from runpod_local.claims import (
    ClaimStore,
    HostClaimRequest,
    default_allocation_from_host,
)
from runpod_local.doctor import (
    CheckCollector,
    _check_claim_state,
    _check_live,
    run_doctor,
)
from runpod_local.profile import ProfileStore
from runpod_local.state import StateStore
from runpod_local.timeutil import utc_timestamp


HOST_OPERATION_ID = "12345678-1234-4234-8234-123456789abc"


def claim_host(
    name: str,
    *,
    created_at: datetime.datetime,
    retention_mode: str = "while-claimed",
) -> dict:
    return {
        "name": name,
        "operation_id": HOST_OPERATION_ID,
        "pod_id": f"pod-{name}",
        "phase": "active",
        "created_at": utc_timestamp(created_at),
        "profile": {"name": "pro-dev", "sha256": "1" * 64},
        "provider_termination_at": utc_timestamp(
            created_at + datetime.timedelta(hours=2)
        ),
        "expected": {
            "gpu_id": "NVIDIA RTX PRO 6000 Blackwell Server Edition",
            "gpu_count": 1,
            "gpu_memory_gb": 96.0,
            "network_volume_id": "volume-123",
            "data_center_id": "EUR-IS-1",
            "image": "fixture/image@sha256:" + "2" * 64,
            "container_disk_gb": 50,
            "min_vcpu_count": 16,
            "min_ram_gb": 64,
        },
        "retention": {
            "mode": retention_mode,
            "empty_grace_seconds": 300,
        },
    }


def claim_request() -> HostClaimRequest:
    return HostClaimRequest(
        owner_system="fixture",
        owner_instance="doctor",
        owner_operation_id="doctor-acquisition-1",
        allowed_profile_names=("pro-dev",),
    )


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
        self.profiles = ProfileStore(self.root / "runpod")
        self.credential_path = self.root / "config" / "api-key"

    def admit_claim(
        self,
        host: dict,
        *,
        now: datetime.datetime,
        bind_acquisition: bool,
    ):
        request = claim_request()
        acquisitions = ClaimAcquisitionStore(self.state)
        acquisitions.begin(request, now=now)
        claims = ClaimStore(self.state)
        ledger = claims.initialize(
            host=host,
            allocation=default_allocation_from_host(host),
            retention=host["retention"]["mode"],
            empty_grace_seconds=300,
            now=now,
        )
        claim = claims.admit(
            ledger,
            request,
            now=now,
            claim_id="claim-" + "1" * 32,
        )
        if bind_acquisition:
            acquisitions.bind(request, claim, now=now)
        return request, claim

    def test_missing_credential_is_reported_without_creating_state(self):
        result = run_doctor(
            state=self.state,
            profiles=self.profiles,
            credential_store=CredentialStore(self.credential_path),
            live=False,
        )

        credential = next(
            check for check in result["checks"] if check["id"] == "credential"
        )
        self.assertEqual(credential["status"], "error")
        self.assertFalse(self.state.root.exists())
        self.assertFalse(self.profiles.root.exists())
        authored = next(
            check
            for check in result["checks"]
            if check["id"] == "authored_runpod_config"
        )
        volumes = next(
            check
            for check in result["checks"]
            if check["id"] == "authored_volume_configs"
        )
        self.assertEqual(authored["status"], "info")
        self.assertFalse(authored["details"]["parsed"])
        self.assertEqual(volumes["status"], "info")
        self.assertFalse(volumes["details"]["parsed"])

    def test_reserved_authored_files_are_reported_without_parsing(self):
        self.profiles.root.mkdir()
        (self.profiles.root / "runpod.toml").write_text(
            "this is intentionally not parsed\n",
            encoding="utf-8",
        )
        volume_directory = self.profiles.root / "volumes"
        volume_directory.mkdir()
        (volume_directory / "cache.toml").write_text(
            "also not parsed\n",
            encoding="utf-8",
        )

        result = run_doctor(
            state=self.state,
            profiles=self.profiles,
            credential_store=CredentialStore(self.credential_path),
            live=False,
        )

        authored = next(
            check
            for check in result["checks"]
            if check["id"] == "authored_runpod_config"
        )
        volumes = next(
            check
            for check in result["checks"]
            if check["id"] == "authored_volume_configs"
        )
        self.assertEqual(authored["status"], "info")
        self.assertFalse(authored["details"]["parsed"])
        self.assertEqual(volumes["details"]["file_count"], 1)
        self.assertFalse(volumes["details"]["parsed"])

    def test_claim_audit_reports_journal_orphan_and_due_retirement(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        created_at = now - datetime.timedelta(minutes=10)
        due_host = claim_host("due96", created_at=created_at)
        orphan_host = claim_host("orphan96", created_at=created_at)
        ClaimStore(self.state).initialize(
            host=due_host,
            allocation=default_allocation_from_host(due_host),
            retention="while-claimed",
            empty_grace_seconds=300,
            now=created_at,
        )
        ClaimAcquisitionStore(self.state).begin(
            claim_request(),
            now=created_at,
        )
        collector = CheckCollector()

        _check_claim_state(
            state=self.state,
            instances=[due_host, orphan_host],
            collector=collector,
        )
        result = collector.result()
        checks = {check["id"]: check for check in result["checks"]}

        self.assertEqual(checks["claim_ledgers"]["status"], "ok")
        self.assertEqual(checks["claim_acquisitions"]["status"], "ok")
        self.assertEqual(
            checks["claim_retirement_due96"]["status"],
            "warning",
        )
        self.assertEqual(
            checks["claim_orphan_orphan96"]["status"],
            "warning",
        )
        self.assertTrue(
            checks["claim_orphan_orphan96"]["details"]["due"]
        )

    def test_doctor_reports_quarantined_while_claimed_host_retirement(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        acquired_at = now - datetime.timedelta(minutes=2)
        host = claim_host("quarantine96", created_at=acquired_at)
        _, claim = self.admit_claim(
            host,
            now=acquired_at,
            bind_acquisition=False,
        )
        ClaimStore(self.state).expire_claims(
            ClaimStore(self.state).load(host["name"]),
            now=now,
        )
        collector = CheckCollector()

        _check_claim_state(
            state=self.state,
            instances=[host],
            collector=collector,
        )
        checks = {check["id"]: check for check in collector.result()["checks"]}

        quarantine = checks["claim_quarantine_quarantine96"]
        self.assertEqual(quarantine["status"], "warning")
        self.assertEqual(
            quarantine["details"]["claim_ids"],
            [claim.claim_id],
        )
        self.assertFalse(
            quarantine["details"]["manual_action_required"]
        )
        self.assertEqual(
            checks["claim_retirement_quarantine96"]["status"],
            "warning",
        )

    def test_doctor_marks_manual_quarantine_as_operator_error(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        acquired_at = now - datetime.timedelta(minutes=2)
        host = claim_host(
            "manual96",
            created_at=acquired_at,
            retention_mode="manual",
        )
        _, claim = self.admit_claim(
            host,
            now=acquired_at,
            bind_acquisition=False,
        )
        claims = ClaimStore(self.state)
        claims.expire_claims(
            claims.load(host["name"]),
            now=now,
        )
        collector = CheckCollector()

        _check_claim_state(
            state=self.state,
            instances=[host],
            collector=collector,
        )
        checks = {check["id"]: check for check in collector.result()["checks"]}

        quarantine = checks["claim_quarantine_manual96"]
        self.assertEqual(quarantine["status"], "error")
        self.assertEqual(
            quarantine["details"]["claim_ids"],
            [claim.claim_id],
        )
        self.assertTrue(
            quarantine["details"]["manual_action_required"]
        )
        self.assertNotIn("claim_retirement_manual96", checks)

    def test_malformed_state_records_do_not_abort_doctor_claim_scan(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        ClaimAcquisitionStore(self.state).begin(
            claim_request(),
            now=now,
        )
        self.state.write("instances", "broken96", {"not": "an instance"})
        self.state.write(
            "hostclaims",
            "broken96",
            {"schema_version": "not-a-claim-ledger"},
        )

        result = run_doctor(
            state=self.state,
            profiles=self.profiles,
            credential_store=CredentialStore(self.credential_path),
            live=False,
        )
        checks = {check["id"]: check for check in result["checks"]}

        self.assertEqual(
            checks["instance_record_broken96"]["status"],
            "error",
        )
        self.assertEqual(
            checks["claim_ledger_broken96"]["status"],
            "error",
        )
        self.assertEqual(checks["claim_acquisitions"]["status"], "ok")

    def test_doctor_reports_current_claim_awaiting_journal_binding(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        host = claim_host("current96", created_at=now)
        _, claim = self.admit_claim(
            host,
            now=now,
            bind_acquisition=False,
        )
        collector = CheckCollector()

        _check_claim_state(
            state=self.state,
            instances=[host],
            collector=collector,
        )
        checks = {check["id"]: check for check in collector.result()["checks"]}

        self.assertEqual(
            checks[f"claim_journal_{claim.claim_id}"]["status"],
            "warning",
        )

    def test_doctor_rejects_claim_on_unclaimable_host_phase(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        host = claim_host("rollback96", created_at=now)
        self.admit_claim(
            host,
            now=now,
            bind_acquisition=True,
        )
        host["phase"] = "rollback_required"
        collector = CheckCollector()

        _check_claim_state(
            state=self.state,
            instances=[host],
            collector=collector,
        )
        checks = {check["id"]: check for check in collector.result()["checks"]}

        self.assertEqual(
            checks["claim_live_host_rollback96"]["status"],
            "error",
        )

    def test_doctor_recognizes_exact_terminal_predecessor_boundary(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        predecessor = claim_host("reuse96", created_at=now)
        predecessor["phase"] = "terminated"
        request = claim_request()
        acquisitions = ClaimAcquisitionStore(self.state)
        acquisitions.begin(request, now=now)
        acquisitions.select_target(
            request,
            host_name="reuse96",
            host_operation_id=(
                "22345678-1234-4234-8234-123456789abc"
            ),
            predecessor_operation_id=predecessor["operation_id"],
            profile={"name": "pro-dev", "sha256": "1" * 64},
            now=now,
        )
        collector = CheckCollector()

        _check_claim_state(
            state=self.state,
            instances=[predecessor],
            collector=collector,
        )
        recovery = next(
            check
            for check in collector.result()["checks"]
            if check["id"].startswith("claim_acquisition_recovery_")
        )

        self.assertEqual(recovery["status"], "warning")
        self.assertIn("predecessor", recovery["message"])

    def test_doctor_recognizes_exact_unsubmitted_target_as_recoverable(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        target = claim_host("intent96", created_at=now)
        target["phase"] = "intent"
        target["pod_id"] = None
        request = claim_request()
        acquisitions = ClaimAcquisitionStore(self.state)
        acquisitions.begin(request, now=now)
        acquisitions.select_target(
            request,
            host_name=target["name"],
            host_operation_id=target["operation_id"],
            predecessor_operation_id=None,
            profile=dict(target["profile"]),
            now=now,
        )
        collector = CheckCollector()

        _check_claim_state(
            state=self.state,
            instances=[target],
            collector=collector,
        )
        recovery = next(
            check
            for check in collector.result()["checks"]
            if check["id"].startswith("claim_acquisition_recovery_")
        )

        self.assertEqual(recovery["status"], "warning")

    def test_doctor_rejects_unbound_claim_on_different_target(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        claim_host_receipt = claim_host("claim96", created_at=now)
        request, claim = self.admit_claim(
            claim_host_receipt,
            now=now,
            bind_acquisition=False,
        )
        other_host = claim_host("other96", created_at=now)
        acquisitions = ClaimAcquisitionStore(self.state)
        acquisitions.select_target(
            request,
            host_name=other_host["name"],
            host_operation_id=other_host["operation_id"],
            predecessor_operation_id=None,
            profile=dict(other_host["profile"]),
            now=now,
        )
        acquisitions.bind_host(request, other_host, now=now)
        collector = CheckCollector()

        _check_claim_state(
            state=self.state,
            instances=[claim_host_receipt, other_host],
            collector=collector,
        )
        checks = {check["id"]: check for check in collector.result()["checks"]}

        self.assertEqual(
            checks[f"claim_journal_{claim.claim_id}"]["status"],
            "error",
        )

    def test_historical_closed_acquisition_survives_host_reuse_and_pruning(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        predecessor = claim_host("reuse96", created_at=now)
        request, claim = self.admit_claim(
            predecessor,
            now=now,
            bind_acquisition=True,
        )
        claims = ClaimStore(self.state)
        claims.release(
            "reuse96",
            claim.claim_id,
            expected_generation=claim.generation,
            now=now,
            retire_now=False,
        )
        predecessor_ledger = claims.load("reuse96")
        closed_claim = predecessor_ledger["closed_claims"][0]
        ClaimAcquisitionStore(self.state).close_claim(
            closed_claim,
            ledger=predecessor_ledger,
            now=now,
        )
        replacement = claim_host("reuse96", created_at=now)
        replacement["operation_id"] = (
            "22345678-1234-4234-8234-123456789abc"
        )
        replacement["pod_id"] = "pod-replacement"
        replacement_ledger = claims.initialize(
            host=replacement,
            allocation=default_allocation_from_host(replacement),
            retention="while-claimed",
            empty_grace_seconds=300,
            now=now,
        )
        replacement_ledger["closed_claims"] = []
        claims.save(replacement_ledger)
        collector = CheckCollector()

        _check_claim_state(
            state=self.state,
            instances=[replacement],
            collector=collector,
        )
        acquisition_errors = [
            check
            for check in collector.result()["checks"]
            if check["id"].startswith("claim_acquisition_")
            and check["status"] == "error"
        ]

        self.assertEqual(acquisition_errors, [])
        self.assertEqual(
            ClaimAcquisitionStore(self.state)
            .load(request)["claim_closure"]["reason"],
            "released",
        )

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
            profiles=self.profiles,
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
