from __future__ import annotations

import datetime
import os
import pathlib
import shlex
import subprocess
import tempfile
import unittest
from unittest import mock

from runpod_local.cli import build_parser, parse_arguments
from runpod_local.errors import RunpodLocalError
from runpod_local.instances import InstanceStore, json_document_hash
from runpod_local.remote import (
    SshEndpoint,
    build_copy_argv,
    build_ssh_argv,
    build_tunnel_argv,
    ensure_known_hosts_file,
    resolve_endpoint,
    run_with_activity,
    sanitized_subprocess_environment,
    validate_remote_copy_path,
)
from runpod_local.state import StateStore
from runpod_local.timeutil import utc_timestamp


GPU_ID = "NVIDIA RTX PRO 6000 Blackwell Server Edition"
SSH_PUBLIC_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f "
    "fixture@example"
)


def active_record(identity_file: pathlib.Path):
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "name": "rp-compiler-123456781234",
        "cloudType": "SECURE",
        "computeType": "GPU",
        "gpuTypeIds": [GPU_ID],
        "gpuTypePriority": "custom",
        "gpuCount": 1,
        "containerDiskInGb": 50,
        "volumeMountPath": "/workspace",
        "ports": ["22/tcp"],
        "env": {"SSH_PUBLIC_KEY": SSH_PUBLIC_KEY},
        "interruptible": False,
        "locked": False,
        "minVCPUPerGPU": 8,
        "minRAMPerGPU": 32,
        "imageName": "runpod/pytorch:fixture",
        "networkVolumeId": "volume123",
    }
    return {
        "schema_version": "runpod.instance.v1",
        "name": "compiler",
        "operation_id": "12345678-1234-4234-8234-123456789abc",
        "remote_name": payload["name"],
        "phase": "active",
        "created_at": utc_timestamp(now - datetime.timedelta(minutes=2)),
        "updated_at": utc_timestamp(now - datetime.timedelta(minutes=1)),
        "intent_expires_at": utc_timestamp(
            now + datetime.timedelta(minutes=13)
        ),
        "submission_started_at": utc_timestamp(
            now - datetime.timedelta(minutes=1)
        ),
        "profile": {"name": "pro-dev", "sha256": "a" * 64},
        "expected": {
            "gpu_id": GPU_ID,
            "gpu_count": 1,
            "network_volume_id": "volume123",
            "data_center_id": "US-NC-2",
            "max_hourly_usd": 3.0,
        },
        "quoted_total_price_per_hour": 1.99,
        "pod_payload": payload,
        "pod_payload_sha256": json_document_hash(payload),
        "connection": {
            "user": "root",
            "identity_file": str(identity_file),
            "internal_ssh_port": 22,
        },
        "lease_request": {
            "ttl_seconds": 3600,
            "idle_timeout_seconds": 900,
        },
        "lease": {
            "activated_at": utc_timestamp(
                now - datetime.timedelta(minutes=1)
            ),
            "expires_at": utc_timestamp(now + datetime.timedelta(minutes=59)),
            "ttl_seconds": 3600,
            "idle_timeout_seconds": 900,
            "last_activity_at": utc_timestamp(now),
            "activity_source": "explicit_heartbeat",
            "expiry_action": "terminate",
        },
        "pod_id": "pod123",
        "provider": None,
        "model": None,
        "events": [],
        "history": [],
    }


def live_pod(**overrides):
    value = {
        "id": "pod123",
        "name": "rp-compiler-123456781234",
        "desired_status": "RUNNING",
        "image": "runpod/pytorch:fixture",
        "template_id": None,
        "interruptible": False,
        "locked": False,
        "gpu_id": GPU_ID,
        "gpu_count": 1,
        "cost_per_hour": 1.99,
        "data_center_id": "US-NC-2",
        "secure_cloud": True,
        "machine_id": "machine123",
        "network_volume_id": "volume123",
        "network_volume": {
            "id": "volume123",
            "name": "model-cache",
            "size_gb": 500,
            "data_center_id": "US-NC-2",
        },
        "public_ip": "100.65.0.119",
        "port_mappings": {"22": 22022},
        "ports": ["22/tcp"],
    }
    value.update(overrides)
    return value


