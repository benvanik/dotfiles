from __future__ import annotations

import pathlib
import tempfile
import unittest

from runpod_local.auth import ApiCredential, CredentialStore
from runpod_local.errors import RunpodLocalError


class CredentialStoreTest(unittest.TestCase):
    def test_store_is_private_and_repr_is_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "config" / "api-key"
            store = CredentialStore(path, environment={})
            credential = store.store("fixture-runpod-token")

            self.assertEqual(path.read_text().strip(), "fixture-runpod-token")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertNotIn("fixture-runpod-token", repr(credential))
            self.assertEqual(store.load().token, "fixture-runpod-token")

    def test_environment_takes_precedence_without_reporting_a_file_path(self):
        store = CredentialStore(
            pathlib.Path("/not/read"),
            environment={"RUNPOD_API_KEY": "environment-fixture-token"},
        )
        credential = store.load()
        self.assertEqual(credential.source, "environment")
        self.assertIsNone(store.status()["path"])

    def test_broad_file_permissions_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "api-key"
            path.write_text("fixture-runpod-token\n")
            path.chmod(0o644)
            with self.assertRaises(RunpodLocalError) as caught:
                CredentialStore(path, environment={}).load()
            self.assertEqual(
                caught.exception.code, "unsafe_credential_permissions"
            )

    def test_store_refuses_to_chmod_a_broad_existing_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = pathlib.Path(directory) / "shared"
            parent.mkdir(mode=0o755)
            parent.chmod(0o755)
            store = CredentialStore(parent / "api-key", environment={})
            with self.assertRaises(RunpodLocalError) as caught:
                store.store("fixture-runpod-token")
            self.assertEqual(caught.exception.code, "unsafe_private_permissions")
            self.assertEqual(parent.stat().st_mode & 0o777, 0o755)

    def test_symlink_credential_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            target = root / "target"
            target.write_text("fixture-runpod-token\n")
            target.chmod(0o600)
            link = root / "api-key"
            link.symlink_to(target)
            with self.assertRaises(RunpodLocalError) as caught:
                CredentialStore(link, environment={}).load()
            self.assertEqual(caught.exception.code, "unsafe_credential_file")

    def test_remove_never_changes_environment_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "api-key"
            store = CredentialStore(
                path,
                environment={"RUNPOD_API_KEY": "environment-fixture-token"},
            )
            self.assertFalse(store.remove())
            self.assertEqual(store.load().source, "environment")

    def test_api_credential_rejects_whitespace(self):
        with self.assertRaises(RunpodLocalError):
            ApiCredential("fixture token", source="test")


if __name__ == "__main__":
    unittest.main()
