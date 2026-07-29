from __future__ import annotations

import dataclasses
import datetime
import os
import pathlib
import socket
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from unittest import mock

from model_session.attachment import ServiceEndpoint, ServiceEndpointBinding
from model_session.service_endpoint import service_workload_identity

from model_lab.configuration import parse_lab_toml
from model_lab.controller import ModelLabController, _PreparationClaimRenewer
from model_lab.errors import ModelLabError
from model_lab.lifecycle import Deployment, DeploymentStore, format_timestamp
from model_lab.paths import (
    authored_root,
    endpoint_receipt_path,
    endpoint_socket_path,
    state_root,
)
from model_lab.profile_binding import (
    PROFILE_BINDING_SCHEMA,
    ProfileBindingStore,
)
from model_lab.runpod_backend import ClaimReleaseResult, HostClaim
from model_lab.service_definition import (
    SERVICE_DEFINITION_SCHEMA,
    SERVICE_PLAN_SCHEMA,
    parse_service_toml,
)
from runpod_local.state import HOST_CONTROLLER_LOCK_SCOPE, StateStore

REVISION = "1" * 40
ADMISSION_EXPIRATION = "2026-07-28T13:00:00Z"


def service_toml(
    *,
    service_id: str = "fixture-chat",
    revision: str = REVISION,
    modalities: str = '"text", "image"',
    language_model_only: str = "false",
    extra: str = "",
) -> bytes:
    return f"""\
schema = "model-lab.service.v1"
service_id = "{service_id}"
driver = "vllm-openai.v1"
runtime_id = "vllm-cu129-v0.25.1"

[model]
source = "huggingface"
repository = "fixture-org/fixture-chat"
revision = "{revision}"
checkpoint = "model.safetensors"
weight_format = "native"

[endpoint]
input_modalities = [{modalities}]
reasoning = true
max_output_tokens = 32768

[compatibility]
minimum_compute_capability = "12.0"

[resources]
gpu_count = 1
gpu_memory_gib = 32
cpu_count = 8
memory_gib = 64
ephemeral_disk_gib = 50
claim_mode = "gpu-exclusive"

[vllm]
model_implementation = "vllm"
dtype = "bfloat16"
quantization = "modelopt_fp4"
tensor_parallel_size = 1
max_model_len = 65536
max_num_sequences = 8
max_num_batched_tokens = 8192
kv_cache_dtype = "bfloat16"
gpu_memory_utilization = 0.90
chunked_prefill = true
load_format = "safetensors"
safetensors_load_strategy = "lazy"
language_model_only = {language_model_only}
mamba_cache_mode = "none"
prefix_caching = false
reasoning_parser = "qwen3"
tool_call_parser = "qwen3_coder"
speculative_method = "mtp"
speculative_tokens = 1
generation_config = "auto"
{extra}
""".encode()


def lab_toml() -> bytes:
    return b"""\
schema = "model-lab.v1"
allowed_runpod_profiles = ["pro6000-is1"]

[lease]
hard_ttl_seconds = 7200
service_idle_ttl_seconds = 1800
renewal_ttl_seconds = 120
minimum_useful_seconds = 1800
startup_timeout_seconds = 300
"""


def ready_deployment(now: datetime.datetime) -> Deployment:
    timestamp = format_timestamp(now)
    return Deployment(
        service_id="fixture-chat",
        deployment_id="deployment-one",
        workload_sha256="a" * 64,
        service_sha256="b" * 64,
        host_name="host-one",
        claim_id="claim-one",
        claim_generation=1,
        endpoint_receipt_path="/run/user/1000/model-lab/services/fixture-chat.json",
        phase="ready",
        created_at=timestamp,
        updated_at=timestamp,
        last_inference_at=timestamp,
        idle_deadline=None,
        host_release_mode=None,
        use_leases=(),
    )


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime.datetime(
            2026,
            7,
            28,
            12,
            0,
            tzinfo=datetime.timezone.utc,
        )

    def __call__(self) -> datetime.datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += datetime.timedelta(seconds=seconds)


class ServiceDefinitionTest(unittest.TestCase):
    def test_service_owns_model_and_resource_semantics(self) -> None:
        service = parse_service_toml(service_toml())

        self.assertEqual(SERVICE_DEFINITION_SCHEMA, "model-lab.service.v1")
        self.assertEqual(service.normalized_plan()["schema"], SERVICE_PLAN_SCHEMA)
        self.assertEqual(service.resources.claim_mode, "gpu-exclusive")
        self.assertEqual(service.resources.gpu_memory_gib, 32)
        self.assertEqual(service.model.revision, REVISION)
        self.assertEqual(service.endpoint.input_modalities, ("image", "text"))
        self.assertEqual(len(service.workload_sha256), 64)
        self.assertEqual(len(service.service_sha256), 64)

    def test_deployment_policy_does_not_change_resume_workload_identity(self) -> None:
        first = parse_service_toml(service_toml())
        second = parse_service_toml(
            service_toml().replace(
                b"gpu_memory_gib = 32",
                b"gpu_memory_gib = 40",
            )
        )

        self.assertEqual(first.workload_sha256, second.workload_sha256)
        self.assertNotEqual(first.service_sha256, second.service_sha256)

    def test_unknown_or_runpod_owned_fields_fail_closed(self) -> None:
        for line in (
            'api_key = "secret"',
            'host_name = "pod"',
            'volume_id = "volume"',
            "remote_port = 8000",
        ):
            with self.subTest(line=line):
                with self.assertRaises(ModelLabError) as caught:
                    parse_service_toml(service_toml(extra=line))
                self.assertEqual(caught.exception.code, "invalid_service_definition")

    def test_multimodal_service_cannot_claim_language_model_only(self) -> None:
        with self.assertRaises(ModelLabError) as caught:
            parse_service_toml(service_toml(language_model_only="true"))
        self.assertEqual(caught.exception.code, "invalid_service_definition")


class ConfigurationAndPathsTest(unittest.TestCase):
    def test_lab_policy_has_no_model_or_provider_secrets(self) -> None:
        configuration = parse_lab_toml(lab_toml())

        self.assertEqual(
            configuration.allowed_runpod_profiles,
            ("pro6000-is1",),
        )
        self.assertEqual(configuration.lease.service_idle_ttl_seconds, 1800)

    def test_model_lab_and_runpod_state_are_siblings(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "HOME": "/home/fixture",
                "XDG_STATE_HOME": "/home/fixture/.local/state",
                "XDG_RUNTIME_DIR": "/run/user/123",
            },
            clear=False,
        ):
            self.assertEqual(authored_root(), pathlib.Path("/mnt/dev/model-lab"))
            self.assertEqual(
                state_root(),
                pathlib.Path("/home/fixture/.local/state/model-lab"),
            )
            self.assertEqual(
                endpoint_socket_path("fixture-chat"),
                pathlib.Path("/run/user/123/model-lab/services/fixture-chat.sock"),
            )
            self.assertEqual(
                endpoint_receipt_path("fixture-chat"),
                pathlib.Path("/run/user/123/model-lab/services/fixture-chat.json"),
            )


class DeploymentLeaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.clock = MutableClock()
        self.store = DeploymentStore(
            pathlib.Path(self.temporary.name) / "state",
            clock=self.clock,
        )
        self.deployment = ready_deployment(self.clock())
        self.store.publish_ready(self.deployment)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run_after_held_deployment_lock(self, operation):
        lock_acquired = threading.Event()
        operation_started = threading.Event()
        release_lock = threading.Event()
        results = []
        errors = []

        def hold_lock():
            with self.store.locked("fixture-chat"):
                lock_acquired.set()
                release_lock.wait()

        def observed_monotonic():
            operation_started.set()
            return time.monotonic()

        def run_operation():
            try:
                results.append(operation(observed_monotonic))
            except BaseException as error:
                errors.append(error)

        holder = threading.Thread(target=hold_lock)
        worker = threading.Thread(target=run_operation)
        holder.start()
        self.assertTrue(lock_acquired.wait(1))
        worker.start()
        try:
            self.assertTrue(operation_started.wait(1))
            self.clock.advance(300)
        finally:
            release_lock.set()
            holder.join()
            worker.join()
        if errors:
            raise errors[0]
        self.assertEqual(len(results), 1)
        return results[0]

    def test_final_use_release_starts_ttl_at_lock_commit_time(self) -> None:
        lease = self.store.acquire_use(
            "fixture-chat",
            lease_id="use-final",
            admission_expires_at=ADMISSION_EXPIRATION,
            admission_release_mode="idle",
            expected_workload_sha256="a" * 64,
            owner_pid=100,
            owner_start_time="start-100",
        )

        result = self._run_after_held_deployment_lock(
            lambda observe: self.store.release_use(
                "fixture-chat",
                lease.lease_id,
                idle_ttl_seconds=1800,
                cleanup_deadline=observe() + 5,
                monotonic=observe,
            )
        )

        self.assertTrue(result.final_use)
        self.assertEqual(result.deployment.updated_at, "2026-07-28T12:05:00Z")
        self.assertEqual(
            result.deployment.idle_deadline,
            "2026-07-28T12:35:00Z",
        )

    def test_use_acquisition_records_lock_commit_time(self) -> None:
        lease = self._run_after_held_deployment_lock(
            lambda observe: self.store.acquire_use(
                "fixture-chat",
                lease_id="use-commit-time",
                admission_expires_at=ADMISSION_EXPIRATION,
                admission_release_mode="idle",
                expected_workload_sha256="a" * 64,
                owner_pid=100,
                owner_start_time="start-100",
                startup_deadline=observe() + 5,
                monotonic=observe,
            )
        )

        self.assertEqual(lease.acquired_at, "2026-07-28T12:05:00Z")
        retained = self.store.load("fixture-chat")
        self.assertEqual(retained.updated_at, "2026-07-28T12:05:00Z")
        self.assertEqual(retained.use_leases, (lease,))

    def test_use_acquisition_is_idempotent_for_caller_lease_identity(
        self,
    ) -> None:
        first = self.store.acquire_use(
            "fixture-chat",
            lease_id="use-idempotent",
            admission_expires_at=ADMISSION_EXPIRATION,
            admission_release_mode="idle",
            expected_workload_sha256="a" * 64,
            owner_pid=100,
            owner_start_time="start-100",
        )
        second = self.store.acquire_use(
            "fixture-chat",
            lease_id="use-idempotent",
            admission_expires_at=ADMISSION_EXPIRATION,
            admission_release_mode="idle",
            expected_workload_sha256="a" * 64,
            owner_pid=100,
            owner_start_time="start-100",
        )

        self.assertEqual(second, first)
        self.assertEqual(
            self.store.load("fixture-chat").use_leases,
            (first,),
        )

        with self.assertRaises(ModelLabError) as caught:
            self.store.acquire_use(
                "fixture-chat",
                lease_id="use-idempotent",
                admission_expires_at=ADMISSION_EXPIRATION,
                admission_release_mode="idle",
                expected_workload_sha256="a" * 64,
                owner_pid=200,
                owner_start_time="start-200",
            )

        self.assertEqual(
            caught.exception.code,
            "use_lease_identity_conflict",
        )
        self.assertEqual(
            self.store.load("fixture-chat").use_leases,
            (first,),
        )

    def test_use_transfer_records_lock_commit_time(self) -> None:
        lease = self.store.acquire_use(
            "fixture-chat",
            lease_id="use-transfer",
            admission_expires_at=ADMISSION_EXPIRATION,
            admission_release_mode="idle",
            expected_workload_sha256="a" * 64,
            owner_pid=100,
            owner_start_time="start-100",
        )

        replacement = self._run_after_held_deployment_lock(
            lambda observe: self.store.transfer_use_owner(
                "fixture-chat",
                lease.lease_id,
                expected_owner_pid=100,
                expected_owner_start_time="start-100",
                owner_pid=200,
                owner_start_time="start-200",
                startup_deadline=observe() + 5,
                monotonic=observe,
            )
        )

        self.assertEqual(replacement.owner_pid, 200)
        self.assertIsNone(replacement.admission_expires_at)
        self.assertIsNone(replacement.admission_release_mode)
        retained = self.store.load("fixture-chat")
        self.assertEqual(retained.updated_at, "2026-07-28T12:05:00Z")
        self.assertEqual(retained.use_leases, (replacement,))

    def test_begin_idle_starts_ttl_at_lock_commit_time(self) -> None:
        deployment = self._run_after_held_deployment_lock(
            lambda observe: self.store.begin_idle(
                "fixture-chat",
                idle_ttl_seconds=1800,
                now=False,
                cleanup_deadline=observe() + 5,
                monotonic=observe,
            )
        )

        self.assertEqual(deployment.updated_at, "2026-07-28T12:05:00Z")
        self.assertEqual(
            deployment.idle_deadline,
            "2026-07-28T12:35:00Z",
        )

    def test_begin_idle_cannot_regress_released_or_cleanup_state(self) -> None:
        quiescing = self.store.begin_idle(
            "fixture-chat",
            idle_ttl_seconds=1800,
            now=True,
        )
        with self.assertRaises(ModelLabError) as caught:
            self.store.begin_idle(
                "fixture-chat",
                idle_ttl_seconds=1800,
                now=True,
            )
        self.assertEqual(
            caught.exception.code,
            "invalid_deployment_transition",
        )
        self.assertEqual(self.store.load("fixture-chat"), quiescing)

        released = dataclasses.replace(quiescing, phase="released")
        self.store.save(released)
        with self.assertRaises(ModelLabError) as caught:
            self.store.begin_idle(
                "fixture-chat",
                idle_ttl_seconds=1800,
                now=True,
            )
        self.assertEqual(
            caught.exception.code,
            "invalid_deployment_transition",
        )
        self.assertEqual(self.store.load("fixture-chat"), released)

    def test_cleanup_escalation_preserves_the_owned_phase(self) -> None:
        idle = self.store.begin_idle(
            "fixture-chat",
            idle_ttl_seconds=1800,
            now=False,
        )
        self.clock.advance(1800)
        quiescing = self.store.begin_idle_cleanup_if_due("fixture-chat")
        self.assertIsNotNone(quiescing)
        assert quiescing is not None
        self.assertEqual(quiescing.host_release_mode, "empty-grace")

        stopping = dataclasses.replace(quiescing, phase="stopping")
        self.store.save(stopping)
        escalated = self.store.escalate_cleanup_now("fixture-chat")

        self.assertEqual(escalated.phase, "stopping")
        self.assertEqual(escalated.host_release_mode, "now")
        self.assertEqual(escalated.deployment_id, idle.deployment_id)

    def test_claim_gone_transition_records_lock_commit_time(self) -> None:
        deployment = self._run_after_held_deployment_lock(
            lambda observe: self.store.begin_claim_gone_cleanup(
                "fixture-chat",
                deployment_id=self.deployment.deployment_id,
                claim_id=self.deployment.claim_id,
                expected_generation=self.deployment.claim_generation,
                cleanup_deadline=observe() + 5,
                monotonic=observe,
            )
        )

        self.assertEqual(deployment.updated_at, "2026-07-28T12:05:00Z")
        self.assertEqual(deployment.phase, "quiescing")
        self.assertEqual(deployment.host_release_mode, "claim-gone")

    def test_inference_accounts_at_lock_commit_time(self) -> None:
        self.store.begin_idle(
            "fixture-chat",
            idle_ttl_seconds=1800,
            now=False,
        )

        deployment = self._run_after_held_deployment_lock(
            lambda observe: self.store.note_inference(
                "fixture-chat",
                idle_ttl_seconds=1800,
                lock_timeout_seconds=5,
                monotonic=observe,
            )
        )

        self.assertEqual(
            deployment.last_inference_at,
            "2026-07-28T12:05:00Z",
        )
        self.assertEqual(
            deployment.idle_deadline,
            "2026-07-28T12:35:00Z",
        )

    def test_inference_accounting_is_bounded_by_lock_timeout(self) -> None:
        lock_acquired = threading.Event()
        release_lock = threading.Event()

        def hold_lock():
            with self.store.locked("fixture-chat"):
                lock_acquired.set()
                release_lock.wait()

        holder = threading.Thread(target=hold_lock)
        holder.start()
        self.assertTrue(lock_acquired.wait(1))
        started_at = time.monotonic()
        try:
            with self.assertRaises(ModelLabError) as caught:
                self.store.note_inference(
                    "fixture-chat",
                    idle_ttl_seconds=1800,
                    lock_timeout_seconds=0.05,
                )
            elapsed = time.monotonic() - started_at
        finally:
            release_lock.set()
            holder.join()

        self.assertEqual(caught.exception.code, "inference_accounting_timeout")
        self.assertLess(elapsed, 1.0)
        retained = self.store.load("fixture-chat")
        self.assertEqual(retained, self.deployment)

    def test_startup_mutation_cannot_publish_after_held_lock_deadline(self):
        lock_acquired = threading.Event()
        release_lock = threading.Event()

        def hold_lock():
            with self.store.locked("fixture-chat"):
                lock_acquired.set()
                release_lock.wait()

        holder = threading.Thread(target=hold_lock)
        holder.start()
        self.assertTrue(lock_acquired.wait(1))
        try:
            with self.assertRaises(ModelLabError) as caught:
                self.store.acquire_use(
                    "fixture-chat",
                    lease_id="use-held-lock",
                    admission_expires_at=ADMISSION_EXPIRATION,
                    admission_release_mode="idle",
                    expected_workload_sha256="a" * 64,
                    owner_pid=100,
                    owner_start_time="start-100",
                    startup_deadline=time.monotonic() + 0.1,
                )
        finally:
            release_lock.set()
            holder.join()

        self.assertEqual(caught.exception.code, "service_startup_timeout")
        self.assertEqual(self.store.load("fixture-chat").use_leases, ())

    def test_preparation_renewer_stop_cancels_held_lock_without_late_mutation(
        self,
    ):
        preparing = dataclasses.replace(
            self.deployment,
            phase="preparing",
            endpoint_receipt_path=None,
        )
        self.store.publish_preparing(preparing)
        host_state = StateStore(
            pathlib.Path(self.temporary.name) / "host-state"
        )
        lock_acquired = threading.Event()
        release_lock = threading.Event()
        renew_started = threading.Event()

        def hold_host_lock():
            with host_state.locked(HOST_CONTROLLER_LOCK_SCOPE):
                lock_acquired.set()
                release_lock.wait()

        class LockingHosts:
            mutations = 0

            def renew(
                self,
                *_arguments,
                startup_deadline=None,
                cancel_event=None,
            ):
                renew_started.set()
                with host_state.locked(
                    HOST_CONTROLLER_LOCK_SCOPE,
                    deadline=startup_deadline,
                    cancel_event=cancel_event,
                ):
                    self.mutations += 1
                raise AssertionError("cancelled renewal mutated state")

        holder = threading.Thread(target=hold_host_lock)
        holder.start()
        self.assertTrue(lock_acquired.wait(1))
        hosts = LockingHosts()
        renewer = _PreparationClaimRenewer(
            hosts=hosts,
            deployments=self.store,
            deployment=preparing,
            renewal_ttl_seconds=120,
            interval_seconds=1.0,
            wait_for_interval=lambda _event, _interval: False,
            startup_deadline=time.monotonic() + 5,
            monotonic=time.monotonic,
        )
        try:
            renewer.start()
            self.assertTrue(renew_started.wait(1))
            self.assertIsNone(renewer.stop())
            self.assertFalse(renewer.thread.is_alive())
            self.assertEqual(hosts.mutations, 0)
        finally:
            release_lock.set()
            holder.join()

    def test_final_use_release_starts_idle_not_host_release(self) -> None:
        first = self.store.acquire_use(
            "fixture-chat",
            lease_id="use-first",
            admission_expires_at=ADMISSION_EXPIRATION,
            admission_release_mode="idle",
            expected_workload_sha256="a" * 64,
            owner_pid=100,
            owner_start_time="start-100",
        )
        second = self.store.acquire_use(
            "fixture-chat",
            lease_id="use-second",
            admission_expires_at=ADMISSION_EXPIRATION,
            admission_release_mode="idle",
            expected_workload_sha256="a" * 64,
            owner_pid=200,
            owner_start_time="start-200",
        )

        nonfinal = self.store.release_use(
            "fixture-chat",
            first.lease_id,
            idle_ttl_seconds=1800,
        )
        self.assertFalse(nonfinal.final_use)
        self.assertEqual(nonfinal.deployment.phase, "ready")
        self.assertIsNone(nonfinal.deployment.idle_deadline)

        final = self.store.release_use(
            "fixture-chat",
            second.lease_id,
            idle_ttl_seconds=1800,
        )
        self.assertTrue(final.final_use)
        self.assertFalse(final.stop_now)
        self.assertEqual(final.deployment.phase, "idle")
        self.assertEqual(
            final.deployment.idle_deadline,
            "2026-07-28T12:30:00Z",
        )

    def test_inference_resets_semantic_idle_deadline(self) -> None:
        lease = self.store.acquire_use(
            "fixture-chat",
            lease_id="use-inference",
            admission_expires_at=ADMISSION_EXPIRATION,
            admission_release_mode="idle",
            expected_workload_sha256="a" * 64,
            owner_pid=100,
            owner_start_time="start-100",
        )
        self.store.release_use(
            "fixture-chat",
            lease.lease_id,
            idle_ttl_seconds=1800,
        )
        self.clock.advance(1200)

        updated = self.store.note_inference(
            "fixture-chat",
            idle_ttl_seconds=1800,
        )

        self.assertEqual(updated.last_inference_at, "2026-07-28T12:20:00Z")
        self.assertEqual(updated.idle_deadline, "2026-07-28T12:50:00Z")
        self.clock.advance(1799)
        self.assertIsNone(
            self.store.begin_idle_cleanup_if_due("fixture-chat")
        )
        self.clock.advance(1)
        due = self.store.begin_idle_cleanup_if_due("fixture-chat")
        self.assertIsNotNone(due)
        self.assertEqual(due.phase, "quiescing")
        self.assertEqual(due.host_release_mode, "empty-grace")

    def test_now_release_quiesces_only_after_final_use(self) -> None:
        first = self.store.acquire_use(
            "fixture-chat",
            lease_id="use-now-first",
            admission_expires_at=ADMISSION_EXPIRATION,
            admission_release_mode="idle",
            expected_workload_sha256="a" * 64,
            owner_pid=100,
            owner_start_time="start-100",
        )
        second = self.store.acquire_use(
            "fixture-chat",
            lease_id="use-now-second",
            admission_expires_at=ADMISSION_EXPIRATION,
            admission_release_mode="idle",
            expected_workload_sha256="a" * 64,
            owner_pid=200,
            owner_start_time="start-200",
        )

        result = self.store.release_use(
            "fixture-chat",
            first.lease_id,
            idle_ttl_seconds=1800,
            now=True,
        )
        self.assertFalse(result.stop_now)
        self.assertEqual(result.deployment.phase, "ready")
        self.assertEqual(result.deployment.host_release_mode, "now")

        result = self.store.release_use(
            "fixture-chat",
            second.lease_id,
            idle_ttl_seconds=1800,
        )
        self.assertTrue(result.stop_now)
        self.assertEqual(result.deployment.phase, "quiescing")
        self.assertEqual(result.deployment.host_release_mode, "now")

    def test_failed_normal_admission_does_not_latch_immediate_release(self):
        active = self.store.acquire_use(
            "fixture-chat",
            lease_id="use-active",
            admission_expires_at=ADMISSION_EXPIRATION,
            admission_release_mode="idle",
            expected_workload_sha256="a" * 64,
            owner_pid=100,
            owner_start_time="start-100",
        )
        failed_admission = self.store.acquire_use(
            "fixture-chat",
            lease_id="use-failed-admission",
            admission_expires_at=ADMISSION_EXPIRATION,
            admission_release_mode="idle",
            expected_workload_sha256="a" * 64,
            owner_pid=200,
            owner_start_time="start-200",
        )

        result = self.store.release_use(
            "fixture-chat",
            failed_admission.lease_id,
            idle_ttl_seconds=1800,
            stop_if_final=True,
        )

        self.assertFalse(result.final_use)
        self.assertFalse(result.stop_now)
        self.assertEqual(result.deployment.phase, "ready")
        self.assertIsNone(result.deployment.host_release_mode)
        self.assertEqual(result.deployment.use_leases, (active,))

        final = self.store.release_use(
            "fixture-chat",
            active.lease_id,
            idle_ttl_seconds=1800,
        )
        self.assertFalse(final.stop_now)
        self.assertEqual(final.deployment.phase, "idle")
        self.assertIsNone(final.deployment.host_release_mode)

    def test_orphan_recovery_honors_latched_immediate_release(self) -> None:
        self.store.acquire_use(
            "fixture-chat",
            lease_id="use-orphan-now",
            admission_expires_at=ADMISSION_EXPIRATION,
            admission_release_mode="now",
            expected_workload_sha256="a" * 64,
            owner_pid=100,
            owner_start_time="start-100",
            stop_on_release=True,
        )

        reconciled = self.store.reconcile_orphaned_uses(
            idle_ttl_seconds=1800,
        )

        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0].phase, "quiescing")
        self.assertEqual(reconciled[0].host_release_mode, "now")
        self.assertIsNone(reconciled[0].idle_deadline)
        self.assertEqual(reconciled[0].use_leases, ())

    def test_orphan_recovery_stops_pending_new_capacity(self) -> None:
        self.store.acquire_use(
            "fixture-chat",
            lease_id="use-orphan-new-capacity",
            admission_expires_at=ADMISSION_EXPIRATION,
            admission_release_mode="stop-if-final",
            expected_workload_sha256="a" * 64,
            owner_pid=100,
            owner_start_time="start-100",
        )

        reconciled = self.store.reconcile_orphaned_uses(
            idle_ttl_seconds=1800,
        )

        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0].phase, "quiescing")
        self.assertEqual(reconciled[0].host_release_mode, "now")
        self.assertIsNone(reconciled[0].idle_deadline)
        self.assertEqual(reconciled[0].use_leases, ())


