from __future__ import annotations

import fcntl
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from model_lab.errors import ModelLabError
from model_lab.migration import (
    MigrationPolicy,
    _tree_sha256,
    migrate_legacy_profile,
)
from model_lab.profile_binding import ProfileBindingStore
from model_session.attachment import (
    ServiceEndpointBinding,
    ServiceWorkload,
)
from model_session.history import enumerate_history
from model_session.lease import acquire_run_from_state
from model_session.materialization import (
    materialize_legacy_run_for_migration,
)
from model_session.profile import (
    SandboxContract,
    StorageContract,
    load_legacy_profile_for_migration,
    load_profile,
)
from model_session.runs import LOCK_SCHEMA_V2, load_run_from_state
from model_session.service_endpoint import service_workload_identity


REVISION = "a" * 40


def _directory(path: pathlib.Path, mode: int = 0o700) -> pathlib.Path:
    path.mkdir(mode=mode, parents=True)
    path.chmod(mode)
    return path


def _file(path: pathlib.Path, content: str) -> pathlib.Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path


def _policy() -> MigrationPolicy:
    storage = StorageContract(
        max_sessions=7,
        work_bytes=8_589_934_592,
        work_inodes=65_536,
        history_bytes=2_147_483_648,
        history_inodes=16_384,
        checkpoint_bytes=18_253_611_008,
        max_sparse_extents=2_621_440,
        max_file_bytes=4_294_967_296,
        max_logical_bytes=17_179_869_184,
    )
    sandbox = SandboxContract(
        memory_bytes=17_179_869_184,
        max_tasks=256,
        max_runtime_seconds=86_400,
        idle_timeout_seconds=3_600,
        shutdown_grace_seconds=30,
    )
    return MigrationPolicy(storage=storage, sandbox=sandbox)


class LegacyFixture:
    def __init__(self, root: pathlib.Path, *, schema: str = "v2") -> None:
        self.root = root
        self.profile_root = _directory(root / "profiles" / "legacy")
        self.state_root = root / "state"
        self.project_root = _directory(root / "project")
        self.pi_root = _directory(root / "pi", 0o755)
        pi_bin = _directory(self.pi_root / "bin", 0o755)
        _file(pi_bin / "pi", "#!/bin/sh\nexit 0\n").chmod(0o755)
        _file(pi_bin / "node", "#!/bin/sh\necho v24.11.1\n").chmod(0o755)
        _file(self.profile_root / "AGENTS.md", "legacy agents\n")
        _file(self.profile_root / "SYSTEM.md", "legacy system\n")
        self.schema = schema
        self.write_profile(("text",))

    def write_profile(self, modalities: tuple[str, ...]) -> None:
        storage = ""
        if self.schema == "v2":
            storage = """
[storage]
max_sessions = 7
work_bytes = 8589934592
work_inodes = 65536
history_bytes = 2147483648
history_inodes = 16384
checkpoint_bytes = 18253611008
max_file_bytes = 4294967296
max_logical_bytes = 17179869184

[sandbox]
memory_bytes = 17179869184
max_tasks = 256
max_runtime_seconds = 86400
idle_timeout_seconds = 3600
shutdown_grace_seconds = 30
"""
        modalities_toml = ", ".join(json.dumps(modality) for modality in modalities)
        document = f"""schema = "model-session.profile.{self.schema}"
profile_id = "legacy"
project_id = "legacy-project"
state_root = "{self.state_root}"
project_root = "{self.project_root}"

[model]
repository = "namespace/model"
revision = "{REVISION}"
context_tokens = 65536
max_output_tokens = 8192
kv_cache_dtype = "bf16"
max_sequences = 2
weight_format = "bf16"

[runtime]
provider = "fixture-provider"
model_id = "served-model"
reasoning = false
input_modalities = [{modalities_toml}]

[pi]
installation_root = "{self.pi_root}"
executable = "bin/pi"
version = "0.82.1"
tools = ["read", "write", "edit", "bash"]
system_prompt_file = "SYSTEM.md"
{storage}"""
        _file(self.profile_root / "profile.toml", document)

    def profile(self):
        return load_legacy_profile_for_migration(self.profile_root)

    def new_run(
        self,
        *,
        marker: str,
        history_timestamp: str,
        history_mtime_ns: int,
    ):
        run = materialize_legacy_run_for_migration(self.profile())
        _file(run.workspace / f"{marker}.txt", f"workspace {marker}\n")
        pi_name = (
            history_timestamp.replace(":", "-").replace(".", "-")
            + f"_{run.session_id}.jsonl"
        )
        history = _file(
            run.pi_sessions / pi_name,
            json.dumps(
                {
                    "type": "session",
                    "version": 3,
                    "id": run.session_id,
                    "timestamp": history_timestamp,
                    "cwd": "/workspace",
                }
            )
            + "\n",
        )
        history.touch()
        history.chmod(0o600)
        os.utime(
            history,
            ns=(history_mtime_ns, history_mtime_ns),
        )
        _file(run.report_directory / f"{marker}.md", f"report {marker}\n")
        _file(run.memory_directory / f"{marker}.md", f"memory {marker}\n")
        return run, history

    @staticmethod
    def binding() -> ServiceEndpointBinding:
        workload = ServiceWorkload(
            repository="namespace/model",
            revision=REVISION,
            provider="fixture-provider",
            model_id="served-model",
            context_tokens=65_536,
            max_output_tokens=8_192,
            weight_format="bf16",
            kv_cache_dtype="bf16",
            runtime_compatibility="fixture-runtime-v1",
            reasoning=False,
        )
        return ServiceEndpointBinding(
            service_id="fixture-service",
            service_sha256="b" * 64,
            workload=workload,
            workload_sha256=service_workload_identity(workload),
            input_modalities=("text", "image"),
        )


