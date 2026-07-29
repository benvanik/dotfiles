from __future__ import annotations

import datetime
import pathlib
import tempfile
import types
import unittest
import uuid
from unittest import mock

from runpod_local.claims import (
    ClaimStore,
    HostClaimRequest,
    claim_admission_reasons,
    claim_id_from_uuid,
)
from runpod_local.errors import RunpodLocalError
from runpod_local.host_control import HostControl
from runpod_local.instances import InstanceStore, profile_hash
from runpod_local.lifecycle import TERMINAL_PHASES
from runpod_local.lifecycle_cli import (
    _active_claim_host_names,
    _guard_unclaimed_host,
    _run_ttl_watch_cycle,
)
from runpod_local.state import StateRecordScan, StateStore
from runpod_local.timeutil import utc_timestamp


NOW = datetime.datetime(
    2026,
    7,
    28,
    20,
    0,
    tzinfo=datetime.timezone.utc,
)
HOST_OPERATION_ID = "12345678-1234-4234-8234-123456789abc"
OWNER_OPERATION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
GPU_ID = "NVIDIA RTX PRO 6000 Blackwell Server Edition"


def host_profile(name: str = "pro6000-is1") -> dict:
    return {
        "name": name,
        "pod": {
            "gpu_type_ids": [GPU_ID],
            "gpu_memory_gb_by_type": {GPU_ID: 96.0},
            "gpu_count": 1,
            "min_vcpu_per_gpu": 16,
            "min_ram_per_gpu": 64,
            "container_disk_gb": 50,
        },
        "retention": {
            "mode": "manual",
            "empty_grace_seconds": 300,
        },
        "lease": {"default_ttl_seconds": 7200},
    }


PROFILE_SHA256 = profile_hash(host_profile())


class MutableClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime.datetime:
        return self.now


def host(
    *,
    name: str = "dev96",
    operation_id: str = HOST_OPERATION_ID,
    pod_id: str = "pod-123",
    profile_name: str = "pro6000-is1",
    profile_sha256: str | None = None,
    phase: str = "active",
    retention_mode: str = "manual",
    created_at: datetime.datetime = NOW,
) -> dict:
    return {
        "name": name,
        "operation_id": operation_id,
        "pod_id": pod_id,
        "phase": phase,
        "created_at": utc_timestamp(created_at),
        "profile": {
            "name": profile_name,
            "sha256": (
                profile_hash(host_profile(profile_name))
                if profile_sha256 is None
                else profile_sha256
            ),
        },
        "provider_termination_at": utc_timestamp(
            NOW + datetime.timedelta(hours=2)
        ),
        "lease_request": {
            "ttl_seconds": 7200,
            "idle_timeout_seconds": None,
        },
        "expected": {
            "gpu_id": GPU_ID,
            "gpu_count": 1,
            "gpu_memory_gb": 96.0,
            "network_volume_id": "volume-123",
            "data_center_id": "EUR-IS-1",
            "image": "fixture/image@sha256:" + "2" * 64,
            "container_disk_gb": 50,
            "min_vcpu_count": 16,
            "min_ram_gb": 64,
        },
        "quoted_total_price_per_hour": 1.99,
        "retention": {
            "mode": retention_mode,
            "empty_grace_seconds": 300,
        },
    }


def allocation() -> dict:
    return {
        "gpu_id": GPU_ID,
        "gpu_count": 1,
        "gpu_memory_gb": 96.0,
        "cpu_count": 16,
        "ram_gb": 64,
        "ephemeral_disk_gb": 50,
        "network_volume_id": "volume-123",
        "image": "fixture/image@sha256:" + "2" * 64,
        "data_center_id": "EUR-IS-1",
    }


def request(**overrides) -> HostClaimRequest:
    arguments = {
        "owner_system": "model-lab",
        "owner_instance": "qwen36-heretic",
        "owner_operation_id": OWNER_OPERATION_ID,
        "allowed_profile_names": ("pro6000-is1",),
        "mode": "shared",
        "gpu_devices": (0,),
        "gpu_memory_gb": 24,
        "cpu_count": 4,
        "ram_gb": 16,
        "ephemeral_disk_gb": 10,
        "endpoint_names": ("api",),
        "minimum_remaining_seconds": 1800,
        "renewal_ttl_seconds": 120,
    }
    arguments.update(overrides)
    return HostClaimRequest(**arguments)


class ClaimStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = StateStore(pathlib.Path(self.temporary.name) / "state")
        self.store = ClaimStore(self.state)
        self.ledger = self.store.initialize(
            host=host(),
            allocation=allocation(),
            retention="while-claimed",
            empty_grace_seconds=300,
            now=NOW,
        )

    def admit(self, value: HostClaimRequest, index: int = 1):
        return self.store.admit(
            self.ledger,
            value,
            now=NOW,
            claim_id=claim_id_from_uuid(uuid.UUID(int=index)),
        )

    def test_shared_claims_get_disjoint_ports_and_account_resources(self):
        first = self.admit(request(), 1)
        self.ledger = self.store.load("dev96")
        second_request = request(
            owner_instance="embeddings",
            owner_operation_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            gpu_memory_gb=32,
            endpoint_names=("metrics", "server"),
        )
        second = self.admit(second_request, 2)

        self.assertEqual(first.ports, {"api": 18000})
        self.assertEqual(
            second.ports,
            {"metrics": 18001, "server": 18002},
        )
        self.assertEqual(
            second.remote_root,
            f"/root/runpod-session/claims/{second.claim_id}",
        )
        ledger = self.store.load("dev96")
        self.assertEqual(len(ledger["claims"]), 2)
        self.assertEqual(
            claim_admission_reasons(
                ledger,
                request(
                    owner_instance="too-large",
                    owner_operation_id=(
                        "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
                    ),
                    gpu_memory_gb=48,
                ),
                now=NOW,
            ),
            ["GPU device 0 memory is exhausted"],
        )

    def test_gpu_and_host_exclusivity_are_real_admission_contracts(self):
        self.admit(request(), 1)
        ledger = self.store.load("dev96")

        gpu_exclusive = request(
            owner_instance="benchmark",
            owner_operation_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            mode="gpu-exclusive",
        )
        self.assertIn(
            "claim mode conflicts",
            " ".join(
                claim_admission_reasons(
                    ledger,
                    gpu_exclusive,
                    now=NOW,
                )
            ),
        )
        host_exclusive = request(
            owner_instance="credential-stage",
            owner_operation_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            mode="host-exclusive",
            gpu_devices=(),
            gpu_memory_gb=0,
        )
        self.assertIn(
            "claim mode conflicts",
            " ".join(
                claim_admission_reasons(
                    ledger,
                    host_exclusive,
                    now=NOW,
                )
            ),
        )

    def test_out_of_range_gpu_is_a_typed_admission_failure(self):
        invalid = request(gpu_devices=(1,))

        self.assertEqual(
            claim_admission_reasons(self.ledger, invalid, now=NOW),
            ["GPU device 1 does not exist"],
        )
        with self.assertRaises(RunpodLocalError) as caught:
            self.admit(invalid)
        self.assertEqual(caught.exception.code, "host_claim_not_admitted")

    def test_owner_operation_is_idempotent_and_request_bound(self):
        first = self.admit(request(), 1)
        self.ledger = self.store.load("dev96")
        repeated = self.admit(request(), 2)

        self.assertEqual(repeated.claim_id, first.claim_id)
        self.assertEqual(len(self.store.load("dev96")["claims"]), 1)
        with self.assertRaises(RunpodLocalError) as caught:
            self.admit(request(gpu_memory_gb=25), 3)
        self.assertEqual(
            caught.exception.code,
            "host_claim_operation_conflict",
        )

    def test_renew_is_generation_guarded_and_provider_bounded(self):
        claim = self.admit(request(), 1)
        renewed = self.store.renew(
            "dev96",
            claim.claim_id,
            expected_generation=claim.generation,
            renewal_ttl_seconds=3 * 60 * 60,
            now=NOW + datetime.timedelta(minutes=1),
        )

        self.assertEqual(renewed.generation, 2)
        self.assertEqual(
            renewed.renewal_deadline,
            utc_timestamp(NOW + datetime.timedelta(hours=2)),
        )
        with self.assertRaises(RunpodLocalError) as caught:
            self.store.renew(
                "dev96",
                claim.claim_id,
                expected_generation=1,
                renewal_ttl_seconds=60,
                now=NOW + datetime.timedelta(minutes=2),
            )
        self.assertEqual(
            caught.exception.code,
            "host_claim_generation_changed",
        )

    def test_generation_guard_rejects_booleans(self):
        claim = self.admit(request(), 1)

        with self.assertRaises(RunpodLocalError) as renew_error:
            self.store.renew(
                "dev96",
                claim.claim_id,
                expected_generation=True,
                renewal_ttl_seconds=60,
                now=NOW + datetime.timedelta(seconds=1),
            )
        self.assertEqual(renew_error.exception.code, "invalid_host_claim")

        with self.assertRaises(RunpodLocalError) as release_error:
            self.store.release(
                "dev96",
                claim.claim_id,
                expected_generation=True,
                now=NOW + datetime.timedelta(seconds=1),
                retire_now=False,
            )
        self.assertEqual(release_error.exception.code, "invalid_host_claim")

    def test_last_release_starts_grace_and_now_makes_it_due(self):
        claim = self.admit(request(), 1)
        released_at = NOW + datetime.timedelta(minutes=1)
        released = self.store.release(
            "dev96",
            claim.claim_id,
            expected_generation=1,
            now=released_at,
            retire_now=False,
        )

        self.assertFalse(released.retirement_due)
        self.assertEqual(
            released.retire_at,
            utc_timestamp(released_at + datetime.timedelta(minutes=5)),
        )

        second = self.store.admit(
            self.store.load("dev96"),
            request(
                owner_operation_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            ),
            now=released_at,
            claim_id=claim_id_from_uuid(uuid.UUID(int=2)),
        )
        immediate = self.store.release(
            "dev96",
            second.claim_id,
            expected_generation=1,
            now=released_at,
            retire_now=True,
        )
        self.assertTrue(immediate.retirement_due)
        self.assertEqual(immediate.retire_at, utc_timestamp(released_at))

    def test_expired_final_claim_quarantines_for_immediate_retirement(self):
        claim = self.admit(request(renewal_ttl_seconds=60), 1)
        self.assertEqual(claim.generation, 1)
        expired_at = NOW + datetime.timedelta(seconds=60)
        ledger, expired = self.store.expire_claims(
            self.store.load("dev96"),
            now=expired_at,
        )

        self.assertEqual(expired, [claim.claim_id])
        self.assertEqual(ledger["claims"], [])
        self.assertEqual(
            ledger["quarantine"],
            {
                "reason": "expired-claim-cleanup-unproven",
                "claim_ids": [claim.claim_id],
                "started_at": utc_timestamp(expired_at),
            },
        )
        self.assertEqual(ledger["empty_grace_applied_seconds"], 0)
        self.assertEqual(
            ledger["retire_at"],
            utc_timestamp(expired_at),
        )

    def test_late_expiry_sweep_makes_original_deadline_retirement_due(self):
        claim = self.admit(request(renewal_ttl_seconds=60), 1)
        observed_at = NOW + datetime.timedelta(minutes=20)

        ledger, expired = self.store.expire_claims(
            self.store.load("dev96"),
            now=observed_at,
        )

        self.assertEqual(expired, [claim.claim_id])
        self.assertEqual(
            ledger["empty_since"],
            utc_timestamp(NOW + datetime.timedelta(minutes=1)),
        )
        self.assertEqual(
            ledger["retire_at"],
            utc_timestamp(NOW + datetime.timedelta(minutes=1)),
        )

    def test_expiry_quarantine_blocks_new_admission_until_host_replacement(self):
        expired_claim = self.admit(
            request(renewal_ttl_seconds=60),
            1,
        )
        expired_at = NOW + datetime.timedelta(minutes=1)
        ledger, _ = self.store.expire_claims(
            self.store.load("dev96"),
            now=expired_at,
        )
        replacement_request = request(
            owner_instance="replacement",
            owner_operation_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        )

        self.assertIn(
            "quarantined",
            " ".join(
                claim_admission_reasons(
                    ledger,
                    replacement_request,
                    now=expired_at,
                )
            ),
        )
        with self.assertRaises(RunpodLocalError) as caught:
            self.store.admit(
                ledger,
                replacement_request,
                now=expired_at,
                claim_id=claim_id_from_uuid(uuid.UUID(int=2)),
            )
        self.assertEqual(caught.exception.code, "host_claim_quarantined")

        replacement = host(
            operation_id="22345678-1234-4234-8234-123456789abc",
            pod_id="pod-replacement",
        )
        replacement_ledger = self.store.initialize(
            host=replacement,
            allocation=allocation(),
            retention="while-claimed",
            empty_grace_seconds=300,
            now=expired_at,
        )
        self.assertIsNone(replacement_ledger["quarantine"])
        self.assertEqual(
            replacement_ledger["closed_claims"][0]["claim_id"],
            expired_claim.claim_id,
        )
        admitted = self.store.admit(
            replacement_ledger,
            replacement_request,
            now=expired_at,
            claim_id=claim_id_from_uuid(uuid.UUID(int=2)),
        )
        self.assertEqual(admitted.operation_id, replacement["operation_id"])

    def test_quarantine_validation_requires_expired_claim_evidence(self):
        ledger = self.store.load("dev96")
        ledger["quarantine"] = {
            "reason": "expired-claim-cleanup-unproven",
            "claim_ids": [claim_id_from_uuid(uuid.UUID(int=1))],
            "started_at": utc_timestamp(NOW),
        }

        with self.assertRaises(RunpodLocalError) as caught:
            self.store.save(ledger)

        self.assertEqual(caught.exception.code, "invalid_host_claim_record")

    def test_closed_owner_operation_cannot_silently_create_again(self):
        claim = self.admit(request(), 1)
        self.store.release(
            "dev96",
            claim.claim_id,
            expected_generation=claim.generation,
            now=NOW + datetime.timedelta(seconds=1),
            retire_now=False,
        )

        ledger = self.store.load("dev96")
        self.assertEqual(
            ledger["closed_claims"][0]["reason"],
            "released",
        )
        with self.assertRaises(RunpodLocalError) as caught:
            self.store.admit(
                ledger,
                request(),
                now=NOW + datetime.timedelta(seconds=2),
                claim_id=claim_id_from_uuid(uuid.UUID(int=2)),
            )
        self.assertEqual(
            caught.exception.code,
            "host_claim_operation_closed",
        )


class FakeProfiles:
    def __init__(self, *, missing: tuple[str, ...] = ()) -> None:
        self.value = host_profile()
        self.missing = set(missing)

    def load(self, name: str) -> dict:
        if name in self.missing:
            raise RunpodLocalError(
                f"profile does not exist: {name}",
                code="profile_not_found",
            )
        if name not in {"pro6000-is1", "fallback-pro6000"}:
            raise AssertionError(f"unexpected profile: {name}")
        return host_profile(name)


class FakeInstances:
    def __init__(self, records=()) -> None:
        self.records = {record["name"]: record for record in records}

    def load(self, name: str, *, required: bool = True):
        result = self.records.get(name)
        if result is None and required:
            raise RunpodLocalError("missing", code="instance_not_found")
        return result

    def list(self):
        return list(self.records.values())

    def scan(self):
        return [
            StateRecordScan(name=name, value=record, error=None)
            for name, record in sorted(self.records.items())
        ]

    def save(self, record):
        self.records[record["name"]] = record


class FakeLifecycle:
    def __init__(
        self,
        launched_host: dict,
        instances: FakeInstances,
    ) -> None:
        self.launched_host = launched_host
        self.instances = instances
        self.launch_calls = []
        self.terminate_calls = []
        self.ineligible_profiles = set()

    def new_operation_id(self):
        return self.launched_host["operation_id"]

    def launch(self, name, profile, **kwargs):
        self.launch_calls.append((name, profile, kwargs))
        if profile["name"] in self.ineligible_profiles:
            raise RunpodLocalError(
                "no eligible GPU",
                code="no_eligible_gpu",
            )
        target_operation_id = kwargs.get("target_operation_id")
        predecessor_operation_id = kwargs.get(
            "predecessor_operation_id"
        )
        current = self.instances.load(name, required=False)
        if target_operation_id is not None:
            allowed = (
                current is None and predecessor_operation_id is None
            ) or (
                current is not None
                and current["operation_id"] == target_operation_id
                and current["phase"] not in TERMINAL_PHASES
            ) or (
                current is not None
                and current["operation_id"] == predecessor_operation_id
                and current["phase"] in TERMINAL_PHASES
            )
            if not allowed:
                raise RunpodLocalError(
                    "fixture operation boundary changed",
                    code="instance_operation_changed",
                )
        value = dict(self.launched_host)
        value["name"] = name
        if kwargs.get("target_operation_id") is not None:
            value["operation_id"] = kwargs["target_operation_id"]
        value["profile"] = {
            "name": profile["name"],
            "sha256": profile_hash(profile),
        }
        value["retention"] = {
            "mode": kwargs.get(
                "retention_mode",
                profile["retention"]["mode"],
            ),
            "empty_grace_seconds": kwargs.get(
                "empty_grace_seconds",
                profile["retention"]["empty_grace_seconds"],
            ),
        }
        self.instances.save(value)
        return value

    def terminate(self, name, **kwargs):
        self.terminate_calls.append((name, kwargs))
        return {
            "schema_version": "runpod.termination-plan.v1",
            "executed": kwargs["execute"],
        }


class HostControlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = StateStore(pathlib.Path(self.temporary.name) / "state")
        self.clock = MutableClock()
        self.instances = FakeInstances()
        self.lifecycle = FakeLifecycle(host(), self.instances)
        self.control = HostControl(
            state=self.state,
            lifecycle=self.lifecycle,
            profiles=FakeProfiles(),
            clock=self.clock,
            uuid_factory=lambda: uuid.UUID(int=1),
        )
        self.control.instances = self.instances

    def test_acquire_reuses_compatible_active_host(self):
        self.instances.save(host())
        claim = self.control.acquire(
            request(host_name="dev96", create_if_missing=False)
        )

        self.assertEqual(claim.host_name, "dev96")
        self.assertEqual(claim.pod_id, "pod-123")
        self.assertEqual(self.lifecycle.launch_calls, [])

    def test_fleet_scan_skips_host_whose_authored_profile_was_removed(self):
        self.instances.save(host(name="stale96"))
        self.instances.save(
            host(
                name="fallback96",
                operation_id="22345678-1234-4234-8234-123456789abc",
                pod_id="pod-456",
                profile_name="fallback-pro6000",
            )
        )
        self.control.profiles = FakeProfiles(
            missing=("pro6000-is1",),
        )

        claim = self.control.acquire(
            request(
                allowed_profile_names=(
                    "pro6000-is1",
                    "fallback-pro6000",
                )
            )
        )

        self.assertEqual(claim.host_name, "fallback96")
        self.assertEqual(self.lifecycle.launch_calls, [])

    def test_explicit_host_requires_its_current_authored_profile(self):
        self.instances.save(host(name="stale96"))
        self.control.profiles = FakeProfiles(
            missing=("pro6000-is1",),
        )

        with self.assertRaises(RunpodLocalError) as caught:
            self.control.acquire(
                request(
                    host_name="stale96",
                    create_if_missing=False,
                )
            )

        self.assertEqual(caught.exception.code, "profile_not_found")

    def test_existing_claim_ledger_must_match_immutable_host_receipt(self):
        self.instances.save(host())
        first = self.control.acquire(
            request(host_name="dev96", create_if_missing=False)
        )
        ledger = self.control.claims.load("dev96")
        ledger["allocation"]["image"] = (
            "fixture/tampered@sha256:" + "3" * 64
        )
        self.control.claims.save(ledger)

        with self.assertRaises(RunpodLocalError) as caught:
            self.control.acquire(
                request(
                    host_name="dev96",
                    create_if_missing=False,
                    owner_instance="second",
                    owner_operation_id=(
                        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
                    ),
                )
            )

        self.assertEqual(caught.exception.code, "host_claim_ledger_drift")
        operations = {
            "idempotent find": lambda: self.control.find(
                request(host_name="dev96", create_if_missing=False)
            ),
            "renew": lambda: self.control.renew(
                "dev96",
                first.claim_id,
                first.generation,
                60,
            ),
            "release": lambda: self.control.release(
                "dev96",
                first.claim_id,
                first.generation,
            ),
            "get": lambda: self.control.get("dev96", first.claim_id),
            "list": lambda: self.control.list("dev96"),
            "status": lambda: self.control.status("dev96"),
        }
        for label, operation in operations.items():
            with self.subTest(operation=label):
                with self.assertRaises(RunpodLocalError) as operation_error:
                    operation()
                self.assertEqual(
                    operation_error.exception.code,
                    "host_claim_ledger_drift",
                )
        self.assertEqual(
            self.control.claims.load("dev96")["claims"][0]["claim_id"],
            first.claim_id,
        )

    def test_acquire_creates_once_and_owner_retry_is_idempotent(self):
        first = self.control.acquire(request())
        second = self.control.acquire(request())

        self.assertEqual(first.claim_id, second.claim_id)
        self.assertEqual(len(self.lifecycle.launch_calls), 1)
        self.assertTrue(first.host_name.startswith("auto-pro6000-is1-"))

    def test_terminal_claim_host_cannot_wedge_unrelated_acquisition(self):
        self.instances.save(host(name="dead96"))
        dead_claim = self.control.acquire(
            request(host_name="dead96", create_if_missing=False)
        )
        dead_host = self.instances.load("dead96")
        dead_host["phase"] = "terminated"
        self.instances.save(dead_host)
        self.control.uuid_factory = lambda: uuid.UUID(int=2)

        replacement = self.control.acquire(
            request(
                owner_instance="replacement",
                owner_operation_id=(
                    "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
                ),
            )
        )

        self.assertNotEqual(replacement.host_name, "dead96")
        dead_ledger = self.control.claims.load("dead96")
        self.assertEqual(dead_ledger["claims"], [])
        self.assertEqual(
            dead_ledger["operation_end"]["reason"],
            "host-operation-ended",
        )
        self.assertEqual(
            dead_ledger["closed_claims"][0]["reason"],
            "host-operation-ended",
        )
        dead_acquisition = next(
            acquisition
            for acquisition in self.control.acquisitions.list()
            if acquisition["owner_instance"] == "qwen36-heretic"
        )
        self.assertEqual(
            dead_acquisition["claim"]["claim_id"],
            dead_claim.claim_id,
        )
        self.assertEqual(
            dead_acquisition["claim_closure"]["reason"],
            "host-operation-ended",
        )

    def test_malformed_unrelated_claim_ledger_does_not_block_exact_claim(self):
        self.instances.save(host())
        self.instances.save(
            host(
                name="broken96",
                operation_id="22345678-1234-4234-8234-123456789abc",
                pod_id="pod-broken",
            )
        )
        self.state.write(
            "hostclaims",
            "broken96",
            {"schema_version": "corrupt"},
        )

        with self.assertRaises(RunpodLocalError) as exact_find:
            self.control.find(
                request(
                    host_name="broken96",
                    create_if_missing=False,
                    owner_instance="broken-find",
                    owner_operation_id=(
                        "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
                    ),
                )
            )
        self.assertEqual(
            exact_find.exception.code,
            "invalid_host_claim_record",
        )

        healthy_request = request(
            host_name="dev96",
            create_if_missing=False,
        )
        claim = self.control.acquire(healthy_request)

        self.assertEqual(claim.host_name, "dev96")
        self.assertEqual(
            self.control.find(healthy_request).claim_id,
            claim.claim_id,
        )
        with self.assertRaises(RunpodLocalError) as affected:
            self.control.acquire(
                request(
                    host_name="broken96",
                    create_if_missing=False,
                    owner_instance="broken",
                    owner_operation_id=(
                        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
                    ),
                )
            )
        self.assertEqual(
            affected.exception.code,
            "invalid_host_claim_record",
        )

    def test_malformed_unrelated_acquisition_journal_does_not_block_claim(self):
        self.instances.save(host())
        self.instances.save(
            host(
                name="closed96",
                operation_id="22345678-1234-4234-8234-123456789abc",
                pod_id="pod-closed",
            )
        )
        closed_request = request(
            host_name="closed96",
            create_if_missing=False,
            owner_instance="closed",
            owner_operation_id=(
                "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            ),
        )
        closed_claim = self.control.acquire(closed_request)
        self.control.release(
            closed_claim.host_name,
            closed_claim.claim_id,
            closed_claim.generation,
        )
        closed_acquisition = self.control.acquisitions.load(
            closed_request,
            required=True,
        )
        self.state.write(
            "hostclaimops",
            closed_acquisition["record_name"],
            {"schema_version": "corrupt"},
        )

        healthy_request = request(
            host_name="dev96",
            create_if_missing=False,
            owner_instance="healthy",
            owner_operation_id=(
                "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
            ),
        )
        claim = self.control.acquire(healthy_request)

        self.assertEqual(claim.host_name, "dev96")
        self.assertEqual(
            self.control.find(healthy_request).claim_id,
            claim.claim_id,
        )

    def test_malformed_unrelated_instance_does_not_block_fleet_placement(self):
        self.instances.save(host())
        valid_scan = self.instances.scan

        def scan_with_broken_instance():
            return [
                StateRecordScan(
                    name="broken96",
                    value=None,
                    error=RunpodLocalError(
                        "fixture malformed instance",
                        code="invalid_instance_record",
                    ),
                ),
                *valid_scan(),
            ]

        self.instances.scan = scan_with_broken_instance

        claim = self.control.acquire(request())

        self.assertEqual(claim.host_name, "dev96")
        self.assertEqual(self.lifecycle.launch_calls, [])

    def test_reused_host_is_bound_before_claim_admission(self):
        self.instances.save(host())
        bind_host = self.control.acquisitions.bind_host
        calls = 0

        def crash_first_host_bind(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("fixture crash after target selection")
            return bind_host(*args, **kwargs)

        self.control.acquisitions.bind_host = crash_first_host_bind
        with self.assertRaisesRegex(RuntimeError, "target selection"):
            self.control.acquire(request())

        acquisition = self.control.acquisitions.load(
            request(),
            required=True,
        )
        self.assertEqual(
            acquisition["target"]["host_name"],
            "dev96",
        )
        self.assertIsNone(acquisition["host"])
        self.assertEqual(
            self.control.claims.load("dev96")["claims"],
            [],
        )

        self.control.acquisitions.bind_host = bind_host
        claim = self.control.acquire(request())

        self.assertEqual(claim.host_name, "dev96")
        self.assertEqual(self.lifecycle.launch_calls, [])
        acquisition = self.control.acquisitions.load(
            request(),
            required=True,
        )
        self.assertEqual(
            acquisition["host"]["host_name"],
            "dev96",
        )
        self.assertEqual(
            acquisition["claim"]["claim_id"],
            claim.claim_id,
        )

    def test_crash_then_corrupt_exact_reused_ledger_cannot_duplicate_claim(self):
        self.instances.save(host())
        bind = self.control.acquisitions.bind

        def crash_before_claim_binding(*_args, **_kwargs):
            raise RuntimeError("fixture crash before claim binding")

        self.control.acquisitions.bind = crash_before_claim_binding
        with self.assertRaisesRegex(RuntimeError, "claim binding"):
            self.control.acquire(request())
        acquisition = self.control.acquisitions.load(
            request(),
            required=True,
        )
        self.assertEqual(
            acquisition["host"]["host_name"],
            "dev96",
        )
        self.assertIsNone(acquisition["claim"])
        self.state.write(
            "hostclaims",
            "dev96",
            {"schema_version": "corrupt"},
        )
        self.control.acquisitions.bind = bind

        for operation in (
            lambda: self.control.find(request()),
            lambda: self.control.acquire(request()),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(RunpodLocalError) as caught:
                    operation()
                self.assertEqual(
                    caught.exception.code,
                    "invalid_host_claim_record",
                )
        self.assertEqual(self.lifecycle.launch_calls, [])
        self.assertEqual(set(self.instances.records), {"dev96"})

    def test_candidate_claim_crash_before_journal_bind_recovers(self):
        self.instances.save(host())
        bind = self.control.acquisitions.bind
        calls = 0

        def crash_first_bind(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("fixture crash before acquisition bind")
            return bind(*args, **kwargs)

        self.control.acquisitions.bind = crash_first_bind
        with self.assertRaisesRegex(RuntimeError, "fixture crash"):
            self.control.acquire(
                request(host_name="dev96", create_if_missing=False)
            )

        claim = self.control.acquire(
            request(host_name="dev96", create_if_missing=False)
        )

        self.assertEqual(claim.host_name, "dev96")
        self.assertEqual(len(self.control.claims.list()[0]["claims"]), 1)
        self.assertEqual(
            self.control.acquisitions.list()[0]["claim"]["claim_id"],
            claim.claim_id,
        )

    def test_acquisition_journal_precedes_launch_and_binds_crash_retry(self):
        launch = self.lifecycle.launch

        def crash_after_provider_return(*args, **kwargs):
            launch(*args, **kwargs)
            raise RuntimeError("fixture process crash after provider return")

        self.lifecycle.launch = crash_after_provider_return
        with self.assertRaisesRegex(RuntimeError, "fixture process crash"):
            self.control.acquire(request())

        acquisitions = self.control.acquisitions.list()
        self.assertEqual(len(acquisitions), 1)
        self.assertIsNone(acquisitions[0]["claim"])
        self.assertEqual(acquisitions[0]["request_sha256"], request().sha256())
        self.assertEqual(len(self.lifecycle.launch_calls), 1)

        with self.assertRaises(RunpodLocalError) as changed:
            self.control.acquire(request(gpu_memory_gb=25))
        self.assertEqual(
            changed.exception.code,
            "host_claim_operation_conflict",
        )
        self.assertEqual(len(self.lifecycle.launch_calls), 1)

        self.lifecycle.launch = launch
        claim = self.control.acquire(request())

        self.assertEqual(len(self.lifecycle.launch_calls), 1)
        binding = self.control.acquisitions.list()[0]["claim"]
        self.assertEqual(binding["host_name"], claim.host_name)
        self.assertEqual(binding["claim_id"], claim.claim_id)
        self.assertEqual(binding["pod_id"], claim.pod_id)

    def test_definitive_no_capacity_advances_acquisition_before_retry(self):
        replacement_operation_id = (
            "22345678-1234-4234-8234-123456789abc"
        )
        operation_ids = iter(
            (HOST_OPERATION_ID, replacement_operation_id)
        )
        self.lifecycle.new_operation_id = lambda: next(operation_ids)
        launch = self.lifecycle.launch
        launch_attempts = 0

        def reject_first_launch(name, profile, **kwargs):
            nonlocal launch_attempts
            launch_attempts += 1
            if launch_attempts == 1:
                self.lifecycle.launch_calls.append((name, profile, kwargs))
                rejected = host(
                    name=name,
                    operation_id=kwargs["target_operation_id"],
                    pod_id=None,
                    phase="aborted",
                    profile_name=profile["name"],
                )
                rejected["provider"] = None
                rejected["events"] = [
                    {
                        "event": "submission_rejected_no_capacity",
                        "at": utc_timestamp(self.clock.now),
                    }
                ]
                self.instances.save(rejected)
                raise RunpodLocalError(
                    "fixture definitive no capacity",
                    code="no_provider_capacity",
                )
            return launch(name, profile, **kwargs)

        self.lifecycle.launch = reject_first_launch

        with self.assertRaises(RunpodLocalError) as rejected:
            self.control.acquire(request())

        self.assertEqual(
            rejected.exception.code,
            "no_provider_capacity",
        )
        acquisition = self.control.acquisitions.load(
            request(),
            required=True,
        )
        self.assertEqual(
            acquisition["target"]["host_operation_id"],
            replacement_operation_id,
        )
        self.assertEqual(
            acquisition["target"]["predecessor_operation_id"],
            HOST_OPERATION_ID,
        )
        self.assertIsNone(acquisition["host"])
        self.assertIsNone(acquisition["claim"])

        claim = self.control.acquire(request())

        self.assertEqual(claim.operation_id, replacement_operation_id)
        self.assertEqual(len(self.lifecycle.launch_calls), 2)
        self.assertEqual(len(self.instances.records), 1)
        acquisition = self.control.acquisitions.load(
            request(),
            required=True,
        )
        self.assertEqual(
            acquisition["host"]["host_operation_id"],
            replacement_operation_id,
        )
        self.assertEqual(
            acquisition["claim"]["claim_id"],
            claim.claim_id,
        )

    def test_crash_before_no_capacity_advance_recovers_exact_predecessor(self):
        skipped_operation_id = (
            "22345678-1234-4234-8234-123456789abc"
        )
        recovered_operation_id = (
            "32345678-1234-4234-8234-123456789abc"
        )
        operation_ids = iter(
            (
                HOST_OPERATION_ID,
                skipped_operation_id,
                recovered_operation_id,
            )
        )
        self.lifecycle.new_operation_id = lambda: next(operation_ids)
        launch = self.lifecycle.launch
        launch_attempts = 0

        def reject_first_launch(name, profile, **kwargs):
            nonlocal launch_attempts
            launch_attempts += 1
            if launch_attempts == 1:
                self.lifecycle.launch_calls.append((name, profile, kwargs))
                rejected = host(
                    name=name,
                    operation_id=kwargs["target_operation_id"],
                    pod_id=None,
                    phase="aborted",
                    profile_name=profile["name"],
                )
                rejected["provider"] = None
                rejected["events"] = [
                    {
                        "event": "submission_rejected_no_capacity",
                        "at": utc_timestamp(self.clock.now),
                    }
                ]
                self.instances.save(rejected)
                raise RunpodLocalError(
                    "fixture definitive no capacity",
                    code="no_provider_capacity",
                )
            return launch(name, profile, **kwargs)

        advance = self.control.acquisitions.advance_rejected_target
        advance_attempts = 0

        def crash_first_advance(*args, **kwargs):
            nonlocal advance_attempts
            advance_attempts += 1
            if advance_attempts == 1:
                raise RuntimeError(
                    "fixture crash before rejected-target advance"
                )
            return advance(*args, **kwargs)

        self.lifecycle.launch = reject_first_launch
        self.control.acquisitions.advance_rejected_target = (
            crash_first_advance
        )

        with self.assertRaisesRegex(RuntimeError, "rejected-target advance"):
            self.control.acquire(request())

        acquisition = self.control.acquisitions.load(
            request(),
            required=True,
        )
        self.assertEqual(
            acquisition["target"]["host_operation_id"],
            HOST_OPERATION_ID,
        )
        self.assertIsNone(acquisition["host"])
        self.control.acquisitions.advance_rejected_target = advance

        claim = self.control.acquire(request())

        self.assertEqual(claim.operation_id, recovered_operation_id)
        self.assertEqual(len(self.lifecycle.launch_calls), 2)
        self.assertEqual(len(self.instances.records), 1)
        acquisition = self.control.acquisitions.load(
            request(),
            required=True,
        )
        self.assertEqual(
            acquisition["target"]["predecessor_operation_id"],
            HOST_OPERATION_ID,
        )
        self.assertEqual(
            acquisition["host"]["host_operation_id"],
            recovered_operation_id,
        )

    def test_unbound_terminal_launch_recovery_cannot_recreate(self):
        launch = self.lifecycle.launch

        def crash_after_provider_return(*args, **kwargs):
            launch(*args, **kwargs)
            raise RuntimeError("fixture process crash after provider return")

        self.lifecycle.launch = crash_after_provider_return
        with self.assertRaises(RuntimeError):
            self.control.acquire(request())
        receipt = next(iter(self.instances.records.values()))
        receipt["phase"] = "terminated"
        self.instances.save(receipt)
        self.lifecycle.launch = launch

        with self.assertRaises(RunpodLocalError) as retry:
            self.control.acquire(request())

        self.assertEqual(
            retry.exception.code,
            "host_claim_acquisition_terminal",
        )
        self.assertEqual(len(self.lifecycle.launch_calls), 1)

    def test_unbound_launch_cannot_adopt_reused_host_name(self):
        launch = self.lifecycle.launch

        def crash_after_provider_return(*args, **kwargs):
            launch(*args, **kwargs)
            raise RuntimeError("fixture process crash after provider return")

        self.lifecycle.launch = crash_after_provider_return
        with self.assertRaises(RuntimeError):
            self.control.acquire(request())
        receipt = next(iter(self.instances.records.values()))
        receipt["phase"] = "terminated"
        self.instances.save(receipt)
        replacement = host(
            name=receipt["name"],
            operation_id="22345678-1234-4234-8234-123456789abc",
            pod_id="pod-replacement",
        )
        self.instances.save(replacement)
        self.lifecycle.launch = launch

        with self.assertRaises(RunpodLocalError) as retry:
            self.control.acquire(request())

        self.assertEqual(
            retry.exception.code,
            "host_claim_acquisition_terminal",
        )
        self.assertEqual(len(self.lifecycle.launch_calls), 1)
        self.assertEqual(
            self.instances.load(receipt["name"])["operation_id"],
            replacement["operation_id"],
        )

    def test_new_host_can_replace_only_recorded_terminal_predecessor(self):
        predecessor = host(
            name="reuse96",
            operation_id="22345678-1234-4234-8234-123456789abc",
            pod_id="pod-predecessor",
        )
        predecessor["phase"] = "terminated"
        self.instances.save(predecessor)

        first = self.control.acquire(request(host_name="reuse96"))
        second = self.control.acquire(request(host_name="reuse96"))

        self.assertEqual(first.operation_id, HOST_OPERATION_ID)
        self.assertEqual(second.claim_id, first.claim_id)
        acquisition = self.control.acquisitions.list()[0]
        self.assertEqual(
            acquisition["target"]["predecessor_operation_id"],
            predecessor["operation_id"],
        )
        self.assertEqual(acquisition["claim"]["claim_id"], first.claim_id)

    def test_provisioning_converges_with_claim_visible_between_polls(self):
        launch = self.lifecycle.launch
        phases = iter(("provisioning", "provisioning", "active"))

        def phased_launch(*args, **kwargs):
            value = launch(*args, **kwargs)
            value["phase"] = next(phases)
            self.instances.save(value)
            return value

        renewals = []

        def observe_and_renew(_seconds):
            ledger = self.control.claims.list()[0]
            self.assertEqual(len(ledger["claims"]), 1)
            current = self.control.get(
                ledger["host_name"],
                ledger["claims"][0]["claim_id"],
            )
            renewed = self.control.renew(
                current.host_name,
                current.claim_id,
                current.generation,
                120,
            )
            renewals.append(renewed)

        self.lifecycle.launch = phased_launch
        self.control.readiness_waiter = observe_and_renew

        claim = self.control.acquire(request())

        self.assertEqual(self.instances.load(claim.host_name)["phase"], "active")
        self.assertEqual(len(self.lifecycle.launch_calls), 3)
        self.assertEqual(len(renewals), 1)
        self.assertEqual(claim.generation, renewals[0].generation)

    def test_active_handoff_rechecks_minimum_useful_hard_lifetime(self):
        self.lifecycle.launched_host["provider_termination_at"] = (
            utc_timestamp(NOW + datetime.timedelta(minutes=31))
        )
        launch = self.lifecycle.launch
        phases = iter(("provisioning", "provisioning", "active"))

        def phased_launch(*args, **kwargs):
            value = launch(*args, **kwargs)
            value["phase"] = next(phases)
            self.instances.save(value)
            return value

        def advance_provisioning(_seconds):
            self.clock.now += datetime.timedelta(minutes=5)

        self.lifecycle.launch = phased_launch
        self.control.readiness_waiter = advance_provisioning

        with self.assertRaises(RunpodLocalError) as caught:
            self.control.acquire(
                request(
                    minimum_remaining_seconds=30 * 60,
                    renewal_ttl_seconds=10 * 60,
                    new_host_hard_ttl_seconds=31 * 60,
                )
            )

        self.assertEqual(
            caught.exception.code,
            "host_minimum_lifetime_elapsed",
        )
        ledger = self.control.claims.list()[0]
        self.assertEqual(ledger["claims"], [])
        self.assertEqual(ledger["closed_claims"][0]["reason"], "released")
        acquisition = self.control.acquisitions.list()[0]
        self.assertEqual(
            acquisition["claim_closure"]["reason"],
            "released",
        )

    def test_provisioning_retry_reuses_bound_claim_after_transient_error(self):
        launch = self.lifecycle.launch
        calls = 0

        def fail_first_reconciliation(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                value = launch(*args, **kwargs)
                value["phase"] = "provisioning"
                self.instances.save(value)
                return value
            if calls == 2:
                raise RunpodLocalError(
                    "fixture provider is not converged",
                    code="submission_ambiguous",
                )
            value = launch(*args, **kwargs)
            value["phase"] = "active"
            self.instances.save(value)
            return value

        self.lifecycle.launch = fail_first_reconciliation

        with self.assertRaises(RunpodLocalError) as transient:
            self.control.acquire(request())
        self.assertEqual(transient.exception.code, "submission_ambiguous")
        durable = self.control.claims.list()[0]["claims"][0]
        durable_claim_id = durable["claim_id"]

        claim = self.control.acquire(request())

        self.assertEqual(claim.claim_id, durable_claim_id)
        self.assertEqual(len(self.control.claims.list()[0]["claims"]), 1)
        self.assertEqual(self.instances.load(claim.host_name)["phase"], "active")

    def test_statically_impossible_request_never_launches_a_host(self):
        with self.assertRaises(RunpodLocalError) as caught:
            self.control.acquire(
                request(
                    gpu_devices=(1,),
                    mode="gpu-exclusive",
                )
            )

        self.assertEqual(caught.exception.code, "no_eligible_host_profile")
        self.assertEqual(self.lifecycle.launch_calls, [])

    def test_actual_allocation_rejection_terminates_new_host(self):
        self.lifecycle.launched_host["expected"] = {
            **self.lifecycle.launched_host["expected"],
            "gpu_memory_gb": 8.0,
        }

        with self.assertRaises(RunpodLocalError) as caught:
            self.control.acquire(request(gpu_memory_gb=24))

        self.assertEqual(caught.exception.code, "no_eligible_host_profile")
        self.assertEqual(len(self.lifecycle.launch_calls), 1)
        self.assertEqual(
            self.lifecycle.terminate_calls[0][1]["reason"],
            "new_host_claim_not_admitted",
        )

    def test_failed_provider_rollback_is_retried_on_exact_target(self):
        launch = self.lifecycle.launch
        terminate = self.lifecycle.terminate

        def rollback_required(*args, **kwargs):
            receipt = launch(*args, **kwargs)
            receipt["phase"] = "rollback_required"
            self.instances.save(receipt)
            raise RunpodLocalError(
                "fixture first rollback delete failed",
                code="rollback_required",
            )

        def finish_rollback(name, **kwargs):
            result = terminate(name, **kwargs)
            receipt = self.instances.load(name)
            receipt["phase"] = "rolled_back"
            self.instances.save(receipt)
            return result

        self.lifecycle.launch = rollback_required
        self.lifecycle.terminate = finish_rollback

        with self.assertRaises(RunpodLocalError) as caught:
            self.control.acquire(request())

        self.assertEqual(caught.exception.code, "allocation_rejected")
        self.assertEqual(len(self.lifecycle.launch_calls), 1)
        self.assertEqual(len(self.lifecycle.terminate_calls), 1)
        self.assertEqual(
            next(iter(self.instances.records.values()))["phase"],
            "rolled_back",
        )
        acquisition = self.control.acquisitions.list()[0]
        self.assertIsNone(acquisition["host"])
        self.assertIsNone(acquisition["claim"])

        with self.assertRaises(RunpodLocalError) as retry:
            self.control.acquire(request())
        self.assertEqual(
            retry.exception.code,
            "host_claim_acquisition_terminal",
        )
        self.assertEqual(len(self.lifecycle.launch_calls), 1)

    def test_terminal_acquisition_cleanup_cannot_mint_replacement_pod(self):
        self.lifecycle.launched_host["expected"] = {
            **self.lifecycle.launched_host["expected"],
            "gpu_memory_gb": 8.0,
        }
        terminate = self.lifecycle.terminate

        def terminate_and_close(name, **kwargs):
            result = terminate(name, **kwargs)
            receipt = self.instances.load(name)
            receipt["phase"] = "terminated"
            self.instances.save(receipt)
            return result

        self.lifecycle.terminate = terminate_and_close

        with self.assertRaises(RunpodLocalError) as rejected:
            self.control.acquire(request(gpu_memory_gb=24))
        self.assertEqual(rejected.exception.code, "no_eligible_host_profile")
        self.assertEqual(len(self.lifecycle.launch_calls), 1)

        with self.assertRaises(RunpodLocalError) as retry:
            self.control.acquire(request(gpu_memory_gb=24))

        self.assertEqual(
            retry.exception.code,
            "host_claim_acquisition_terminal",
        )
        self.assertEqual(len(self.lifecycle.launch_calls), 1)

    def test_failed_new_host_cleanup_remains_due_for_retirement(self):
        self.lifecycle.launched_host["expected"] = {
            **self.lifecycle.launched_host["expected"],
            "gpu_memory_gb": 8.0,
        }
        terminate = self.lifecycle.terminate
        attempts = 0

        def fail_first_termination(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RunpodLocalError(
                    "fixture provider deletion failed",
                    code="http_error",
                )
            return terminate(*args, **kwargs)

        self.lifecycle.terminate = fail_first_termination

        with self.assertRaises(RunpodLocalError) as cleanup_error:
            self.control.acquire(request(gpu_memory_gb=24))
        self.assertEqual(cleanup_error.exception.code, "http_error")
        ledger = self.control.claims.list()[0]
        self.assertEqual(ledger["claims"], [])
        self.assertEqual(
            ledger["retire_at"],
            utc_timestamp(NOW + datetime.timedelta(minutes=5)),
        )

        self.clock.now += datetime.timedelta(minutes=5)
        result = self.control.enforce_retirement(execute=True)

        self.assertEqual(attempts, 2)
        self.assertTrue(result["actions"][0]["due"])
        self.assertTrue(result["actions"][0]["executed"])

    def test_find_is_read_only_and_request_bound(self):
        self.assertIsNone(self.control.find(request()))
        self.assertEqual(self.lifecycle.launch_calls, [])

        claim = self.control.acquire(request())
        found = self.control.find(request())

        self.assertEqual(found, claim)
        self.assertEqual(len(self.lifecycle.launch_calls), 1)
        with self.assertRaises(RunpodLocalError) as caught:
            self.control.find(request(gpu_memory_gb=25))
        self.assertEqual(
            caught.exception.code,
            "host_claim_operation_conflict",
        )

    def test_new_host_tries_allowed_profiles_in_order(self):
        self.lifecycle.ineligible_profiles.add("pro6000-is1")

        claim = self.control.acquire(
            request(
                allowed_profile_names=(
                    "pro6000-is1",
                    "fallback-pro6000",
                )
            )
        )

        self.assertEqual(claim.profile_name, "fallback-pro6000")
        self.assertEqual(
            [
                call[1]["name"]
                for call in self.lifecycle.launch_calls
            ],
            ["pro6000-is1", "fallback-pro6000"],
        )

    def test_facade_accepts_byte_count_protocol_used_by_model_lab(self):
        facade_request = types.SimpleNamespace(
            owner_system="model-lab",
            owner_instance="qwen36-heretic",
            operation_id="model-lab-" + "a" * 32,
            host_name=None,
            allowed_profile_names=("pro6000-is1",),
            create_if_missing=True,
            mode="gpu-exclusive",
            gpu_device_count=1,
            gpu_memory_bytes=24 * 1024**3,
            cpu_count=4,
            memory_bytes=16 * 1024**3,
            ephemeral_disk_bytes=10 * 1024**3,
            endpoint_names=("openai",),
            minimum_remaining_seconds=1800,
            renewal_ttl_seconds=120,
            new_host_hard_ttl_seconds=7200,
            new_host_retention="while-claimed",
        )

        claim = self.control.acquire(facade_request)

        self.assertEqual(claim.operation_id, HOST_OPERATION_ID)
        self.assertEqual(claim.provider_resource_id, "pod-123")
        self.assertEqual(claim.endpoints, {"openai": 18000})
        self.assertEqual(
            claim.hard_expires_at,
            utc_timestamp(NOW + datetime.timedelta(hours=2)),
        )

    def test_last_release_now_cannot_retire_manual_host(self):
        self.instances.save(host())
        claim = self.control.acquire(
            request(host_name="dev96", create_if_missing=False)
        )
        result = self.control.release(
            "dev96",
            claim.claim_id,
            claim.generation,
            now=True,
        )

        self.assertFalse(result.retirement_due)
        self.assertIsNone(result.retire_at)
        self.assertEqual(self.lifecycle.terminate_calls, [])

    def test_release_from_manual_host_does_not_start_retirement(self):
        self.instances.save(host())
        claim = self.control.acquire(
            request(host_name="dev96", create_if_missing=False)
        )
        result = self.control.release(
            "dev96",
            claim.claim_id,
            claim.generation,
        )

        self.assertFalse(result.retirement_due)
        self.assertIsNone(result.retire_at)
        self.assertEqual(self.lifecycle.terminate_calls, [])

    def test_expired_manual_host_is_quarantined_until_exact_retirement(self):
        self.instances.save(host())
        claim = self.control.acquire(
            request(
                host_name="dev96",
                create_if_missing=False,
                renewal_ttl_seconds=60,
            )
        )
        self.clock.now += datetime.timedelta(minutes=1)

        with self.assertRaises(RunpodLocalError) as caught:
            self.control.acquire(
                request(
                    host_name="dev96",
                    create_if_missing=False,
                    owner_instance="replacement",
                    owner_operation_id=(
                        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
                    ),
                )
            )

        self.assertEqual(caught.exception.code, "host_claim_quarantined")
        ledger = self.control.claims.load("dev96")
        self.assertEqual(
            ledger["quarantine"]["claim_ids"],
            [claim.claim_id],
        )
        enforced = self.control.enforce_retirement(execute=True)
        action = next(
            item
            for item in enforced["actions"]
            if item["host_name"] == "dev96"
        )
        self.assertTrue(action["manual_action_required"])
        self.assertFalse(action["due"])
        self.assertIsNone(action["retire_at"])
        self.assertEqual(self.lifecycle.launch_calls, [])
        self.assertEqual(self.lifecycle.terminate_calls, [])

    def test_now_release_leaves_quarantined_manual_host_for_operator(self):
        self.instances.save(host())
        claim_ids = iter([uuid.UUID(int=1), uuid.UUID(int=2)])
        self.control.uuid_factory = lambda: next(claim_ids)
        expiring = self.control.acquire(
            request(
                host_name="dev96",
                create_if_missing=False,
                renewal_ttl_seconds=60,
            )
        )
        remaining = self.control.acquire(
            request(
                host_name="dev96",
                create_if_missing=False,
                owner_instance="remaining",
                owner_operation_id=(
                    "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
                ),
                renewal_ttl_seconds=600,
            )
        )
        self.clock.now += datetime.timedelta(minutes=1)
        self.control.status("dev96")

        released = self.control.release(
            "dev96",
            remaining.claim_id,
            remaining.generation,
            now=True,
        )

        self.assertFalse(released.retirement_due)
        self.assertIsNone(released.retire_at)
        ledger = self.control.claims.load("dev96")
        self.assertEqual(
            ledger["quarantine"]["claim_ids"],
            [expiring.claim_id],
        )
        self.assertEqual(ledger["claims"], [])
        self.assertEqual(self.lifecycle.terminate_calls, [])

    def test_release_from_automatic_host_starts_empty_grace(self):
        claim = self.control.acquire(request())
        result = self.control.release(
            claim.host_name,
            claim.claim_id,
            claim.generation,
        )

        self.assertFalse(result.retirement_due)
        self.assertEqual(
            result.retire_at,
            utc_timestamp(NOW + datetime.timedelta(minutes=5)),
        )
        self.clock.now += datetime.timedelta(minutes=5)
        enforced = self.control.enforce_retirement(execute=True)
        self.assertTrue(enforced["actions"][0]["executed"])
        self.assertEqual(len(self.lifecycle.terminate_calls), 1)

    def test_last_release_now_exactly_retires_while_claimed_host(self):
        claim = self.control.acquire(request())

        result = self.control.release(
            claim.host_name,
            claim.claim_id,
            claim.generation,
            now=True,
        )

        self.assertTrue(result.retirement_due)
        self.assertEqual(result.retire_at, utc_timestamp(NOW))
        self.assertEqual(
            self.lifecycle.terminate_calls,
            [
                (
                    claim.host_name,
                    {
                        "execute": True,
                        "reason": "last_claim_released_now",
                        "expected_operation_id": claim.operation_id,
                    },
                )
            ],
        )

    def test_peer_expiry_blocks_admission_and_final_release_retires_now(self):
        claim_ids = iter(
            [
                uuid.UUID(int=1),
                uuid.UUID(int=2),
                uuid.UUID(int=3),
            ]
        )
        self.control.uuid_factory = lambda: next(claim_ids)
        expiring = self.control.acquire(
            request(renewal_ttl_seconds=60)
        )
        remaining_request = request(
            host_name=expiring.host_name,
            create_if_missing=False,
            owner_instance="remaining",
            owner_operation_id=(
                "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            ),
            renewal_ttl_seconds=600,
        )
        remaining = self.control.acquire(remaining_request)
        self.clock.now += datetime.timedelta(minutes=1)
        status = self.control.status(expiring.host_name)

        self.assertEqual(
            status["claim_ledger"]["quarantine"]["claim_ids"],
            [expiring.claim_id],
        )
        self.assertEqual(
            [claim["claim_id"] for claim in status["claim_ledger"]["claims"]],
            [remaining.claim_id],
        )
        self.assertIsNone(status["claim_ledger"]["retire_at"])
        with self.assertRaises(RunpodLocalError) as caught:
            self.control.acquire(
                request(
                    host_name=expiring.host_name,
                    create_if_missing=False,
                    owner_instance="newcomer",
                    owner_operation_id=(
                        "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
                    ),
                )
            )
        self.assertEqual(caught.exception.code, "host_claim_quarantined")
        with self.assertRaises(RunpodLocalError) as idempotent:
            self.control.acquire(remaining_request)
        self.assertEqual(
            idempotent.exception.code,
            "host_claim_quarantined",
        )
        with self.assertRaises(RunpodLocalError) as renewal:
            self.control.renew(
                remaining.host_name,
                remaining.claim_id,
                remaining.generation,
                600,
            )
        self.assertEqual(
            renewal.exception.code,
            "host_claim_quarantined",
        )

        released = self.control.release(
            remaining.host_name,
            remaining.claim_id,
            remaining.generation,
        )

        self.assertTrue(released.retirement_due)
        self.assertEqual(released.retire_at, utc_timestamp(self.clock.now))
        self.assertEqual(
            self.lifecycle.terminate_calls,
            [
                (
                    remaining.host_name,
                    {
                        "execute": True,
                        "reason": "quarantined_host_claims_closed",
                        "expected_operation_id": remaining.operation_id,
                    },
                )
            ],
        )

    def test_generic_ttl_watcher_exactly_retires_expired_claim_host(self):
        claim = self.control.acquire(
            request(renewal_ttl_seconds=60)
        )
        self.clock.now += datetime.timedelta(minutes=1)
        ttl_calls = []

        def enforce_ttl(*, execute, protected_instance_names):
            ttl_calls.append((execute, protected_instance_names))
            return {
                "schema_version": "runpod.ttl-enforcement.v1",
                "actions": [],
            }

        self.lifecycle.enforce_ttl = enforce_ttl
        cycle = _run_ttl_watch_cycle(
            state=self.state,
            lifecycle=self.lifecycle,
            hosts=self.control,
            now=self.clock.now,
        )

        action = next(
            item
            for item in cycle["claim_retirement"]["actions"]
            if item["host_name"] == claim.host_name
        )
        self.assertEqual(action["expired_claim_ids"], [claim.claim_id])
        self.assertTrue(action["due"])
        self.assertTrue(action["executed"])
        self.assertEqual(
            self.lifecycle.terminate_calls,
            [
                (
                    claim.host_name,
                    {
                        "execute": True,
                        "reason": "quarantined_host_retirement",
                        "expected_operation_id": claim.operation_id,
                    },
                )
            ],
        )
        self.assertEqual(ttl_calls, [(True, set())])

    def test_enforcement_recovers_host_created_before_ledger_publication(self):
        self.instances.save(
            host(
                name="orphan96",
                retention_mode="while-claimed",
            )
        )
        self.clock.now += datetime.timedelta(minutes=10)

        enforced = self.control.enforce_retirement(execute=True)

        action = next(
            item
            for item in enforced["actions"]
            if item["host_name"] == "orphan96"
        )
        self.assertTrue(action["due"])
        self.assertTrue(action["executed"])
        self.assertEqual(
            action["retire_at"],
            utc_timestamp(NOW + datetime.timedelta(minutes=5)),
        )

    def test_historical_ledger_cannot_mask_replacement_orphan_retirement(self):
        original = host(
            name="reuse96",
            retention_mode="while-claimed",
        )
        self.instances.save(original)
        self.control.acquire(
            request(host_name="reuse96", create_if_missing=False)
        )
        original["phase"] = "terminated"
        self.instances.save(original)
        self.assertIsNone(
            self.control.find(
                request(
                    owner_instance="probe",
                    owner_operation_id=(
                        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
                    ),
                )
            )
        )
        replacement = host(
            name="reuse96",
            operation_id="22345678-1234-4234-8234-123456789abc",
            pod_id="pod-replacement",
            retention_mode="while-claimed",
        )
        self.instances.save(replacement)
        self.clock.now += datetime.timedelta(minutes=10)

        result = self.control.enforce_retirement(execute=True)

        action = next(
            item
            for item in result["actions"]
            if item["host_name"] == "reuse96"
        )
        self.assertEqual(
            action["host_operation_id"],
            replacement["operation_id"],
        )
        self.assertTrue(action["due"])
        self.assertTrue(action["executed"])
        current = self.control.claims.load("reuse96")
        self.assertEqual(
            current["host_operation_id"],
            replacement["operation_id"],
        )
        self.assertIsNone(current["operation_end"])
        self.assertEqual(
            self.lifecycle.terminate_calls[-1],
            (
                "reuse96",
                {
                    "execute": True,
                    "reason": "empty_host_retention_expired",
                    "expected_operation_id": replacement["operation_id"],
                },
            ),
        )

    def test_retirement_plan_is_strictly_read_only(self):
        claim = self.control.acquire(request(renewal_ttl_seconds=60))
        self.clock.now += datetime.timedelta(minutes=10)

        def snapshot():
            return {
                str(path.relative_to(self.state.root)): path.read_bytes()
                for path in self.state.root.rglob("*")
                if path.is_file()
            }

        before = snapshot()
        result = self.control.enforce_retirement(execute=False)
        after = snapshot()

        self.assertEqual(after, before)
        action = next(
            item
            for item in result["actions"]
            if item["host_name"] == claim.host_name
        )
        self.assertEqual(action["expired_claim_ids"], [claim.claim_id])
        self.assertTrue(action["due"])
        persisted = self.control.claims.load(claim.host_name)
        self.assertEqual(len(persisted["claims"]), 1)
        self.assertEqual(persisted["closed_claims"], [])

    def test_retirement_failure_does_not_block_later_due_hosts(self):
        for name, pod_id in (
            ("bad96", "pod-bad"),
            ("good96", "pod-good"),
        ):
            receipt = host(
                name=name,
                pod_id=pod_id,
                retention_mode="while-claimed",
            )
            self.instances.save(receipt)
            self.control.claims.initialize(
                host=receipt,
                allocation=allocation(),
                retention="while-claimed",
                empty_grace_seconds=300,
                now=NOW,
            )
        terminate = self.lifecycle.terminate

        def fail_one_host(name, **kwargs):
            if name == "bad96":
                raise RunpodLocalError(
                    "fixture provider deletion failed",
                    code="http_error",
                )
            return terminate(name, **kwargs)

        self.lifecycle.terminate = fail_one_host
        self.clock.now += datetime.timedelta(minutes=5)

        result = self.control.enforce_retirement(execute=True)
        actions = {
            action["host_name"]: action for action in result["actions"]
        }

        self.assertEqual(actions["bad96"]["error"]["code"], "http_error")
        self.assertFalse(actions["bad96"]["executed"])
        self.assertTrue(actions["good96"]["executed"])
        self.assertEqual(
            [call[0] for call in self.lifecycle.terminate_calls],
            ["good96"],
        )

    def test_malformed_claim_record_does_not_block_due_host(self):
        receipt = host(
            name="good96",
            pod_id="pod-good",
            retention_mode="while-claimed",
        )
        self.instances.save(receipt)
        self.control.claims.initialize(
            host=receipt,
            allocation=allocation(),
            retention="while-claimed",
            empty_grace_seconds=300,
            now=NOW,
        )
        self.state.write(
            "hostclaims",
            "broken96",
            {"schema_version": "not-a-claim-ledger"},
        )
        broken_path = self.state.record_path("hostclaims", "broken96")
        broken_path.write_text("{", encoding="utf-8")
        self.clock.now += datetime.timedelta(minutes=5)

        result = self.control.enforce_retirement(execute=True)
        actions = {
            action["host_name"]: action for action in result["actions"]
        }

        self.assertEqual(
            actions["broken96"]["error"]["code"],
            "invalid_state_record",
        )
        self.assertTrue(actions["good96"]["executed"])

    def test_malformed_instance_record_does_not_block_due_host(self):
        receipt = host(
            name="good96",
            pod_id="pod-good",
            retention_mode="while-claimed",
        )
        self.instances.save(receipt)
        self.control.claims.initialize(
            host=receipt,
            allocation=allocation(),
            retention="while-claimed",
            empty_grace_seconds=300,
            now=NOW,
        )
        self.state.write("instances", "broken96", {"not": "an instance"})
        memory_scan = self.instances.scan
        self.instances.scan = lambda: [
            *InstanceStore(self.state).scan(),
            *memory_scan(),
        ]
        self.clock.now += datetime.timedelta(minutes=5)

        result = self.control.enforce_retirement(execute=True)
        actions = {
            action["host_name"]: action for action in result["actions"]
        }

        self.assertEqual(
            actions["broken96"]["error"]["code"],
            "invalid_instance_record",
        )
        self.assertTrue(actions["good96"]["executed"])

    def test_status_expires_claim_before_presenting_live_state(self):
        claim = self.control.acquire(request(renewal_ttl_seconds=120))
        self.clock.now += datetime.timedelta(seconds=120)

        status = self.control.status(claim.host_name)

        self.assertEqual(status["expired_claim_ids"], [claim.claim_id])
        self.assertEqual(status["claim_ledger"]["claims"], [])
        self.assertEqual(
            status["claim_ledger"]["retire_at"],
            utc_timestamp(self.clock.now),
        )
        self.assertEqual(
            status["claim_ledger"]["quarantine"]["claim_ids"],
            [claim.claim_id],
        )
        self.assertEqual(self.control.list(claim.host_name), [])

    def test_status_rejects_empty_ledger_from_predecessor_operation(self):
        predecessor = host()
        self.instances.save(predecessor)
        self.control.claims.initialize(
            host=predecessor,
            allocation=allocation(),
            retention="manual",
            empty_grace_seconds=300,
            now=NOW,
        )
        self.instances.save(
            host(
                operation_id="22345678-1234-4234-8234-123456789abc",
                pod_id="pod-replacement",
            )
        )

        with self.assertRaises(RunpodLocalError) as caught:
            self.control.status("dev96")

        self.assertEqual(caught.exception.code, "host_claim_host_changed")

    def test_release_closure_outbox_repairs_journal_after_crash(self):
        claim = self.control.acquire(request())
        close_claim = self.control.acquisitions.close_claim
        calls = 0

        def crash_first_close(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("fixture crash after ledger closure")
            return close_claim(*args, **kwargs)

        self.control.acquisitions.close_claim = crash_first_close
        with self.assertRaisesRegex(RuntimeError, "fixture crash"):
            self.control.release(
                claim.host_name,
                claim.claim_id,
                claim.generation,
            )
        self.assertIsNone(
            self.control.acquisitions.list()[0]["claim_closure"]
        )

        with self.assertRaises(RunpodLocalError) as retry:
            self.control.release(
                claim.host_name,
                claim.claim_id,
                claim.generation,
            )

        self.assertEqual(retry.exception.code, "host_claim_not_found")
        self.assertEqual(
            self.control.acquisitions.list()[0]["claim_closure"]["reason"],
            "released",
        )

    def test_expiry_closure_outbox_flushes_before_host_reuse(self):
        claim = self.control.acquire(request(renewal_ttl_seconds=60))
        close_claim = self.control.acquisitions.close_claim
        calls = 0

        def crash_first_close(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("fixture crash after ledger expiry")
            return close_claim(*args, **kwargs)

        self.control.acquisitions.close_claim = crash_first_close
        self.clock.now += datetime.timedelta(minutes=2)
        with self.assertRaisesRegex(RuntimeError, "fixture crash"):
            self.control.status(claim.host_name)
        self.assertIsNone(
            self.control.acquisitions.list()[0]["claim_closure"]
        )

        replacement = host(
            name=claim.host_name,
            operation_id="22345678-1234-4234-8234-123456789abc",
            pod_id="pod-replacement",
        )
        self.instances.save(replacement)
        self.control.uuid_factory = lambda: uuid.UUID(int=2)
        acquired = self.control.acquire(
            request(
                host_name=claim.host_name,
                create_if_missing=False,
                owner_instance="replacement",
                owner_operation_id=(
                    "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
                ),
            )
        )

        self.assertEqual(acquired.operation_id, replacement["operation_id"])
        original = next(
            acquisition
            for acquisition in self.control.acquisitions.list()
            if acquisition["owner_instance"] == "qwen36-heretic"
        )
        self.assertEqual(
            original["claim_closure"]["reason"],
            "expired",
        )

    def test_direct_lifecycle_sweep_replays_prior_expiry_outbox(self):
        claim = self.control.acquire(request(renewal_ttl_seconds=60))
        sweep_time = NOW + datetime.timedelta(minutes=2)
        ledger = self.control.claims.load(claim.host_name)
        self.control.claims.expire_claims(ledger, now=sweep_time)
        self.assertIsNone(
            self.control.acquisitions.list()[0]["claim_closure"]
        )

        active_hosts = _active_claim_host_names(
            self.state,
            now=sweep_time,
            expire=True,
        )

        self.assertEqual(active_hosts, set())
        self.assertEqual(
            self.control.acquisitions.list()[0]["claim_closure"]["reason"],
            "expired",
        )

    def test_direct_lifecycle_sweep_isolates_corrupt_acquisition(self):
        self.instances.save(host(name="broken96", pod_id="pod-broken"))
        broken = self.control.acquire(
            request(
                host_name="broken96",
                create_if_missing=False,
                renewal_ttl_seconds=60,
            )
        )
        self.instances.save(
            host(
                name="healthy96",
                operation_id="22345678-1234-4234-8234-123456789abc",
                pod_id="pod-healthy",
            )
        )
        self.control.uuid_factory = lambda: uuid.UUID(int=2)
        healthy = self.control.acquire(
            request(
                host_name="healthy96",
                create_if_missing=False,
                owner_instance="healthy",
                owner_operation_id=(
                    "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
                ),
                renewal_ttl_seconds=600,
            )
        )
        broken_acquisition = next(
            acquisition
            for acquisition in self.control.acquisitions.list()
            if acquisition["claim"]["claim_id"] == broken.claim_id
        )
        self.state.write(
            "hostclaimops",
            broken_acquisition["record_name"],
            {"schema_version": "not-an-acquisition"},
        )
        self.clock.now += datetime.timedelta(seconds=60)
        errors = []

        active_hosts = _active_claim_host_names(
            self.state,
            now=self.clock.now,
            expire=True,
            errors=errors,
        )

        self.assertEqual(active_hosts, {"broken96", "healthy96"})
        self.assertEqual(
            errors,
            [
                {
                    "host_name": "broken96",
                    "host_operation_id": HOST_OPERATION_ID,
                    "protects_current_host": True,
                    "record_namespace": "hostclaims",
                    "record_name": "broken96",
                    "error": {
                        "code": "invalid_host_claim_acquisition",
                        "message": (
                            "host claim acquisition has an unsupported schema"
                        ),
                    },
                }
            ],
        )
        self.assertEqual(
            self.control.claims.load("broken96")["claims"],
            [],
        )
        self.assertEqual(
            self.control.claims.load("healthy96")["claims"][0]["claim_id"],
            healthy.claim_id,
        )

    def test_corrupt_predecessor_journal_does_not_protect_replacement(self):
        self.instances.save(host())
        old_claim = self.control.acquire(
            request(
                host_name="dev96",
                create_if_missing=False,
                renewal_ttl_seconds=60,
            )
        )
        old_acquisition = self.control.acquisitions.load(
            request(
                host_name="dev96",
                create_if_missing=False,
                renewal_ttl_seconds=60,
            ),
            required=True,
        )
        self.state.write(
            "hostclaimops",
            old_acquisition["record_name"],
            {"schema_version": "not-an-acquisition"},
        )
        replacement = host(
            operation_id="22345678-1234-4234-8234-123456789abc",
            pod_id="pod-replacement",
        )
        self.instances.save(replacement)
        self.clock.now += datetime.timedelta(seconds=60)
        self.control.enforce_retirement(execute=True)
        errors = []

        with mock.patch(
            "runpod_local.lifecycle_cli.InstanceStore.load",
            return_value=replacement,
        ):
            active_hosts = _active_claim_host_names(
                self.state,
                now=self.clock.now,
                expire=True,
                errors=errors,
            )
            _guard_unclaimed_host(
                self.state,
                name="dev96",
                now=self.clock.now,
            )

        self.assertEqual(active_hosts, set())
        self.assertEqual(
            self.control.claims.load("dev96")["operation_end"]["reason"],
            "host-operation-ended",
        )
        self.assertEqual(
            errors[0]["host_operation_id"],
            old_claim.operation_id,
        )
        self.assertFalse(errors[0]["protects_current_host"])

    def test_unexpired_predecessor_claim_does_not_protect_replacement(self):
        self.instances.save(host())
        self.control.acquire(
            request(
                host_name="dev96",
                create_if_missing=False,
                renewal_ttl_seconds=600,
            )
        )
        replacement = host(
            operation_id="22345678-1234-4234-8234-123456789abc",
            pod_id="pod-replacement",
        )
        self.instances.save(replacement)

        with mock.patch(
            "runpod_local.lifecycle_cli.InstanceStore.load",
            return_value=replacement,
        ):
            for expire in (False, True):
                with self.subTest(expire=expire):
                    self.assertEqual(
                        _active_claim_host_names(
                            self.state,
                            now=self.clock.now,
                            expire=expire,
                        ),
                        set(),
                    )
            _guard_unclaimed_host(
                self.state,
                name="dev96",
                now=self.clock.now,
            )

    def test_corrupt_acquisition_cannot_block_quarantined_host_retirement(self):
        self.instances.save(
            host(
                retention_mode="while-claimed",
            )
        )
        claim = self.control.acquire(
            request(
                host_name="dev96",
                create_if_missing=False,
                renewal_ttl_seconds=60,
            )
        )
        acquisition = self.control.acquisitions.load(
            request(
                host_name="dev96",
                create_if_missing=False,
                renewal_ttl_seconds=60,
            ),
            required=True,
        )
        self.state.write(
            "hostclaimops",
            acquisition["record_name"],
            {"schema_version": "not-an-acquisition"},
        )
        self.clock.now += datetime.timedelta(seconds=60)

        result = self.control.enforce_retirement(execute=True)
        action = next(
            action
            for action in result["actions"]
            if action["host_name"] == "dev96"
        )

        self.assertEqual(action["remaining_claim_count"], 0)
        self.assertTrue(action["due"])
        self.assertTrue(action["executed"])
        self.assertEqual(
            action["error"]["code"],
            "invalid_host_claim_acquisition",
        )
        self.assertEqual(
            self.lifecycle.terminate_calls[-1][0],
            claim.host_name,
        )

    def test_expired_owner_retry_does_not_create_a_second_host(self):
        claim = self.control.acquire(request(renewal_ttl_seconds=60))
        self.clock.now += datetime.timedelta(seconds=60)

        with self.assertRaises(RunpodLocalError) as caught:
            self.control.acquire(request(renewal_ttl_seconds=60))

        self.assertEqual(
            caught.exception.code,
            "host_claim_operation_closed",
        )
        self.assertEqual(len(self.lifecycle.launch_calls), 1)
        ledger = self.control.claims.load(claim.host_name)
        self.assertEqual(
            ledger["closed_claims"][0]["reason"],
            "expired",
        )


if __name__ == "__main__":
    unittest.main()
