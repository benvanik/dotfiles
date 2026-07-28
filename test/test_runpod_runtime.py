from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

from runpod_local.errors import RunpodLocalError
from runpod_local.runtime_catalog import (
    _RUNTIME_CATALOG,
    _load_runtime_entry,
    load_runtime,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
BOOTSTRAP_ROOT = ROOT / "runpod" / "bootstrap" / "ssh"
BOOTSTRAP_PATH = BOOTSTRAP_ROOT / "bootstrap.sh"
RUNTIME_ROOT = ROOT / "runpod" / "runtimes" / "vllm-cu129"
BASE_IMAGE = (
    "vllm/vllm-openai@sha256:"
    "fb463d6a216c7ee82bf947f321cae7dd7105bfb5084ea35827c2ceb816994b15"
)
RUNTIME_ID = "vllm-cu129-v0.25.1"


class RunpodUpstreamRuntimeTest(unittest.TestCase):
    def test_runtime_manifest_pins_one_exact_official_image(self):
        manifest = json.loads((RUNTIME_ROOT / "runtime-manifest.json").read_text())

        self.assertEqual(
            manifest["schema_version"],
            "runpod.upstream-runtime.v1",
        )
        self.assertEqual(manifest["architecture"], "linux/amd64")
        self.assertEqual(manifest["image"], BASE_IMAGE)
        self.assertEqual(
            manifest["oci_entrypoint"],
            ["vllm", "serve"],
        )
        self.assertEqual(
            manifest["launch_overlay"]["docker_entrypoint"],
            ["/bin/bash", "-c"],
        )
        self.assertNotIn(":latest", manifest["image"])

    def test_bootstrap_identity_is_bound_into_runtime_manifest(self):
        manifest = json.loads((RUNTIME_ROOT / "runtime-manifest.json").read_text())
        observed = hashlib.sha256(BOOTSTRAP_PATH.read_bytes()).hexdigest()

        self.assertEqual(
            manifest["launch_overlay"]["bootstrap_path"],
            "runpod/bootstrap/ssh/bootstrap.sh",
        )
        self.assertEqual(
            manifest["launch_overlay"]["bootstrap_sha256"],
            observed,
        )

    def test_there_is_no_image_recipe_or_publication_surface(self):
        runpod_root = ROOT / "runpod"
        controlled_roots = (BOOTSTRAP_ROOT, RUNTIME_ROOT)
        controlled_files = [
            path
            for root in controlled_roots
            for path in root.iterdir()
            if path.is_file()
        ]
        combined = "\n".join(path.read_text() for path in controlled_files)

        image_root = runpod_root / "images"
        self.assertFalse(
            image_root.exists()
            and any(path.is_file() for path in image_root.rglob("*"))
        )
        self.assertFalse(
            any(
                path.name == "Dockerfile"
                for root in controlled_roots
                for path in root.rglob("*")
            )
        )
        self.assertNotRegex(combined, re.compile(r"(?m)^\s*FROM\s+"))
        self.assertNotRegex(combined, re.compile(r"\bdocker\s+(?:build|push)\b"))

    def test_assets_are_model_and_credential_agnostic(self):
        controlled_roots = (BOOTSTRAP_ROOT, RUNTIME_ROOT)
        combined = "\n".join(
            path.read_text()
            for root in controlled_roots
            for path in root.iterdir()
            if path.is_file()
        )

        self.assertNotIn("Qwen3.6-27B", combined)
        self.assertNotIn("llmfan46", combined)
        self.assertNotIn("117225df", combined)
        self.assertNotIn("HF_TOKEN=", combined)

    def test_bootstrap_is_tiny_and_installs_only_openssh(self):
        bootstrap = BOOTSTRAP_PATH.read_text()

        self.assertLess(len(bootstrap.encode()), 6 * 1024)
        self.assertIn(
            "apt-get install --yes --no-install-recommends openssh-server",
            bootstrap,
        )
        for forbidden in ("curl ", "git ", "pip ", "uv ", "wget "):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, bootstrap)
        self.assertNotIn("command -v hf", bootstrap)
        self.assertNotIn("command -v vllm", bootstrap)

    def test_bootstrap_never_invokes_rm(self):
        command = re.compile(r"(?:^|[;&|]\s*|\s)rm(?:\s|$)")
        self.assertIsNone(command.search(BOOTSTRAP_PATH.read_text()))

    def test_bootstrap_has_valid_bash_syntax(self):
        subprocess.run(["bash", "-n", str(BOOTSTRAP_PATH)], check=True)

    def test_bootstrap_emits_ordered_machine_parseable_milestones(self):
        bootstrap = BOOTSTRAP_PATH.read_text()
        milestones = (
            "phase=apt-start",
            "phase=apt-complete",
            "phase=authorized-key-ready fingerprint=%s",
            "phase=host-key-ready fingerprint=%s",
            "phase=sshd-ready port=22",
        )

        positions = [bootstrap.index(milestone) for milestone in milestones]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("sleep ", bootstrap)

    def test_ssh_surface_is_key_only_and_local_forwarding_only(self):
        bootstrap = BOOTSTRAP_PATH.read_text()

        self.assertIn("'AuthenticationMethods publickey'", bootstrap)
        self.assertIn("'PasswordAuthentication no'", bootstrap)
        self.assertIn("'KbdInteractiveAuthentication no'", bootstrap)
        self.assertIn("'PermitRootLogin prohibit-password'", bootstrap)
        self.assertIn("'AllowTcpForwarding local'", bootstrap)
        self.assertIn("'GatewayPorts no'", bootstrap)
        self.assertIn("'AllowAgentForwarding no'", bootstrap)
        self.assertIn("'PermitTunnel no'", bootstrap)

    def test_bootstrap_validates_a_container_local_host_identity(self):
        bootstrap = BOOTSTRAP_PATH.read_text()

        self.assertIn(
            "ssh-keygen -q -t ed25519 -N '' -f \"$host_key_path\"",
            bootstrap,
        )
        self.assertIn(
            'mktemp -d -p "$runtime_directory" ssh-host-key.XXXXXX',
            bootstrap,
        )
        self.assertIn(
            'ssh-keygen -l -E sha256 -f "$host_key_path.pub"',
            bootstrap,
        )
        self.assertIn('"HostKey $host_key_path"', bootstrap)
        self.assertNotIn("HostKey /etc/ssh", bootstrap)
        self.assertIn(
            '/usr/sbin/sshd -t -f "$sshd_configuration_path"',
            bootstrap,
        )
        self.assertIn(
            'exec /usr/sbin/sshd -D -e -f "$sshd_configuration_path"',
            bootstrap,
        )

    def test_runtime_verifier_is_copied_in_and_manifest_driven(self):
        verifier = RUNTIME_ROOT / "verify-runtime.py"
        verifier_source = verifier.read_text()
        completed = subprocess.run(
            [sys.executable, str(verifier), "--help"],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("--manifest MANIFEST", completed.stdout)
        self.assertNotIn("/usr/local/share", verifier_source)
        self.assertIn(
            'versions["vllm"].partition("+")[0]',
            verifier_source,
        )


class ReviewedRuntimeCatalogTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.entry = _RUNTIME_CATALOG[RUNTIME_ID]

    def write_catalog(
        self,
        *,
        manifest_bytes: bytes | None = None,
        verifier_bytes: bytes | None = None,
        bootstrap_bytes: bytes | None = None,
    ) -> None:
        manifest_path = self.root / self.entry.manifest_path
        verifier_path = self.root / self.entry.verifier_path
        bootstrap_path = self.root / self.entry.bootstrap_path
        manifest_path.parent.mkdir(parents=True)
        verifier_path.parent.mkdir(parents=True, exist_ok=True)
        bootstrap_path.parent.mkdir(parents=True)
        if manifest_bytes is not None:
            manifest_path.write_bytes(manifest_bytes)
        verifier_path.write_bytes(
            verifier_bytes
            if verifier_bytes is not None
            else (ROOT / self.entry.verifier_path).read_bytes()
        )
        if bootstrap_bytes is not None:
            bootstrap_path.write_bytes(bootstrap_bytes)

    def exact_source_bytes(self) -> tuple[bytes, bytes, bytes]:
        return (
            (ROOT / self.entry.manifest_path).read_bytes(),
            (ROOT / self.entry.verifier_path).read_bytes(),
            (ROOT / self.entry.bootstrap_path).read_bytes(),
        )

    def test_catalog_loads_only_the_reviewed_exact_runtime(self):
        runtime = load_runtime(RUNTIME_ID)

        self.assertEqual(runtime.image, BASE_IMAGE)
        self.assertEqual(runtime.bootstrap_path, self.entry.bootstrap_path)
        self.assertEqual(
            hashlib.sha256(runtime.bootstrap_text.encode()).hexdigest(),
            self.entry.bootstrap_sha256,
        )
        self.assertEqual(
            runtime.template_contract(name="fixture")["volume_in_gb"],
            0,
        )

    def test_unknown_runtime_fails_with_a_typed_error(self):
        for runtime_id in ("unknown", 7, None):
            with self.subTest(runtime_id=runtime_id):
                with self.assertRaises(RunpodLocalError) as caught:
                    load_runtime(runtime_id)
                self.assertEqual(caught.exception.code, "unknown_runtime")

    def test_manifest_and_bootstrap_hash_drift_fail_closed(self):
        manifest, verifier, bootstrap = self.exact_source_bytes()
        for changed_manifest, changed_bootstrap in (
            (manifest + b" ", bootstrap),
            (manifest, bootstrap + b"\n"),
        ):
            with self.subTest(manifest_changed=changed_manifest != manifest):
                root = (
                    self.root
                    / hashlib.sha256(changed_manifest + changed_bootstrap).hexdigest()
                )
                manifest_path = root / self.entry.manifest_path
                bootstrap_path = root / self.entry.bootstrap_path
                verifier_path = root / self.entry.verifier_path
                manifest_path.parent.mkdir(parents=True)
                verifier_path.parent.mkdir(parents=True, exist_ok=True)
                bootstrap_path.parent.mkdir(parents=True)
                manifest_path.write_bytes(changed_manifest)
                verifier_path.write_bytes(verifier)
                bootstrap_path.write_bytes(changed_bootstrap)
                with self.assertRaises(RunpodLocalError) as caught:
                    _load_runtime_entry(root, self.entry)
                self.assertEqual(
                    caught.exception.code,
                    "runtime_catalog_drift",
                )

    def test_schema_and_runtime_field_drift_fail_after_identity_check(self):
        manifest, verifier, bootstrap = self.exact_source_bytes()
        for field, value, error_code in (
            ("schema_version", "other.schema", "runtime_catalog_drift"),
            (
                "image",
                "vllm/vllm-openai@sha256:" + "0" * 64,
                "runtime_catalog_drift",
            ),
        ):
            with self.subTest(field=field):
                document = json.loads(manifest)
                document[field] = value
                changed = json.dumps(
                    document,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                entry = dataclasses.replace(
                    self.entry,
                    manifest_sha256=hashlib.sha256(changed).hexdigest(),
                )
                root = self.root / field
                manifest_path = root / entry.manifest_path
                bootstrap_path = root / entry.bootstrap_path
                verifier_path = root / entry.verifier_path
                manifest_path.parent.mkdir(parents=True)
                verifier_path.parent.mkdir(parents=True, exist_ok=True)
                bootstrap_path.parent.mkdir(parents=True)
                manifest_path.write_bytes(changed)
                verifier_path.write_bytes(verifier)
                bootstrap_path.write_bytes(bootstrap)
                with self.assertRaises(RunpodLocalError) as caught:
                    _load_runtime_entry(root, entry)
                self.assertEqual(caught.exception.code, error_code)

    def test_symlinked_manifest_or_bootstrap_is_never_followed(self):
        manifest, verifier, bootstrap = self.exact_source_bytes()
        for symlink_field in ("manifest", "verifier", "bootstrap"):
            with self.subTest(symlink_field=symlink_field):
                root = self.root / symlink_field
                manifest_path = root / self.entry.manifest_path
                verifier_path = root / self.entry.verifier_path
                bootstrap_path = root / self.entry.bootstrap_path
                manifest_path.parent.mkdir(parents=True)
                verifier_path.parent.mkdir(parents=True, exist_ok=True)
                bootstrap_path.parent.mkdir(parents=True)
                outside = root / f"{symlink_field}.outside"
                if symlink_field == "manifest":
                    outside.write_bytes(manifest)
                    manifest_path.symlink_to(outside)
                    verifier_path.write_bytes(verifier)
                    bootstrap_path.write_bytes(bootstrap)
                elif symlink_field == "verifier":
                    manifest_path.write_bytes(manifest)
                    outside.write_bytes(verifier)
                    verifier_path.symlink_to(outside)
                    bootstrap_path.write_bytes(bootstrap)
                else:
                    manifest_path.write_bytes(manifest)
                    verifier_path.write_bytes(verifier)
                    outside.write_bytes(bootstrap)
                    bootstrap_path.symlink_to(outside)
                with self.assertRaises(RunpodLocalError) as caught:
                    _load_runtime_entry(root, self.entry)
                self.assertEqual(
                    caught.exception.code,
                    "unsafe_runtime_catalog",
                )

    def test_private_primary_group_write_is_accepted_but_world_write_is_not(self):
        manifest, verifier, bootstrap = self.exact_source_bytes()
        self.write_catalog(
            manifest_bytes=manifest,
            verifier_bytes=verifier,
            bootstrap_bytes=bootstrap,
        )
        manifest_path = self.root / self.entry.manifest_path
        verifier_path = self.root / self.entry.verifier_path
        bootstrap_path = self.root / self.entry.bootstrap_path
        manifest_path.chmod(0o664)
        verifier_path.chmod(0o664)
        bootstrap_path.chmod(0o664)
        self.assertEqual(
            _load_runtime_entry(self.root, self.entry).runtime_id,
            RUNTIME_ID,
        )

        bootstrap_path.chmod(0o666)
        with self.assertRaises(RunpodLocalError) as caught:
            _load_runtime_entry(self.root, self.entry)
        self.assertEqual(caught.exception.code, "unsafe_runtime_catalog")

    def test_world_writable_or_symlinked_parent_is_not_a_trusted_source(self):
        manifest, verifier, bootstrap = self.exact_source_bytes()
        world_root = self.root / "world-parent"
        manifest_path = world_root / self.entry.manifest_path
        verifier_path = world_root / self.entry.verifier_path
        bootstrap_path = world_root / self.entry.bootstrap_path
        manifest_path.parent.mkdir(parents=True)
        verifier_path.parent.mkdir(parents=True, exist_ok=True)
        bootstrap_path.parent.mkdir(parents=True)
        manifest_path.write_bytes(manifest)
        verifier_path.write_bytes(verifier)
        bootstrap_path.write_bytes(bootstrap)
        bootstrap_path.parent.chmod(0o777)
        with self.assertRaises(RunpodLocalError) as world_caught:
            _load_runtime_entry(world_root, self.entry)
        self.assertEqual(
            world_caught.exception.code,
            "unsafe_runtime_catalog",
        )

        symlink_root = self.root / "symlink-parent"
        outside_root = self.root / "outside-parent"
        outside_manifest = outside_root / self.entry.manifest_path
        outside_verifier = outside_root / self.entry.verifier_path
        outside_bootstrap = outside_root / self.entry.bootstrap_path
        outside_manifest.parent.mkdir(parents=True)
        outside_verifier.parent.mkdir(parents=True, exist_ok=True)
        outside_bootstrap.parent.mkdir(parents=True)
        outside_manifest.write_bytes(manifest)
        outside_verifier.write_bytes(verifier)
        outside_bootstrap.write_bytes(bootstrap)
        symlink_root.mkdir()
        (symlink_root / "runpod").symlink_to(
            outside_root / "runpod",
            target_is_directory=True,
        )
        with self.assertRaises(RunpodLocalError) as symlink_caught:
            _load_runtime_entry(symlink_root, self.entry)
        self.assertEqual(
            symlink_caught.exception.code,
            "unsafe_runtime_catalog",
        )

    def test_catalog_paths_cannot_escape_the_trusted_source_root(self):
        manifest, verifier, bootstrap = self.exact_source_bytes()
        self.write_catalog(
            manifest_bytes=manifest,
            verifier_bytes=verifier,
            bootstrap_bytes=bootstrap,
        )
        entry = dataclasses.replace(
            self.entry,
            manifest_path="../runtime-manifest.json",
        )
        with self.assertRaises(RunpodLocalError) as caught:
            _load_runtime_entry(self.root, entry)
        self.assertEqual(caught.exception.code, "unsafe_runtime_catalog")


if __name__ == "__main__":
    unittest.main()