class ModelLabMigrationTest(unittest.TestCase):
    def test_migrates_history_and_rebuilds_v3_service_envelopes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fixture = LegacyFixture(_directory(root / "source"))
            first, first_history = fixture.new_run(
                marker="first",
                history_timestamp="2026-07-27T01:02:03.004Z",
                history_mtime_ns=2_000_000_000_123_456_789,
            )
            source_sparse = first.workspace / "sparse.bin"
            with source_sparse.open("wb") as sparse:
                sparse.seek(4 * 1024 * 1024)
                sparse.write(b"end")
            source_sparse.chmod(0o600)
            recovered_root = _directory(fixture.state_root / "recovered")
            recovered_profile = _directory(recovered_root / "legacy")
            recovered = _directory(recovered_profile / first.session_id)
            _file(recovered / "recovered.jsonl", '{"recovered":true}\n')
            fixture.write_profile(("text", "image"))
            second, _ = fixture.new_run(
                marker="second",
                history_timestamp="2026-07-27T02:03:04.005Z",
                history_mtime_ns=2_000_000_001_123_456_789,
            )
            destination = root / "model-lab"
            source_hashes = {
                "state": _tree_sha256(fixture.state_root),
                "project": _tree_sha256(fixture.project_root),
                "profile": _tree_sha256(fixture.profile_root),
                "pi": _tree_sha256(fixture.pi_root),
            }

            result = migrate_legacy_profile(
                fixture.profile_root,
                destination,
                service_binding=fixture.binding(),
                target_profile_id="chat",
                target_project_id="playground",
            )

            self.assertEqual(result.profile_id, "chat")
            self.assertEqual(result.project_id, "playground")
            self.assertEqual(
                {run.session_id for run in result.runs},
                {first.session_id, second.session_id},
            )
            profile = load_profile(destination / "profiles" / "chat")
            self.assertEqual(profile.contract.service_id, "fixture-service")
            self.assertEqual(
                profile.contract.endpoint.required_input_modalities,
                ("text", "image"),
            )
            migrated_first = load_run_from_state(
                destination,
                "chat",
                first.session_id,
            )
            migrated_second = load_run_from_state(
                destination,
                "chat",
                second.session_id,
            )
            self.assertEqual(
                migrated_first.service_binding.input_modalities,
                ("text",),
            )
            self.assertEqual(
                migrated_second.service_binding.input_modalities,
                ("text", "image"),
            )
            for migrated in (migrated_first, migrated_second):
                lock = json.loads(
                    (migrated.snapshot_root / "lock.json").read_text(encoding="utf-8")
                )
                self.assertEqual(lock["schema"], LOCK_SCHEMA_V2)
                self.assertEqual(
                    lock["service"]["workload_sha256"],
                    fixture.binding().workload_sha256,
                )
                self.assertEqual(
                    lock["profile"]["state_root"],
                    str(destination),
                )
                self.assertEqual(
                    lock["project"]["report_directory"],
                    str(
                        destination
                        / "projects"
                        / "playground"
                        / "reports"
                        / migrated.session_id
                    ),
                )
            copied_history = migrated_first.pi_sessions / first_history.name
            self.assertEqual(copied_history.read_bytes(), first_history.read_bytes())
            self.assertEqual(
                copied_history.stat().st_mtime_ns,
                first_history.stat().st_mtime_ns,
            )
            self.assertEqual(
                (migrated_first.workspace / "first.txt").read_text(encoding="utf-8"),
                "workspace first\n",
            )
            target_sparse = migrated_first.workspace / "sparse.bin"
            self.assertEqual(target_sparse.stat().st_size, source_sparse.stat().st_size)
            self.assertEqual(target_sparse.read_bytes(), source_sparse.read_bytes())
            if source_sparse.stat().st_blocks * 512 < source_sparse.stat().st_size:
                self.assertLess(
                    target_sparse.stat().st_blocks * 512,
                    target_sparse.stat().st_size,
                )
            self.assertEqual(
                (migrated_first.report_directory / "first.md").read_text(
                    encoding="utf-8"
                ),
                "report first\n",
            )
            self.assertEqual(
                (migrated_first.memory_directory / "first.md").read_text(
                    encoding="utf-8"
                ),
                "memory first\n",
            )
            self.assertEqual(
                (
                    destination
                    / "recovered"
                    / "chat"
                    / first.session_id
                    / "recovered.jsonl"
                ).read_text(encoding="utf-8"),
                '{"recovered":true}\n',
            )
            migrated_result = next(
                migrated
                for migrated in result.runs
                if migrated.session_id == first.session_id
            )
            self.assertIsNotNone(migrated_result.recovered_sha256)
            with enumerate_history(destination, "chat") as history:
                self.assertEqual(
                    {entry.session_id for entry in history.entries},
                    {first.session_id, second.session_id},
                )
                self.assertTrue(
                    all(entry.history_error is None for entry in history.entries)
                )
            permanent = ProfileBindingStore(destination).load("chat")
            self.assertEqual(
                permanent.workload_sha256,
                fixture.binding().workload_sha256,
            )
            self.assertTrue(result.receipt_path.is_file())
            self.assertEqual(
                source_hashes,
                {
                    "state": _tree_sha256(fixture.state_root),
                    "project": _tree_sha256(fixture.project_root),
                    "profile": _tree_sha256(fixture.profile_root),
                    "pi": _tree_sha256(fixture.pi_root),
                },
            )

            repeated = migrate_legacy_profile(
                fixture.profile_root,
                destination,
                service_binding=fixture.binding(),
                target_profile_id="chat",
                target_project_id="playground",
            )
            self.assertEqual(repeated.migration_id, result.migration_id)
            self.assertEqual(repeated.runs, result.runs)

    def test_profile_last_publication_recovers_without_source_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fixture = LegacyFixture(_directory(root / "source"))
            run, _ = fixture.new_run(
                marker="crash",
                history_timestamp="2026-07-27T01:02:03.004Z",
                history_mtime_ns=2_000_000_000_123_456_789,
            )
            destination = root / "model-lab"
            source_state_hash = _tree_sha256(fixture.state_root)

            import model_lab.migration as migration_module

            publish = migration_module._publish_directory

            def interrupt_profile(staging, target):
                if target == destination / "profiles" / "chat":
                    raise ModelLabError(
                        "injected publication interruption",
                        code="injected_interruption",
                    )
                return publish(staging, target)

            with mock.patch.object(
                migration_module,
                "_publish_directory",
                side_effect=interrupt_profile,
            ):
                with self.assertRaisesRegex(
                    ModelLabError,
                    "injected publication interruption",
                ):
                    migrate_legacy_profile(
                        fixture.profile_root,
                        destination,
                        service_binding=fixture.binding(),
                        target_profile_id="chat",
                        target_project_id="playground",
                    )

            self.assertFalse((destination / "profiles" / "chat").exists())
            self.assertTrue(
                (destination / "sessions" / "chat" / run.session_id).is_dir()
            )
            self.assertEqual(
                _tree_sha256(fixture.state_root),
                source_state_hash,
            )

            recovered = migrate_legacy_profile(
                fixture.profile_root,
                destination,
                service_binding=fixture.binding(),
                target_profile_id="chat",
                target_project_id="playground",
            )
            self.assertTrue(recovered.profile_root.is_dir())
            load_run_from_state(destination, "chat", run.session_id)

    def test_recovery_rejects_sessions_from_a_different_exact_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fixture = LegacyFixture(_directory(root / "source"))
            run, _ = fixture.new_run(
                marker="plan",
                history_timestamp="2026-07-27T01:02:03.004Z",
                history_mtime_ns=2_000_000_000_123_456_789,
            )
            destination = root / "model-lab"

            import model_lab.migration as migration_module

            publish = migration_module._publish_directory

            def interrupt_profile(staging, target):
                if target == destination / "profiles" / "chat":
                    raise ModelLabError(
                        "injected publication interruption",
                        code="injected_interruption",
                    )
                return publish(staging, target)

            with mock.patch.object(
                migration_module,
                "_publish_directory",
                side_effect=interrupt_profile,
            ):
                with self.assertRaises(ModelLabError):
                    migrate_legacy_profile(
                        fixture.profile_root,
                        destination,
                        service_binding=fixture.binding(),
                        target_profile_id="chat",
                        target_project_id="first-project",
                    )

            with self.assertRaises(ModelLabError) as raised:
                migrate_legacy_profile(
                    fixture.profile_root,
                    destination,
                    service_binding=fixture.binding(),
                    target_profile_id="chat",
                    target_project_id="second-project",
                )
            self.assertEqual(
                raised.exception.code,
                "published_migration_requires_recovery",
            )
            self.assertFalse((destination / "profiles" / "chat").exists())
            stale = load_run_from_state(destination, "chat", run.session_id)
            self.assertEqual(stale.profile.project_id, "first-project")

            recovered = migrate_legacy_profile(
                fixture.profile_root,
                destination,
                service_binding=fixture.binding(),
                target_profile_id="chat",
                target_project_id="first-project",
            )
            self.assertEqual(recovered.project_id, "first-project")

    def test_rejects_destination_component_symlink_without_escape_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fixture = LegacyFixture(_directory(root / "source"))
            run, _ = fixture.new_run(
                marker="links",
                history_timestamp="2026-07-27T01:02:03.004Z",
                history_mtime_ns=2_000_000_000_123_456_789,
            )
            recovered = _directory(
                fixture.state_root / "recovered" / "legacy" / run.session_id
            )
            _file(recovered / "recovered.jsonl", '{"recovered":true}\n')
            destination = _directory(root / "model-lab")
            escape = _directory(root / "escape")
            _directory(escape / "chat")
            (destination / "recovered").symlink_to(
                escape,
                target_is_directory=True,
            )

            with self.assertRaises(ModelLabError) as raised:
                migrate_legacy_profile(
                    fixture.profile_root,
                    destination,
                    service_binding=fixture.binding(),
                    target_profile_id="chat",
                    target_project_id="playground",
                )
            self.assertEqual(
                raised.exception.code,
                "unsafe_legacy_migration_destination",
            )
            self.assertFalse((escape / "chat" / run.session_id).exists())
            self.assertFalse((destination / "profiles" / "chat").exists())

    def test_holds_source_materialization_and_run_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fixture = LegacyFixture(_directory(root / "source"))
            run, _ = fixture.new_run(
                marker="authority",
                history_timestamp="2026-07-27T01:02:03.004Z",
                history_mtime_ns=2_000_000_000_123_456_789,
            )

            import model_lab.migration as migration_module

            class SourceLockObserved(Exception):
                pass

            def observe_source_lock(*_args, **_kwargs):
                descriptor = os.open(
                    fixture.state_root,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
                )
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(
                            descriptor,
                            fcntl.LOCK_EX | fcntl.LOCK_NB,
                        )
                finally:
                    os.close(descriptor)
                raise SourceLockObserved

            with mock.patch.object(
                migration_module,
                "_migrate_legacy_profile_quiesced",
                side_effect=observe_source_lock,
            ):
                with self.assertRaises(SourceLockObserved):
                    migrate_legacy_profile(
                        fixture.profile_root,
                        root / "model-lab",
                        service_binding=fixture.binding(),
                    )

            with acquire_run_from_state(
                fixture.state_root,
                "legacy",
                run.session_id,
            ):
                with self.assertRaises(ModelLabError) as raised:
                    migrate_legacy_profile(
                        fixture.profile_root,
                        root / "model-lab",
                        service_binding=fixture.binding(),
                    )
                self.assertEqual(
                    raised.exception.code,
                    "legacy_migration_source_in_use",
                )

            ensure_model_session_lock = migration_module._ensure_model_session_lock
            observed_target_lock = False

            def observe_target_lock(target_root):
                nonlocal observed_target_lock
                descriptor = os.open(
                    target_root,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
                )
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(
                            descriptor,
                            fcntl.LOCK_EX | fcntl.LOCK_NB,
                        )
                finally:
                    os.close(descriptor)
                observed_target_lock = True
                return ensure_model_session_lock(target_root)

            with mock.patch.object(
                migration_module,
                "_ensure_model_session_lock",
                side_effect=observe_target_lock,
            ):
                migrate_legacy_profile(
                    fixture.profile_root,
                    root / "model-lab",
                    service_binding=fixture.binding(),
                )
            self.assertTrue(observed_target_lock)

    def test_rejects_workload_drift_before_creating_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fixture = LegacyFixture(_directory(root / "source"))
            destination = root / "model-lab"
            binding = fixture.binding()
            changed_workload = ServiceWorkload(
                **{
                    **binding.workload.__dict__,
                    "revision": "c" * 40,
                }
            )
            changed = ServiceEndpointBinding(
                **{
                    **binding.__dict__,
                    "workload": changed_workload,
                    "workload_sha256": service_workload_identity(changed_workload),
                }
            )

            with self.assertRaises(ModelLabError) as raised:
                migrate_legacy_profile(
                    fixture.profile_root,
                    destination,
                    service_binding=changed,
                )
            self.assertEqual(
                raised.exception.code,
                "legacy_service_workload_mismatch",
            )
            self.assertFalse(destination.exists())

    def test_empty_profile_requires_an_existing_lockable_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fixture = LegacyFixture(_directory(root / "source"))
            destination = root / "model-lab"

            with self.assertRaises(ModelLabError) as raised:
                migrate_legacy_profile(
                    fixture.profile_root,
                    destination,
                    service_binding=fixture.binding(),
                )
            self.assertEqual(
                raised.exception.code,
                "legacy_migration_source_state_missing",
            )
            self.assertFalse(destination.exists())

    def test_v1_requires_explicit_policy_and_migrates_with_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fixture = LegacyFixture(
                _directory(root / "source"),
                schema="v1",
            )
            run, _ = fixture.new_run(
                marker="v1",
                history_timestamp="2026-07-27T01:02:03.004Z",
                history_mtime_ns=2_000_000_000_123_456_789,
            )
            refused = root / "refused" / "model-lab"
            with self.assertRaises(ModelLabError) as raised:
                migrate_legacy_profile(
                    fixture.profile_root,
                    refused,
                    service_binding=fixture.binding(),
                )
            self.assertEqual(
                raised.exception.code,
                "legacy_v1_policy_required",
            )
            self.assertFalse(refused.exists())

            destination = root / "accepted" / "model-lab"
            result = migrate_legacy_profile(
                fixture.profile_root,
                destination,
                service_binding=fixture.binding(),
                v1_policy=_policy(),
            )
            self.assertEqual(result.runs[0].session_id, run.session_id)
            loaded = load_run_from_state(
                destination,
                "legacy",
                run.session_id,
            )
            self.assertIsNotNone(loaded.profile.storage)
            self.assertIsNotNone(loaded.profile.sandbox)


if __name__ == "__main__":
    unittest.main()
