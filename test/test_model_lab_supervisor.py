from __future__ import annotations

import datetime
import os
import pathlib
import socket
import stat
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from model_lab.configuration import parse_lab_toml
from model_lab.controller import ModelLabController, ServiceUse
from model_lab.errors import ModelLabError
from model_lab.lifecycle import DeploymentStore
from model_lab.profile_binding import ProfileBindingStore
from model_lab.service_definition import parse_service_toml
from model_lab.supervisor import ModelLabSupervisor
from model_lab.supervisor_client import (
    PendingPiUse,
    PiLeaseChannel,
    SupervisorClient,
    subprocess_model_session,
)
from model_lab.supervisor_protocol import (
    SESSION_USE_ACCEPTED_SCHEMA,
    SESSION_USE_ADMIT_SCHEMA,
    SUPERVISOR_ERROR_SCHEMA,
    process_start_time,
    receive_document,
    send_document,
)
from model_session.attachment import (
    ServiceEndpoint,
    ServiceEndpointBinding,
    ServiceWorkload,
)
from test_model_lab_core import (
    FakeProfile,
    FakeRuntime,
    NotReadyRuntime,
    QuarantinedHosts,
    lab_toml,
    service_toml,
)


class FakeDeployments:
    def __init__(self) -> None:
        self.transfers = []

    def reconcile_orphaned_uses(self, *, idle_ttl_seconds):
        return ()

    def transfer_use_owner(
        self,
        service_id,
        lease_id,
        *,
        expected_owner_pid,
        expected_owner_start_time,
        owner_pid,
        owner_start_time,
    ):
        self.transfers.append(
            (
                service_id,
                lease_id,
                expected_owner_pid,
                expected_owner_start_time,
                owner_pid,
                owner_start_time,
            )
        )
        return SimpleNamespace(
            lease_id=lease_id,
            owner_pid=owner_pid,
            owner_start_time=owner_start_time,
        )

    def list(self):
        return ()


class FakeController:
    def __init__(self) -> None:
        self.lab = SimpleNamespace(
            lease=SimpleNamespace(
                service_idle_ttl_seconds=1800,
                renewal_ttl_seconds=120,
            )
        )
        self.deployments = FakeDeployments()
        self.preparations = SimpleNamespace(list=lambda: ())
        self.acquisitions = 0
        self.releases = []
        self.active_mutations = 0
        self.maximum_mutations = 0

    def acquire_for_profile(
        self,
        route,
        service,
        *,
        host_name,
        owner_pid,
        owner_start_time,
    ):
        self.active_mutations += 1
        self.maximum_mutations = max(
            self.maximum_mutations,
            self.active_mutations,
        )
        time.sleep(0.01)
        self.acquisitions += 1
        self.active_mutations -= 1
        return ServiceUse(
            deployment=SimpleNamespace(
                service_id=service.service_id,
                deployment_id="deployment-one",
                workload_sha256="a" * 64,
            ),
            endpoint=SimpleNamespace(),
            lease=SimpleNamespace(
                lease_id=f"use-{self.acquisitions}",
                owner_pid=owner_pid,
                owner_start_time=owner_start_time,
            ),
        )

    def release_profile_use(self, service, use, *, now):
        self.releases.append((service.service_id, use.lease.lease_id, now))

    @staticmethod
    def is_claim_quarantined(error):
        return error.code == "host_claim_quarantined"


class SupervisorLeaseChannelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temporary.name)
        self.authored = root / "authored"
        self.state = root / "state"
        self.runtime = root / "runtime"
        for path in (self.authored, self.state):
            path.mkdir(mode=0o700)
        self.route = SimpleNamespace(
            profile_id="chat",
            project_id="playground",
            service_id="fixture-chat",
            required_input_modalities=("text",),
        )
        self.service = SimpleNamespace(
            service_id="fixture-chat",
            workload_sha256="a" * 64,
        )
        self.controller = FakeController()
        self.supervisor = ModelLabSupervisor(
            controller=self.controller,
            authored_root=self.authored,
            state_root=self.state,
            runtime_root=self.runtime,
            maintenance_interval_seconds=3600,
        )
        self.supervisor.deployed_services.publish = lambda service: None
        self.patches = (
            mock.patch(
                "model_lab.supervisor.load_profile_route",
                return_value=self.route,
            ),
            mock.patch(
                "model_lab.supervisor.load_service_id",
                return_value=self.service,
            ),
        )
        for patch in self.patches:
            patch.start()
        self.thread = threading.Thread(
            target=self.supervisor.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.assertTrue(self.supervisor.ready_event.wait(2))
        self.client = SupervisorClient(
            authored_root=self.authored,
            state_root=self.state,
            runtime_root=self.runtime,
            launcher=lambda *_: self.fail("running supervisor must be reused"),
        )

    def tearDown(self) -> None:
        self.supervisor.stop()
        self.thread.join(2)
        for patch in reversed(self.patches):
            patch.stop()
        self.temporary.cleanup()

    def _admit(self, channel: PiLeaseChannel) -> dict:
        send_document(
            channel.connection,
            {
                "schema": SESSION_USE_ADMIT_SCHEMA,
                "profile_id": "chat",
                "service_id": "fixture-chat",
                "pid": os.getpid(),
                "start_time": process_start_time(os.getpid()),
            },
        )
        return receive_document(channel.connection)

    def test_same_connected_stream_becomes_held_use_lease(self) -> None:
        channel = self.client.acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )
        accepted = self._admit(channel)

        self.assertEqual(
            accepted["schema"],
            SESSION_USE_ACCEPTED_SCHEMA,
            accepted,
        )
        self.assertEqual(accepted["use_lease_id"], "use-1")
        self.assertEqual(accepted["session_pid"], os.getpid())
        self.assertEqual(
            accepted["supervisor_pid"],
            self.supervisor._supervisor_pid,
        )
        self.assertEqual(len(self.controller.deployments.transfers), 1)
        self.assertEqual(self.controller.releases, [])

        channel.close()
        deadline = time.monotonic() + 2
        while not self.controller.releases and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(
            self.controller.releases,
            [("fixture-chat", "use-1", False)],
        )

    def test_two_clients_are_serialized_and_each_owns_one_channel(self) -> None:
        channels = []
        barrier = threading.Barrier(3)

        def acquire() -> None:
            barrier.wait()
            channel = self.client.acquire_pi(
                profile_id="chat",
                host_name=None,
                stop_on_release=False,
            )
            channels.append(channel)

        threads = [threading.Thread(target=acquire) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(2)
        self.assertEqual(len(channels), 2)
        self.assertEqual(self.controller.acquisitions, 2)
        self.assertEqual(self.controller.maximum_mutations, 1)

        for channel in channels:
            self._admit(channel)
        for channel in channels:
            channel.close()

    def test_up_serializes_a_real_service_endpoint(self) -> None:
        published_at = datetime.datetime(
            2026,
            7,
            28,
            12,
            0,
            tzinfo=datetime.timezone.utc,
        )
        workload = ServiceWorkload(
            repository="fixture/model",
            revision="c" * 40,
            provider="runpod-vllm",
            model_id="fixture-chat",
            context_tokens=32768,
            max_output_tokens=4096,
            weight_format="native",
            kv_cache_dtype="bf16",
            runtime_compatibility="fixture-runtime",
            reasoning=False,
        )
        endpoint = ServiceEndpoint(
            publication_id="d" * 32,
            binding=ServiceEndpointBinding(
                service_id="fixture-chat",
                service_sha256="e" * 64,
                workload=workload,
                workload_sha256="f" * 64,
                input_modalities=("image", "text"),
            ),
            socket_path=self.runtime / "services" / "fixture-chat.sock",
            socket_device=31,
            socket_inode=47,
            published_at=published_at,
            admission_expires_at=published_at + datetime.timedelta(seconds=120),
            receipt_path=self.runtime / "services" / "fixture-chat.json",
        )
        deployment = SimpleNamespace(
            normalized=lambda: {
                "service_id": "fixture-chat",
                "host_name": "host-one",
                "idle_deadline": "2026-07-28T12:30:00Z",
            }
        )
        self.controller.ensure_ready = lambda *_args, **_kwargs: (
            deployment,
            endpoint,
        )
        self.controller.down = lambda *_args, **_kwargs: deployment

        result = self.client.request(
            "up",
            {
                "service_id": "fixture-chat",
                "host_name": None,
            },
        )

        self.assertEqual(result["deployment"], deployment.normalized())
        self.assertEqual(
            result["endpoint"],
            {
                "publication_id": "d" * 32,
                "binding": {
                    "service_id": "fixture-chat",
                    "service_sha256": "e" * 64,
                    "workload": workload.as_dict(),
                    "workload_sha256": "f" * 64,
                    "input_modalities": ["image", "text"],
                },
                "socket_path": str(self.runtime / "services" / "fixture-chat.sock"),
                "socket_device": 31,
                "socket_inode": 47,
                "published_at": "2026-07-28T12:00:00.000000Z",
                "admission_expires_at": "2026-07-28T12:02:00.000000Z",
                "receipt_path": str(self.runtime / "services" / "fixture-chat.json"),
            },
        )

    def test_hard_expiry_closes_active_pi_channel_before_claim_recovery(self):
        channel = self.client.acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )
        self._admit(channel)
        deployment = SimpleNamespace(
            service_id="fixture-chat",
            service_sha256="b" * 64,
            deployment_id="deployment-one",
            phase="ready",
            use_leases=(SimpleNamespace(lease_id="use-1"),),
        )
        recovered = []
        self.controller.deployments.list = lambda: (deployment,)
        self.supervisor.deployed_services.load = lambda *_: self.service
        self.controller.renew_deployment_claim = lambda _deployment: (
            _ for _ in ()
        ).throw(
            ModelLabError(
                "controlled provider hard expiry",
                code="host_claim_expired",
            )
        )
        self.controller.is_claim_gone = (
            lambda error: error.code == "host_claim_expired"
        )
        self.controller.reconcile_claim_gone = (
            lambda service, current: recovered.append(
                (service.service_id, current.deployment_id)
            )
        )
        self.controller.hosts = SimpleNamespace(
            enforce_retirement=lambda *, execute: None
        )

        self.supervisor.maintain_once()

        channel.connection.settimeout(1)
        self.assertEqual(channel.connection.recv(1), b"")
        self.assertEqual(
            recovered,
            [("fixture-chat", "deployment-one")],
        )
        channel.close()

    def test_kernel_sender_credentials_reject_a_forged_session_pid(self) -> None:
        channel = self.client.acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )
        send_document(
            channel.connection,
            {
                "schema": SESSION_USE_ADMIT_SCHEMA,
                "profile_id": "chat",
                "service_id": "fixture-chat",
                "pid": os.getpid() + 100000,
                "start_time": "1",
            },
        )

        response = receive_document(channel.connection)

        self.assertEqual(response["schema"], SUPERVISOR_ERROR_SCHEMA)
        self.assertEqual(
            response["code"],
            "session_use_admission_mismatch",
        )
        channel.close()

    def test_singleton_remains_held_until_mutating_worker_finishes(self) -> None:
        release_entered = threading.Event()
        allow_release = threading.Event()

        def blocking_release(service, use, *, now):
            release_entered.set()
            allow_release.wait()
            self.controller.releases.append(
                (service.service_id, use.lease.lease_id, now)
            )

        self.controller.release_profile_use = blocking_release
        channel = self.client.acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )
        self._admit(channel)
        channel.close()
        self.assertTrue(release_entered.wait(2))
        self.supervisor.stop()

        replacement = ModelLabSupervisor(
            controller=FakeController(),
            authored_root=self.authored,
            state_root=self.state,
            runtime_root=self.runtime,
            maintenance_interval_seconds=3600,
        )
        try:
            with self.assertRaises(ModelLabError) as caught:
                replacement._acquire_singleton()
            self.assertEqual(
                caught.exception.code,
                "supervisor_already_running",
            )
        finally:
            allow_release.set()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())

        replacement._acquire_singleton()
        replacement._close_runtime()


class ExpiredClaimPiRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temporary.name)
        self.authored = root / "authored"
        self.state = root / "state"
        self.runtime_root = root / "runtime"
        (self.authored / "profiles" / "chat").mkdir(
            mode=0o700,
            parents=True,
        )
        self.socket_path = root / "inference.sock"
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(self.socket_path))
        self.hosts = QuarantinedHosts()
        self.service_runtime = FakeRuntime(self.socket_path)
        self.controller = ModelLabController(
            hosts=self.hosts,
            runtime=self.service_runtime,
            deployments=DeploymentStore(self.state),
            bindings=ProfileBindingStore(self.authored),
            lab=parse_lab_toml(lab_toml()),
        )
        self.service = parse_service_toml(service_toml())
        self.supervisor = ModelLabSupervisor(
            controller=self.controller,
            authored_root=self.authored,
            state_root=self.state,
            runtime_root=self.runtime_root,
            maintenance_interval_seconds=3600,
        )
        self.patches = (
            mock.patch(
                "model_lab.supervisor.load_profile_route",
                return_value=FakeProfile(),
            ),
            mock.patch(
                "model_lab.supervisor.load_service_id",
                return_value=self.service,
            ),
        )
        for patch in self.patches:
            patch.start()
        self.thread = threading.Thread(
            target=self.supervisor.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.assertTrue(self.supervisor.ready_event.wait(2))
        self.client = SupervisorClient(
            authored_root=self.authored,
            state_root=self.state,
            runtime_root=self.runtime_root,
            launcher=lambda *_: self.fail("running supervisor must be reused"),
        )

    def tearDown(self) -> None:
        self.supervisor.stop()
        self.thread.join(2)
        for patch in reversed(self.patches):
            patch.stop()
        self.listener.close()
        self.temporary.cleanup()

    @staticmethod
    def _admit(channel: PiLeaseChannel) -> dict:
        send_document(
            channel.connection,
            {
                "schema": SESSION_USE_ADMIT_SCHEMA,
                "profile_id": "chat",
                "service_id": "fixture-chat",
                "pid": os.getpid(),
                "start_time": process_start_time(os.getpid()),
            },
        )
        return receive_document(channel.connection)

    def test_second_pi_closes_old_channel_before_replacing_expired_claim(self):
        first = self.client.acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )
        self._admit(first)
        first_deployment_id = first.pending.deployment_id
        self.hosts.active = False

        second = self.client.acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )

        first.connection.settimeout(1)
        self.assertEqual(first.connection.recv(1), b"")
        self.assertNotEqual(
            second.pending.deployment_id,
            first_deployment_id,
        )
        self.assertEqual(self.service_runtime.lost_claim_cleanups, 1)
        self.assertEqual(self.service_runtime.starts, 2)
        accepted = self._admit(second)
        self.assertEqual(
            accepted["deployment_id"],
            second.pending.deployment_id,
        )
        first.close()
        second.close()

    def test_second_pi_replaces_a_terminated_exact_host_operation(self):
        first = self.client.acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )
        self._admit(first)
        first_deployment_id = first.pending.deployment_id
        self.hosts.gone_code = "host_claim_host_changed"
        self.hosts.active = False

        second = self.client.acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )

        first.connection.settimeout(1)
        self.assertEqual(first.connection.recv(1), b"")
        self.assertNotEqual(
            second.pending.deployment_id,
            first_deployment_id,
        )
        self.assertEqual(self.service_runtime.lost_claim_cleanups, 1)
        self._admit(second)
        first.close()
        second.close()

    def test_sibling_expiry_drains_claim_before_second_pi_reacquires(self):
        first = self.client.acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )
        self._admit(first)
        first_deployment_id = first.pending.deployment_id
        self.hosts.quarantined = True

        second = self.client.acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )

        first.connection.settimeout(1)
        self.assertEqual(first.connection.recv(1), b"")
        self.assertNotEqual(
            second.pending.deployment_id,
            first_deployment_id,
        )
        self.assertEqual(self.service_runtime.stops, 1)
        self.assertEqual(self.service_runtime.lost_claim_cleanups, 0)
        self.assertEqual(self.hosts.releases, [(1, True)])
        self._admit(second)
        first.close()
        second.close()

    def test_second_pi_closes_old_channel_before_replacing_dead_runtime(self):
        first = self.client.acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )
        self._admit(first)
        first_deployment_id = first.pending.deployment_id
        replacement_runtime = NotReadyRuntime(self.socket_path)
        self.controller.runtime = replacement_runtime

        second = self.client.acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )

        first.connection.settimeout(1)
        self.assertEqual(first.connection.recv(1), b"")
        self.assertNotEqual(
            second.pending.deployment_id,
            first_deployment_id,
        )
        self.assertEqual(replacement_runtime.stops, 1)
        self.assertEqual(replacement_runtime.starts, 1)
        self._admit(second)
        first.close()
        second.close()

    def test_second_pi_rebinds_active_sessions_after_transport_replacement(self):
        first = self.client.acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )
        self._admit(first)
        first_deployment_id = first.pending.deployment_id
        original_attest = self.service_runtime.attest_ready
        replacement_reported = False

        def attest_after_transport_replacement(
            service,
            claim,
            deployment,
        ):
            nonlocal replacement_reported
            if not replacement_reported:
                replacement_reported = True
                raise ModelLabError(
                    "controlled transport replacement",
                    code="service_transport_replaced",
                )
            return original_attest(service, claim, deployment)

        self.service_runtime.attest_ready = (
            attest_after_transport_replacement
        )

        second = self.client.acquire_pi(
            profile_id="chat",
            host_name=None,
            stop_on_release=False,
        )

        first.connection.settimeout(1)
        self.assertEqual(first.connection.recv(1), b"")
        self.assertEqual(
            second.pending.deployment_id,
            first_deployment_id,
        )
        self.assertEqual(self.service_runtime.starts, 1)
        self.assertEqual(self.hosts.releases, [])
        self._admit(second)
        first.close()
        second.close()


