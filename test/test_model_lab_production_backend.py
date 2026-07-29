from __future__ import annotations

import dataclasses
import datetime
import pathlib
import socket
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from model_lab.errors import ModelLabError
from model_lab.production_backend import (
    ProductionModelServiceBackend,
    RunpodHostControlAdapter,
    ServiceEndpointPublisher,
)
from model_lab.runpod_backend import HostClaim
from model_lab.service_runtime import PreparedService, TransportBinding
from runpod_local.errors import RunpodLocalError
from runpod_local.remote import SshEndpoint


class Tunnel:
    def __init__(self) -> None:
        self.terminated = 0
        self.waited = 0
        self.wait_timeouts = []
        self.killed = 0

    def poll(self):
        return None

    def terminate(self):
        self.terminated += 1

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        self.waited += 1
        return 0

    def kill(self):
        self.killed += 1


class Proxy:
    def __init__(self, *, bind_error=None, **_):
        self.bind_error = bind_error
        self.closed = 0
        self.close_timeouts = []

    def bind(self):
        if self.bind_error is not None:
            raise self.bind_error

    def serve(self):
        pass

    def close(self, *, timeout_seconds=None):
        self.closed += 1
        self.close_timeouts.append(timeout_seconds)
        return True


class MonotonicClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def wait(self, seconds: float) -> None:
        self.value += seconds


class RunpodHostControlAdapterReadinessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.claim = HostClaim(
            host_name="host-one",
            claim_id="claim-one",
            generation=1,
            operation_id="operation-one",
            provider_resource_id="pod-one",
            profile_name="gpu-one",
            remote_root="/root/runpod-session/claims/claim-one",
            endpoints={"openai": 18000},
            hard_expires_at="2099-01-01T00:00:00Z",
        )
        self.endpoint = SshEndpoint(
            instance_name="host-one",
            operation_id="operation-one",
            pod_id="pod-one",
            host="203.0.113.7",
            port=22022,
            user="root",
            identity_file=self.root / "identity",
            known_hosts_file=self.root / "known-hosts",
            host_key_alias="runpod-pod-one",
        )
        self.control = mock.Mock()
        self.control.get.return_value = self.claim
        self.clock = MonotonicClock()
        self.probes = []
        self.adapter = RunpodHostControlAdapter(
            self.control,
            runpod_state=mock.sentinel.runpod_state,
            api=mock.sentinel.api,
            instances=mock.sentinel.instances,
            monotonic=self.clock,
            poll_waiter=self.clock.wait,
            ssh_readiness_seconds=80.0,
            ssh_probe=lambda endpoint: self.probes.append(endpoint) or True,
        )

    def test_acquire_threads_cleanup_deadline_authority_to_host_control(self):
        cleanup_deadline_factory = mock.Mock(return_value=70.0)
        self.control.acquire.return_value = self.claim

        acquired = self.adapter.acquire(
            mock.sentinel.request,
            startup_deadline=42.0,
            cleanup_deadline_factory=cleanup_deadline_factory,
        )

        self.assertIs(acquired, self.claim)
        self.control.acquire.assert_called_once_with(
            mock.sentinel.request,
            startup_deadline=42.0,
            cleanup_deadline_factory=cleanup_deadline_factory,
        )
        cleanup_deadline_factory.assert_not_called()

    def test_wait_reconciles_provider_until_exact_ssh_probe_succeeds(self):
        with mock.patch(
            "model_lab.production_backend.resolve_endpoint",
            side_effect=[
                RunpodLocalError("mapping pending", code="pod_not_ready"),
                self.endpoint,
            ],
        ) as resolve:
            ready = self.adapter.wait_ready(
                self.claim,
                renewal_ttl_seconds=120,
            )

        self.assertIs(ready, self.claim)
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(self.clock.value, 1.0)
        self.assertEqual(self.probes, [self.endpoint])
        self.assertEqual(self.control.renew.call_count, 0)

    def test_wait_renews_unpersisted_claim_before_readiness(self):
        renewed = dataclasses.replace(self.claim, generation=2)
        self.control.renew.return_value = renewed
        self.control.get.side_effect = [
            self.claim,
            self.claim,
            self.claim,
            renewed,
            renewed,
        ]
        attempts = 0

        def resolve(*_args, **_keywords):
            nonlocal attempts
            attempts += 1
            if attempts < 4:
                raise RunpodLocalError(
                    "mapping pending",
                    code="pod_not_ready",
                )
            return self.endpoint

        with mock.patch(
            "model_lab.production_backend.resolve_endpoint",
            side_effect=resolve,
        ):
            ready = self.adapter.wait_ready(
                self.claim,
                renewal_ttl_seconds=6,
            )

        self.assertEqual(ready.generation, 2)
        self.control.renew.assert_called_once_with(
            "host-one",
            "claim-one",
            1,
            6,
            startup_deadline=80.0,
            cancel_event=None,
        )

    def test_slow_successful_probe_renews_before_returning_ready_claim(self):
        renewed = dataclasses.replace(self.claim, generation=2)
        self.control.renew.return_value = renewed

        def slow_probe(_endpoint):
            self.clock.value += 75.0
            return True

        self.adapter.ssh_probe = slow_probe
        with mock.patch(
            "model_lab.production_backend.resolve_endpoint",
            return_value=self.endpoint,
        ):
            ready = self.adapter.wait_ready(
                self.claim,
                renewal_ttl_seconds=120,
                startup_deadline=80.0,
            )

        self.assertEqual(ready, renewed)
        self.control.renew.assert_called_once_with(
            "host-one",
            "claim-one",
            1,
            120,
            startup_deadline=80.0,
            cancel_event=None,
        )

    def test_release_normalizes_generic_claim_controller_result(self):
        self.control.release.return_value = SimpleNamespace(
            host_name="host-one",
            claim_id="claim-one",
            released_generation=3,
            remaining_claim_count=0,
            retention="while-claimed",
            empty_since="2026-07-29T12:00:00Z",
            retire_at="2026-07-29T12:05:00Z",
            retirement_due=False,
        )

        released = self.adapter.release(
            "host-one",
            "claim-one",
            3,
            now=False,
            cleanup_deadline=42.0,
        )

        self.assertTrue(released.released)
        self.assertTrue(released.final_claim)
        self.assertEqual(released.retirement, "while-claimed")
        self.assertEqual(
            released.empty_deadline,
            "2026-07-29T12:05:00Z",
        )
        self.control.release.assert_called_once_with(
            "host-one",
            "claim-one",
            3,
            now=False,
            cleanup_deadline=42.0,
        )

    def test_wait_fails_fast_if_host_operation_is_replaced(self):
        self.control.get.return_value = dataclasses.replace(
            self.claim,
            operation_id="operation-two",
            provider_resource_id="pod-two",
        )

        with self.assertRaises(ModelLabError) as caught:
            self.adapter.wait_ready(
                self.claim,
                renewal_ttl_seconds=120,
            )

        self.assertEqual(
            caught.exception.code,
            "service_host_claim_mismatch",
        )
        self.assertEqual(self.probes, [])

    def test_wait_is_bounded_and_never_probes_an_unmapped_pod(self):
        with (
            mock.patch(
                "model_lab.production_backend.resolve_endpoint",
                side_effect=RunpodLocalError(
                    "mapping pending",
                    code="pod_not_ready",
                ),
            ) as resolve,
            self.assertRaises(ModelLabError) as caught,
        ):
            self.adapter.wait_ready(
                self.claim,
                renewal_ttl_seconds=120,
            )

        self.assertEqual(
            caught.exception.code,
            "service_host_ssh_not_ready",
        )
        self.assertEqual(self.clock.value, 5.0)
        self.assertEqual(resolve.call_count, 5)
        self.assertEqual(self.probes, [])

    def test_default_probe_executes_true_against_exact_operation(self):
        adapter = RunpodHostControlAdapter(
            self.control,
            runpod_state=mock.sentinel.runpod_state,
            api=mock.sentinel.api,
            instances=mock.sentinel.instances,
            ssh_readiness_seconds=80.0,
        )
        with (
            mock.patch("model_lab.production_backend.ensure_known_hosts_file"),
            mock.patch(
                "model_lab.production_backend.run_with_activity",
                return_value=0,
            ) as run,
        ):
            self.assertTrue(
                adapter._probe_ssh(
                    self.endpoint,
                    startup_deadline=80.0,
                )
            )

        arguments, keywords = run.call_args
        self.assertEqual(
            arguments[0][-1],
            "exec /usr/bin/true",
        )
        self.assertEqual(
            keywords["expected_operation_id"],
            "operation-one",
        )
        self.assertEqual(keywords["expected_pod_id"], "pod-one")
        self.assertEqual(keywords["deadline"], 80.0)
        self.assertIs(keywords["monotonic"], adapter.monotonic)


class ProductionBackendTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.runtime_root = self.root / "runtime"
        self.prepared = PreparedService(
            service_id="chat",
            deployment_id="deployment-one",
            host_name="host-one",
            claim_id="claim-one",
            handle="a" * 64,
        )
        self.claim = HostClaim(
            host_name="host-one",
            claim_id="claim-one",
            generation=1,
            operation_id="operation-one",
            provider_resource_id="pod-one",
            profile_name="gpu-one",
            remote_root="/root/runpod-session/claims/claim-one",
            endpoints={"openai": 18000},
            hard_expires_at="2099-01-01T00:00:00Z",
        )
        self.endpoint = SshEndpoint(
            instance_name="host-one",
            operation_id="operation-one",
            pod_id="pod-one",
            host="203.0.113.7",
            port=22022,
            user="root",
            identity_file=self.root / "identity",
            known_hosts_file=self.root / "known-hosts",
            host_key_alias="runpod-pod-one",
        )
        self.instances = mock.Mock()
        self.tunnel = Tunnel()
        self.backend = ProductionModelServiceBackend(
            source_root=self.root,
            state_root=self.root / "state",
            runtime_root=self.runtime_root,
            runpod_state=mock.sentinel.runpod_state,
            api=mock.sentinel.api,
            hosts=mock.sentinel.hosts,
            instances=self.instances,
            installations=mock.sentinel.installations,
            popen_factory=lambda *_, **__: self.tunnel,
        )
        self.backend._context = mock.Mock(
            return_value=(
                self.claim,
                self.endpoint,
                SimpleNamespace(installation_sha256="b" * 64),
            )
        )
        self.backend._socket_accepting = mock.Mock(return_value=True)
        self.removed = []
        self.backend._remove_stale_socket = mock.Mock(
            side_effect=lambda path: self.removed.append(path)
        )

    def test_remote_startup_expiration_uses_injected_wall_and_monotonic_clocks(
        self,
    ) -> None:
        wall = datetime.datetime(
            2026,
            7,
            29,
            12,
            0,
            tzinfo=datetime.timezone.utc,
        )
        self.backend.clock = lambda: wall
        self.backend.monotonic = lambda: 100.0

        expiration = self.backend._remote_startup_expiration(125.0)

        self.assertEqual(expiration, "2026-07-29T12:00:25Z")

    def test_activity_failure_reaps_tunnel_and_both_socket_paths(self) -> None:
        self.instances.touch.side_effect = RunpodLocalError(
            "lease changed",
            code="instance_identity_changed",
        )

        with (
            mock.patch(
                "model_lab.production_backend.prepare_local_tunnel_socket"
            ),
            mock.patch(
                "model_lab.production_backend.ensure_known_hosts_file"
            ),
            self.assertRaises(ModelLabError) as caught,
        ):
            self.backend.open_transport(self.prepared, completed=lambda: None)

        self.assertEqual(caught.exception.code, "instance_identity_changed")
        self.assertEqual(self.tunnel.terminated, 1)
        self.assertEqual(self.tunnel.waited, 1)
        self.assertEqual(
            self.removed,
            [
                self.runtime_root / "services" / "chat.sock",
                self.runtime_root / "transports" / "deployment-one.sock",
            ],
        )

    def test_proxy_bind_failure_reaps_tunnel_and_closes_proxy(self) -> None:
        created = Proxy(bind_error=OSError("bind failed"))
        with (
            mock.patch(
                "model_lab.production_backend.prepare_local_tunnel_socket"
            ),
            mock.patch(
                "model_lab.production_backend.ensure_known_hosts_file"
            ),
            mock.patch(
                "model_lab.production_backend.MeteredUnixProxy",
                return_value=created,
            ),
            self.assertRaises(ModelLabError) as caught,
        ):
            self.backend.open_transport(self.prepared, completed=lambda: None)

        self.assertEqual(caught.exception.code, "model_lab_backend_error")
        self.assertEqual(created.closed, 1)
        self.assertEqual(self.tunnel.terminated, 1)
        self.assertEqual(self.tunnel.waited, 1)
        self.assertEqual(len(self.removed), 2)

    def test_transport_probe_and_poll_use_only_remaining_startup_budget(
        self,
    ) -> None:
        clock = MonotonicClock()
        self.backend.monotonic = clock
        self.backend.poll_waiter = mock.Mock(side_effect=clock.wait)
        self.backend._socket_accepting = mock.Mock(return_value=False)

        with (
            mock.patch("model_lab.production_backend.prepare_local_tunnel_socket"),
            mock.patch("model_lab.production_backend.ensure_known_hosts_file"),
            self.assertRaises(ModelLabError) as caught,
        ):
            self.backend.open_transport(
                self.prepared,
                completed=lambda: None,
                startup_deadline=0.03,
            )

        self.assertEqual(caught.exception.code, "service_startup_timeout")
        self.backend._socket_accepting.assert_called_once_with(
            self.runtime_root / "transports" / "deployment-one.sock",
            timeout_seconds=0.03,
        )
        self.backend.poll_waiter.assert_called_once_with(0.03)
        self.assertEqual(clock.value, 0.03)

    def test_expired_startup_rollback_never_performs_unbounded_joins(
        self,
    ) -> None:
        clock = MonotonicClock()
        proxy = Proxy()

        class DeadlineThread:
            def __init__(self, **_kwargs):
                self.join_timeouts = []
                self.alive = True

            def start(self):
                clock.value = 1.0

            def join(self, timeout=None):
                self.join_timeouts.append(timeout)
                self.alive = False

            def is_alive(self):
                return self.alive

        class DeadlineTunnel(Tunnel):
            def wait(self, timeout=None):
                self.wait_timeouts.append(timeout)
                self.waited += 1
                if not self.killed:
                    raise subprocess.TimeoutExpired("ssh", timeout)
                return 0

        tunnel = DeadlineTunnel()
        self.backend.monotonic = clock
        self.backend.popen_factory = lambda *_, **__: tunnel
        self.backend._socket_accepting = mock.Mock(return_value=True)
        thread = DeadlineThread()

        with (
            mock.patch("model_lab.production_backend.prepare_local_tunnel_socket"),
            mock.patch("model_lab.production_backend.ensure_known_hosts_file"),
            mock.patch(
                "model_lab.production_backend.MeteredUnixProxy",
                return_value=proxy,
            ),
            mock.patch(
                "model_lab.production_backend.threading.Thread",
                return_value=thread,
            ),
            self.assertRaises(ModelLabError) as caught,
        ):
            self.backend.open_transport(
                self.prepared,
                completed=lambda: None,
                startup_deadline=1.0,
            )

        self.assertEqual(caught.exception.code, "service_startup_timeout")
        self.assertEqual(proxy.close_timeouts, [0.0])
        self.assertEqual(thread.join_timeouts, [0.0])
        self.assertEqual(tunnel.wait_timeouts, [0.0, 0])
        self.assertEqual(tunnel.terminated, 1)
        self.assertEqual(tunnel.killed, 1)

    def test_close_transport_spends_only_one_absolute_cleanup_budget(
        self,
    ) -> None:
        clock = MonotonicClock()

        class DeadlineProxy(Proxy):
            def close(self, *, timeout_seconds=None):
                super().close(timeout_seconds=timeout_seconds)
                clock.wait(timeout_seconds)
                return False

        class DeadlineThread:
            def __init__(self):
                self.join_timeouts = []

            def join(self, timeout=None):
                self.join_timeouts.append(timeout)

            def is_alive(self):
                return True

        class DeadlineTunnel(Tunnel):
            def wait(self, timeout=None):
                self.wait_timeouts.append(timeout)
                self.waited += 1
                if not self.killed:
                    raise subprocess.TimeoutExpired("ssh", timeout)
                return 0

        proxy = DeadlineProxy()
        thread = DeadlineThread()
        tunnel = DeadlineTunnel()
        binding = TransportBinding(
            socket_path=str(
                self.runtime_root / "services" / "chat.sock"
            ),
            handle="transport-one",
        )
        self.backend.monotonic = clock
        self.backend._transports[self.prepared.deployment_id] = (
            SimpleNamespace(
                binding=binding,
                prepared=self.prepared,
                upstream_path=(
                    self.runtime_root
                    / "transports"
                    / "deployment-one.sock"
                ),
                public_path=(
                    self.runtime_root / "services" / "chat.sock"
                ),
                tunnel=tunnel,
                proxy=proxy,
                proxy_thread=thread,
            )
        )

        with self.assertRaises(ModelLabError) as caught:
            self.backend.close_transport(
                self.prepared,
                binding,
                startup_deadline=10.0,
            )

        self.assertEqual(
            caught.exception.code,
            "service_transport_cleanup_failed",
        )
        self.assertEqual(proxy.close_timeouts, [10.0])
        self.assertEqual(thread.join_timeouts, [0.0])
        self.assertEqual(tunnel.wait_timeouts, [0.0, 0])
        self.assertEqual(tunnel.terminated, 1)
        self.assertEqual(tunnel.killed, 1)
        self.assertIn(
            self.prepared.deployment_id,
            self.backend._transports,
        )

    def test_ordinary_tunnel_cleanup_retains_five_second_grace(self) -> None:
        class GraceTunnel(Tunnel):
            def wait(self, timeout=None):
                self.wait_timeouts.append(timeout)
                self.waited += 1
                if not self.killed:
                    raise subprocess.TimeoutExpired("ssh", timeout)
                return 0

        tunnel = GraceTunnel()

        self.backend._reap_tunnel(tunnel)

        self.assertEqual(tunnel.wait_timeouts, [5.0, 0])
        self.assertEqual(tunnel.terminated, 1)
        self.assertEqual(tunnel.killed, 1)

    def test_exited_ssh_tunnel_is_not_a_live_transport(self) -> None:
        binding = TransportBinding(
            socket_path=str(
                self.runtime_root / "services" / "chat.sock"
            ),
            handle="transport-one",
        )
        self.backend._transports[self.prepared.deployment_id] = (
            SimpleNamespace(
                binding=binding,
                prepared=self.prepared,
                upstream_path=(
                    self.runtime_root
                    / "transports"
                    / "deployment-one.sock"
                ),
                public_path=(
                    self.runtime_root / "services" / "chat.sock"
                ),
                tunnel=SimpleNamespace(poll=lambda: 17),
                proxy_thread=SimpleNamespace(is_alive=lambda: True),
            )
        )

        self.assertFalse(
            self.backend.transport_is_live(self.prepared, binding)
        )

    def test_replaced_socket_inode_invalidates_transport_identity(self) -> None:
        for replaced_name in ("upstream", "public"):
            with self.subTest(replaced_name=replaced_name):
                socket_root = self.root / replaced_name
                socket_root.mkdir()
                upstream_path = socket_root / "upstream.sock"
                public_path = socket_root / "public.sock"
                upstream = socket.socket(socket.AF_UNIX)
                public = socket.socket(socket.AF_UNIX)
                replacement = socket.socket(socket.AF_UNIX)
                try:
                    upstream.bind(str(upstream_path))
                    public.bind(str(public_path))
                    upstream_metadata = upstream_path.lstat()
                    public_metadata = public_path.lstat()
                    binding = TransportBinding(
                        socket_path=str(public_path),
                        handle="transport-one",
                    )
                    self.backend._transports[
                        self.prepared.deployment_id
                    ] = SimpleNamespace(
                        binding=binding,
                        prepared=self.prepared,
                        upstream_path=upstream_path,
                        upstream_socket_device=upstream_metadata.st_dev,
                        upstream_socket_inode=upstream_metadata.st_ino,
                        public_path=public_path,
                        public_socket_device=public_metadata.st_dev,
                        public_socket_inode=public_metadata.st_ino,
                        tunnel=SimpleNamespace(poll=lambda: None),
                        proxy_thread=SimpleNamespace(
                            is_alive=lambda: True
                        ),
                    )
                    self.assertTrue(
                        self.backend.transport_is_live(
                            self.prepared,
                            binding,
                        )
                    )
                    target_path = (
                        upstream_path
                        if replaced_name == "upstream"
                        else public_path
                    )
                    target_path.unlink()
                    replacement.bind(str(target_path))

                    self.assertFalse(
                        self.backend.transport_is_live(
                            self.prepared,
                            binding,
                        )
                    )
                finally:
                    upstream.close()
                    public.close()
                    replacement.close()


