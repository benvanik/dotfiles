from __future__ import annotations

import hashlib
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import model_session.runs as runs_module
from model_session import (
    ModelSessionError,
    load_profile,
    load_run,
    load_run_from_state,
    materialize_new_run,
)


REVISION = "a" * 40


class ProfileFixture:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.profile_root = root / "profiles" / "fixture-model"
        self.state_root = root / "state"
        self.project_root = root / "project"
        self.pi_root = root / "pi-0.82.1"
        self.profile_root.mkdir(parents=True)
        self.profile_root.chmod(0o755)
        self.project_root.mkdir()
        (self.pi_root / "bin").mkdir(parents=True)
        target = self.pi_root / "lib" / "node_modules" / "pi" / "cli.js"
        target.parent.mkdir(parents=True)
        target.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        target.chmod(0o755)
        for pi_directory in (
            self.pi_root,
            self.pi_root / "bin",
            self.pi_root / "lib",
            self.pi_root / "lib" / "node_modules",
            target.parent,
        ):
            pi_directory.chmod(0o755)
        (self.pi_root / "bin" / "pi").symlink_to(
            pathlib.Path("../lib/node_modules/pi/cli.js")
        )
        (self.profile_root / "AGENTS.md").write_text(
            "fixture agents v1\n",
            encoding="utf-8",
        )
        (self.profile_root / "AGENTS.md").chmod(0o644)
        (self.profile_root / "SYSTEM.md").write_text(
            "fixture system v1\n",
            encoding="utf-8",
        )
        (self.profile_root / "SYSTEM.md").chmod(0o644)
        self.write_profile()

    def document(self, **overrides: object) -> str:
        values: dict[str, object] = {
            "schema": "model-session.profile.v1",
            "profile_id": "fixture-model",
            "project_id": "fixture-project",
            "state_root": str(self.state_root),
            "project_root": str(self.project_root),
            "repository": "example-org/example-model",
            "revision": REVISION,
            "context_tokens": 65536,
            "max_output_tokens": 8192,
            "kv_cache_dtype": "bf16",
            "max_sequences": 1,
            "weight_format": "bf16",
            "provider": "fixture-provider",
            "model_id": "fixture-model-bf16",
            "reasoning": "false",
            "input_modalities": '["text"]',
            "installation_root": str(self.pi_root),
            "executable": "bin/pi",
            "version": "0.82.1",
            "tools": '["read", "write", "edit", "bash"]',
            "system_prompt_line": 'system_prompt_file = "SYSTEM.md"',
            "extra": "",
        }
        values.update(overrides)
        return f"""schema = "{values["schema"]}"
profile_id = "{values["profile_id"]}"
project_id = "{values["project_id"]}"
state_root = "{values["state_root"]}"
project_root = "{values["project_root"]}"
{values["extra"]}
[model]
repository = "{values["repository"]}"
revision = "{values["revision"]}"
context_tokens = {values["context_tokens"]}
max_output_tokens = {values["max_output_tokens"]}
kv_cache_dtype = "{values["kv_cache_dtype"]}"
max_sequences = {values["max_sequences"]}
weight_format = "{values["weight_format"]}"

[runtime]
provider = "{values["provider"]}"
model_id = "{values["model_id"]}"
reasoning = {values["reasoning"]}
input_modalities = {values["input_modalities"]}

[pi]
installation_root = "{values["installation_root"]}"
executable = "{values["executable"]}"
version = "{values["version"]}"
tools = {values["tools"]}
{values["system_prompt_line"]}
"""

    def write_profile(self, **overrides: object) -> None:
        (self.profile_root / "profile.toml").write_text(
            self.document(**overrides),
            encoding="utf-8",
        )
        (self.profile_root / "profile.toml").chmod(0o644)


