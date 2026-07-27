from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from runpod_local.errors import RunpodLocalError
from runpod_local.huggingface_credentials import (
    HF_CLI_VERSION,
    huggingface_token_path,
    load_huggingface_token,
    open_huggingface_token_file,
)


class HuggingFaceCredentialTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.secret_directory = self.root / "config" / "huggingface"
        self.secret_directory.mkdir(parents=True, mode=0o700)
        self.token_path = self.secret_directory / "token"

    def write_token(self, value: bytes = b"fixture-private-token") -> None:
        self.token_path.write_bytes(value)
        self.token_path.chmod(0o600)

    def test_default_token_path_uses_private_config_not_cache(self):
        path = huggingface_token_path(
            environment={
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_CACHE_HOME": str(self.root / "cache"),
            },
            home=self.root,
        )
        self.assertEqual(path, self.token_path)
        self.assertNotIn("cache", path.parts)

    def test_token_loader_accepts_one_private_owned_regular_file(self):
        self.write_token(b"fixture-private-token\n")

        self.assertEqual(
            load_huggingface_token(self.token_path, required=True),
            "fixture-private-token",
        )
        with open_huggingface_token_file(self.token_path) as token_file:
            self.assertEqual(token_file.read(), b"fixture-private-token\n")

    def test_token_loader_rejects_unsafe_or_malformed_files(self):
        cases = (
            b"",
            b"two tokens",
            b"two\nlines",
            b"x" * 8193,
        )
        for value in cases:
            with self.subTest(value_length=len(value)):
                self.write_token(value)
                with self.assertRaises(RunpodLocalError):
                    load_huggingface_token(self.token_path, required=True)

        self.write_token()
        self.token_path.chmod(0o644)
        with self.assertRaises(RunpodLocalError) as caught:
            load_huggingface_token(self.token_path, required=True)
        self.assertEqual(
            caught.exception.code, "unsafe_hf_credential_permissions"
        )

        self.token_path.unlink()
        target = self.root / "target"
        target.write_bytes(b"fixture-private-token")
        target.chmod(0o600)
        self.token_path.symlink_to(target)
        with self.assertRaises(RunpodLocalError) as caught:
            load_huggingface_token(self.token_path, required=True)
        self.assertEqual(caught.exception.code, "unsafe_hf_credential_file")

        self.token_path.unlink()
        os.mkfifo(self.token_path, mode=0o600)
        with self.assertRaises(RunpodLocalError) as caught:
            load_huggingface_token(self.token_path, required=True)
        self.assertEqual(caught.exception.code, "unsafe_hf_credential_file")

    def test_token_loader_rejects_foreign_owner(self):
        self.write_token()
        with mock.patch(
            "runpod_local.huggingface_credentials.os.getuid",
            return_value=self.token_path.stat().st_uid + 1,
        ):
            with self.assertRaises(RunpodLocalError) as caught:
                load_huggingface_token(self.token_path, required=True)
        self.assertEqual(caught.exception.code, "unsafe_hf_credential_file")

    def test_wrapper_separates_cache_and_credentials_and_rejects_update(self):
        home = self.root / "home"
        tool = home / "tools" / "hf" / HF_CLI_VERSION / "bin" / "hf"
        tool.parent.mkdir(parents=True)
        capture = self.root / "captured"
        tool.write_text(
            "#!/bin/sh\n"
            "env | sort > \"$HF_WRAPPER_TEST_CAPTURE\"\n"
            "printf '%s\\n' \"$@\" >> \"$HF_WRAPPER_TEST_CAPTURE\"\n"
        )
        tool.chmod(0o700)
        wrapper = pathlib.Path(__file__).resolve().parents[1] / "bin" / "hf"
        environment = {
            "HOME": str(home),
            "PATH": os.environ["PATH"],
            "HF_WRAPPER_TEST_CAPTURE": str(capture),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
        }

        result = subprocess.run(
            [str(wrapper), "auth", "whoami"],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        captured = capture.read_text()
        self.assertIn(
            f"HF_TOKEN_PATH={home}/.config/huggingface/token", captured
        )
        self.assertIn(f"HF_HOME={home}/.cache/huggingface", captured)
        self.assertIn("HF_HUB_DISABLE_UPDATE_CHECK=1", captured)
        self.assertIn("HF_XET_HIGH_PERFORMANCE=1", captured)
        self.assertIn("auth\nwhoami\n", captured)
        self.assertEqual(
            (home / ".config" / "huggingface").stat().st_mode & 0o777,
            0o700,
        )

        rejected = subprocess.run(
            [str(wrapper), "update"],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("cannot self-update", rejected.stderr)

        for arguments in (
            ("auth", "login", "--token", "fixture-secret"),
            ("auth", "login", "--token=fixture-secret"),
            ("auth", "login", "--add-to-git-credential"),
            ("auth", "token"),
        ):
            with self.subTest(arguments=arguments):
                rejected = subprocess.run(
                    [str(wrapper), *arguments],
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(rejected.returncode, 1)
                self.assertNotIn("fixture-secret", rejected.stdout)
                self.assertNotIn("fixture-secret", rejected.stderr)

    def test_wrapper_refuses_environment_token_without_printing_it(self):
        wrapper = pathlib.Path(__file__).resolve().parents[1] / "bin" / "hf"
        secret = "fixture-environment-secret"
        result = subprocess.run(
            [str(wrapper), "auth", "whoami"],
            env={
                "HOME": str(self.root / "home"),
                "PATH": os.environ["PATH"],
                "HF_TOKEN": secret,
            },
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(secret, result.stdout)
        self.assertNotIn(secret, result.stderr)


if __name__ == "__main__":
    unittest.main()
