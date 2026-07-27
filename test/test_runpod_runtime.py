from __future__ import annotations

import hashlib
import json
import pathlib
import re
import subprocess
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
BOOTSTRAP_ROOT = ROOT / "runpod" / "bootstrap" / "ssh"
BOOTSTRAP_PATH = BOOTSTRAP_ROOT / "bootstrap.sh"
RUNTIME_ROOT = ROOT / "runpod" / "runtimes" / "vllm-cu129"
BASE_IMAGE = (
    "vllm/vllm-openai@sha256:"
    "fb463d6a216c7ee82bf947f321cae7dd7105bfb5084ea35827c2ceb816994b15"
)


class RunpodUpstreamRuntimeTest(unittest.TestCase):
    def test_runtime_manifest_pins_one_exact_official_image(self):
        manifest = json.loads(
            (RUNTIME_ROOT / "runtime-manifest.json").read_text()
        )

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
        manifest = json.loads(
            (RUNTIME_ROOT / "runtime-manifest.json").read_text()
        )
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
            "/usr/sbin/sshd -t -f \"$sshd_configuration_path\"",
            bootstrap,
        )
        self.assertIn(
            "exec /usr/sbin/sshd -D -e -f \"$sshd_configuration_path\"",
            bootstrap,
        )

    def test_runtime_verifier_is_copied_in_and_manifest_driven(self):
        verifier = RUNTIME_ROOT / "verify-runtime.py"
        completed = subprocess.run(
            [sys.executable, str(verifier), "--help"],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("--manifest MANIFEST", completed.stdout)
        self.assertNotIn("/usr/local/share", verifier.read_text())


if __name__ == "__main__":
    unittest.main()