class FakeHosts:
    def __init__(self) -> None:
        self.claim = HostClaim(
            host_name="host-one",
            claim_id="claim-one",
            generation=1,
            operation_id="operation-one",
            provider_resource_id="pod-one",
            profile_name="pro6000-is1",
            remote_root="/root/runpod-session/claims/claim-one",
            endpoints={"openai": 18000},
            hard_expires_at="2026-07-28T14:00:00Z",
        )
        self.requests = []
        self.releases = []
        self.renewals = []
        self.logical_seconds = 0
        self.claim_expires_at = 120
        self.condition = threading.Condition()
        self.find_result = None
        self.find_requests = []
        self.cancel_requests = []
        self.cancel_error = None
        self.readiness_claims = []
        self.acquisition_deadlines = []
        self.startup_deadlines = []
        self.cleanup_deadlines = []
        self.cleanup_deadline_factory = None

    def acquire(
        self,
        request,
        *,
        startup_deadline,
        cleanup_deadline_factory=None,
    ):
        self.requests.append(request)
        self.acquisition_deadlines.append(startup_deadline)
        self.cleanup_deadline_factory = cleanup_deadline_factory
        return self.claim

    def wait_ready(
        self,
        claim,
        *,
        renewal_ttl_seconds,
        startup_deadline,
    ):
        if startup_deadline <= 0:
            raise AssertionError("fixture received an expired startup deadline")
        self.assert_identity(claim.host_name, claim.claim_id)
        self.readiness_claims.append(
            (claim.operation_id, claim.provider_resource_id, renewal_ttl_seconds)
        )
        self.startup_deadlines.append(startup_deadline)
        return self.claim

    def find(self, request):
        self.find_requests.append(request)
        return self.find_result

    def cancel(self, request, *, cleanup_deadline=None):
        self.cleanup_deadlines.append(cleanup_deadline)
        self.cancel_requests.append(request)
        if self.cancel_error is not None:
            error = self.cancel_error
            self.cancel_error = None
            raise error
        claim = self.find(request)
        if claim is not None:
            self.release(
                claim.host_name,
                claim.claim_id,
                claim.generation,
                now=True,
            )

    def get(
        self,
        host_name,
        claim_id,
        *,
        startup_deadline=None,
    ):
        del startup_deadline
        self.assert_identity(host_name, claim_id)
        return self.claim

    def renew(
        self,
        host_name,
        claim_id,
        expected_generation,
        renewal_ttl_seconds,
        *,
        startup_deadline=None,
        cancel_event=None,
    ):
        del startup_deadline
        if cancel_event is not None and cancel_event.is_set():
            raise ModelLabError(
                "controlled renewal cancellation",
                code="state_lock_cancelled",
            )
        self.assert_identity(host_name, claim_id)
        with self.condition:
            if expected_generation != self.claim.generation:
                raise AssertionError("wrong claim generation")
            if self.logical_seconds >= self.claim_expires_at:
                raise ModelLabError(
                    "controlled claim expired",
                    code="host_claim_expired",
                )
            self.claim = dataclasses.replace(
                self.claim,
                generation=self.claim.generation + 1,
            )
            self.claim_expires_at = (
                self.logical_seconds + renewal_ttl_seconds
            )
            self.renewals.append(
                (self.logical_seconds, self.claim.generation)
            )
            self.condition.notify_all()
            return self.claim

    def advance_and_wait_for_renewal(
        self,
        *,
        seconds: int,
        renewal_count: int,
        trigger: Callable[[], None],
    ) -> None:
        with self.condition:
            self.logical_seconds += seconds
            trigger()
            self.condition.wait_for(
                lambda: len(self.renewals) >= renewal_count
            )

    def release(
        self,
        host_name,
        claim_id,
        expected_generation,
        *,
        now=False,
        cleanup_deadline=None,
    ):
        self.assert_identity(host_name, claim_id)
        self.cleanup_deadlines.append(cleanup_deadline)
        self.releases.append((expected_generation, now))
        return ClaimReleaseResult(
            host_name=host_name,
            claim_id=claim_id,
            released=True,
            final_claim=True,
            retirement="now" if now else "empty-grace",
            empty_deadline=None if now else "2026-07-28T12:05:00Z",
        )

    def assert_identity(self, host_name, claim_id):
        if (host_name, claim_id) != (
            self.claim.host_name,
            self.claim.claim_id,
        ):
            raise AssertionError("wrong claim")


