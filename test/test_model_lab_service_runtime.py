from __future__ import annotations

import datetime
import pathlib
import socket
import tempfile
import unittest
from types import SimpleNamespace

from model_session.attachment import ServiceEndpoint, ServiceEndpointBinding
from model_session.service_endpoint import service_workload_identity

from model_lab.cleanup import CleanupBudget
from model_lab.errors import ModelLabError
from model_lab.lifecycle import DeploymentStore
from model_lab.runpod_backend import HostClaim
from model_lab.service_definition import parse_service_toml
from model_lab.service_runtime import (
    PreparedService,
    ProductionServiceRuntime,
    TransportBinding,
    cache_mode_for_state,
)
from test_model_lab_core import service_toml


class Backend:
    def __init__(self, socket_path: pathlib.Path) -> None:
        self.socket_path = socket_path
        self.events = []
        self.cache_state = "accepted"
        self.stage_error = None
        self.load_error = None
        self.execute_errors = {}
        self.open_error = None
        self.close_error = None
        self.clear_error = None
        self.credential_present = False
        self.transport_live = True
        self.deadline_events = []

    def _record_deadline(self, operation, startup_deadline):
        if startup_deadline is not None:
            self.deadline_events.append((operation, startup_deadline))

    def prepare(
        self,
        service,
        claim,
        *,
        deployment_id,
        startup_deadline=None,
    ):
        self._record_deadline("prepare", startup_deadline)
        self.events.append("prepare")
        return PreparedService(
            service_id=service.service_id,
            deployment_id=deployment_id,
            host_name=claim.host_name,
            claim_id=claim.claim_id,
            handle="prepared-one",
        )

    def load(
        self,
        service,
        claim,
        deployment,
        *,
        startup_deadline=None,
    ):
        self._record_deadline("load", startup_deadline)
        self.events.append("load")
        if self.load_error is not None:
            raise self.load_error
        return PreparedService(
            service_id=service.service_id,
            deployment_id=deployment.deployment_id,
            host_name=claim.host_name,
            claim_id=claim.claim_id,
            handle="prepared-one",
        )

    def push_huggingface_credential(
        self,
        prepared,
        *,
        startup_deadline=None,
    ):
        self._record_deadline("credential-push", startup_deadline)
        self.events.append("credential-push")
        self.credential_present = True

    def clear_huggingface_credential(
        self,
        prepared,
        *,
        startup_deadline=None,
    ):
        self._record_deadline("credential-clear", startup_deadline)
        self.events.append("credential-clear")
        if self.clear_error is not None:
            raise self.clear_error
        self.credential_present = False

    def execute(
        self,
        prepared,
        action,
        *,
        cache_mode=None,
        startup_deadline=None,
    ):
        self._record_deadline(action, startup_deadline)
        self.events.append((action, cache_mode))
        if action == "stage-snapshot" and self.stage_error is not None:
            raise self.stage_error
        if action in self.execute_errors:
            raise self.execute_errors[action]
        if action == "status":
            return {"phase": "ready", "ready": True}
        return {"status": "completed"}

    def inspect_cache(self, prepared, *, startup_deadline=None):
        self._record_deadline("cache-inspect", startup_deadline)
        self.events.append("cache-inspect")
        return self.cache_state

    def open_transport(
        self,
        prepared,
        *,
        completed,
        startup_deadline=None,
    ):
        self._record_deadline("transport-open", startup_deadline)
        self.events.append("transport-open")
        if self.open_error is not None:
            raise self.open_error
        return TransportBinding(str(self.socket_path), "transport-one")

    def restore_transport(
        self,
        prepared,
        *,
        completed,
        startup_deadline=None,
    ):
        self._record_deadline("transport-restore", startup_deadline)
        self.events.append("transport-restore")
        self.transport_live = True
        return TransportBinding(str(self.socket_path), "transport-one")

    def transport_is_live(self, prepared, transport):
        return self.transport_live

    def close_transport(
        self,
        prepared,
        transport,
        *,
        startup_deadline=None,
    ):
        self._record_deadline("transport-close", startup_deadline)
        self.events.append("transport-close")
        if self.close_error is not None:
            raise self.close_error


