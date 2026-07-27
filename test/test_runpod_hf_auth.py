from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from runpod_local.cli import build_parser, parse_arguments
from runpod_local.errors import RunpodLocalError
from runpod_local.huggingface_credentials import (
    MAX_HF_TOKEN_BYTES,
    REMOTE_HF_CREDENTIAL_ABSENT,
    REMOTE_HF_CREDENTIAL_INVALID,
    REMOTE_HF_CREDENTIAL_UNSAFE,
    REMOTE_HF_TOKEN_PATH,
    build_remote_hf_credential_argv,
    build_remote_hf_probe_argv,
)
from runpod_local.remote import SshEndpoint
from runpod_local.remote_cli import _run_hf_auth
from runpod_local.runtime_catalog import load_runtime

IMAGE = (
    "runpod/pytorch@sha256:"
    "1111111111111111111111111111111111111111111111111111111111111111"
)
RUNTIME = load_runtime("vllm-cu129-v0.25.1")


class RemoteHuggingFaceCredentialProgramTest(unittest.TestCase):
    def setUp(self):
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

    def test_push_status_and_clear_keep_token_ephemeral_and_private(self):
        secret = b"fixture-private-token"
        push = self.run_action("push", token=secret)

        self.assertEqual(push.returncode, 0, push.stderr)
        self.assertEqual(push.stdout, b"")
        self.assertNotIn(secret, push.stderr)
        self.assertEqual(self.token_path.read_bytes(), secret)
        for directory in (
            self.session_root,
            self.session_root / "secrets",
            self.session_root / "secrets" / "huggingface",
        ):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.token_path.stat().st_mode & 0o777, 0o600)

        status = self.run_action("status")
        self.assertEqual(status.returncode, 0, status.stderr)

        clear = self.run_action("clear")
        self.assertEqual(clear.returncode, 0, clear.stderr)
        self.assertFalse(self.token_path.exists())
        self.assertEqual(
            self.run_action("status").returncode,
            REMOTE_HF_CREDENTIAL_ABSENT,
        )
        self.assertEqual(
            self.run_action("clear").returncode,
            REMOTE_HF_CREDENTIAL_ABSENT,
        )

    def test_program_rejects_unsafe_paths_without_following_symlinks(self):
        self.session_root.mkdir(mode=0o755)
        unsafe = self.run_action("push", token=b"fixture-private-token")
        self.assertEqual(
            unsafe.returncode,
            REMOTE_HF_CREDENTIAL_UNSAFE,
        )
        self.assertFalse(self.token_path.exists())

        self.session_root.chmod(0o700)
        accepted = self.run_action("push", token=b"first-token")
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.token_path.unlink()
        target = self.root / "target"
        target.write_bytes(b"must-not-change")
        target.chmod(0o600)
        self.token_path.symlink_to(target)

        rejected = self.run_action("push", token=b"second-token")

        self.assertEqual(
            rejected.returncode,
            REMOTE_HF_CREDENTIAL_UNSAFE,
        )
        self.assertEqual(target.read_bytes(), b"must-not-change")
        self.assertTrue(self.token_path.is_symlink())

    def test_program_rejects_unsafe_intermediate_directory(self):
        for symlink_name in ("secrets", "huggingface"):
            with self.subTest(symlink_name=symlink_name):
                if self.session_root.exists():
                    for child in reversed(
                        list(self.session_root.rglob("*"))
                    ):
                        if child.is_symlink() or child.is_file():
                            child.unlink()
                        else:
                            child.rmdir()
                    self.session_root.rmdir()
                self.session_root.mkdir(mode=0o700)
                target = self.root / f"target-{symlink_name}"
                target.mkdir(mode=0o700)
                if symlink_name == "secrets":
                    symlink = self.session_root / "secrets"
                else:
                    secrets = self.session_root / "secrets"
                    secrets.mkdir(mode=0o700)
                    symlink = secrets / "huggingface"
                symlink.symlink_to(target)

                rejected = self.run_action(
                    "push", token=b"fixture-private-token"
                )

                self.assertEqual(
                    rejected.returncode,
                    REMOTE_HF_CREDENTIAL_UNSAFE,
                )
                self.assertEqual(list(target.iterdir()), [])

    def test_orphaned_atomic_install_is_never_reported_absent(self):
        accepted = self.run_action("push", token=b"installed-token")
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.token_path.unlink()
        orphan = self.token_path.parent / (
            ".token.1234.0123456789abcdef01234567"
        )
        orphan.write_bytes(b"orphaned-private-token")
        orphan.chmod(0o600)

        status = self.run_action("status")

        self.assertEqual(
            status.returncode,
            REMOTE_HF_CREDENTIAL_UNSAFE,
        )
        self.assertNotIn(b"orphaned-private-token", status.stderr)
        cleared = self.run_action("clear")
        self.assertEqual(cleared.returncode, 0, cleared.stderr)
        self.assertFalse(orphan.exists())
        self.assertEqual(
            self.run_action("status").returncode,
            REMOTE_HF_CREDENTIAL_ABSENT,
        )

    def test_push_replaces_orphaned_atomic_install(self):
        accepted = self.run_action("push", token=b"installed-token")
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        orphan = self.token_path.parent / (
            ".token.1234.0123456789abcdef01234567"
        )
        orphan.write_bytes(b"orphaned-private-token")
        orphan.chmod(0o600)

        replaced = self.run_action("push", token=b"replacement-token")

        self.assertEqual(replaced.returncode, 0, replaced.stderr)
        self.assertFalse(orphan.exists())
        self.assertEqual(self.token_path.read_bytes(), b"replacement-token")

    def test_unsafe_atomic_install_candidates_are_preserved(self):
        accepted = self.run_action("push", token=b"installed-token")
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.token_path.unlink()
        target = self.root / "symlink-target"
        target.write_bytes(b"must-not-change")
        target.chmod(0o600)
        candidates = (
            (
                "symlink",
                ".token.1.111111111111111111111111",
                lambda path: path.symlink_to(target),
            ),
            (
                "wrong-mode",
                ".token.2.222222222222222222222222",
                lambda path: (
                    path.write_bytes(b"private-token"),
                    path.chmod(0o640),
                ),
            ),
            (
                "oversized",
                ".token.3.333333333333333333333333",
                lambda path: (
                    path.write_bytes(b"x" * (MAX_HF_TOKEN_BYTES + 1)),
                    path.chmod(0o600),
                ),
            ),
            (
                "hard-link",
                ".token.4.444444444444444444444444",
                lambda path: os.link(target, path),
            ),
        )
        for label, name, create in candidates:
            with self.subTest(label=label):
                candidate = self.token_path.parent / name
                create(candidate)

                self.assertEqual(
                    self.run_action("status").returncode,
                    REMOTE_HF_CREDENTIAL_UNSAFE,
                )
                self.assertEqual(
                    self.run_action("clear").returncode,
                    REMOTE_HF_CREDENTIAL_UNSAFE,
                )
                self.assertTrue(candidate.exists() or candidate.is_symlink())
                candidate.unlink()
        self.assertEqual(target.read_bytes(), b"must-not-change")

    def test_nonmatching_temporary_name_is_never_claimed(self):
        accepted = self.run_action("push", token=b"installed-token")
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.token_path.unlink()
        unrelated = self.token_path.parent / ".token.not-owned"
        unrelated.write_bytes(b"unrelated-private-bytes")
        unrelated.chmod(0o600)

        self.assertEqual(
            self.run_action("status").returncode,
            REMOTE_HF_CREDENTIAL_ABSENT,
        )
        self.assertEqual(
            self.run_action("clear").returncode,
            REMOTE_HF_CREDENTIAL_ABSENT,
        )
        self.assertEqual(
            unrelated.read_bytes(), b"unrelated-private-bytes"
        )

    def test_program_rejects_malformed_input_without_echoing_it(self):
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

    def test_remote_argv_contains_only_program_action_and_fixed_path(self):
        secret = "fixture-private-token"
        arguments = build_remote_hf_credential_argv("push")

        self.assertEqual(arguments[-2:], ["push", "/root/runpod-session"])
        self.assertNotIn(secret, repr(arguments))
        self.assertEqual(
            REMOTE_HF_TOKEN_PATH,
            "/root/runpod-session/secrets/huggingface/token",
        )
        self.assertEqual(arguments[0:2], ["/usr/bin/env", "-i"])
        self.assertIn("/usr/bin/python3.12", arguments)
        self.assertIn("-I", arguments)
        self.assertIn("-S", arguments)
        probe = build_remote_hf_probe_argv()
        self.assertEqual(
            probe,
            [
                "/usr/bin/env",
                "-i",
                "HOME=/root",
                "PATH=/usr/bin:/bin",
                "/usr/bin/python3.12",
                "-I",
                "-S",
                "-c",
                mock.ANY,
            ],
        )
        self.assertIn("sys.version_info[:2]==(3,12)", probe[-1])
        self.assertIn("sys.flags.isolated", probe[-1])
        self.assertIn("sys.flags.no_site", probe[-1])
        self.assertIn("sys.flags.ignore_environment", probe[-1])
        self.assertIn("sys.flags.safe_path", probe[-1])

    def test_remote_program_ignores_hostile_python_environment(self):
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
            token=b"fixture-private-token",
            environment=environment,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(
            self.token_path.read_bytes(), b"fixture-private-token"
        )


class HuggingFaceAuthCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.token_path = self.root / "token"
        self.secret = "fixture-private-token"
        self.token_path.write_text(self.secret)
        self.token_path.chmod(0o600)
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
            "pod_payload": {"imageName": IMAGE},
            "expected": {
                "image": IMAGE,
                "template_contract": None,
            },
        }

    def test_parser_exposes_push_status_clear_and_agent_help(self):
        parser = build_parser()
        push = parse_arguments(
            parser,
            [
                "hf-auth",
                "push",
                "compiler",
                "--token-file",
                str(self.token_path),
                "--json",
            ],
        )
        self.assertEqual(push.hf_auth_action, "push")
        self.assertEqual(push.name, "compiler")
        self.assertTrue(push.json)

        status = parse_arguments(
            parser, ["hf-auth", "status", "compiler"]
        )
        self.assertEqual(status.hf_auth_action, "status")
        clear = parse_arguments(
            parser, ["hf-auth", "clear", "compiler"]
        )
        self.assertEqual(clear.hf_auth_action, "clear")

    def test_push_probes_host_before_streaming_exact_token_file(self):
        parser = build_parser()
        arguments = parse_arguments(
            parser,
            [
                "hf-auth",
                "push",
                "compiler",
                "--token-file",
                str(self.token_path),
                "--json",
            ],
        )
        output = io.StringIO()
        with (
            mock.patch(
                "runpod_local.remote_cli._endpoint",
                return_value=(
                    mock.sentinel.state,
                    self.instances,
                    self.endpoint,
                ),
            ),
            mock.patch(
                "runpod_local.remote_cli.ensure_known_hosts_file"
            ) as ensure_known_hosts,
            mock.patch(
                "runpod_local.remote_cli.run_with_activity",
                side_effect=(0, 0),
            ) as run_remote,
            contextlib.redirect_stdout(output),
        ):
            result = _run_hf_auth(arguments)

        self.assertEqual(result, 0)
        self.assertEqual(run_remote.call_count, 2)
        probe = run_remote.call_args_list[0]
        transfer = run_remote.call_args_list[1]
        self.assertIsNone(probe.kwargs["stdin"])
        self.assertIn("stdin", transfer.kwargs)
        self.assertTrue(transfer.kwargs["stdin"].closed)
        self.assertNotIn(self.secret, repr(probe.args))
        self.assertNotIn(self.secret, repr(transfer.args))
        self.assertEqual(ensure_known_hosts.call_count, 2)

        payload = json.loads(output.getvalue())
        self.assertTrue(payload["configured"])
        self.assertEqual(payload["storage"], "ephemeral_container")
        self.assertNotIn(self.secret, output.getvalue())
        self.assertNotIn(str(self.token_path), output.getvalue())

    def test_status_treats_absence_as_safe_state(self):
        arguments = parse_arguments(
            build_parser(),
            ["hf-auth", "status", "compiler", "--json"],
        )
        output = io.StringIO()
        with (
            mock.patch(
                "runpod_local.remote_cli._endpoint",
                return_value=(
                    mock.sentinel.state,
                    self.instances,
                    self.endpoint,
                ),
            ),
            mock.patch(
                "runpod_local.remote_cli.ensure_known_hosts_file"
            ),
            mock.patch(
                "runpod_local.remote_cli.run_with_activity",
                return_value=REMOTE_HF_CREDENTIAL_ABSENT,
            ),
            contextlib.redirect_stdout(output),
        ):
            result = _run_hf_auth(arguments)

        self.assertEqual(result, 0)
        self.assertFalse(json.loads(output.getvalue())["configured"])

    def test_probe_failure_never_opens_a_credential_connection(self):
        arguments = parse_arguments(
            build_parser(),
            [
                "hf-auth",
                "push",
                "compiler",
                "--token-file",
                str(self.token_path),
            ],
        )
        with (
            mock.patch(
                "runpod_local.remote_cli._endpoint",
                return_value=(
                    mock.sentinel.state,
                    self.instances,
                    self.endpoint,
                ),
            ),
            mock.patch(
                "runpod_local.remote_cli.ensure_known_hosts_file"
            ),
            mock.patch(
                "runpod_local.remote_cli.run_with_activity",
                return_value=255,
            ) as run_remote,
        ):
            with self.assertRaises(RunpodLocalError) as caught:
                _run_hf_auth(arguments)

        self.assertEqual(
            caught.exception.code, "remote_hf_credential_probe_failed"
        )
        self.assertEqual(run_remote.call_count, 1)
        self.assertIsNone(run_remote.call_args.kwargs["stdin"])

    def test_push_rejects_unattested_image_sources_before_ssh(self):
        arguments = parse_arguments(
            build_parser(),
            [
                "hf-auth",
                "push",
                "compiler",
                "--token-file",
                str(self.token_path),
            ],
        )
        for record in (
            {
                "pod_payload": {"imageName": "runpod/pytorch:mutable"},
                "expected": {
                    "image": "runpod/pytorch:mutable",
                    "template_contract": None,
                },
            },
            {
                "pod_payload": {"templateId": "template123"},
                "expected": {
                    "image": IMAGE,
                    "template_contract": None,
                },
            },
        ):
            with self.subTest(record=record):
                self.instances.load.return_value = record
                with (
                    mock.patch(
                        "runpod_local.remote_cli._endpoint",
                        return_value=(
                            mock.sentinel.state,
                            self.instances,
                            self.endpoint,
                        ),
                    ),
                    mock.patch(
                        "runpod_local.remote_cli.run_with_activity"
                    ) as run_remote,
                ):
                    with self.assertRaises(RunpodLocalError) as caught:
                        _run_hf_auth(arguments)

                self.assertEqual(
                    caught.exception.code, "hf_auth_unpinned_image"
                )
                run_remote.assert_not_called()

    def test_push_accepts_template_with_attested_exact_image(self):
        contract = RUNTIME.template_contract(
            name="upstream-vllm",
            template_id="template123",
        )
        self.instances.load.return_value = {
            "pod_payload": {"templateId": "template123"},
            "expected": {
                "image": RUNTIME.image,
                "runtime": RUNTIME.safe_summary(),
                "template_contract": contract,
            },
        }
        arguments = parse_arguments(
            build_parser(),
            [
                "hf-auth",
                "push",
                "compiler",
                "--token-file",
                str(self.token_path),
            ],
        )
        with (
            mock.patch(
                "runpod_local.remote_cli._endpoint",
                return_value=(
                    mock.sentinel.state,
                    self.instances,
                    self.endpoint,
                ),
            ),
            mock.patch(
                "runpod_local.remote_cli.ensure_known_hosts_file"
            ),
            mock.patch(
                "runpod_local.remote_cli.run_with_activity",
                side_effect=(0, 0),
            ) as run_remote,
        ):
            self.assertEqual(_run_hf_auth(arguments), 0)
        self.assertEqual(run_remote.call_count, 2)

    def test_clear_reports_change_and_unsafe_status_fails_closed(self):
        clear = parse_arguments(
            build_parser(),
            ["hf-auth", "clear", "compiler", "--json"],
        )
        output = io.StringIO()
        with (
            mock.patch(
                "runpod_local.remote_cli._endpoint",
                return_value=(
                    mock.sentinel.state,
                    self.instances,
                    self.endpoint,
                ),
            ),
            mock.patch(
                "runpod_local.remote_cli.ensure_known_hosts_file"
            ),
            mock.patch(
                "runpod_local.remote_cli.run_with_activity",
                return_value=0,
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(_run_hf_auth(clear), 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["changed"])
        self.assertFalse(payload["configured"])

        status = parse_arguments(
            build_parser(),
            ["hf-auth", "status", "compiler"],
        )
        with (
            mock.patch(
                "runpod_local.remote_cli._endpoint",
                return_value=(
                    mock.sentinel.state,
                    self.instances,
                    self.endpoint,
                ),
            ),
            mock.patch(
                "runpod_local.remote_cli.ensure_known_hosts_file"
            ),
            mock.patch(
                "runpod_local.remote_cli.run_with_activity",
                return_value=REMOTE_HF_CREDENTIAL_UNSAFE,
            ),
        ):
            with self.assertRaises(RunpodLocalError) as caught:
                _run_hf_auth(status)
        self.assertEqual(
            caught.exception.code, "unsafe_remote_hf_credential"
        )


if __name__ == "__main__":
    unittest.main()