class CrashSafeHosts(FakeHosts):
    def __init__(self) -> None:
        super().__init__()
        self.active = True
        self.crash_before_release = False
        self.gone_code = "host_claim_not_found"

    def get(
        self,
        host_name,
        claim_id,
        *,
        startup_deadline=None,
    ):
        del startup_deadline
        self.assert_identity(host_name, claim_id)
        if not self.active:
            raise ModelLabError(
                "controlled claim is gone",
                code=self.gone_code,
            )
        return self.claim

    def release(
        self,
        host_name,
        claim_id,
        expected_generation,
        *,
        now=False,
        cleanup_deadline=None,
    ):
        if self.crash_before_release:
            self.crash_before_release = False
            raise SystemExit("controlled crash before host release")
        result = super().release(
            host_name,
            claim_id,
            expected_generation,
            now=now,
            cleanup_deadline=cleanup_deadline,
        )
        self.active = False
        return result


class RotatingHosts(CrashSafeHosts):
    def acquire(
        self,
        request,
        *,
        startup_deadline,
        cleanup_deadline_factory=None,
    ):
        if not self.active:
            self.claim = dataclasses.replace(
                self.claim,
                host_name="host-two",
                claim_id="claim-two",
                generation=1,
                operation_id="operation-two",
                provider_resource_id="pod-two",
                remote_root="/root/runpod-session/claims/claim-two",
            )
            self.active = True
        return super().acquire(
            request,
            startup_deadline=startup_deadline,
            cleanup_deadline_factory=cleanup_deadline_factory,
        )


class QuarantinedHosts(RotatingHosts):
    def __init__(self) -> None:
        super().__init__()
        self.quarantined = False

    def renew(
        self,
        host_name,
        claim_id,
        expected_generation,
        renewal_ttl_seconds,
        *,
        startup_deadline=None,
        cancel_event=None,
    ):
        if self.quarantined:
            self.assert_identity(host_name, claim_id)
            raise ModelLabError(
                "controlled sibling claim expired",
                code="host_claim_quarantined",
            )
        return super().renew(
            host_name,
            claim_id,
            expected_generation,
            renewal_ttl_seconds,
            startup_deadline=startup_deadline,
            cancel_event=cancel_event,
        )


class FakeRuntime:
    def __init__(self, socket_path: pathlib.Path) -> None:
        self.socket_path = socket_path
        self.starts = 0
        self.stops = 0
        self.lost_claim_cleanups = 0
        self.stop_error = None
        self.startup_deadlines = []
        self.cleanup_deadlines = []

    def _receipt(self, service, claim, deployment_id):
        metadata = self.socket_path.stat()
        workload = service.service_workload()
        return ServiceEndpoint(
            publication_id="1" * 32,
            binding=ServiceEndpointBinding(
                service_id=service.service_id,
                service_sha256=service.service_sha256,
                workload=workload,
                workload_sha256=service_workload_identity(workload),
                input_modalities=service.endpoint.input_modalities,
            ),
            socket_path=self.socket_path,
            socket_device=metadata.st_dev,
            socket_inode=metadata.st_ino,
            published_at=datetime.datetime(
                2026,
                7,
                28,
                12,
                0,
                tzinfo=datetime.timezone.utc,
            ),
            admission_expires_at=datetime.datetime(
                2099,
                1,
                1,
                tzinfo=datetime.timezone.utc,
            ),
            receipt_path=self.socket_path.with_suffix(".json"),
        )

    def ensure_ready(
        self,
        service,
        claim,
        *,
        deployment_id,
        startup_deadline,
        cleanup_budget=None,
    ):
        del cleanup_budget
        self.startup_deadlines.append(startup_deadline)
        self.starts += 1
        return self._receipt(service, claim, deployment_id)

    def attest_ready(
        self,
        service,
        claim,
        deployment,
        *,
        startup_deadline=None,
    ):
        if startup_deadline is not None:
            self.startup_deadlines.append(startup_deadline)
        return self._receipt(service, claim, deployment.deployment_id)

    def stop(
        self,
        service,
        claim,
        deployment,
        *,
        cleanup_deadline=None,
    ):
        self.cleanup_deadlines.append(cleanup_deadline)
        self.stops += 1
        if self.stop_error is not None:
            error = self.stop_error
            self.stop_error = None
            raise error

    def cleanup_lost_claim(
        self,
        service,
        deployment,
        *,
        cleanup_deadline=None,
    ):
        self.cleanup_deadlines.append(cleanup_deadline)
        self.lost_claim_cleanups += 1


class ControlledSlowRuntime(FakeRuntime):
    def __init__(
        self,
        socket_path: pathlib.Path,
        hosts: FakeHosts,
        renewal_waiter,
    ) -> None:
        super().__init__(socket_path)
        self.hosts = hosts
        self.renewal_waiter = renewal_waiter

    def ensure_ready(
        self,
        service,
        claim,
        *,
        deployment_id,
        startup_deadline,
        cleanup_budget=None,
    ):
        for renewal_count in range(1, 4):
            self.hosts.advance_and_wait_for_renewal(
                seconds=80,
                renewal_count=renewal_count,
                trigger=self.renewal_waiter.trigger,
            )
        return super().ensure_ready(
            service,
            claim,
            deployment_id=deployment_id,
            startup_deadline=startup_deadline,
            cleanup_budget=cleanup_budget,
        )


class ControlledRenewalWaiter:
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.permits = 0

    def __call__(self, stop_event, _interval):
        with self.condition:
            while self.permits == 0 and not stop_event.is_set():
                self.condition.wait(timeout=0.01)
            if stop_event.is_set():
                return True
            self.permits -= 1
            return False

    def trigger(self) -> None:
        with self.condition:
            self.permits += 1
            self.condition.notify_all()


class NotReadyRuntime(FakeRuntime):
    def attest_ready(
        self,
        service,
        claim,
        deployment,
        *,
        startup_deadline=None,
    ):
        del startup_deadline
        raise ModelLabError(
            "controlled runtime is not ready",
            code="service_not_ready",
        )


@dataclasses.dataclass(frozen=True)
class FakeProfile:
    profile_id: str = "chat"
    project_id: str = "model-playground"
    service_id: str = "fixture-chat"
    required_input_modalities: tuple[str, ...] = ("text", "image")


class ControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.socket_path = self.root / "inference.sock"
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.bind(str(self.socket_path))
        self.hosts = FakeHosts()
        self.runtime = FakeRuntime(self.socket_path)
        self.clock = MutableClock()
        self.store = DeploymentStore(self.root / "state", clock=self.clock)
        self.authored_root = self.root / "model-lab"
        (self.authored_root / "profiles" / "chat").mkdir(
            mode=0o700,
            parents=True,
        )
        self.bindings = ProfileBindingStore(self.authored_root)
        self.service = parse_service_toml(service_toml())
        self.controller = ModelLabController(
            hosts=self.hosts,
            runtime=self.runtime,
            deployments=self.store,
            bindings=self.bindings,
            lab=parse_lab_toml(lab_toml()),
            clock=self.clock,
        )

    def tearDown(self) -> None:
        self.socket.close()
        self.temporary.cleanup()

    def test_pi_use_auto_acquires_host_and_final_release_starts_model_idle(self):
        use = self.controller.acquire_for_profile(
            FakeProfile(),
            self.service,
            owner_pid=100,
            owner_start_time="start-100",
        )

        self.assertEqual(self.runtime.starts, 1)
        self.assertEqual(len(self.hosts.requests), 1)
        request = self.hosts.requests[0]
        self.assertEqual(request.owner_system, "model-lab")
        self.assertEqual(request.allowed_profile_names, ("pro6000-is1",))
        self.assertEqual(request.mode, "gpu-exclusive")
        self.assertEqual(request.gpu_memory_bytes, 32 * 1024**3)
        self.assertEqual(request.endpoint_names, ("openai",))
        self.assertEqual(request.acquisition_timeout_seconds, 300)
        self.assertEqual(request.minimum_remaining_seconds, 2100)
        self.assertEqual(
            self.hosts.readiness_claims,
            [("operation-one", "pod-one", 120)],
        )
        self.assertEqual(
            self.hosts.startup_deadlines,
            self.runtime.startup_deadlines,
        )

        self.controller.release_profile_use(self.service, use)
        retained = self.store.load("fixture-chat")
        self.assertIsNotNone(retained)
        self.assertEqual(retained.phase, "idle")
        self.assertEqual(self.hosts.releases, [])

    def test_use_lease_identity_is_allocated_before_paid_capacity(self):
        identity_error = OSError("controlled entropy failure")

        with (
            mock.patch(
                "model_lab.controller.secrets.token_hex",
                side_effect=identity_error,
            ),
            self.assertRaises(OSError) as caught,
        ):
            self.controller.acquire_for_profile(
                FakeProfile(),
                self.service,
                owner_pid=100,
                owner_start_time="start-100",
            )

        self.assertIs(caught.exception, identity_error)
        self.assertEqual(self.hosts.requests, [])
        self.assertEqual(self.runtime.starts, 0)
        self.assertIsNone(self.store.load(self.service.service_id))

    def test_failed_use_acquisition_releases_new_lease_free_deployment(self):
        acquisition_error = ModelLabError(
            "controlled use acquisition failure",
            code="controlled_use_acquisition_failure",
        )

        with (
            mock.patch.object(
                self.store,
                "acquire_use",
                side_effect=acquisition_error,
            ),
            self.assertRaises(ModelLabError) as caught,
        ):
            self.controller.acquire_for_profile(
                FakeProfile(),
                self.service,
                owner_pid=100,
                owner_start_time="start-100",
            )

        self.assertIs(caught.exception, acquisition_error)
        self.assertEqual(self.runtime.stops, 1)
        self.assertEqual(self.hosts.releases, [(1, True)])
        self.assertEqual(
            self.store.load(self.service.service_id).phase,
            "released",
        )

    def test_failed_use_acquisition_idles_reused_lease_free_deployment(self):
        existing, _ = self.controller.ensure_ready(self.service)
        acquisition_error = ModelLabError(
            "controlled use acquisition failure",
            code="controlled_use_acquisition_failure",
        )

        with (
            mock.patch.object(
                self.store,
                "acquire_use",
                side_effect=acquisition_error,
            ),
            self.assertRaises(ModelLabError) as caught,
        ):
            self.controller.acquire_for_profile(
                FakeProfile(),
                self.service,
                owner_pid=100,
                owner_start_time="start-100",
            )

        self.assertIs(caught.exception, acquisition_error)
        retained = self.store.load(self.service.service_id)
        self.assertEqual(retained.deployment_id, existing.deployment_id)
        self.assertEqual(retained.phase, "idle")
        self.assertEqual(self.runtime.stops, 0)
        self.assertEqual(self.hosts.releases, [])

    def test_failed_use_acquisition_retains_cleanup_failure(self):
        acquisition_error = ModelLabError(
            "controlled use acquisition failure",
            code="controlled_use_acquisition_failure",
        )
        self.runtime.stop_error = ModelLabError(
            "controlled runtime cleanup failure",
            code="controlled_runtime_cleanup_failure",
        )

        with (
            mock.patch.object(
                self.store,
                "acquire_use",
                side_effect=acquisition_error,
            ),
            self.assertRaises(ModelLabError) as caught,
        ):
            self.controller.acquire_for_profile(
                FakeProfile(),
                self.service,
                owner_pid=100,
                owner_start_time="start-100",
            )

        self.assertEqual(caught.exception.code, "service_cleanup_required")
        self.assertIs(caught.exception.__cause__, acquisition_error)
        self.assertEqual(
            self.store.load(self.service.service_id).phase,
            "quiescing",
        )
        self.assertEqual(self.hosts.releases, [])

    def test_post_commit_use_acquisition_failure_releases_exact_new_lease(
        self,
    ):
        acquisition_error = OSError(
            "controlled post-commit acquisition failure"
        )
        original_save = self.store.save
        failed_after_commit = False

        def fail_after_lease_commit(deployment):
            nonlocal failed_after_commit
            original_save(deployment)
            if not failed_after_commit and deployment.use_leases:
                failed_after_commit = True
                raise acquisition_error

        with (
            mock.patch.object(
                self.store,
                "save",
                side_effect=fail_after_lease_commit,
            ),
            self.assertRaises(OSError) as caught,
        ):
            self.controller.acquire_for_profile(
                FakeProfile(),
                self.service,
                owner_pid=100,
                owner_start_time="start-100",
            )

        self.assertIs(caught.exception, acquisition_error)
        retained = self.store.load(self.service.service_id)
        self.assertEqual(retained.phase, "released")
        self.assertEqual(retained.use_leases, ())
        self.assertEqual(self.runtime.stops, 1)
        self.assertEqual(self.hosts.releases, [(1, True)])

    def test_post_commit_use_acquisition_failure_idles_reused_service(
        self,
    ):
        existing, _ = self.controller.ensure_ready(self.service)
        acquisition_error = OSError(
            "controlled post-commit acquisition failure"
        )
        original_save = self.store.save
        failed_after_commit = False

        def fail_after_lease_commit(deployment):
            nonlocal failed_after_commit
            original_save(deployment)
            if not failed_after_commit and deployment.use_leases:
                failed_after_commit = True
                raise acquisition_error

        with (
            mock.patch.object(
                self.store,
                "save",
                side_effect=fail_after_lease_commit,
            ),
            self.assertRaises(OSError) as caught,
        ):
            self.controller.acquire_for_profile(
                FakeProfile(),
                self.service,
                owner_pid=100,
                owner_start_time="start-100",
            )

        self.assertIs(caught.exception, acquisition_error)
        retained = self.store.load(self.service.service_id)
        self.assertEqual(retained.deployment_id, existing.deployment_id)
        self.assertEqual(retained.phase, "idle")
        self.assertEqual(retained.use_leases, ())
        self.assertEqual(self.runtime.stops, 0)
        self.assertEqual(self.hosts.releases, [])

    def test_normal_release_post_commit_ambiguity_is_durably_confirmed(
        self,
    ):
        use = self.controller.acquire_for_profile(
            FakeProfile(),
            self.service,
            owner_pid=100,
            owner_start_time="start-100",
        )
        cleanup_error = OSError("controlled post-commit cleanup failure")
        original_save = self.store.save
        failed_after_commit = False

        def fail_after_release_commit(deployment):
            nonlocal failed_after_commit
            original_save(deployment)
            if (
                not failed_after_commit
                and deployment.phase == "idle"
                and not deployment.use_leases
            ):
                failed_after_commit = True
                raise cleanup_error

        with mock.patch.object(
            self.store,
            "save",
            side_effect=fail_after_release_commit,
        ):
            self.controller.release_profile_use(self.service, use)

        self.assertTrue(failed_after_commit)
        retained = self.store.load(self.service.service_id)
        self.assertEqual(retained.phase, "idle")
        self.assertEqual(retained.use_leases, ())
        self.assertEqual(self.runtime.stops, 0)
        self.assertEqual(self.hosts.releases, [])

    def test_post_commit_cleanup_ambiguity_is_durably_confirmed(self):
        acquisition_error = OSError(
            "controlled post-commit acquisition failure"
        )
        cleanup_error = OSError("controlled post-commit cleanup failure")
        original_save = self.store.save
        acquisition_committed = False

        def fail_after_each_commit(deployment):
            nonlocal acquisition_committed
            original_save(deployment)
            if not acquisition_committed and deployment.use_leases:
                acquisition_committed = True
                raise acquisition_error
            if (
                acquisition_committed
                and deployment.phase == "quiescing"
                and not deployment.use_leases
            ):
                raise cleanup_error

        with (
            mock.patch.object(
                self.store,
                "save",
                side_effect=fail_after_each_commit,
            ),
            self.assertRaises(OSError) as caught,
        ):
            self.controller.acquire_for_profile(
                FakeProfile(),
                self.service,
                owner_pid=100,
                owner_start_time="start-100",
            )

        self.assertIs(caught.exception, acquisition_error)
        retained = self.store.load(self.service.service_id)
        self.assertEqual(retained.phase, "released")
        self.assertEqual(retained.use_leases, ())
        self.assertEqual(self.runtime.stops, 1)
        self.assertEqual(self.hosts.releases, [(1, True)])

    def test_pre_commit_cleanup_failure_retains_expiring_pending_lease(self):
        acquisition_error = OSError(
            "controlled post-commit acquisition failure"
        )
        cleanup_error = OSError("controlled pre-commit cleanup failure")
        original_save = self.store.save
        acquisition_committed = False
        cleanup_attempts = 0

        def fail_before_cleanup_commit(deployment):
            nonlocal acquisition_committed, cleanup_attempts
            if not acquisition_committed and deployment.use_leases:
                original_save(deployment)
                acquisition_committed = True
                raise acquisition_error
            if acquisition_committed and not deployment.use_leases:
                cleanup_attempts += 1
                raise cleanup_error
            original_save(deployment)

        with (
            mock.patch.object(
                self.store,
                "save",
                side_effect=fail_before_cleanup_commit,
            ),
            self.assertRaises(ModelLabError) as caught,
        ):
            self.controller.acquire_for_profile(
                FakeProfile(),
                self.service,
                owner_pid=100,
                owner_start_time="start-100",
            )

        self.assertEqual(caught.exception.code, "service_cleanup_required")
        self.assertIs(caught.exception.__cause__, acquisition_error)
        self.assertEqual(cleanup_attempts, 2)
        retained = self.store.load(self.service.service_id)
        self.assertEqual(retained.phase, "ready")
        self.assertEqual(len(retained.use_leases), 1)
        pending = retained.use_leases[0]
        self.assertEqual(pending.admission_release_mode, "stop-if-final")
        self.assertIsNotNone(pending.admission_expires_at)

        self.clock.advance(300)
        self.assertTrue(
            self.controller.release_expired_pending_uses(self.service)
        )
        retained = self.store.load(self.service.service_id)
        self.assertEqual(retained.phase, "released")
        self.assertEqual(retained.use_leases, ())
        self.assertEqual(self.runtime.stops, 1)
        self.assertEqual(self.hosts.releases, [(1, True)])

    def test_expired_admission_reaper_preserves_transferred_session(self):
        use = self.controller.acquire_for_profile(
            FakeProfile(),
            self.service,
            owner_pid=100,
            owner_start_time="start-100",
        )
        transferred = self.store.transfer_use_owner(
            self.service.service_id,
            use.lease.lease_id,
            expected_owner_pid=100,
            expected_owner_start_time="start-100",
            owner_pid=200,
            owner_start_time="start-200",
        )
        self.clock.advance(300)

        self.assertFalse(
            self.controller.release_expired_pending_uses(self.service)
        )
        retained = self.store.load(self.service.service_id)
        self.assertEqual(retained.use_leases, (transferred,))
        self.assertEqual(retained.phase, "ready")

    def test_ssh_readiness_failure_releases_claim_before_deployment_publish(self):
        def fail_after_renewal(
            claim,
            *,
            renewal_ttl_seconds,
            startup_deadline,
        ):
            del renewal_ttl_seconds, startup_deadline
            self.hosts.claim = dataclasses.replace(
                claim,
                generation=2,
            )
            raise ModelLabError(
                "controlled SSH readiness timeout",
                code="service_host_ssh_not_ready",
            )

        self.hosts.wait_ready = mock.Mock(side_effect=fail_after_renewal)

        with self.assertRaises(ModelLabError) as caught:
            self.controller.ensure_ready(self.service)

        self.assertEqual(
            caught.exception.code,
            "service_host_ssh_not_ready",
        )
        self.assertIsNone(self.store.load(self.service.service_id))
        self.assertEqual(self.runtime.starts, 0)
        self.assertEqual(self.hosts.releases, [(2, True)])
        self.assertIsNone(
            self.controller.preparations.load(self.service.service_id)
        )

    def test_host_rollback_and_intent_cleanup_share_one_lazy_deadline(self):
        monotonic_now = [10.0]
        started_deadlines = []
        completion_deadlines = []

        def fail_after_host_rollback(
            _request,
            *,
            startup_deadline,
            cleanup_deadline_factory,
        ):
            self.assertGreater(startup_deadline, monotonic_now[0])
            started_deadlines.append(cleanup_deadline_factory())
            monotonic_now[0] = 25.0
            raise ModelLabError(
                "controlled host rollback failure",
                code="rollback_required",
            )

        self.hosts.acquire = fail_after_host_rollback
        self.controller = ModelLabController(
            hosts=self.hosts,
            runtime=self.runtime,
            deployments=self.store,
            bindings=self.bindings,
            lab=parse_lab_toml(lab_toml()),
            clock=self.clock,
            monotonic=lambda: monotonic_now[0],
        )
        complete_intent = self.controller.preparations.complete

        def complete_with_observation(intent, **arguments):
            completion_deadlines.append(arguments["deadline"])
            return complete_intent(intent, **arguments)

        self.controller.preparations.complete = complete_with_observation

        with self.assertRaises(ModelLabError) as caught:
            self.controller.ensure_ready(self.service)

        self.assertEqual(caught.exception.code, "rollback_required")
        self.assertEqual(started_deadlines, [70.0])
        self.assertEqual(self.hosts.cleanup_deadlines, [70.0])
        self.assertEqual(completion_deadlines, [70.0])
        self.assertIsNone(
            self.controller.preparations.load(self.service.service_id)
        )

    def test_new_claim_gets_fresh_budget_after_stale_identity_cleanup(self):
        monotonic_now = [10.0]
        self.controller = ModelLabController(
            hosts=self.hosts,
            runtime=self.runtime,
            deployments=self.store,
            bindings=self.bindings,
            lab=parse_lab_toml(lab_toml()),
            clock=self.clock,
            monotonic=lambda: monotonic_now[0],
        )
        self.controller.preparations.begin(
            service_id=self.service.service_id,
            workload_sha256=self.service.workload_sha256,
            service_sha256=self.service.service_sha256,
            startup_expires_at=self.controller.new_startup_expiration(),
            claim_request_factory=lambda operation_id: (
                self.controller._claim_request(
                    self.service,
                    operation_id=operation_id,
                    host_name=None,
                )
            ),
        )
        acquisition_cleanup_deadlines = []

        def fail_new_acquisition(
            _request,
            *,
            startup_deadline,
            cleanup_deadline_factory,
        ):
            self.assertGreater(startup_deadline, monotonic_now[0])
            monotonic_now[0] = 80.0
            acquisition_cleanup_deadlines.append(
                cleanup_deadline_factory()
            )
            raise ModelLabError(
                "controlled replacement acquisition failure",
                code="controlled_replacement_acquisition_failure",
            )

        self.hosts.acquire = fail_new_acquisition

        with self.assertRaises(ModelLabError) as caught:
            self.controller.ensure_ready(self.service)

        self.assertEqual(
            caught.exception.code,
            "controlled_replacement_acquisition_failure",
        )
        self.assertEqual(self.hosts.cleanup_deadlines, [70.0, 140.0])
        self.assertEqual(acquisition_cleanup_deadlines, [140.0])
        self.assertIsNone(
            self.controller.preparations.load(self.service.service_id)
        )

    def test_runtime_rollback_stop_and_claim_release_share_one_deadline(self):
        monotonic_now = [10.0]
        runtime_rollback_deadlines = []

        def fail_after_runtime_rollback(
            _service,
            _claim,
            *,
            deployment_id,
            startup_deadline,
            cleanup_budget,
        ):
            self.assertIsInstance(deployment_id, str)
            self.assertGreater(startup_deadline, monotonic_now[0])
            runtime_rollback_deadlines.append(cleanup_budget.deadline())
            monotonic_now[0] = 25.0
            raise ModelLabError(
                "controlled runtime rollback failure",
                code="controlled_runtime_failure",
            )

        self.runtime.ensure_ready = fail_after_runtime_rollback
        self.controller = ModelLabController(
            hosts=self.hosts,
            runtime=self.runtime,
            deployments=self.store,
            bindings=self.bindings,
            lab=parse_lab_toml(lab_toml()),
            clock=self.clock,
            monotonic=lambda: monotonic_now[0],
        )

        with self.assertRaises(ModelLabError) as caught:
            self.controller.ensure_ready(self.service)

        self.assertEqual(caught.exception.code, "controlled_runtime_failure")
        self.assertEqual(runtime_rollback_deadlines, [70.0])
        self.assertEqual(self.runtime.cleanup_deadlines, [70.0])
        self.assertEqual(self.hosts.cleanup_deadlines, [70.0])
        self.assertEqual(self.store.load(self.service.service_id).phase, "released")

    def test_crash_before_failed_readiness_release_recovers_from_intent(self):
        self.hosts.wait_ready = mock.Mock(
            side_effect=ModelLabError(
                "controlled SSH readiness timeout",
                code="service_host_ssh_not_ready",
            )
        )
        release = self.hosts.release
        self.hosts.release = mock.Mock(
            side_effect=SystemExit(
                "controlled crash before readiness claim release"
            )
        )

        with self.assertRaisesRegex(SystemExit, "before readiness claim"):
            self.controller.ensure_ready(self.service)

        intent = self.controller.preparations.load(
            self.service.service_id
        )
        self.assertIsNotNone(intent)
        self.assertIsNone(self.store.load(self.service.service_id))
        self.hosts.release = release
        self.hosts.find_result = self.hosts.claim

        self.controller.reconcile_acquire_intent(intent)

        self.assertEqual(self.hosts.releases, [(1, True)])
        self.assertIsNone(
            self.controller.preparations.load(self.service.service_id)
        )

    def test_now_on_pi_exit_stops_service_and_releases_host_claim_now(self):
        use = self.controller.acquire_for_profile(
            FakeProfile(),
            self.service,
            owner_pid=100,
            owner_start_time="start-100",
            stop_on_release=True,
        )

        self.controller.release_profile_use(self.service, use)

        self.assertEqual(self.runtime.stops, 1)
        self.assertEqual(self.hosts.releases, [(1, True)])
        self.assertEqual(
            self.runtime.cleanup_deadlines,
            self.hosts.cleanup_deadlines,
        )
        self.assertEqual(len(self.runtime.cleanup_deadlines), 1)
        self.assertEqual(self.store.load("fixture-chat").phase, "released")

    def test_idle_stop_releases_claim_through_runpod_empty_grace(self):
        use = self.controller.acquire_for_profile(
            FakeProfile(),
            self.service,
            owner_pid=100,
            owner_start_time="start-100",
        )
        self.controller.release_profile_use(self.service, use)
        self.clock.advance(1800)

        self.assertTrue(self.controller.stop_if_idle_due(self.service))
        self.assertEqual(self.runtime.stops, 1)
        self.assertEqual(self.hosts.releases, [(1, False)])

    def test_completed_inference_winning_idle_transition_prevents_stop(self):
        use = self.controller.acquire_for_profile(
            FakeProfile(),
            self.service,
            owner_pid=100,
            owner_start_time="start-100",
        )
        self.controller.release_profile_use(self.service, use)
        self.clock.advance(1800)
        atomic_transition = self.store.begin_idle_cleanup_if_due
        inference_committed = False

        def inference_wins_before_recheck(service_id, **keywords):
            nonlocal inference_committed
            if not inference_committed:
                inference_committed = True
                self.store.note_inference(
                    service_id,
                    idle_ttl_seconds=1800,
                )
            return atomic_transition(service_id, **keywords)

        self.store.begin_idle_cleanup_if_due = inference_wins_before_recheck

        self.assertFalse(self.controller.stop_if_idle_due(self.service))
        retained = self.store.load(self.service.service_id)
        self.assertEqual(retained.phase, "idle")
        self.assertEqual(
            retained.idle_deadline,
            "2026-07-28T13:00:00Z",
        )
        self.assertEqual(self.runtime.stops, 0)
        self.assertEqual(self.hosts.releases, [])

    def test_vanished_claim_requires_channel_closure_before_reacquire(self):
        self.hosts = RotatingHosts()
        self.controller = ModelLabController(
            hosts=self.hosts,
            runtime=self.runtime,
            deployments=self.store,
            bindings=self.bindings,
            lab=parse_lab_toml(lab_toml()),
            clock=self.clock,
        )
        old_use = self.controller.acquire_for_profile(
            FakeProfile(),
            self.service,
            owner_pid=100,
            owner_start_time="start-100",
        )
        old_deployment_id = old_use.deployment.deployment_id
        self.hosts.active = False

        with self.assertRaises(ModelLabError) as caught:
            self.controller.acquire_for_profile(
                FakeProfile(),
                self.service,
                owner_pid=200,
                owner_start_time="start-200",
            )

        self.assertEqual(caught.exception.code, "service_claim_lost")
        self.assertEqual(self.runtime.lost_claim_cleanups, 1)
        self.assertEqual(self.runtime.starts, 1)
        self.assertEqual(
            self.store.load(self.service.service_id).phase,
            "released",
        )
        new_use = self.controller.acquire_for_profile(
            FakeProfile(),
            self.service,
            owner_pid=200,
            owner_start_time="start-200",
        )
        self.assertNotEqual(
            new_use.deployment.deployment_id,
            old_deployment_id,
        )
        self.assertEqual(new_use.deployment.host_name, "host-two")
        self.assertEqual(self.runtime.starts, 2)
        retained = self.store.load(self.service.service_id)
        self.assertEqual(
            [lease.lease_id for lease in retained.use_leases],
            [new_use.lease.lease_id],
        )
        with self.assertRaises(ModelLabError) as caught:
            self.controller.release_profile_use(self.service, old_use)
        self.assertEqual(caught.exception.code, "use_lease_not_found")

    def test_cleanup_failure_retains_claim_until_idempotent_retry(self):
        self.controller.ensure_ready(self.service)
        self.runtime.stop_error = ModelLabError(
            "controlled credential clear failure",
            code="remote_hf_credential_failed",
        )

        with self.assertRaises(ModelLabError) as caught:
            self.controller.down(self.service, now=True)

        self.assertEqual(caught.exception.code, "service_cleanup_required")
        retained = self.store.load(self.service.service_id)
        self.assertEqual(retained.phase, "quiescing")
        self.assertEqual(self.hosts.releases, [])

        released = self.controller.reconcile_cleanup(
            self.service,
            retained,
        )

        self.assertEqual(released.phase, "released")
        self.assertEqual(self.runtime.stops, 2)
        self.assertEqual(self.hosts.releases, [(1, True)])

    def test_sibling_expiry_drains_valid_claim_before_reacquiring_elsewhere(self):
        self.hosts = QuarantinedHosts()
        self.controller = ModelLabController(
            hosts=self.hosts,
            runtime=self.runtime,
            deployments=self.store,
            bindings=self.bindings,
            lab=parse_lab_toml(lab_toml()),
            clock=self.clock,
        )
        old_use = self.controller.acquire_for_profile(
            FakeProfile(),
            self.service,
            owner_pid=100,
            owner_start_time="start-100",
        )
        old_deployment_id = old_use.deployment.deployment_id
        self.hosts.quarantined = True

        with self.assertRaises(ModelLabError) as caught:
            self.controller.acquire_for_profile(
                FakeProfile(),
                self.service,
                owner_pid=200,
                owner_start_time="start-200",
            )

        self.assertEqual(caught.exception.code, "service_claim_drained")
        self.assertEqual(self.runtime.stops, 1)
        self.assertEqual(self.runtime.lost_claim_cleanups, 0)
        self.assertEqual(self.hosts.releases, [(1, True)])
        self.hosts.quarantined = False
        new_use = self.controller.acquire_for_profile(
            FakeProfile(),
            self.service,
            owner_pid=200,
            owner_start_time="start-200",
        )
        self.assertNotEqual(
            new_use.deployment.deployment_id,
            old_deployment_id,
        )
        self.assertEqual(new_use.deployment.host_name, "host-two")

    def test_renewal_adopts_generation_committed_before_local_crash(self):
        deployment, _ = self.controller.ensure_ready(self.service)
        original_renew_generation = self.store.renew_claim_generation
        crash_once = True

        def crash_after_provider_renewal(*args, **kwargs):
            nonlocal crash_once
            if crash_once and kwargs["generation"] == 2:
                crash_once = False
                raise SystemExit("controlled crash after provider renewal")
            return original_renew_generation(*args, **kwargs)

        self.store.renew_claim_generation = crash_after_provider_renewal
        with self.assertRaisesRegex(SystemExit, "provider renewal"):
            self.controller.renew_deployment_claim(deployment)
        self.store.renew_claim_generation = original_renew_generation

        self.assertEqual(self.hosts.claim.generation, 2)
        self.assertEqual(
            self.store.load(self.service.service_id).claim_generation,
            1,
        )

        recovered = self.controller.renew_deployment_claim(
            self.store.load(self.service.service_id)
        )

        self.assertEqual(recovered.claim_generation, 3)
        self.assertEqual(self.hosts.claim.generation, 3)

    def test_ready_admission_adopts_then_renews_provider_generation(self):
        self.controller.ensure_ready(self.service)
        self.hosts.claim = dataclasses.replace(
            self.hosts.claim,
            generation=2,
        )

        deployment, _ = self.controller.ensure_ready(self.service)

        self.assertEqual(deployment.claim_generation, 3)
        self.assertEqual(
            self.store.load(self.service.service_id).claim_generation,
            3,
        )

    def test_not_ready_runtime_is_stopped_and_replaced_in_one_ensure(self):
        first, _ = self.controller.ensure_ready(self.service)
        replacement_runtime = NotReadyRuntime(self.socket_path)
        self.runtime = replacement_runtime
        self.controller.runtime = replacement_runtime

        recovered, _ = self.controller.ensure_ready(self.service)

        self.assertNotEqual(
            recovered.deployment_id,
            first.deployment_id,
        )
        self.assertEqual(replacement_runtime.stops, 1)
        self.assertEqual(replacement_runtime.starts, 1)
        self.assertEqual(self.hosts.releases, [(2, True)])
        self.assertEqual(len(self.hosts.requests), 2)

    def test_wall_rollback_cannot_refresh_recovery_startup_deadline(self):
        class RollbackMonotonic:
            def __init__(self) -> None:
                self.rollback = False
                self.rollback_values = iter((109.0, 111.0, 111.0))

            def __call__(self) -> float:
                if not self.rollback:
                    return 100.0
                return next(self.rollback_values, 111.0)

        monotonic = RollbackMonotonic()
        self.controller = ModelLabController(
            hosts=self.hosts,
            runtime=self.runtime,
            deployments=self.store,
            bindings=self.bindings,
            lab=parse_lab_toml(lab_toml()),
            clock=self.clock,
            monotonic=monotonic,
        )
        self.controller.ensure_ready(
            self.service,
            startup_expires_at=self.controller.new_startup_expiration(),
            startup_deadline=110.0,
        )
        replacement_runtime = NotReadyRuntime(self.socket_path)

        def fail_after_wall_rollback(*_args, **_kwargs):
            self.clock.value -= datetime.timedelta(hours=1)
            monotonic.rollback = True
            raise ModelLabError(
                "controlled runtime is not ready",
                code="service_not_ready",
            )

        replacement_runtime.attest_ready = fail_after_wall_rollback
        self.controller.runtime = replacement_runtime
        expiration = self.controller.new_startup_expiration()

        with self.assertRaises(ModelLabError) as caught:
            self.controller.ensure_ready(
                self.service,
                startup_expires_at=expiration,
                startup_deadline=110.0,
            )

        self.assertEqual(caught.exception.code, "service_startup_timeout")
        self.assertEqual(replacement_runtime.stops, 1)
        self.assertEqual(replacement_runtime.starts, 0)
        self.assertEqual(len(self.hosts.requests), 1)
        self.assertEqual(
            self.store.load(self.service.service_id).phase,
            "released",
        )

    def test_restart_resumes_after_runtime_stop_without_stopping_twice(self):
        self.hosts = CrashSafeHosts()
        self.hosts.crash_before_release = True
        self.controller = ModelLabController(
            hosts=self.hosts,
            runtime=self.runtime,
            deployments=self.store,
            bindings=self.bindings,
            lab=parse_lab_toml(lab_toml()),
            clock=self.clock,
        )
        self.controller.ensure_ready(self.service)

        with self.assertRaisesRegex(SystemExit, "controlled crash"):
            self.controller.down(self.service, now=True)

        stopping = self.store.load(self.service.service_id)
        self.assertEqual(stopping.phase, "stopping")
        self.assertEqual(stopping.host_release_mode, "now")
        self.assertEqual(self.runtime.stops, 1)

        recovered = self.controller.reconcile_cleanup(
            self.service,
            stopping,
        )

        self.assertEqual(recovered.phase, "released")
        self.assertEqual(self.runtime.stops, 1)
        self.assertEqual(self.hosts.releases, [(1, True)])

    def test_restart_finishes_after_claim_release_before_final_checkpoint(self):
        self.hosts = CrashSafeHosts()
        self.controller = ModelLabController(
            hosts=self.hosts,
            runtime=self.runtime,
            deployments=self.store,
            bindings=self.bindings,
            lab=parse_lab_toml(lab_toml()),
            clock=self.clock,
        )
        self.controller.ensure_ready(self.service)
        original_save = self.store.save
        crash_once = True

        def crash_before_released_checkpoint(deployment):
            nonlocal crash_once
            if deployment.phase == "released" and crash_once:
                crash_once = False
                raise SystemExit("controlled crash before released checkpoint")
            return original_save(deployment)

        self.store.save = crash_before_released_checkpoint
        self.controller.down(self.service, now=False)
        self.clock.advance(1800)
        with self.assertRaisesRegex(SystemExit, "released checkpoint"):
            self.controller.stop_if_idle_due(self.service)
        self.store.save = original_save

        stopping = self.store.load(self.service.service_id)
        self.assertEqual(stopping.phase, "stopping")
        self.assertFalse(self.hosts.active)
        self.assertEqual(self.runtime.stops, 1)

        recovered = self.controller.reconcile_cleanup(
            self.service,
            stopping,
        )

        self.assertEqual(recovered.phase, "released")
        self.assertEqual(self.runtime.stops, 1)
        self.assertEqual(self.hosts.releases, [(1, False)])

    def test_slow_start_renews_persisted_preparing_claim(self):
        renewal_waiter = ControlledRenewalWaiter()
        self.runtime = ControlledSlowRuntime(
            self.socket_path,
            self.hosts,
            renewal_waiter,
        )
        self.controller = ModelLabController(
            hosts=self.hosts,
            runtime=self.runtime,
            deployments=self.store,
            bindings=self.bindings,
            lab=parse_lab_toml(lab_toml()),
            preparation_renewal_interval_seconds=40,
            preparation_waiter=renewal_waiter,
            clock=self.clock,
        )

        deployment, _ = self.controller.ensure_ready(self.service)

        self.assertEqual(
            self.hosts.renewals,
            [(80, 2), (160, 3), (240, 4)],
        )
        self.assertEqual(deployment.phase, "ready")
        self.assertEqual(deployment.claim_generation, 4)
        self.assertEqual(
            self.store.load("fixture-chat").claim_generation,
            4,
        )
        self.assertGreater(self.hosts.logical_seconds, 120)

    def test_preparing_claim_lookup_uses_persisted_startup_deadline(self):
        monotonic_now = [100.0]
        self.controller = ModelLabController(
            hosts=self.hosts,
            runtime=self.runtime,
            deployments=self.store,
            bindings=self.bindings,
            lab=parse_lab_toml(lab_toml()),
            clock=self.clock,
            monotonic=lambda: monotonic_now[0],
        )
        preparing = self._preparing_deployment()
        self.store.publish_preparing(preparing)
        self.clock.advance(200)
        claim_lookup_deadlines = []
        get_claim = self.hosts.get

        def observe_claim_lookup(
            host_name,
            claim_id,
            *,
            startup_deadline=None,
        ):
            claim_lookup_deadlines.append(startup_deadline)
            return get_claim(
                host_name,
                claim_id,
                startup_deadline=startup_deadline,
            )

        self.hosts.get = observe_claim_lookup

        recovered, _ = self.controller.ensure_ready(
            self.service,
            startup_expires_at=self.controller.new_startup_expiration(),
            startup_deadline=400.0,
        )

        self.assertEqual(claim_lookup_deadlines, [200.0])
        self.assertEqual(self.runtime.startup_deadlines, [200.0])
        self.assertEqual(recovered.phase, "ready")

    def test_expired_preparation_is_rejected_before_startup_claim_lookup(self):
        monotonic_now = [100.0]
        self.controller = ModelLabController(
            hosts=self.hosts,
            runtime=self.runtime,
            deployments=self.store,
            bindings=self.bindings,
            lab=parse_lab_toml(lab_toml()),
            clock=self.clock,
            monotonic=lambda: monotonic_now[0],
        )
        preparing = self._preparing_deployment()
        self.store.publish_preparing(preparing)
        self.clock.advance(300)
        claim_lookup_deadlines = []
        get_claim = self.hosts.get

        def observe_claim_lookup(
            host_name,
            claim_id,
            *,
            startup_deadline=None,
        ):
            claim_lookup_deadlines.append(startup_deadline)
            return get_claim(
                host_name,
                claim_id,
                startup_deadline=startup_deadline,
            )

        self.hosts.get = observe_claim_lookup

        with self.assertRaises(ModelLabError) as caught:
            self.controller.ensure_ready(
                self.service,
                startup_expires_at=self.controller.new_startup_expiration(),
                startup_deadline=400.0,
            )

        self.assertEqual(caught.exception.code, "service_startup_timeout")
        self.assertEqual(claim_lookup_deadlines, [160.0, 160.0])
        self.assertEqual(self.runtime.stops, 1)
        self.assertEqual(self.hosts.releases, [(1, True)])
        self.assertEqual(
            self.store.load(self.service.service_id).phase,
            "released",
        )
        self.assertIsNone(
            self.controller.preparations.load(self.service.service_id)
        )

    def _preparing_deployment(self) -> Deployment:
        intent = self.controller.preparations.begin(
            service_id=self.service.service_id,
            workload_sha256=self.service.workload_sha256,
            service_sha256=self.service.service_sha256,
            startup_expires_at=self.controller.new_startup_expiration(),
            claim_request_factory=lambda operation_id: (
                self.controller._claim_request(
                    self.service,
                    operation_id=operation_id,
                    host_name=None,
                )
            ),
        )
        self.hosts.claim = dataclasses.replace(
            self.hosts.claim,
            operation_id=intent.operation_id,
        )
        return Deployment(
            service_id=self.service.service_id,
            deployment_id=intent.deployment_id,
            workload_sha256=self.service.workload_sha256,
            service_sha256=self.service.service_sha256,
            host_name=self.hosts.claim.host_name,
            claim_id=self.hosts.claim.claim_id,
            claim_generation=1,
            endpoint_receipt_path=None,
            phase="preparing",
            created_at=intent.created_at,
            updated_at=intent.created_at,
            last_inference_at=intent.created_at,
            idle_deadline=None,
            host_release_mode=None,
            use_leases=(),
        )

    def test_boot_recovery_adopts_renewed_generation_and_ready_runtime(self):
        preparing = self._preparing_deployment()
        self.store.publish_preparing(preparing)
        self.hosts.claim = dataclasses.replace(
            self.hosts.claim,
            generation=2,
        )

        recovered = self.controller.reconcile_preparing(
            self.service,
            preparing,
        )

        self.assertEqual(recovered.phase, "ready")
        self.assertEqual(recovered.claim_generation, 2)
        self.assertEqual(
            recovered.endpoint_receipt_path,
            str(self.socket_path.with_suffix(".json")),
        )

    def test_boot_recovery_stops_partial_runtime_and_releases_latest_claim(self):
        preparing = self._preparing_deployment()
        self.store.publish_preparing(preparing)
        self.runtime = NotReadyRuntime(self.socket_path)
        self.controller = ModelLabController(
            hosts=self.hosts,
            runtime=self.runtime,
            deployments=self.store,
            bindings=self.bindings,
            lab=parse_lab_toml(lab_toml()),
            clock=self.clock,
        )

        recovered = self.controller.reconcile_preparing(
            self.service,
            preparing,
        )

        self.assertEqual(recovered.phase, "released")
        self.assertEqual(self.runtime.stops, 1)
        self.assertEqual(self.hosts.releases, [(1, True)])

    def test_boot_recovery_fails_closed_without_preparation_intent(self):
        preparing = self._preparing_deployment()
        intent = self.controller.preparations.load(
            self.service.service_id
        )
        self.assertIsNotNone(intent)
        self.controller.preparations.complete(intent)
        self.store.publish_preparing(preparing)

        recovered = self.controller.reconcile_preparing(
            self.service,
            preparing,
        )

        self.assertEqual(recovered.phase, "released")
        self.assertEqual(self.runtime.stops, 1)
        self.assertEqual(self.hosts.releases, [(1, True)])

    def test_pre_acquire_intent_releases_only_found_exact_claim(self):
        intent = self.controller.preparations.begin(
            service_id=self.service.service_id,
            workload_sha256=self.service.workload_sha256,
            service_sha256=self.service.service_sha256,
            startup_expires_at=self.controller.new_startup_expiration(),
            claim_request_factory=lambda operation_id: (
                self.controller._claim_request(
                    self.service,
                    operation_id=operation_id,
                    host_name=None,
                )
            ),
        )
        self.hosts.find_result = dataclasses.replace(
            self.hosts.claim,
            generation=7,
        )

        self.controller.reconcile_acquire_intent(intent)

        self.assertEqual(self.hosts.requests, [])
        self.assertEqual(self.hosts.find_requests, [intent.claim_request])
        self.assertEqual(self.hosts.cancel_requests, [intent.claim_request])
        self.assertEqual(self.hosts.releases, [(7, True)])
        self.assertIsNone(
            self.controller.preparations.load(self.service.service_id)
        )

    def test_pre_acquire_intent_survives_failed_exact_cancellation(self):
        intent = self.controller.preparations.begin(
            service_id=self.service.service_id,
            workload_sha256=self.service.workload_sha256,
            service_sha256=self.service.service_sha256,
            startup_expires_at=self.controller.new_startup_expiration(),
            claim_request_factory=lambda operation_id: (
                self.controller._claim_request(
                    self.service,
                    operation_id=operation_id,
                    host_name=None,
                )
            ),
        )
        self.hosts.cancel_error = ModelLabError(
            "controlled ambiguous provider cleanup",
            code="host_acquisition_timeout_cleanup_required",
        )

        with self.assertRaises(ModelLabError) as caught:
            self.controller.reconcile_acquire_intent(intent)

        self.assertEqual(
            caught.exception.code,
            "host_acquisition_timeout_cleanup_required",
        )
        self.assertEqual(
            self.controller.preparations.load(self.service.service_id),
            intent,
        )

        self.controller.reconcile_acquire_intent(intent)

        self.assertIsNone(
            self.controller.preparations.load(self.service.service_id)
        )
        self.assertEqual(
            self.hosts.cancel_requests,
            [intent.claim_request, intent.claim_request],
        )

    def test_pre_acquire_intent_survives_cleanup_deadline_expiry(self):
        monotonic_now = [10.0]
        self.controller = ModelLabController(
            hosts=self.hosts,
            runtime=self.runtime,
            deployments=self.store,
            bindings=self.bindings,
            lab=parse_lab_toml(lab_toml()),
            clock=self.clock,
            monotonic=lambda: monotonic_now[0],
        )
        intent = self.controller.preparations.begin(
            service_id=self.service.service_id,
            workload_sha256=self.service.workload_sha256,
            service_sha256=self.service.service_sha256,
            startup_expires_at=self.controller.new_startup_expiration(),
            claim_request_factory=lambda operation_id: (
                self.controller._claim_request(
                    self.service,
                    operation_id=operation_id,
                    host_name=None,
                )
            ),
        )
        cancellation_deadlines = []

        def expire_during_cancel(_request, *, cleanup_deadline):
            cancellation_deadlines.append(cleanup_deadline)
            monotonic_now[0] = cleanup_deadline

        self.hosts.cancel = expire_during_cancel

        with self.assertRaises(ModelLabError) as caught:
            self.controller.reconcile_acquire_intent(
                intent,
                cleanup_deadline=70.0,
            )

        self.assertEqual(caught.exception.code, "service_cleanup_required")
        self.assertEqual(cancellation_deadlines, [70.0])
        self.assertEqual(
            self.controller.preparations.load(self.service.service_id),
            intent,
        )

    def test_intent_completion_expiry_during_validation_is_nonmutating(self):
        intent = self.controller.preparations.begin(
            service_id=self.service.service_id,
            workload_sha256=self.service.workload_sha256,
            service_sha256=self.service.service_sha256,
            startup_expires_at=self.controller.new_startup_expiration(),
            claim_request_factory=lambda operation_id: (
                self.controller._claim_request(
                    self.service,
                    operation_id=operation_id,
                    host_name=None,
                )
            ),
        )
        monotonic_now = [10.0]
        load_intent = self.controller.preparations.load

        def load_then_expire(service_id):
            loaded = load_intent(service_id)
            monotonic_now[0] = 70.0
            return loaded

        with (
            mock.patch.object(
                self.controller.preparations,
                "load",
                side_effect=load_then_expire,
            ),
            self.assertRaises(ModelLabError) as caught,
        ):
            self.controller.preparations.complete(
                intent,
                deadline=70.0,
                monotonic=lambda: monotonic_now[0],
                deadline_error_code="service_startup_timeout",
            )

        self.assertEqual(caught.exception.code, "service_startup_timeout")
        self.assertEqual(
            load_intent(self.service.service_id),
            intent,
        )

    def test_profile_workload_drift_fails_before_host_claim(self):
        self.bindings.attest(FakeProfile(), self.service)
        changed = parse_service_toml(
            service_toml(revision="2" * 40),
        )

        with self.assertRaises(ModelLabError) as caught:
            self.controller.acquire_for_profile(
                FakeProfile(),
                changed,
                owner_pid=100,
                owner_start_time="start-100",
            )

        self.assertEqual(caught.exception.code, "profile_binding_drift")
        self.assertEqual(self.hosts.requests, [])
        binding = self.bindings.load("chat")
        self.assertEqual(binding.service_id, "fixture-chat")
        self.assertEqual(binding.workload_sha256, self.service.workload_sha256)

    def test_profile_service_drift_fails_before_host_claim(self):
        self.bindings.attest(FakeProfile(), self.service)
        changed = parse_service_toml(
            service_toml(service_id="other-chat"),
        )

        with self.assertRaises(ModelLabError) as caught:
            self.controller.acquire_for_profile(
                FakeProfile(service_id="other-chat"),
                changed,
                owner_pid=100,
                owner_start_time="start-100",
            )

        self.assertEqual(caught.exception.code, "profile_binding_drift")
        self.assertEqual(self.hosts.requests, [])

    def test_profile_binding_is_canonical_and_portable(self):
        binding = self.bindings.attest(FakeProfile(), self.service)

        self.assertEqual(binding.profile_id, "chat")
        document = self.bindings.path("chat").read_text(encoding="ascii")
        self.assertIn(f'"schema":"{PROFILE_BINDING_SCHEMA}"', document)
        self.assertTrue(document.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