class Publisher:
    def __init__(self, service, socket_path):
        self.service = service
        self.socket_path = socket_path
        self.endpoint = None
        self.events = []
        self.deadline_events = []
        self.publish_error = None
        self.revoke_error = None
        self.load_error = None
        self.load_active = True

    def _record_deadline(
        self,
        operation,
        startup_deadline,
        deadline_error_code,
    ):
        if startup_deadline is not None:
            self.deadline_events.append(
                (
                    operation,
                    startup_deadline,
                    deadline_error_code,
                )
            )

    def publish(
        self,
        service,
        transport,
        *,
        ttl_seconds,
        startup_deadline=None,
        deadline_error_code="service_startup_timeout",
    ):
        self._record_deadline(
            "publish",
            startup_deadline,
            deadline_error_code,
        )
        self.events.append(("publish", ttl_seconds))
        if self.publish_error is not None:
            raise self.publish_error
        metadata = self.socket_path.stat()
        workload = service.service_workload()
        self.endpoint = ServiceEndpoint(
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
            published_at=datetime.datetime.now(datetime.timezone.utc),
            admission_expires_at=datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=ttl_seconds),
            receipt_path=self.socket_path.with_suffix(".json"),
        )
        return self.endpoint

    def revoke(
        self,
        endpoint,
        *,
        startup_deadline=None,
        deadline_error_code="service_startup_timeout",
    ):
        self._record_deadline(
            "revoke",
            startup_deadline,
            deadline_error_code,
        )
        self.events.append(("revoke", endpoint.publication_id))
        if self.revoke_error is not None:
            raise self.revoke_error
        self.endpoint = None

    def load(
        self,
        service,
        *,
        startup_deadline=None,
        deadline_error_code="service_startup_timeout",
    ):
        self._record_deadline(
            "load",
            startup_deadline,
            deadline_error_code,
        )
        if self.load_error is not None:
            raise self.load_error
        return self.endpoint if self.load_active else None

    def inspect(
        self,
        service,
        *,
        startup_deadline=None,
        deadline_error_code="service_startup_timeout",
    ):
        self._record_deadline(
            "inspect",
            startup_deadline,
            deadline_error_code,
        )
        if self.load_error is not None:
            raise self.load_error
        return self.endpoint


