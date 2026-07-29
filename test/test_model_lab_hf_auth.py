"""Hugging Face credentials remain model-lab-owned and ephemeral remotely."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from model_lab.errors import ModelLabError
from model_lab.hf_auth import manage_huggingface_credential
from model_lab.huggingface_credentials import (
    MAX_HF_TOKEN_BYTES,
    REMOTE_HF_CREDENTIAL_ABSENT,
    REMOTE_HF_CREDENTIAL_INVALID,
    REMOTE_HF_CREDENTIAL_UNSAFE,
    REMOTE_HF_TOKEN_PATH,
    build_remote_hf_credential_argv,
    build_remote_hf_probe_argv,
)
from runpod_local.remote import SshEndpoint


IMAGE = (
    "runpod/pytorch@sha256:"
    "1111111111111111111111111111111111111111111111111111111111111111"
)


class RemoteHuggingFaceCredentialProgramTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.session_root = self.root / "runpod-session"
        self.token_path = (
            self.session_root / "secrets" / "huggingface" / "token"
        )

    def run_action(
        self,
        action: str,
        *,
        token: bytes | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        arguments = build_remote_hf_credential_argv(
            action,
            session_root=str(self.session_root),
        )
        arguments[arguments.index("/usr/bin/python3.12")] = sys.executable
        return subprocess.run(
            arguments,
            input=token,
            check=False,
            capture_output=True,
            env=environment,
        )

    def test_push_status_and_clear_keep_token_private(self) -> None:
        secret = b"fixture-private-token"

        pushed = self.run_action("push", token=secret)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(self.token_path.read_bytes(), secret)
        for directory in (
            self.session_root,
            self.session_root / "secrets",
            self.session_root / "secrets" / "huggingface",
        ):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.token_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.run_action("status").returncode, 0)

        cleared = self.run_action("clear")
        self.assertEqual(cleared.returncode, 0, cleared.stderr)
        self.assertFalse(self.token_path.exists())
        self.assertEqual(
            self.run_action("status").returncode,
            REMOTE_HF_CREDENTIAL_ABSENT,
        )
        self.assertEqual(
            self.run_action("clear").returncode,
            REMOTE_HF_CREDENTIAL_ABSENT,
        )

    def test_unsafe_paths_and_orphaned_installs_fail_closed(self) -> None:
        self.session_root.mkdir(mode=0o755)
        unsafe = self.run_action("push", token=b"private-token")
        self.assertEqual(unsafe.returncode, REMOTE_HF_CREDENTIAL_UNSAFE)
        self.assertFalse(self.token_path.exists())

        self.session_root.chmod(0o700)
        self.assertEqual(
            self.run_action("push", token=b"installed-token").returncode,
            0,
        )
        self.token_path.unlink()
        orphan = self.token_path.parent / (
            ".token.1234.0123456789abcdef01234567"
        )
        orphan.write_bytes(b"orphaned-private-token")
        orphan.chmod(0o600)

        self.assertEqual(
            self.run_action("status").returncode,
            REMOTE_HF_CREDENTIAL_UNSAFE,
        )
        self.assertEqual(self.run_action("clear").returncode, 0)
        self.assertFalse(orphan.exists())

    def test_program_rejects_malformed_input_without_echoing_it(self) -> None:
        for token in (
            b"",
            b"two tokens",
            b"two\nlines",
            b"x" * (MAX_HF_TOKEN_BYTES + 1),
        ):
            with self.subTest(token_length=len(token)):
                result = self.run_action("push", token=token)
                self.assertEqual(
                    result.returncode,
                    REMOTE_HF_CREDENTIAL_INVALID,
                )
                if token:
                    self.assertNotIn(token, result.stdout)
                    self.assertNotIn(token, result.stderr)
                self.assertFalse(self.token_path.exists())

    def test_remote_argv_is_fixed_and_ignores_python_environment(self) -> None:
        arguments = build_remote_hf_credential_argv("push")
        self.assertEqual(arguments[-2:], ["push", "/root/runpod-session"])
        self.assertEqual(
            REMOTE_HF_TOKEN_PATH,
            "/root/runpod-session/secrets/huggingface/token",
        )
        self.assertEqual(arguments[0:2], ["/usr/bin/env", "-i"])
        self.assertIn("/usr/bin/python3.12", arguments)
        self.assertIn("-I", arguments)
        self.assertIn("-S", arguments)
        probe = build_remote_hf_probe_argv()
        self.assertIn("sys.version_info[:2]==(3,12)", probe[-1])
        self.assertIn("sys.flags.isolated", probe[-1])

        shadow = self.root / "shadow"
        shadow.mkdir()
        marker = self.root / "imported-host-module"
        hostile_source = (
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('x')\n"
            "raise RuntimeError('host module imported')\n"
        )
        for name in ("base64.py", "secrets.py", "sitecustomize.py"):
            (shadow / name).write_text(hostile_source)
        environment = dict(os.environ)
        environment.update(
            {
                "PATH": str(shadow),
                "PYTHONPATH": str(shadow),
                "PYTHONUSERBASE": str(shadow),
            }
        )
        result = self.run_action(
            "push",
            token=b"private-token",
            environment=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())


class HuggingFaceHostCredentialTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.token_path = self.root / "token"
        self.secret = b"fixture-private-token"
        self.token_path.write_bytes(self.secret)
        self.token_path.chmod(0o600)
        self.credentials_path = self.root / "runpod-credential"
        self.credentials_path.write_text("fixture-runpod-key\n")
        self.credentials_path.chmod(0o600)
        self.endpoint = SshEndpoint(
            instance_name="compiler",
            operation_id="12345678-1234-4234-8234-123456789abc",
            pod_id="pod123",
            host="203.0.113.10",
            port=22022,
            user="root",
            identity_file=self.root / "id_ed25519",
            known_hosts_file=self.root / "known-hosts",
            host_key_alias="runpod-pod123",
        )
        self.instances = mock.Mock()
        self.instances.load.return_value = {
            "expected": {"image": IMAGE},
        }

    def invoke(
        self,
        action: str,
        *,
        remote_results: tuple[int, ...],
    ) -> tuple[dict[str, object], mock.Mock]:
        run_remote = mock.Mock(side_effect=remote_results)
        with (
            mock.patch(
                "model_lab.hf_auth.InstanceStore",
                return_value=self.instances,
            ),
            mock.patch(
                "model_lab.hf_auth.resolve_endpoint",
                return_value=self.endpoint,
            ),
            mock.patch("model_lab.hf_auth.ensure_known_hosts_file"),
            mock.patch(
                "model_lab.hf_auth.run_with_activity",
                run_remote,
            ),
        ):
            result = manage_huggingface_credential(
                action,
                "compiler",
                token_file=self.token_path,
                runpod_state_root=self.root / "runpod-state",
                credentials_path=self.credentials_path,
                api_factory=lambda _: SimpleNamespace(),
            )
        return result, run_remote

    def test_push_probes_then_streams_token_only_on_stdin(self) -> None:
        result, run_remote = self.invoke("push", remote_results=(0, 0))

        self.assertTrue(result["configured"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["storage"], "ephemeral-container")
        self.assertEqual(run_remote.call_count, 2)
        probe, transfer = run_remote.call_args_list
        self.assertIsNone(probe.kwargs["stdin"])
        self.assertTrue(transfer.kwargs["stdin"].closed)
        self.assertNotIn(self.secret.decode(), repr(run_remote.call_args_list))
        self.assertNotIn(str(self.token_path), repr(result))

    def test_status_and_clear_treat_absence_as_safe_state(self) -> None:
        status, _ = self.invoke(
            "status",
            remote_results=(REMOTE_HF_CREDENTIAL_ABSENT,),
        )
        clear, _ = self.invoke(
            "clear",
            remote_results=(REMOTE_HF_CREDENTIAL_ABSENT,),
        )

        self.assertFalse(status["configured"])
        self.assertFalse(status["changed"])
        self.assertFalse(clear["configured"])
        self.assertFalse(clear["changed"])

    def test_probe_failure_never_opens_credential_stream(self) -> None:
        with self.assertRaises(ModelLabError) as caught:
            self.invoke("push", remote_results=(255,))

        self.assertEqual(
            caught.exception.code,
            "remote_hf_credential_probe_failed",
        )

    def test_push_requires_digest_pinned_host_receipt(self) -> None:
        self.instances.load.return_value = {
            "expected": {"image": "runpod/pytorch:mutable"}
        }

        with self.assertRaises(ModelLabError) as caught:
            self.invoke("push", remote_results=())

        self.assertEqual(caught.exception.code, "hf_auth_unpinned_image")

    def test_remote_unsafe_state_is_preserved_as_stable_error(self) -> None:
        with self.assertRaises(ModelLabError) as caught:
            self.invoke(
                "status",
                remote_results=(REMOTE_HF_CREDENTIAL_UNSAFE,),
            )

        self.assertEqual(
            caught.exception.code,
            "unsafe_remote_hf_credential",
        )


if __name__ == "__main__":
    unittest.main()