class ModelSessionProfileTest(unittest.TestCase):
    def fixture(self, directory: str) -> ProfileFixture:
        return ProfileFixture(pathlib.Path(directory))

    def test_valid_profile_pins_model_runtime_pi_and_resources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            profile = load_profile(fixture.profile_root)

            self.assertEqual(profile.contract.profile_id, "fixture-model")
            self.assertEqual(profile.contract.project_id, "fixture-project")
            self.assertEqual(profile.contract.model.revision, REVISION)
            self.assertEqual(profile.contract.model.context_tokens, 65536)
            self.assertEqual(profile.contract.model.max_output_tokens, 8192)
            self.assertEqual(profile.contract.model.kv_cache_dtype, "bf16")
            self.assertEqual(profile.contract.model.max_sequences, 1)
            self.assertEqual(profile.contract.model.weight_format, "bf16")
            self.assertEqual(
                profile.contract.runtime.provider,
                "fixture-provider",
            )
            self.assertEqual(
                profile.contract.runtime.model_id,
                "fixture-model-bf16",
            )
            self.assertFalse(profile.contract.runtime.reasoning)
            self.assertEqual(
                profile.contract.runtime.input_modalities,
                ("text",),
            )
            self.assertEqual(profile.contract.pi.executable.as_posix(), "bin/pi")
            self.assertEqual(profile.contract.pi.version, "0.82.1")
            self.assertEqual(
                profile.contract.pi.tools,
                ("read", "write", "edit", "bash"),
            )
            self.assertEqual(
                profile.resource_for_role("agents").content,
                b"fixture agents v1\n",
            )
            self.assertEqual(
                profile.resource_for_role("system_prompt").content,
                b"fixture system v1\n",
            )
            self.assertFalse(fixture.state_root.exists())

    def test_profile_contract_rejects_mutable_or_ambiguous_authority(self) -> None:
        cases = (
            ({"revision": "main"}, "invalid_profile"),
            ({"max_output_tokens": 65537}, "invalid_profile"),
            ({"tools": "[]"}, "invalid_profile"),
            ({"tools": '["read", "network"]'}, "invalid_profile"),
            ({"tools": '["read", "read"]'}, "invalid_profile"),
            ({"weight_format": "openai-chat"}, "invalid_profile"),
            ({"kv_cache_dtype": "int8"}, "invalid_profile"),
            ({"max_sequences": 0}, "invalid_profile"),
            ({"reasoning": '"false"'}, "invalid_profile"),
            ({"input_modalities": '["image"]'}, "invalid_profile"),
            ({"input_modalities": '["text", "text"]'}, "invalid_profile"),
            ({"state_root": "relative/state"}, "invalid_profile"),
            ({"extra": 'api_key = "literal-secret"'}, "secret_field_rejected"),
        )
        for overrides, code in cases:
            with self.subTest(overrides=overrides):
                with tempfile.TemporaryDirectory() as directory:
                    fixture = self.fixture(directory)
                    fixture.write_profile(**overrides)
                    with self.assertRaises(ModelSessionError) as caught:
                        load_profile(fixture.profile_root)
                    self.assertEqual(caught.exception.code, code)

    def test_profile_rejects_dotfiles_containment_and_root_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            fake_dotfiles = fixture.root / "profiles"
            with mock.patch(
                "model_session.profile.infrastructure_root",
                return_value=fake_dotfiles,
            ):
                with self.assertRaises(ModelSessionError) as caught:
                    load_profile(fixture.profile_root)
            self.assertEqual(caught.exception.code, "profile_inside_infrastructure")

        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            fixture.write_profile(state_root=str(fixture.project_root / "state"))
            with self.assertRaises(ModelSessionError) as caught:
                load_profile(fixture.profile_root)
            self.assertEqual(caught.exception.code, "overlapping_profile_paths")

        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            fixture.write_profile(state_root="/")
            with self.assertRaises(ModelSessionError) as caught:
                load_profile(fixture.profile_root)
            self.assertEqual(caught.exception.code, "unsafe_profile_path")

    def test_explicit_state_root_under_home_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            fixture.state_root = fixture.root / ".local" / "model-sessions"
            fixture.write_profile()
            with mock.patch("pathlib.Path.home", return_value=fixture.root):
                profile = load_profile(fixture.profile_root)
            self.assertEqual(profile.contract.state_root, fixture.state_root)

    def test_agents_must_be_an_owned_regular_non_symlink_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            agents = fixture.profile_root / "AGENTS.md"
            agents.unlink()
            agents.symlink_to("SYSTEM.md")
            with self.assertRaises(ModelSessionError) as caught:
                load_profile(fixture.profile_root)
            self.assertEqual(caught.exception.code, "unsafe_profile_resource")

        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            agents = fixture.profile_root / "AGENTS.md"
            agents.unlink()
            os.mkfifo(agents, mode=0o600)
            with self.assertRaises(ModelSessionError) as caught:
                load_profile(fixture.profile_root)
            self.assertEqual(caught.exception.code, "unsafe_profile_resource")

        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            agents = fixture.profile_root / "AGENTS.md"
            agents.unlink()
            agents.mkdir()
            with self.assertRaises(ModelSessionError) as caught:
                load_profile(fixture.profile_root)
            self.assertEqual(caught.exception.code, "unsafe_profile_resource")

    def test_pi_symlink_must_resolve_inside_a_nonwritable_installation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            external = fixture.root / "external-pi"
            external.write_text("#!/bin/sh\n", encoding="utf-8")
            external.chmod(0o755)
            executable = fixture.pi_root / "bin" / "pi"
            executable.unlink()
            executable.symlink_to(external)
            with self.assertRaises(ModelSessionError) as caught:
                load_profile(fixture.profile_root)
            self.assertEqual(caught.exception.code, "unsafe_pi_installation")

        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            executable = fixture.pi_root / "bin" / "pi"
            target = (
                fixture.pi_root
                / "lib"
                / "node_modules"
                / "pi"
                / "cli.js"
            )
            executable.unlink()
            executable.symlink_to(target)
            with self.assertRaises(ModelSessionError) as caught:
                load_profile(fixture.profile_root)
            self.assertEqual(caught.exception.code, "unsafe_pi_installation")

        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            fixture.pi_root.chmod(0o775)
            with self.assertRaises(ModelSessionError) as caught:
                load_profile(fixture.profile_root)
            self.assertEqual(caught.exception.code, "unsafe_pi_installation")

        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            (fixture.pi_root / "lib" / "node_modules").chmod(0o775)
            with self.assertRaises(ModelSessionError) as caught:
                load_profile(fixture.profile_root)
            self.assertEqual(caught.exception.code, "unsafe_pi_installation")

    def test_new_run_is_private_locked_and_never_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            profile = load_profile(fixture.profile_root)
            first = materialize_new_run(profile)
            second = materialize_new_run(profile)

            self.assertNotEqual(first.session_id, second.session_id)
            self.assertNotEqual(first.root, second.root)
            for path in (
                fixture.state_root,
                first.root,
                first.snapshot_root,
                first.workspace,
                first.workspace / ".pi",
                first.pi_home,
                first.pi_config,
                first.pi_sessions,
                first.report_directory,
                first.memory_directory,
            ):
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            for path in (
                first.root / "run.json",
                first.snapshot_root / "lock.json",
                first.snapshot_root / "profile" / "profile.toml",
                first.snapshot_root / "profile" / "AGENTS.md",
                first.workspace / "AGENTS.md",
            ):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                (first.workspace / "AGENTS.md").read_bytes(),
                b"fixture agents v1\n",
            )
            self.assertEqual(tuple((first.workspace / ".pi").iterdir()), ())
            resumed = load_run(profile, first.session_id)
            self.assertEqual(resumed.root, first.root)
            self.assertEqual(resumed.profile.model.revision, REVISION)

    def test_new_run_recovers_an_unpublished_crash_staging_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            profile = load_profile(fixture.profile_root)
            materialize_new_run(profile)
            orphan_id = "20000101T000000000000Z-0000000000000000"
            profile_sessions = (
                fixture.state_root / "sessions" / "fixture-model"
            )
            staging = profile_sessions / f".creating-{orphan_id}"
            staging.mkdir(mode=0o700)
            staging.chmod(0o700)
            orphan_report = fixture.project_root / "reports" / orphan_id
            orphan_memory = fixture.project_root / "memory" / orphan_id
            orphan_report.mkdir(mode=0o700)
            orphan_memory.mkdir(mode=0o700)
            orphan_report.chmod(0o700)
            orphan_memory.chmod(0o700)

            materialize_new_run(profile)
            self.assertFalse(staging.exists())
            self.assertFalse(orphan_report.exists())
            self.assertFalse(orphan_memory.exists())

    def test_crash_recovery_preserves_unrecognized_staging_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            profile = load_profile(fixture.profile_root)
            materialize_new_run(profile)
            orphan_id = "20000101T000000000000Z-0000000000000000"
            profile_sessions = (
                fixture.state_root / "sessions" / "fixture-model"
            )
            staging = profile_sessions / f".creating-{orphan_id}"
            staging.mkdir(mode=0o700)
            staging.chmod(0o700)
            retained = staging / "retained.txt"
            retained.write_text("not proven disposable\n", encoding="utf-8")
            retained.chmod(0o600)

            with self.assertRaises(ModelSessionError) as caught:
                materialize_new_run(profile)
            self.assertEqual(
                caught.exception.code,
                "session_recovery_required",
            )
            self.assertEqual(
                retained.read_text(encoding="utf-8"),
                "not proven disposable\n",
            )

    def test_crash_recovery_refuses_to_delete_nonempty_project_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            profile = load_profile(fixture.profile_root)
            materialize_new_run(profile)
            orphan_id = "20000101T000000000000Z-0000000000000000"
            profile_sessions = (
                fixture.state_root / "sessions" / "fixture-model"
            )
            staging = profile_sessions / f".creating-{orphan_id}"
            staging.mkdir(mode=0o700)
            staging.chmod(0o700)
            orphan_report = fixture.project_root / "reports" / orphan_id
            orphan_report.mkdir(mode=0o700)
            orphan_report.chmod(0o700)
            retained = orphan_report / "retained.txt"
            retained.write_text("do not delete\n", encoding="utf-8")

            with self.assertRaises(ModelSessionError) as caught:
                materialize_new_run(profile)
            self.assertEqual(
                caught.exception.code,
                "session_recovery_required",
            )
            self.assertTrue(staging.is_dir())
            self.assertEqual(
                retained.read_text(encoding="utf-8"),
                "do not delete\n",
            )

    def test_staging_entry_is_fsynced_before_project_state_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            profile = load_profile(fixture.profile_root)
            events: list[str] = []
            staging_creation_started = False
            real_create = runs_module._create_private_child_directory
            real_fsync = runs_module.os.fsync
            real_make_project = runs_module._make_project_session_directories

            def create(*args: object, **kwargs: object) -> int:
                nonlocal staging_creation_started
                if kwargs.get("label") == "session staging directory":
                    staging_creation_started = True
                    events.append("staging-create")
                return real_create(*args, **kwargs)

            def fsync(descriptor: int) -> None:
                real_fsync(descriptor)
                if staging_creation_started and "project-create" not in events:
                    events.append("staging-parent-fsync")

            def make_project(*args: object, **kwargs: object) -> object:
                events.append("project-create")
                return real_make_project(*args, **kwargs)

            with (
                mock.patch.object(
                    runs_module,
                    "_create_private_child_directory",
                    side_effect=create,
                ),
                mock.patch.object(runs_module.os, "fsync", side_effect=fsync),
                mock.patch.object(
                    runs_module,
                    "_make_project_session_directories",
                    side_effect=make_project,
                ),
            ):
                materialize_new_run(profile)

            self.assertLess(
                events.index("staging-create"),
                events.index("staging-parent-fsync"),
            )
            self.assertLess(
                events.index("staging-parent-fsync"),
                events.index("project-create"),
            )

    def test_materialization_rejects_a_replaced_state_path_component(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            fixture.state_root = fixture.root / "state-route" / "state"
            fixture.write_profile()
            profile = load_profile(fixture.profile_root)
            redirected = fixture.root / "redirected-state"
            redirected.mkdir(mode=0o700)
            (fixture.root / "state-route").symlink_to(
                redirected,
                target_is_directory=True,
            )

            with self.assertRaises(ModelSessionError) as caught:
                materialize_new_run(profile)
            self.assertEqual(caught.exception.code, "unsafe_session_state")
            self.assertFalse((redirected / "state").exists())
            self.assertFalse((fixture.project_root / "reports").exists())
            self.assertFalse((fixture.project_root / "memory").exists())

    def test_materialization_rejects_a_replaced_project_path_component(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            project_route = fixture.root / "project-route"
            fixture.project_root = project_route / "project"
            fixture.project_root.mkdir(parents=True)
            fixture.write_profile()
            profile = load_profile(fixture.profile_root)

            original_route = fixture.root / "original-project-route"
            project_route.rename(original_route)
            redirected_route = fixture.root / "redirected-project-route"
            (redirected_route / "project").mkdir(parents=True)
            project_route.symlink_to(redirected_route, target_is_directory=True)

            with self.assertRaises(ModelSessionError) as caught:
                materialize_new_run(profile)
            self.assertEqual(caught.exception.code, "unsafe_session_state")
            self.assertFalse(
                (redirected_route / "project" / "reports").exists()
            )
            self.assertFalse(
                (redirected_route / "project" / "memory").exists()
            )

    def test_resume_rejects_a_symlinked_profile_session_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            profile = load_profile(fixture.profile_root)
            run = materialize_new_run(profile)
            profile_sessions = (
                fixture.state_root / "sessions" / "fixture-model"
            )
            moved = fixture.root / "moved-profile-sessions"
            profile_sessions.rename(moved)
            profile_sessions.symlink_to(moved, target_is_directory=True)

            with self.assertRaises(ModelSessionError) as caught:
                load_run_from_state(
                    fixture.state_root,
                    "fixture-model",
                    run.session_id,
                )
            self.assertEqual(caught.exception.code, "unsafe_session_state")

    def test_post_publication_validation_failure_names_durable_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            profile = load_profile(fixture.profile_root)
            validation_error = ModelSessionError(
                "injected persisted-state failure",
                code="invalid_session_state",
            )
            with mock.patch.object(
                runs_module,
                "load_run",
                side_effect=validation_error,
            ):
                with self.assertRaises(ModelSessionError) as caught:
                    materialize_new_run(profile)

            self.assertEqual(
                caught.exception.code,
                "published_session_requires_recovery",
            )
            profile_sessions = (
                fixture.state_root / "sessions" / "fixture-model"
            )
            published = tuple(
                entry
                for entry in profile_sessions.iterdir()
                if not entry.name.startswith(".creating-")
            )
            self.assertEqual(len(published), 1)
            self.assertIn(str(published[0]), str(caught.exception))
            self.assertTrue((published[0] / "run.json").is_file())

    def test_post_publication_fsync_failure_reports_unknown_durability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            profile = load_profile(fixture.profile_root)
            renamed = False
            real_rename = runs_module.os.rename
            real_fsync = runs_module.os.fsync

            def rename(*args: object, **kwargs: object) -> None:
                nonlocal renamed
                real_rename(*args, **kwargs)
                renamed = True

            def fsync(descriptor: int) -> None:
                if renamed:
                    raise OSError("injected parent fsync failure")
                real_fsync(descriptor)

            with (
                mock.patch.object(
                    runs_module.os,
                    "rename",
                    side_effect=rename,
                ),
                mock.patch.object(
                    runs_module.os,
                    "fsync",
                    side_effect=fsync,
                ),
            ):
                with self.assertRaises(ModelSessionError) as caught:
                    materialize_new_run(profile)

            self.assertEqual(
                caught.exception.code,
                "published_session_durability_unknown",
            )
            profile_sessions = (
                fixture.state_root / "sessions" / "fixture-model"
            )
            published = tuple(
                entry
                for entry in profile_sessions.iterdir()
                if not entry.name.startswith(".creating-")
            )
            self.assertEqual(len(published), 1)
            self.assertIn(str(published[0]), str(caught.exception))

    def test_canonical_edits_only_affect_new_runs_and_workspace_is_local(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            original_profile = load_profile(fixture.profile_root)
            first = materialize_new_run(original_profile)
            (first.workspace / "AGENTS.md").write_text(
                "session-local agents\n",
                encoding="utf-8",
            )
            (fixture.profile_root / "AGENTS.md").write_text(
                "fixture agents v2\n",
                encoding="utf-8",
            )
            (fixture.profile_root / "SYSTEM.md").write_text(
                "fixture system v2\n",
                encoding="utf-8",
            )
            updated_profile = load_profile(fixture.profile_root)

            resumed = load_run(updated_profile, first.session_id)
            self.assertEqual(
                (resumed.workspace / "AGENTS.md").read_bytes(),
                b"session-local agents\n",
            )
            self.assertEqual(
                resumed.resource_for_role("agents").path.read_bytes(),
                b"fixture agents v1\n",
            )
            self.assertEqual(
                resumed.resource_for_role("system_prompt").path.read_bytes(),
                b"fixture system v1\n",
            )

            second = materialize_new_run(updated_profile)
            self.assertEqual(
                second.resource_for_role("agents").path.read_bytes(),
                b"fixture agents v2\n",
            )
            self.assertEqual(
                second.resource_for_role("system_prompt").path.read_bytes(),
                b"fixture system v2\n",
            )

    def test_resume_uses_snapshot_after_canonical_profile_moves(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            profile = load_profile(fixture.profile_root)
            run = materialize_new_run(profile)
            fixture.profile_root.rename(fixture.root / "profile-moved-away")

            resumed = load_run_from_state(
                fixture.state_root,
                "fixture-model",
                run.session_id,
            )
            self.assertEqual(resumed.session_id, run.session_id)
            self.assertEqual(
                resumed.resource_for_role("agents").path.read_bytes(),
                b"fixture agents v1\n",
            )

    def test_resume_treats_replaced_canonical_profile_path_as_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            profile = load_profile(fixture.profile_root)
            run = materialize_new_run(profile)
            moved = fixture.root / "profile-moved-away"
            fixture.profile_root.rename(moved)
            fixture.profile_root.symlink_to(moved, target_is_directory=True)

            resumed = load_run_from_state(
                fixture.state_root,
                "fixture-model",
                run.session_id,
            )
            self.assertEqual(resumed.session_id, run.session_id)
            self.assertEqual(
                resumed.profile.profile_root,
                fixture.profile_root,
            )
            self.assertEqual(
                resumed.resource_for_role("system_prompt").path.read_bytes(),
                b"fixture system v1\n",
            )

    def test_resume_rejects_immutable_snapshot_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            profile = load_profile(fixture.profile_root)
            run = materialize_new_run(profile)
            agents = run.snapshot_root / "profile" / "AGENTS.md"
            agents.write_text("tampered\n", encoding="utf-8")
            agents.chmod(0o600)

            with self.assertRaises(ModelSessionError) as caught:
                load_run(profile, run.session_id)
            self.assertEqual(caught.exception.code, "immutable_snapshot_changed")

    def test_resume_rejects_layout_symlinks_but_allows_workspace_edits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            profile = load_profile(fixture.profile_root)
            run = materialize_new_run(profile)
            workspace_agents = run.workspace / "AGENTS.md"
            workspace_agents.unlink()
            workspace_agents.symlink_to(run.snapshot_root / "profile" / "AGENTS.md")

            with self.assertRaises(ModelSessionError) as caught:
                load_run(profile, run.session_id)
            self.assertEqual(caught.exception.code, "unsafe_session_state")

    def test_resume_rejects_special_state_files_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            profile = load_profile(fixture.profile_root)
            run = materialize_new_run(profile)
            receipt = run.root / "run.json"
            receipt.unlink()
            os.mkfifo(receipt, mode=0o600)

            with self.assertRaises(ModelSessionError) as caught:
                load_run(profile, run.session_id)
            self.assertEqual(caught.exception.code, "unsafe_session_state")

        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            profile = load_profile(fixture.profile_root)
            run = materialize_new_run(profile)
            injected = run.workspace / ".pi" / "settings.json"
            injected.write_text("{}\n", encoding="utf-8")
            injected.chmod(0o600)

            with self.assertRaises(ModelSessionError) as caught:
                load_run(profile, run.session_id)
            self.assertEqual(caught.exception.code, "unsafe_session_state")

    def test_lock_manifest_is_bound_to_the_run_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            profile = load_profile(fixture.profile_root)
            run = materialize_new_run(profile)
            receipt_path = run.root / "run.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["lock_sha256"] = "0" * 64
            receipt_path.write_text(
                json.dumps(receipt, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            receipt_path.chmod(0o600)

            with self.assertRaises(ModelSessionError) as caught:
                load_run(profile, run.session_id)
            self.assertEqual(caught.exception.code, "immutable_snapshot_changed")

    def test_locked_resource_roles_are_bound_to_profile_toml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            profile = load_profile(fixture.profile_root)
            run = materialize_new_run(profile)
            manifest_path = run.snapshot_root / "lock.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            agents = next(
                resource
                for resource in manifest["resources"]
                if resource["path"] == "profile/AGENTS.md"
            )
            agents["roles"].append("append_system_prompt")
            manifest_bytes = (
                json.dumps(
                    manifest,
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                )
                + "\n"
            ).encode("utf-8")
            manifest_path.write_bytes(manifest_bytes)
            manifest_path.chmod(0o600)

            receipt_path = run.root / "run.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["lock_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
            receipt_path.write_text(
                json.dumps(receipt, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            receipt_path.chmod(0o600)

            with self.assertRaises(ModelSessionError) as caught:
                load_run_from_state(
                    fixture.state_root,
                    "fixture-model",
                    run.session_id,
                )
            self.assertEqual(caught.exception.code, "immutable_snapshot_changed")


if __name__ == "__main__":
    unittest.main()