class FakeApi:
    def __init__(self, pod=None):
        self.pod = pod or live_pod()

    def get_pod(self, pod_id):
        if pod_id != self.pod["id"]:
            raise AssertionError("unexpected Pod ID")
        return dict(self.pod)


class RemoteBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.identity = self.root / "id_ed25519"
        self.identity.write_text("fixture private identity\n")
        self.identity.chmod(0o600)
        self.state = StateStore(self.root / "state")
        InstanceStore(self.state).save(active_record(self.identity))
        self.key_pair_patch = mock.patch(
            "runpod_local.remote.validate_ssh_key_pair"
        )
        self.key_pair_validator = self.key_pair_patch.start()
        self.addCleanup(self.key_pair_patch.stop)

    def endpoint(self, pod=None):
        return resolve_endpoint(
            "compiler",
            instances=InstanceStore(self.state),
            api=FakeApi(pod),
            state=self.state,
        )

    def test_endpoint_requires_exact_live_identity_and_accepts_cgnat_ipv4(self):
        endpoint = self.endpoint()

        self.key_pair_validator.assert_called_with(
            str(self.identity), SSH_PUBLIC_KEY
        )
        self.assertEqual(endpoint.host, "100.65.0.119")
        self.assertEqual(endpoint.port, 22022)
        self.assertEqual(endpoint.host_key_alias, "runpod-pod123")
        self.assertEqual(
            endpoint.known_hosts_file,
            self.state.root / "ssh" / "known-hosts" / "pod123",
        )

        for field, value in (
            ("name", "other-name"),
            ("gpu_id", "NVIDIA H200"),
            ("gpu_count", 2),
            ("data_center_id", "OTHER-DC"),
            ("network_volume_id", "other-volume"),
            ("cost_per_hour", 3.01),
        ):
            with self.subTest(field=field):
                with self.assertRaises(RunpodLocalError):
                    self.endpoint(live_pod(**{field: value}))

    def test_endpoint_requires_the_injected_public_key_snapshot(self):
        record = InstanceStore(self.state).load("compiler")
        del record["pod_payload"]["env"]["SSH_PUBLIC_KEY"]
        record["pod_payload_sha256"] = json_document_hash(
            record["pod_payload"]
        )
        InstanceStore(self.state).save(record)

        with self.assertRaises(RunpodLocalError) as caught:
            self.endpoint()
        self.assertEqual(caught.exception.code, "invalid_instance_record")

    def test_endpoint_rejects_unready_or_unsafe_address_and_port(self):
        cases = (
            live_pod(public_ip=None),
            live_pod(public_ip="127.0.0.1"),
            live_pod(public_ip="10.0.0.1"),
            live_pod(public_ip="::1"),
            live_pod(port_mappings={}),
            live_pod(port_mappings={"22": "22022"}),
            live_pod(port_mappings={"22": True}),
            live_pod(port_mappings={"22": 65536}),
        )
        for pod in cases:
            with self.subTest(pod=pod):
                with self.assertRaises(RunpodLocalError):
                    self.endpoint(pod)

    def test_identity_must_be_private_owned_regular_file(self):
        self.identity.chmod(0o644)
        with self.assertRaises(RunpodLocalError):
            self.endpoint()

        self.identity.chmod(0o600)
        linked = self.root / "linked-identity"
        linked.symlink_to(self.identity)
        record = InstanceStore(self.state).load("compiler")
        record["connection"]["identity_file"] = str(linked)
        InstanceStore(self.state).save(record)
        with self.assertRaises(RunpodLocalError):
            self.endpoint()

    def test_ssh_remote_command_is_one_shell_quoted_argument(self):
        endpoint = self.endpoint()
        remote = [
            "python3",
            "-c",
            "print('$HOME'); touch /tmp/not-executed-locally",
            "space value",
        ]

        argv = build_ssh_argv(endpoint, remote)

        self.assertEqual(argv[0], "ssh")
        self.assertEqual(argv[-2], "root@100.65.0.119")
        self.assertTrue(argv[-1].startswith("exec "))
        self.assertEqual(shlex.split(argv[-1][5:]), remote)
        self.assertEqual(argv.count("root@100.65.0.119"), 1)

    def test_tunnel_binds_both_sides_to_loopback(self):
        argv = build_tunnel_argv(
            self.endpoint(), local_port=8000, remote_port=8000
        )

        self.assertIn(
            "127.0.0.1:8000:127.0.0.1:8000",
            argv,
        )
        self.assertIn("ExitOnForwardFailure=yes", argv)
        for invalid in (0, 65536, True, "8000"):
            with self.subTest(port=invalid):
                with self.assertRaises(RunpodLocalError):
                    build_tunnel_argv(
                        self.endpoint(),
                        local_port=invalid,
                        remote_port=8000,
                    )

    def test_copy_is_limited_to_canonical_persistent_or_session_paths(self):
        source = self.root / "tensor.safetensors"
        source.write_bytes(b"fixture")
        endpoint = self.endpoint()

        push = build_copy_argv(
            endpoint,
            direction="push",
            source=str(source),
            destination="/workspace/models/example/tensor.safetensors",
            recursive=False,
        )
        pull = build_copy_argv(
            endpoint,
            direction="pull",
            source="/workspace/results/profile.json",
            destination=str(self.root / "profile.json"),
            recursive=False,
        )

        self.assertEqual(push[-3], "--")
        self.assertTrue(push[-2].startswith("/"))
        self.assertEqual(pull[-3], "--")
        self.assertTrue(pull[-1].startswith("/"))
        self.assertEqual(
            validate_remote_copy_path(
                "/root/runpod-session/results/private.json"
            ),
            "/root/runpod-session/results/private.json",
        )
        for path in (
            "/workspace/../secret",
            "/workspace/model*",
            "/workspace/with space",
            "/workspace/$(touch x)",
            "/tmp/file",
            "workspace/relative",
        ):
            with self.subTest(path=path):
                with self.assertRaises(RunpodLocalError):
                    validate_remote_copy_path(path)

    def test_known_hosts_file_is_private_and_symlink_safe(self):
        path = self.state.root / "ssh" / "known-hosts" / "pod123"
        ensure_known_hosts_file(path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

        other = self.root / "other-known-hosts"
        other.write_text("")
        other.chmod(0o600)
        path.unlink()
        path.symlink_to(other)
        with self.assertRaises(RunpodLocalError):
            ensure_known_hosts_file(path)

    def test_subprocess_environment_drops_secret_and_agent_channels(self):
        clean = sanitized_subprocess_environment(
            {
                "PATH": "/usr/bin",
                "RUNPOD_API_KEY": "fixture-secret",
                "HF_TOKEN": "fixture-hf-secret",
                "SSH_AUTH_SOCK": "/tmp/agent",
                "MONKEY": "banana",
            }
        )

        self.assertEqual(clean, {"PATH": "/usr/bin", "MONKEY": "banana"})

    def test_remote_process_heartbeats_without_extending_hard_deadline(self):
        class FakeProcess:
            def __init__(self):
                self.wait_count = 0

            def wait(self, timeout=None):
                self.wait_count += 1
                if self.wait_count == 1:
                    raise subprocess.TimeoutExpired(["ssh"], timeout)
                return 0

        captured = {}
        process = FakeProcess()

        def popen(argv, **kwargs):
            captured["argv"] = argv
            captured.update(kwargs)
            return process

        times = iter(
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=offset)
            for offset in (1, 2, 3, 4)
        )
        store = InstanceStore(self.state)
        original_deadline = store.load("compiler")["lease"]["expires_at"]
        with mock.patch.dict(
            os.environ,
            {
                "RUNPOD_API_KEY": "fixture-secret",
                "HF_TOKEN": "fixture-hf-secret",
            },
        ):
            result = run_with_activity(
                ["ssh", "fixture"],
                instances=store,
                name="compiler",
                expected_operation_id=(
                    "12345678-1234-4234-8234-123456789abc"
                ),
                expected_pod_id="pod123",
                source="fixture_remote",
                popen_factory=popen,
                clock=lambda: next(times),
            )

        self.assertEqual(result, 0)
        self.assertFalse(captured["shell"])
        self.assertNotIn("RUNPOD_API_KEY", captured["env"])
        self.assertNotIn("HF_TOKEN", captured["env"])
        record = store.load("compiler")
        self.assertEqual(record["lease"]["expires_at"], original_deadline)
        self.assertEqual(record["lease"]["activity_source"], "fixture_remote")

    def test_remote_process_cannot_heartbeat_a_reused_local_name(self):
        record = InstanceStore(self.state).load("compiler")
        record["operation_id"] = "87654321-4321-4321-8321-ba9876543210"
        InstanceStore(self.state).save(record)
        popen = mock.Mock()

        with self.assertRaises(RunpodLocalError) as caught:
            run_with_activity(
                ["ssh", "fixture"],
                instances=InstanceStore(self.state),
                name="compiler",
                expected_operation_id=(
                    "12345678-1234-4234-8234-123456789abc"
                ),
                expected_pod_id="pod123",
                source="stale_remote",
                popen_factory=popen,
            )

        self.assertEqual(caught.exception.code, "instance_identity_changed")
        popen.assert_not_called()

    def test_tunnel_presence_does_not_refresh_idle_activity(self):
        class FakeProcess:
            def __init__(self):
                self.wait_count = 0

            def wait(self, timeout=None):
                self.wait_count += 1
                if self.wait_count == 1:
                    raise subprocess.TimeoutExpired(["ssh"], timeout)
                return 0

        store = InstanceStore(self.state)
        before = store.load("compiler")
        initial_events = len(before["events"])
        start = datetime.datetime.now(datetime.timezone.utc)
        times = iter(
            (
                start,
                start + datetime.timedelta(seconds=30),
            )
        )

        result = run_with_activity(
            ["ssh", "fixture"],
            instances=store,
            name="compiler",
            expected_operation_id=before["operation_id"],
            expected_pod_id="pod123",
            source="ssh_tunnel",
            maintain_activity=False,
            popen_factory=lambda *_args, **_kwargs: FakeProcess(),
            clock=lambda: next(times),
        )

        self.assertEqual(result, 0)
        after = store.load("compiler")
        self.assertEqual(len(after["events"]), initial_events)
        self.assertEqual(
            after["lease"]["last_activity_at"],
            before["lease"]["last_activity_at"],
        )

    def test_failed_remote_client_start_does_not_refresh_activity(self):
        store = InstanceStore(self.state)
        before = store.load("compiler")

        def fail_to_start(*_args, **_kwargs):
            raise OSError("fixture executable failure")

        with self.assertRaises(RunpodLocalError) as caught:
            run_with_activity(
                ["ssh", "fixture"],
                instances=store,
                name="compiler",
                expected_operation_id=before["operation_id"],
                expected_pod_id="pod123",
                source="fixture_remote",
                popen_factory=fail_to_start,
            )

        self.assertEqual(caught.exception.code, "remote_client_start_failed")
        after = store.load("compiler")
        self.assertEqual(after["events"], before["events"])
        self.assertEqual(after["lease"], before["lease"])

    def test_cli_preserves_remote_arguments_after_double_dash(self):
        parser = build_parser()
        arguments = parse_arguments(
            parser,
            ["ssh", "compiler", "--", "printf", "%s", "hello world"]
        )

        self.assertEqual(
            arguments.remote_command,
            ["printf", "%s", "hello world"],
        )


if __name__ == "__main__":
    unittest.main()
