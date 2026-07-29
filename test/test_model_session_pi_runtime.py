from __future__ import annotations

import hashlib
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from model_session.errors import ModelSessionError
from model_session.pi_runtime import (
    INFERENCE_RELAY_ROLE,
    PI_INFERENCE_BASE_URL,
    PI_INSTALLATION_IDENTITY_SCHEMA,
    PI_LOCAL_API_KEY,
    PI_MODELS_ROLE,
    SESSION_POLICY_ROLE,
    committed_pi_runtime_assets,
    fingerprint_pi_installation,
    fingerprint_pi_installation_for_root_descriptor,
    generated_pi_configuration_assets,
    parse_pi_installation_identity,
    pi_runtime_assets,
    render_pi_models_json,
)
from model_session.profile import (
    PROFILE_SCHEMA_V2,
    ModelContract,
    PiContract,
    ProfileContract,
    RuntimeContract,
    SandboxContract,
    StorageContract,
)
from model_session.storage_limits import STORAGE_PAGE_SIZE


REVISION = "a" * 40


def make_contract(
    root: pathlib.Path,
    *,
    reasoning: bool = True,
    input_modalities: tuple[str, ...] = ("text", "image"),
) -> ProfileContract:
    installation_root = root / "pi"
    return ProfileContract(
        schema=PROFILE_SCHEMA_V2,
        profile_id="fixture-model",
        project_id="fixture-project",
        profile_root=root / "profile",
        state_root=root / "state",
        project_root=root / "project",
        model=ModelContract(
            repository="example-org/example-model",
            revision=REVISION,
            context_tokens=131072,
            max_output_tokens=16384,
            kv_cache_dtype="bf16",
            max_sequences=1,
            weight_format="bf16",
        ),
        runtime=RuntimeContract(
            provider="fixture-provider",
            model_id="fixture-model-bf16",
            reasoning=reasoning,
            input_modalities=input_modalities,
        ),
        pi=PiContract(
            installation_root=installation_root,
            executable=pathlib.PurePosixPath("bin/pi"),
            version="0.82.1",
            tools=("read", "write", "edit", "bash"),
            system_prompt_file=None,
            append_system_prompt_file=None,
        ),
        storage=StorageContract(
            max_sessions=7,
            work_bytes=8 * 1024**3,
            work_inodes=65_536,
            history_bytes=2 * 1024**3,
            history_inodes=16_384,
            checkpoint_bytes=17 * 1024**3,
            max_sparse_extents=((10 * 1024**3) // STORAGE_PAGE_SIZE),
            max_file_bytes=4 * 1024**3,
            max_logical_bytes=16 * 1024**3,
        ),
        sandbox=SandboxContract(
            memory_bytes=16 * 1024**3,
            max_tasks=256,
            max_runtime_seconds=86_400,
            idle_timeout_seconds=3_600,
            shutdown_grace_seconds=30,
        ),
    )


def create_installation(root: pathlib.Path) -> ProfileContract:
    contract = make_contract(root)
    installation = contract.pi.installation_root
    (installation / "bin").mkdir(parents=True)
    (installation / "lib").mkdir()
    (installation / "lib" / "pi.js").write_bytes(b"console.log('pi');\n")
    (installation / "lib" / "pi.js").chmod(0o755)
    (installation / "bin" / "node").write_bytes(b"#!/bin/sh\necho v24.11.1\n")
    (installation / "bin" / "node").chmod(0o755)
    (installation / "README.md").write_bytes(b"fixture installation\n")
    (installation / "README.md").chmod(0o644)
    (installation / "bin" / "pi").symlink_to("../lib/pi.js")
    for directory in (installation, installation / "bin", installation / "lib"):
        directory.chmod(0o755)
    return contract


class PiRuntimeConfigurationTest(unittest.TestCase):
    def test_models_are_canonical_and_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            contract = make_contract(pathlib.Path(directory))
            models_bytes = render_pi_models_json(contract)

        self.assertTrue(models_bytes.endswith(b"\n"))
        self.assertEqual(models_bytes.count(b"\n"), 1)
        models = json.loads(models_bytes)
        provider = models["providers"]["fixture-provider"]
        self.assertEqual(provider["baseUrl"], PI_INFERENCE_BASE_URL)
        self.assertEqual(provider["api"], "openai-completions")
        self.assertEqual(provider["apiKey"], PI_LOCAL_API_KEY)
        self.assertEqual(
            provider["compat"],
            {
                "supportsDeveloperRole": False,
                "supportsReasoningEffort": False,
            },
        )
        self.assertEqual(
            provider["models"],
            [
                {
                    "contextWindow": 131072,
                    "cost": {
                        "cacheRead": 0,
                        "cacheWrite": 0,
                        "input": 0,
                        "output": 0,
                    },
                    "id": "fixture-model-bf16",
                    "input": ["text", "image"],
                    "maxTokens": 16384,
                    "reasoning": True,
                }
            ],
        )
        self.assertEqual(
            models_bytes,
            json.dumps(
                models,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n",
        )

    def test_generated_configuration_contains_no_secret_resolution_marker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            contract = make_contract(pathlib.Path(directory))
            models = json.loads(render_pi_models_json(contract))

        key = models["providers"]["fixture-provider"]["apiKey"]
        self.assertFalse(key.startswith("!"))
        self.assertNotIn("$", key)

    def test_assets_have_unique_roles_paths_sizes_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            contract = make_contract(pathlib.Path(directory))
            generated = generated_pi_configuration_assets(contract)
            committed = committed_pi_runtime_assets()
            assets = pi_runtime_assets(contract)

        self.assertEqual(
            {asset.roles[0] for asset in generated},
            {PI_MODELS_ROLE},
        )
        self.assertEqual(
            {asset.roles[0] for asset in committed},
            {INFERENCE_RELAY_ROLE, SESSION_POLICY_ROLE},
        )
        committed_by_role = {asset.roles[0]: asset for asset in committed}
        infrastructure_root = pathlib.Path(__file__).resolve().parents[1]
        self.assertEqual(
            committed_by_role[INFERENCE_RELAY_ROLE].content,
            (infrastructure_root / "lib" / "model_session" / "relay.py").read_bytes(),
        )
        self.assertEqual(
            committed_by_role[SESSION_POLICY_ROLE].content,
            (infrastructure_root / "model-session" / "session-policy.js").read_bytes(),
        )
        self.assertEqual(len(assets), 3)
        self.assertEqual(
            len({role for asset in assets for role in asset.roles}),
            3,
        )
        self.assertEqual(
            len({asset.relative_path for asset in assets}),
            3,
        )
        for asset in assets:
            self.assertEqual(asset.size, len(asset.content))
            self.assertEqual(
                asset.sha256,
                hashlib.sha256(asset.content).hexdigest(),
            )
            self.assertTrue(asset.content)


class PiInstallationIdentityTest(unittest.TestCase):
    def test_complete_tree_identity_and_confined_relative_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            contract = create_installation(pathlib.Path(directory))
            first = fingerprint_pi_installation(contract)
            second = fingerprint_pi_installation(contract)

        self.assertEqual(first, second)
        self.assertEqual(first.schema, PI_INSTALLATION_IDENTITY_SCHEMA)
        self.assertEqual(first.entry_count, 7)
        self.assertEqual(first.directory_count, 3)
        self.assertEqual(first.regular_file_count, 3)
        self.assertEqual(first.symlink_count, 1)
        self.assertEqual(
            first.total_bytes,
            len(b"console.log('pi');\n")
            + len(b"fixture installation\n")
            + len(b"#!/bin/sh\necho v24.11.1\n"),
        )
        self.assertRegex(first.sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(first.as_dict()["sha256"], first.sha256)
        self.assertEqual(
            parse_pi_installation_identity(first.as_dict()),
            first,
        )

    def test_locked_identity_parser_rejects_inconsistent_representation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            contract = create_installation(pathlib.Path(directory))
            value = fingerprint_pi_installation(contract).as_dict()

        value["entry_count"] += 1
        with self.assertRaises(ModelSessionError) as caught:
            parse_pi_installation_identity(value)
        self.assertEqual(caught.exception.code, "immutable_snapshot_changed")

    def test_content_change_changes_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            contract = create_installation(root)
            before = fingerprint_pi_installation(contract)
            target = contract.pi.installation_root / "README.md"
            target.write_bytes(b"changed installation\n")
            target.chmod(0o644)
            after = fingerprint_pi_installation(contract)

        self.assertNotEqual(before.sha256, after.sha256)
        self.assertEqual(before.total_bytes, after.total_bytes)

    def test_mode_change_changes_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            contract = create_installation(root)
            before = fingerprint_pi_installation(contract)
            target = contract.pi.installation_root / "README.md"
            target.chmod(0o640)
            after = fingerprint_pi_installation(contract)

        self.assertNotEqual(before.sha256, after.sha256)
        self.assertEqual(before.total_bytes, after.total_bytes)

    def test_absolute_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            contract = create_installation(root)
            (contract.pi.installation_root / "absolute").symlink_to("/etc/passwd")
            with self.assertRaises(ModelSessionError) as caught:
                fingerprint_pi_installation(contract)

        self.assertEqual(caught.exception.code, "unsafe_pi_installation")

    def test_lexically_escaping_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            contract = create_installation(root)
            (contract.pi.installation_root / "bin" / "escape").symlink_to(
                "../../outside"
            )
            with self.assertRaises(ModelSessionError) as caught:
                fingerprint_pi_installation(contract)

        self.assertEqual(caught.exception.code, "unsafe_pi_installation")

    def test_special_file_is_rejected_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            contract = create_installation(root)
            os.mkfifo(contract.pi.installation_root / "named-pipe", 0o600)
            with self.assertRaises(ModelSessionError) as caught:
                fingerprint_pi_installation(contract)

        self.assertEqual(caught.exception.code, "unsafe_pi_installation")

    def test_group_writable_regular_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            contract = create_installation(root)
            target = contract.pi.installation_root / "README.md"
            target.chmod(0o664)
            with self.assertRaises(ModelSessionError) as caught:
                fingerprint_pi_installation(contract)

        self.assertEqual(caught.exception.code, "unsafe_pi_installation")

    def test_hardlinked_regular_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            contract = create_installation(root)
            source = contract.pi.installation_root / "README.md"
            alias = root / "outside-alias.md"
            os.link(source, alias)

            with self.assertRaises(ModelSessionError) as caught:
                fingerprint_pi_installation(contract)
        self.assertEqual(caught.exception.code, "unsafe_pi_installation")

    def test_retained_root_descriptor_is_bound_to_the_fingerprinted_tree(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            contract = create_installation(root)
            installation = contract.pi.installation_root
            descriptor = os.open(
                installation,
                os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            try:
                self.assertEqual(
                    fingerprint_pi_installation_for_root_descriptor(
                        contract,
                        descriptor,
                    ),
                    fingerprint_pi_installation(contract),
                )
                moved = root / "moved-installation"
                installation.rename(moved)
                installation.mkdir(mode=0o755)
                installation.chmod(0o755)
                with self.assertRaises(ModelSessionError) as caught:
                    fingerprint_pi_installation_for_root_descriptor(
                        contract,
                        descriptor,
                    )
            finally:
                os.close(descriptor)
        self.assertEqual(caught.exception.code, "pi_installation_changed")

    def test_file_mutation_during_scan_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            contract = create_installation(root)
            target = contract.pi.installation_root / "README.md"
            real_read = os.read
            changed = False

            def mutate_after_read(
                descriptor: int,
                count: int,
            ) -> bytes:
                nonlocal changed
                content = real_read(descriptor, count)
                if not changed:
                    changed = True
                    target.write_bytes(b"mutated during scan\n")
                    target.chmod(0o644)
                return content

            with mock.patch(
                "model_session.pi_runtime.os.read",
                side_effect=mutate_after_read,
            ):
                with self.assertRaises(ModelSessionError) as caught:
                    fingerprint_pi_installation(contract)

        self.assertEqual(caught.exception.code, "pi_installation_changed")

    def test_already_scanned_file_mutation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            contract = create_installation(root)
            target = contract.pi.installation_root / "README.md"
            real_open = os.open
            changed = False

            def mutate_when_later_directory_opens(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal changed
                descriptor = real_open(
                    path,
                    flags,
                    mode,
                    dir_fd=dir_fd,
                )
                if path == "bin" and dir_fd is not None and not changed:
                    changed = True
                    target.write_bytes(b"mutated after its scan\n")
                    target.chmod(0o644)
                return descriptor

            with mock.patch(
                "model_session.pi_runtime.os.open",
                side_effect=mutate_when_later_directory_opens,
            ):
                with self.assertRaises(ModelSessionError) as caught:
                    fingerprint_pi_installation(contract)

        self.assertTrue(changed)
        self.assertEqual(caught.exception.code, "pi_installation_changed")


if __name__ == "__main__":
    unittest.main()