class ModelSessionSubprocessTest(unittest.TestCase):
    def test_child_receives_unix_stream_fd_at_least_three(self) -> None:
        client, supervisor = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_STREAM,
        )
        channel = PiLeaseChannel(
            pending=PendingPiUse(
                profile_id="chat",
                service_id="fixture-chat",
                workload_sha256="a" * 64,
                deployment_id="deployment-one",
                use_lease_id="use-one",
            ),
            connection=client,
        )
        captured = {}

        def popen(arguments, *, close_fds, pass_fds):
            self.assertTrue(close_fds)
            self.assertEqual(len(pass_fds), 1)
            descriptor = pass_fds[0]
            self.assertGreaterEqual(descriptor, 3)
            metadata = os.fstat(descriptor)
            self.assertTrue(stat.S_ISSOCK(metadata.st_mode))
            captured["arguments"] = arguments
            captured["descriptor"] = descriptor
            return SimpleNamespace(wait=lambda: 17)

        try:
            with mock.patch(
                "model_lab.supervisor_client.subprocess.Popen",
                side_effect=popen,
            ):
                result = subprocess_model_session(
                    pathlib.Path("/mnt/dev/model-lab/profiles/chat"),
                    ["resume", "session-one"],
                    channel,
                )
            self.assertEqual(result, 17)
            self.assertEqual(
                captured["arguments"][1:5],
                [
                    "--model-lab-use-fd",
                    str(captured["descriptor"]),
                    "--profile",
                    "/mnt/dev/model-lab/profiles/chat",
                ],
            )
            with self.assertRaises(OSError):
                os.fstat(captured["descriptor"])
            supervisor.settimeout(1)
            self.assertEqual(supervisor.recv(1), b"")
        finally:
            supervisor.close()


if __name__ == "__main__":
    unittest.main()