def claim() -> HostClaim:
    return HostClaim(
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


class ProductionServiceRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.socket_path = self.root / "endpoint.sock"
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(self.socket_path))
        self.service = parse_service_toml(service_toml())
        self.backend = Backend(self.socket_path)
        self.publisher = Publisher(self.service, self.socket_path)
        self.runtime = ProductionServiceRuntime(
            backend=self.backend,
            publisher=self.publisher,
            deployments=DeploymentStore(self.root / "state"),
            endpoint_ttl_seconds=1800,
            service_idle_ttl_seconds=900,
        )

    def tearDown(self):
        self.listener.close()
        self.temporary.cleanup()

    def test_exact_stage_cache_setup_start_order(self):
        endpoint = self.runtime.ensure_ready(
            self.service,
            claim(),
            deployment_id="deployment-one",
        )

        self.assertEqual(endpoint.binding.service_id, self.service.service_id)
        self.assertEqual(
            self.backend.events,
            [
                "prepare",
                "credential-push",
                ("stage-snapshot", None),
                "credential-clear",
                "cache-inspect",
                ("prepare-cache", "accepted"),
                ("setup", "accepted"),
                ("start", "accepted"),
                ("status", None),
                "transport-open",
            ],
        )

    def test_one_startup_deadline_reaches_every_remote_stage(self):
        self.runtime.monotonic = lambda: 0.0

        self.runtime.ensure_ready(
            self.service,
            claim(),
            deployment_id="deployment-one",
            startup_deadline=42.0,
        )

        self.assertEqual(
            self.backend.deadline_events,
            [
                ("prepare", 42.0),
                ("credential-push", 42.0),
                ("stage-snapshot", 42.0),
                ("credential-clear", 42.0),
                ("cache-inspect", 42.0),
                ("prepare-cache", 42.0),
                ("setup", 42.0),
                ("start", 42.0),
                ("status", 42.0),
                ("transport-open", 42.0),
            ],
        )

    def test_endpoint_publication_and_cleanup_share_caller_deadlines(self):
        self.runtime.monotonic = lambda: 10.0
        endpoint = self.runtime.ensure_ready(
            self.service,
            claim(),
            deployment_id="deployment-one",
            startup_deadline=42.0,
        )

        self.runtime.stop(
            self.service,
            claim(),
            SimpleNamespace(
                deployment_id="deployment-one",
                use_leases=(),
            ),
            cleanup_deadline=70.0,
        )

        self.assertIsNotNone(endpoint)
        self.assertEqual(
            self.publisher.deadline_events,
            [
                ("publish", 42.0, "service_startup_timeout"),
                ("inspect", 70.0, "service_cleanup_required"),
                ("revoke", 70.0, "service_cleanup_required"),
            ],
        )

    def test_stage_failure_still_clears_ephemeral_hf_token(self):
        self.backend.stage_error = ModelLabError("stage failed")

        with self.assertRaisesRegex(ModelLabError, "stage failed"):
            self.runtime.ensure_ready(
                self.service,
                claim(),
                deployment_id="deployment-one",
            )

        self.assertEqual(
            self.backend.events,
            [
                "prepare",
                "credential-push",
                ("stage-snapshot", None),
                "credential-clear",
                "transport-close",
                ("stop", None),
            ],
        )

    def test_publish_failure_rolls_back_transport_and_started_runtime(self):
        self.publisher.publish_error = ModelLabError(
            "controlled publication failure",
            code="controlled_publish_failure",
        )

        with self.assertRaisesRegex(
            ModelLabError,
            "controlled publication failure",
        ):
            self.runtime.ensure_ready(
                self.service,
                claim(),
                deployment_id="deployment-one",
            )

        self.assertEqual(
            self.backend.events[-3:],
            ["transport-open", "transport-close", ("stop", None)],
        )
        self.assertNotIn("deployment-one", self.runtime.transports)

    def test_caller_cleanup_budget_survives_partial_rollback_and_retry(self):
        monotonic_now = [10.0]
        self.runtime.monotonic = lambda: monotonic_now[0]
        cleanup_budget = CleanupBudget(
            timeout_seconds=60.0,
            monotonic=lambda: monotonic_now[0],
        )
        self.publisher.publish_error = ModelLabError(
            "controlled publication failure",
            code="controlled_publish_failure",
        )

        with self.assertRaises(ModelLabError):
            self.runtime.ensure_ready(
                self.service,
                claim(),
                deployment_id="deployment-one",
                cleanup_budget=cleanup_budget,
            )

        self.assertEqual(cleanup_budget.started_deadline, 70.0)
        monotonic_now[0] = 25.0
        self.publisher.publish_error = None
        self.runtime.stop(
            self.service,
            claim(),
            SimpleNamespace(
                deployment_id="deployment-one",
                use_leases=(),
            ),
            cleanup_deadline=cleanup_budget.deadline(),
        )
        cleanup_deadlines = [
            deadline
            for _, deadline in self.backend.deadline_events
            if deadline is not None
        ]
        self.assertTrue(cleanup_deadlines)
        self.assertEqual(set(cleanup_deadlines), {70.0})

    def test_publication_crossing_deadline_rolls_back_all_authority(self):
        current = [0.0]
        self.runtime.monotonic = lambda: current[0]
        publish = self.publisher.publish

        def publish_at_deadline(*args, **kwargs):
            endpoint = publish(*args, **kwargs)
            current[0] = 42.0
            return endpoint

        self.publisher.publish = publish_at_deadline

        with self.assertRaises(ModelLabError) as caught:
            self.runtime.ensure_ready(
                self.service,
                claim(),
                deployment_id="deployment-one",
                startup_deadline=42.0,
            )

        self.assertEqual(caught.exception.code, "service_startup_timeout")
        self.assertIsNone(self.publisher.endpoint)
        self.assertNotIn("deployment-one", self.runtime.transports)
        self.assertEqual(
            self.backend.events[-2:],
            ["transport-close", ("stop", None)],
        )
        self.assertEqual(
            self.backend.deadline_events[-2:],
            [
                ("transport-close", 102.0),
                ("stop", 102.0),
            ],
        )

    def test_rollback_attempts_every_cleanup_and_reports_all_failures(self):
        self.publisher.publish_error = ModelLabError("publish failed")
        self.publisher.load_error = ModelLabError("load failed")
        self.backend.close_error = ModelLabError("close failed")
        self.backend.execute_errors["stop"] = ModelLabError("stop failed")

        with self.assertRaises(ModelLabError) as caught:
            self.runtime.ensure_ready(
                self.service,
                claim(),
                deployment_id="deployment-one",
            )

        self.assertEqual(caught.exception.code, "service_cleanup_required")
        self.assertIn("endpoint-load=load failed", str(caught.exception))
        self.assertIn("transport=close failed", str(caught.exception))
        self.assertIn("runtime=stop failed", str(caught.exception))
        self.assertEqual(
            self.backend.events[-3:],
            ["transport-open", "transport-close", ("stop", None)],
        )

    def test_stop_attempts_endpoint_transport_and_runtime_independently(self):
        endpoint = self.runtime.ensure_ready(
            self.service,
            claim(),
            deployment_id="deployment-one",
        )
        self.publisher.endpoint = endpoint
        self.publisher.revoke_error = ModelLabError("revoke failed")
        self.backend.close_error = ModelLabError("close failed")
        self.backend.execute_errors["stop"] = ModelLabError("stop failed")
        deployment = SimpleNamespace(
            deployment_id="deployment-one",
            use_leases=(),
        )

        with self.assertRaises(ModelLabError) as caught:
            self.runtime.stop(self.service, claim(), deployment)

        self.assertEqual(caught.exception.code, "service_cleanup_required")
        self.assertIn("endpoint=revoke failed", str(caught.exception))
        self.assertIn("transport=close failed", str(caught.exception))
        self.assertIn("runtime=stop failed", str(caught.exception))

    def test_stop_shares_one_fresh_deadline_across_every_owned_cleanup(self):
        self.runtime.monotonic = lambda: 10.0
        deployment = SimpleNamespace(
            deployment_id="deployment-one",
            use_leases=(),
        )

        self.runtime.stop(self.service, claim(), deployment)

        self.assertEqual(
            self.backend.deadline_events,
            [
                ("load", 70.0),
                ("credential-clear", 70.0),
                ("transport-close", 70.0),
                ("stop", 70.0),
            ],
        )

    def test_unreadable_installation_revokes_local_authority_but_retains_error(
        self,
    ):
        endpoint = self.runtime.ensure_ready(
            self.service,
            claim(),
            deployment_id="deployment-one",
        )
        self.publisher.endpoint = endpoint
        self.backend.load_error = ModelLabError(
            "installation record is missing",
            code="service_installation_not_found",
        )
        deployment = SimpleNamespace(
            deployment_id="deployment-one",
            use_leases=(),
        )

        with self.assertRaises(ModelLabError) as caught:
            self.runtime.stop(self.service, claim(), deployment)

        self.assertEqual(caught.exception.code, "service_cleanup_required")
        self.assertIn(
            "installation=installation record is missing",
            str(caught.exception),
        )
        self.assertIsNone(self.publisher.endpoint)
        self.assertNotIn("deployment-one", self.runtime.transports)
        self.assertEqual(self.backend.events[-2:], ["load", "transport-close"])

    def test_restart_cleanup_clears_token_left_after_staging_process_death(self):
        prepared = self.backend.prepare(
            self.service,
            claim(),
            deployment_id="deployment-one",
        )
        self.backend.push_huggingface_credential(prepared)
        recovered_runtime = ProductionServiceRuntime(
            backend=self.backend,
            publisher=self.publisher,
            deployments=DeploymentStore(self.root / "recovered-state"),
            endpoint_ttl_seconds=1800,
            service_idle_ttl_seconds=900,
        )

        recovered_runtime.stop(
            self.service,
            claim(),
            SimpleNamespace(deployment_id="deployment-one"),
        )

        self.assertFalse(self.backend.credential_present)
        self.assertLess(
            self.backend.events.index("credential-clear"),
            self.backend.events.index(("stop", None)),
        )

    def test_failed_credential_clear_is_retried_idempotently(self):
        self.backend.credential_present = True
        self.backend.clear_error = ModelLabError(
            "controlled credential clear failure",
            code="remote_hf_credential_failed",
        )
        deployment = SimpleNamespace(
            deployment_id="deployment-one",
            use_leases=(),
        )

        with self.assertRaises(ModelLabError) as caught:
            self.runtime.stop(self.service, claim(), deployment)

        self.assertEqual(caught.exception.code, "service_cleanup_required")
        self.assertIn("credential=controlled credential clear failure", str(caught.exception))
        self.assertTrue(self.backend.credential_present)

        self.backend.clear_error = None
        self.runtime.stop(self.service, claim(), deployment)

        self.assertFalse(self.backend.credential_present)
        self.assertEqual(self.backend.events.count("credential-clear"), 2)

    def test_lost_claim_cleanup_revokes_only_local_authority(self):
        endpoint = self.runtime.ensure_ready(
            self.service,
            claim(),
            deployment_id="deployment-one",
        )
        self.publisher.endpoint = endpoint
        remote_event_count = len(
            [
                event
                for event in self.backend.events
                if isinstance(event, tuple)
            ]
        )

        self.runtime.cleanup_lost_claim(
            self.service,
            SimpleNamespace(
                deployment_id="deployment-one",
                host_name="host-one",
                claim_id="claim-one",
            ),
        )

        self.assertIsNone(self.publisher.endpoint)
        self.assertNotIn("deployment-one", self.runtime.transports)
        self.assertEqual(
            len(
                [
                    event
                    for event in self.backend.events
                    if isinstance(event, tuple)
                ]
            ),
            remote_event_count,
        )
        self.assertEqual(self.backend.events[-1], "transport-close")

    def test_cache_state_selects_one_explicit_mode(self):
        self.assertEqual(cache_mode_for_state("accepted"), "accepted")
        self.assertEqual(cache_mode_for_state("candidate"), "candidate-proof")
        self.assertEqual(cache_mode_for_state("absent"), "author")
        with self.assertRaises(ModelLabError):
            cache_mode_for_state("corrupt")

    def test_attestation_republishes_over_retained_stale_receipt(self):
        first = self.runtime.ensure_ready(
            self.service,
            claim(),
            deployment_id="deployment-one",
        )
        self.assertIs(self.publisher.inspect(self.service), first)
        self.publisher.load_active = False
        deployment = SimpleNamespace(deployment_id="deployment-one")

        second = self.runtime.attest_ready(
            self.service,
            claim(),
            deployment,
        )

        self.assertIs(second, self.publisher.endpoint)
        self.assertEqual(
            [event for event in self.publisher.events if event[0] == "publish"],
            [("publish", 1800), ("publish", 1800)],
        )
        self.assertNotIn("transport-restore", self.backend.events)

    def test_attestation_replaces_a_dead_local_transport(self):
        first = self.runtime.ensure_ready(
            self.service,
            claim(),
            deployment_id="deployment-one",
        )
        self.publisher.endpoint = first
        self.backend.transport_live = False
        deployment = SimpleNamespace(
            deployment_id="deployment-one",
            use_leases=(),
        )

        second = self.runtime.attest_ready(
            self.service,
            claim(),
            deployment,
        )

        self.assertIs(second, self.publisher.endpoint)
        self.assertEqual(
            self.backend.events[-4:],
            [
                "load",
                ("status", None),
                "transport-close",
                "transport-restore",
            ],
        )
        self.assertEqual(
            [event[0] for event in self.publisher.events],
            ["publish", "revoke", "publish"],
        )
        self.assertIn("deployment-one", self.runtime.transports)

    def test_dead_transport_with_active_users_requires_channel_rebind(self):
        first = self.runtime.ensure_ready(
            self.service,
            claim(),
            deployment_id="deployment-one",
        )
        self.publisher.endpoint = first
        self.backend.transport_live = False
        deployment = SimpleNamespace(
            deployment_id="deployment-one",
            use_leases=(SimpleNamespace(lease_id="use-one"),),
        )

        with self.assertRaises(ModelLabError) as caught:
            self.runtime.attest_ready(
                self.service,
                claim(),
                deployment,
            )

        self.assertEqual(
            caught.exception.code,
            "service_transport_replaced",
        )
        self.assertTrue(self.backend.transport_live)
        rebound = self.runtime.attest_ready(
            self.service,
            claim(),
            deployment,
        )
        self.assertIs(rebound, self.publisher.endpoint)


if __name__ == "__main__":
    unittest.main()