class ProductionBackendRetainedDeploymentTest(unittest.TestCase):
    def test_load_attests_runtime_image_with_shared_startup_deadline(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        claim = HostClaim(
            host_name="host-one",
            claim_id="claim-one",
            generation=1,
            operation_id="operation-one",
            provider_resource_id="pod-one",
            profile_name="gpu-one",
            remote_root="/root/runpod-session/claims/claim-one",
            endpoints={"openai": 18000},
            hard_expires_at="2099-01-01T00:00:00Z",
        )
        endpoint = SshEndpoint(
            instance_name="host-one",
            operation_id="operation-one",
            pod_id="pod-one",
            host="203.0.113.7",
            port=22022,
            user="root",
            identity_file=root / "identity",
            known_hosts_file=root / "known-hosts",
            host_key_alias="runpod-pod-one",
        )
        installations = mock.Mock()
        installations.load.return_value = SimpleNamespace(
            request=SimpleNamespace(
                service_plan_sha256="plan-one",
                remote_port=18000,
            ),
            materialization=SimpleNamespace(
                materialization_sha256="a" * 64,
            ),
        )
        backend = ProductionModelServiceBackend(
            source_root=root,
            state_root=root / "state",
            runtime_root=root / "runtime",
            runpod_state=mock.sentinel.runpod_state,
            api=mock.sentinel.api,
            hosts=mock.sentinel.hosts,
            instances=mock.sentinel.instances,
            installations=installations,
            monotonic=lambda: 10.0,
        )
        backend._endpoint_for_claim = mock.Mock(return_value=endpoint)
        backend._attest_runtime_image = mock.Mock()
        service = SimpleNamespace(
            service_id="chat",
            plan_sha256="plan-one",
            runtime_id="runtime-one",
        )
        deployment = SimpleNamespace(
            deployment_id="deployment-one",
            host_name="host-one",
            claim_id="claim-one",
        )

        with (
            mock.patch(
                "model_lab.production_backend.require_current_instance"
            ),
            mock.patch(
                "model_lab.production_backend.load_runtime",
                return_value=SimpleNamespace(image="image-one"),
            ),
        ):
            prepared = backend.load(
                service,
                claim,
                deployment,
                startup_deadline=42.0,
            )

        self.assertEqual(prepared.handle, "a" * 64)
        backend._attest_runtime_image.assert_called_once_with(
            endpoint,
            "image-one",
            startup_deadline=42.0,
        )


class ServiceEndpointPublisherTest(unittest.TestCase):
    def test_load_retains_an_unexpired_exact_socket_publication(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        socket_path = root / "endpoint.sock"
        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        listener.bind(str(socket_path))
        metadata = socket_path.stat()
        now = datetime.datetime(
            2026,
            7,
            29,
            12,
            0,
            tzinfo=datetime.timezone.utc,
        )
        endpoint = SimpleNamespace(
            admission_expires_at=now + datetime.timedelta(minutes=1),
            socket_path=socket_path,
            socket_device=metadata.st_dev,
            socket_inode=metadata.st_ino,
        )
        publisher = ServiceEndpointPublisher(root, clock=lambda: now)
        publisher.inspect = mock.Mock(return_value=endpoint)

        loaded = publisher.load(mock.sentinel.service)

        self.assertIs(loaded, endpoint)


if __name__ == "__main__":
    unittest.main()
