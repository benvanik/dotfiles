from __future__ import annotations

import pathlib
import socket
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from model_lab.errors import ModelLabError
from model_lab.production_backend import ProductionModelServiceBackend
from model_lab.runpod_backend import HostClaim
from model_lab.service_runtime import PreparedService, TransportBinding
from runpod_local.errors import RunpodLocalError
from runpod_local.remote import SshEndpoint


class Tunnel:
    def __init__(self) -> None:
        self.terminated = 0
        self.waited = 0
        self.killed = 0

    def poll(self):
        return None

    def terminate(self):
        self.terminated += 1

    def wait(self, timeout=None):
        del timeout
        self.waited += 1
        return 0

    def kill(self):
        self.killed += 1


class Proxy:
    def __init__(self, *, bind_error=None, **_):
        self.bind_error = bind_error
        self.closed = 0

    def bind(self):
        if self.bind_error is not None:
            raise self.bind_error

    def serve(self):
        pass

    def close(self):
        self.closed += 1


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


if __name__ == "__main__":
    unittest.main()
