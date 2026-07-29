from __future__ import annotations

import hashlib
import inspect
import json
import os
import pathlib
import socket
import tempfile
import unittest
from unittest import mock

import model_session.materialization as materialization_module
from model_session.attachment import (
    ServiceWorkload,
)
from model_session.service_endpoint import (
    load_service_endpoint,
    publish_service_endpoint,
)
from model_session.errors import ModelSessionError
from model_session.materialization import materialize_new_run
from model_session.profile import (
    PROFILE_SCHEMA_V3,
    load_profile,
    load_profile_route,
)
from model_session.runs import LOCK_SCHEMA_V2, load_run_from_state


REVISION = "a" * 40


def _private_directory(path: pathlib.Path) -> pathlib.Path:
    path.mkdir(mode=0o700, parents=True)
    path.chmod(0o700)
    return path


class ProfileV3Fixture:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.lab_root = _private_directory(root / "model-lab")
        self.profile_root = _private_directory(self.lab_root / "profiles" / "chat")
        self.project_root = _private_directory(
            self.lab_root / "projects" / "playground"
        )
        self.pi_root = _private_directory(self.lab_root / "runtimes" / "pi" / "0.82.1")
        pi_bin = _private_directory(self.pi_root / "bin")
        pi = pi_bin / "pi"
        pi.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        pi.chmod(0o700)
        node = pi_bin / "node"
        node.write_text("#!/bin/sh\necho v24.11.1\n", encoding="utf-8")
        node.chmod(0o700)
        for name, content in (
            ("AGENTS.md", "isolated workspace\n"),
            ("SYSTEM.md", "system prompt\n"),
        ):
            path = self.profile_root / name
            path.write_text(content, encoding="utf-8")
            path.chmod(0o600)
        self.profile_path = self.profile_root / "profile.toml"
        self.profile_path.write_text(
            """schema = "model-session.profile.v3"
profile_id = "chat"
project_id = "playground"
service_id = "qwen-service"

[endpoint]
required_input_modalities = ["text"]

[pi]
version = "0.82.1"
tools = ["read", "write", "edit", "bash"]
system_prompt_file = "SYSTEM.md"

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
""",
            encoding="utf-8",
        )
        self.profile_path.chmod(0o600)

        self.runtime_root = _private_directory(root / "runtime")
        services = _private_directory(self.runtime_root / "services")
        self.socket_path = services / "qwen-service.sock"
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(os.fspath(self.socket_path))
        self.listener.listen(16)
        self.socket_path.chmod(0o600)
        self.workload = ServiceWorkload(
            repository="example-org/example-model",
            revision=REVISION,
            provider="local-openai",
            model_id="served-qwen",
            context_tokens=65_536,
            max_output_tokens=8_192,
            weight_format="bf16",
            kv_cache_dtype="bf16",
            runtime_compatibility="vllm-cu129-v1",
            reasoning=False,
        )
        self.endpoint = self.publish()

    def publish(
        self,
        *,
        workload: ServiceWorkload | None = None,
        service_sha256: str = "b" * 64,
        modalities: tuple[str, ...] = ("text",),
    ):
        return publish_service_endpoint(
            "qwen-service",
            service_sha256=service_sha256,
            workload=workload or self.workload,
            input_modalities=modalities,
            ttl_seconds=3600,
            socket_path=self.socket_path,
            runtime_root=self.runtime_root,
        )

    def close(self) -> None:
        self.listener.close()


class ModelSessionProfileV3Test(unittest.TestCase):
    def test_materializer_has_no_endpoint_object_authority_bypass(self) -> None:
        self.assertNotIn(
            "service_endpoint",
            inspect.signature(materialize_new_run).parameters,
        )

    def test_profile_derives_every_path_from_the_model_lab_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProfileV3Fixture(pathlib.Path(directory))
            try:
                profile = load_profile(fixture.profile_root)
            finally:
                fixture.close()

            self.assertEqual(profile.contract.schema, PROFILE_SCHEMA_V3)
            self.assertEqual(profile.contract.service_id, "qwen-service")
            self.assertEqual(profile.contract.state_root, fixture.lab_root)
            self.assertEqual(
                profile.contract.project_root,
                fixture.lab_root / "projects" / "playground",
            )
            self.assertEqual(
                profile.contract.pi.installation_root,
                fixture.lab_root / "runtimes" / "pi" / "0.82.1",
            )
            self.assertEqual(
                profile.contract.pi.executable.as_posix(),
                "bin/pi",
            )
            self.assertIsNone(profile.contract.model)
            self.assertIsNone(profile.contract.runtime)
            serialized = profile.contract.as_dict()
            self.assertEqual(
                serialized["endpoint"]["required_input_modalities"],
                ["text"],
            )
            self.assertNotIn("model", serialized)
            self.assertNotIn("runtime", serialized)

    def test_new_session_freezes_workload_and_generated_pi_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProfileV3Fixture(pathlib.Path(directory))
            try:
                profile = load_profile(fixture.profile_root)
                fixture.endpoint = fixture.publish(
                    service_sha256="c" * 64,
                    modalities=("text", "image"),
                )
                run = materialize_new_run(
                    profile,
                    endpoint_runtime_root=fixture.runtime_root,
                )
                loaded = load_run_from_state(
                    fixture.lab_root,
                    "chat",
                    run.session_id,
                )
            finally:
                fixture.close()

            self.assertIsNotNone(loaded.service_binding)
            self.assertEqual(
                loaded.service_binding.workload_sha256,
                fixture.endpoint.binding.workload_sha256,
            )
            self.assertEqual(
                loaded.service_binding.input_modalities,
                ("text",),
            )
            lock = json.loads(
                (loaded.snapshot_root / "lock.json").read_text(encoding="utf-8")
            )
            self.assertEqual(lock["schema"], LOCK_SCHEMA_V2)
            self.assertEqual(
                lock["service"]["workload_sha256"],
                fixture.endpoint.binding.workload_sha256,
            )
            models = json.loads(
                (loaded.snapshot_root / "pi" / "models.json").read_text(
                    encoding="utf-8"
                )
            )
            model = models["providers"]["local-openai"]["models"][0]
            self.assertEqual(model["id"], "served-qwen")
            self.assertEqual(model["contextWindow"], 65_536)
            self.assertEqual(model["input"], ["text"])

    def test_body_deadline_rolls_back_before_session_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProfileV3Fixture(pathlib.Path(directory))
            clock = {"now": 0.0}
            body_advanced = False
            real_write = materialization_module.os.write

            def write(descriptor: int, content: object) -> int:
                nonlocal body_advanced
                written = real_write(descriptor, content)
                descriptor_path = os.readlink(f"/proc/self/fd/{descriptor}")
                if (
                    not body_advanced
                    and not descriptor_path.endswith(
                        tuple(
                            materialization_module._PROJECT_LEAF_OWNERSHIP_FILES.values()
                        )
                    )
                ):
                    body_advanced = True
                    clock["now"] = 11.0
                return written

            try:
                profile = load_profile(fixture.profile_root)
                with (
                    mock.patch.object(
                        materialization_module.os,
                        "write",
                        side_effect=write,
                    ),
                    self.assertRaises(ModelSessionError) as caught,
                ):
                    materialize_new_run(
                        profile,
                        endpoint_runtime_root=fixture.runtime_root,
                        startup_deadline=10.0,
                        monotonic=lambda: clock["now"],
                    )
            finally:
                fixture.close()

            self.assertTrue(body_advanced)
            self.assertEqual(caught.exception.code, "service_startup_timeout")
            profile_sessions = fixture.lab_root / "sessions" / "chat"
            self.assertEqual(tuple(profile_sessions.iterdir()), ())
            self.assertEqual(
                tuple((fixture.project_root / "reports").iterdir()),
                (),
            )
            self.assertEqual(
                tuple((fixture.project_root / "memory").iterdir()),
                (),
            )

    def test_cleanup_deadline_preserves_staging_recovery_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProfileV3Fixture(pathlib.Path(directory))
            clock = {"now": 0.0}
            body_advanced = False
            cleanup_advanced = False
            real_write = materialization_module.os.write
            real_rmdir = materialization_module.os.rmdir

            def write(descriptor: int, content: object) -> int:
                nonlocal body_advanced
                written = real_write(descriptor, content)
                descriptor_path = os.readlink(f"/proc/self/fd/{descriptor}")
                if (
                    not body_advanced
                    and not descriptor_path.endswith(
                        tuple(
                            materialization_module._PROJECT_LEAF_OWNERSHIP_FILES.values()
                        )
                    )
                ):
                    body_advanced = True
                    clock["now"] = 11.0
                return written

            def rmdir(name: object, *args: object, **kwargs: object) -> None:
                nonlocal cleanup_advanced
                real_rmdir(name, *args, **kwargs)
                if not cleanup_advanced:
                    cleanup_advanced = True
                    clock["now"] += (
                        materialization_module.MATERIALIZATION_CLEANUP_GRACE_SECONDS
                        + 1.0
                    )

            try:
                profile = load_profile(fixture.profile_root)
                with (
                    mock.patch.object(
                        materialization_module.os,
                        "write",
                        side_effect=write,
                    ),
                    mock.patch.object(
                        materialization_module.os,
                        "rmdir",
                        side_effect=rmdir,
                    ),
                    self.assertRaises(ModelSessionError) as caught,
                ):
                    materialize_new_run(
                        profile,
                        endpoint_runtime_root=fixture.runtime_root,
                        startup_deadline=10.0,
                        monotonic=lambda: clock["now"],
                    )
            finally:
                fixture.close()

            self.assertTrue(body_advanced)
            self.assertTrue(cleanup_advanced)
            self.assertEqual(
                caught.exception.code,
                "session_materialization_cleanup_required",
            )
            profile_sessions = fixture.lab_root / "sessions" / "chat"
            staging_entries = tuple(
                entry
                for entry in profile_sessions.iterdir()
                if entry.name.startswith(".creating-")
            )
            published_entries = tuple(
                entry
                for entry in profile_sessions.iterdir()
                if not entry.name.startswith(".creating-")
            )
            self.assertEqual(len(staging_entries), 1)
            self.assertEqual(published_entries, ())
            self.assertTrue(any(staging_entries[0].iterdir()))
            session_id = staging_entries[0].name.removeprefix(".creating-")
            self.assertTrue(
                (fixture.project_root / "reports" / session_id).is_dir()
            )
            self.assertFalse(
                (fixture.project_root / "memory" / session_id).exists()
            )

    def test_collision_retry_receives_a_fresh_cleanup_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProfileV3Fixture(pathlib.Path(directory))
            clock = {"now": 0.0}
            staging_attempts = 0
            real_create = (
                materialization_module._create_private_child_directory
            )

            def create_child(*args: object, **kwargs: object) -> int:
                nonlocal staging_attempts
                if kwargs.get("label") == "session staging directory":
                    staging_attempts += 1
                    if staging_attempts == 1:
                        raise ModelSessionError(
                            "controlled raced staging collision",
                            code="session_id_collision",
                        )
                return real_create(*args, **kwargs)

            def fail_body(*args: object, **kwargs: object) -> None:
                clock["now"] = 10.0
                raise ModelSessionError(
                    "controlled body timeout",
                    code="service_startup_timeout",
                )

            try:
                profile = load_profile(fixture.profile_root)
                with (
                    mock.patch.object(
                        materialization_module,
                        "_create_private_child_directory",
                        side_effect=create_child,
                    ),
                    mock.patch.object(
                        materialization_module,
                        "_materialize_staging",
                        side_effect=fail_body,
                    ),
                    self.assertRaises(ModelSessionError) as caught,
                ):
                    materialize_new_run(
                        profile,
                        endpoint_runtime_root=fixture.runtime_root,
                        startup_deadline=100.0,
                        monotonic=lambda: clock["now"],
                    )
            finally:
                fixture.close()

            self.assertEqual(staging_attempts, 2)
            self.assertEqual(caught.exception.code, "service_startup_timeout")
            self.assertEqual(
                tuple((fixture.lab_root / "sessions" / "chat").iterdir()),
                (),
            )
            self.assertEqual(
                tuple((fixture.project_root / "reports").iterdir()),
                (),
            )
            self.assertEqual(
                tuple((fixture.project_root / "memory").iterdir()),
                (),
            )

    def test_final_staging_removal_may_cross_cleanup_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProfileV3Fixture(pathlib.Path(directory))
            clock = {"now": 0.0}
            staging_removed = False
            real_materialize = materialization_module._materialize_staging
            real_rmdir = materialization_module.os.rmdir

            def materialize(*args: object, **kwargs: object) -> None:
                real_materialize(*args, **kwargs)
                clock["now"] = 11.0
                raise ModelSessionError(
                    "controlled post-body timeout",
                    code="service_startup_timeout",
                )

            def rmdir(name: object, *args: object, **kwargs: object) -> None:
                nonlocal staging_removed
                real_rmdir(name, *args, **kwargs)
                if str(name).startswith(".creating-"):
                    staging_removed = True
                    clock["now"] = 17.0

            try:
                profile = load_profile(fixture.profile_root)
                with (
                    mock.patch.object(
                        materialization_module,
                        "_materialize_staging",
                        side_effect=materialize,
                    ),
                    mock.patch.object(
                        materialization_module.os,
                        "rmdir",
                        side_effect=rmdir,
                    ),
                    self.assertRaises(ModelSessionError) as caught,
                ):
                    materialize_new_run(
                        profile,
                        endpoint_runtime_root=fixture.runtime_root,
                        startup_deadline=100.0,
                        monotonic=lambda: clock["now"],
                    )
            finally:
                fixture.close()

            self.assertTrue(staging_removed)
            self.assertEqual(caught.exception.code, "service_startup_timeout")
            self.assertEqual(
                tuple((fixture.lab_root / "sessions" / "chat").iterdir()),
                (),
            )
            self.assertEqual(
                tuple((fixture.project_root / "reports").iterdir()),
                (),
            )
            self.assertEqual(
                tuple((fixture.project_root / "memory").iterdir()),
                (),
            )

    def test_mkdir_before_attestation_fails_closed_on_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProfileV3Fixture(pathlib.Path(directory))
            real_attest = (
                materialization_module._write_project_leaf_attestation
            )

            def attest(*args: object, **kwargs: object):
                if kwargs.get("root_name") == "memory":
                    raise KeyboardInterrupt(
                        "controlled crash before memory attestation"
                    )
                return real_attest(*args, **kwargs)

            try:
                profile = load_profile(fixture.profile_root)
                with (
                    mock.patch.object(
                        materialization_module,
                        "_write_project_leaf_attestation",
                        side_effect=attest,
                    ),
                    self.assertRaises(KeyboardInterrupt),
                ):
                    materialize_new_run(
                        profile,
                        endpoint_runtime_root=fixture.runtime_root,
                    )
                staging = next(
                    (fixture.lab_root / "sessions" / "chat").iterdir()
                )
                session_id = staging.name.removeprefix(".creating-")
                report = fixture.project_root / "reports" / session_id
                memory = fixture.project_root / "memory" / session_id

                with self.assertRaises(ModelSessionError) as caught:
                    materialize_new_run(
                        profile,
                        endpoint_runtime_root=fixture.runtime_root,
                    )
            finally:
                fixture.close()

            self.assertEqual(caught.exception.code, "session_recovery_required")
            self.assertTrue(staging.is_dir())
            self.assertTrue(report.is_dir())
            self.assertTrue(memory.is_dir())

    def test_recovery_rejects_a_recreated_project_leaf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProfileV3Fixture(pathlib.Path(directory))

            try:
                profile = load_profile(fixture.profile_root)
                with (
                    mock.patch.object(
                        materialization_module,
                        "_materialize_staging",
                        side_effect=KeyboardInterrupt(
                            "controlled attested crash"
                        ),
                    ),
                    self.assertRaises(KeyboardInterrupt),
                ):
                    materialize_new_run(
                        profile,
                        endpoint_runtime_root=fixture.runtime_root,
                    )
                staging = next(
                    (fixture.lab_root / "sessions" / "chat").iterdir()
                )
                session_id = staging.name.removeprefix(".creating-")
                report = fixture.project_root / "reports" / session_id
                memory = fixture.project_root / "memory" / session_id
                receipt = json.loads(
                    (
                        staging
                        / materialization_module._PROJECT_LEAF_OWNERSHIP_FILES[
                            "memory"
                        ]
                    ).read_text(encoding="utf-8")
                )
                expected_identity = (
                    receipt["leaf"]["device"],
                    receipt["leaf"]["inode"],
                    receipt["leaf"]["ctime_ns"],
                )
                memory.rmdir()
                memory.mkdir(mode=0o700)
                memory.chmod(0o700)
                actual = memory.stat()
                self.assertNotEqual(
                    (
                        actual.st_dev,
                        actual.st_ino,
                        actual.st_ctime_ns,
                    ),
                    expected_identity,
                )

                with self.assertRaises(ModelSessionError) as caught:
                    materialize_new_run(
                        profile,
                        endpoint_runtime_root=fixture.runtime_root,
                    )
            finally:
                fixture.close()

            self.assertEqual(caught.exception.code, "session_recovery_required")
            self.assertTrue(staging.is_dir())
            self.assertTrue(report.is_dir())
            self.assertTrue(memory.is_dir())

    def test_attested_empty_crash_state_is_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProfileV3Fixture(pathlib.Path(directory))

            try:
                profile = load_profile(fixture.profile_root)
                with (
                    mock.patch.object(
                        materialization_module,
                        "_materialize_staging",
                        side_effect=KeyboardInterrupt(
                            "controlled attested crash"
                        ),
                    ),
                    self.assertRaises(KeyboardInterrupt),
                ):
                    materialize_new_run(
                        profile,
                        endpoint_runtime_root=fixture.runtime_root,
                    )
                staging = next(
                    (fixture.lab_root / "sessions" / "chat").iterdir()
                )
                session_id = staging.name.removeprefix(".creating-")
                self.assertEqual(
                    set(entry.name for entry in staging.iterdir()),
                    set(
                        materialization_module._PROJECT_LEAF_OWNERSHIP_FILES.values()
                    ),
                )

                run = materialize_new_run(
                    profile,
                    endpoint_runtime_root=fixture.runtime_root,
                )
            finally:
                fixture.close()

            self.assertFalse(staging.exists())
            self.assertFalse(
                (fixture.project_root / "reports" / session_id).exists()
            )
            self.assertFalse(
                (fixture.project_root / "memory" / session_id).exists()
            )
            self.assertTrue(run.report_directory.is_dir())
            self.assertTrue(run.memory_directory.is_dir())
            self.assertFalse(
                set(materialization_module._PROJECT_LEAF_OWNERSHIP_FILES.values())
                & set(entry.name for entry in run.root.iterdir())
            )

    def test_leaf_open_failure_rolls_back_before_losing_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProfileV3Fixture(pathlib.Path(directory))
            real_open = (
                materialization_module._open_private_child_directory
            )

            def open_child(*args: object, **kwargs: object) -> int:
                if kwargs.get("label") == "session report directory":
                    raise ModelSessionError(
                        "controlled post-mkdir open failure",
                        code="unsafe_session_state",
                    )
                return real_open(*args, **kwargs)

            try:
                profile = load_profile(fixture.profile_root)
                with (
                    mock.patch.object(
                        materialization_module,
                        "_open_private_child_directory",
                        side_effect=open_child,
                    ),
                    self.assertRaises(ModelSessionError) as caught,
                ):
                    materialize_new_run(
                        profile,
                        endpoint_runtime_root=fixture.runtime_root,
                    )
            finally:
                fixture.close()

            self.assertEqual(caught.exception.code, "unsafe_session_state")
            self.assertEqual(
                tuple((fixture.lab_root / "sessions" / "chat").iterdir()),
                (),
            )
            self.assertEqual(
                tuple((fixture.project_root / "reports").iterdir()),
                (),
            )

    def test_leaf_rollback_failure_retains_staging_join_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProfileV3Fixture(pathlib.Path(directory))
            real_open = (
                materialization_module._open_private_child_directory
            )

            def open_child(*args: object, **kwargs: object) -> int:
                if kwargs.get("label") == "session report directory":
                    raise ModelSessionError(
                        "controlled post-mkdir open failure",
                        code="unsafe_session_state",
                    )
                return real_open(*args, **kwargs)

            try:
                profile = load_profile(fixture.profile_root)
                with (
                    mock.patch.object(
                        materialization_module,
                        "_open_private_child_directory",
                        side_effect=open_child,
                    ),
                    mock.patch.object(
                        materialization_module.os,
                        "rmdir",
                        side_effect=OSError(
                            "controlled leaf rollback failure"
                        ),
                    ),
                    self.assertRaises(ModelSessionError) as caught,
                ):
                    materialize_new_run(
                        profile,
                        endpoint_runtime_root=fixture.runtime_root,
                    )
            finally:
                fixture.close()

            self.assertEqual(
                caught.exception.code,
                "session_materialization_cleanup_required",
            )
            staging_entries = tuple(
                (fixture.lab_root / "sessions" / "chat").iterdir()
            )
            self.assertEqual(len(staging_entries), 1)
            self.assertTrue(staging_entries[0].name.startswith(".creating-"))
            session_id = staging_entries[0].name.removeprefix(".creating-")
            self.assertTrue(
                (fixture.project_root / "reports" / session_id).is_dir()
            )

    def test_post_publication_deadline_preserves_durable_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProfileV3Fixture(pathlib.Path(directory))
            clock = {"now": 0.0}
            renamed = False
            real_rename = materialization_module.os.rename
            real_fsync = materialization_module.os.fsync

            def rename(*args: object, **kwargs: object) -> None:
                nonlocal renamed
                real_rename(*args, **kwargs)
                renamed = True

            def fsync(descriptor: int) -> None:
                real_fsync(descriptor)
                if renamed:
                    clock["now"] = 11.0

            try:
                profile = load_profile(fixture.profile_root)
                with (
                    mock.patch.object(
                        materialization_module.os,
                        "rename",
                        side_effect=rename,
                    ),
                    mock.patch.object(
                        materialization_module.os,
                        "fsync",
                        side_effect=fsync,
                    ),
                    mock.patch.object(
                        materialization_module,
                        "_rollback_unpublished_materialization",
                    ) as rollback,
                    self.assertRaises(ModelSessionError) as caught,
                ):
                    materialize_new_run(
                        profile,
                        endpoint_runtime_root=fixture.runtime_root,
                        startup_deadline=10.0,
                        monotonic=lambda: clock["now"],
                    )
            finally:
                fixture.close()

            self.assertTrue(renamed)
            self.assertEqual(
                caught.exception.code,
                "published_session_requires_recovery",
            )
            rollback.assert_not_called()
            profile_sessions = fixture.lab_root / "sessions" / "chat"
            published = tuple(
                entry
                for entry in profile_sessions.iterdir()
                if not entry.name.startswith(".creating-")
            )
            self.assertEqual(len(published), 1)
            self.assertFalse(
                any(
                    entry.name.startswith(".creating-")
                    for entry in profile_sessions.iterdir()
                )
            )
            loaded = load_run_from_state(
                fixture.lab_root,
                "chat",
                published[0].name,
            )
            self.assertEqual(loaded.session_id, published[0].name)

    def test_resume_accepts_capability_growth_but_not_workload_change(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProfileV3Fixture(pathlib.Path(directory))
            try:
                profile = load_profile(fixture.profile_root)
                run = materialize_new_run(
                    profile,
                    endpoint_runtime_root=fixture.runtime_root,
                )
                fixture.publish(
                    service_sha256="c" * 64,
                    modalities=("text", "image"),
                )
                compatible = load_service_endpoint(
                    run.profile,
                    expected_binding=run.service_binding,
                    runtime_root=fixture.runtime_root,
                )
                self.assertEqual(
                    compatible.binding.workload_sha256,
                    run.service_binding.workload_sha256,
                )

                changed = ServiceWorkload(
                    **{
                        **fixture.workload.__dict__,
                        "revision": "e" * 40,
                    }
                )
                fixture.publish(
                    workload=changed,
                    service_sha256="d" * 64,
                    modalities=("text", "image"),
                )
                with self.assertRaises(ModelSessionError) as caught:
                    load_service_endpoint(
                        run.profile,
                        expected_binding=run.service_binding,
                        runtime_root=fixture.runtime_root,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "service_endpoint_workload_mismatch",
                )
            finally:
                fixture.close()

    def test_active_profile_rejects_legacy_and_noncanonical_layouts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProfileV3Fixture(pathlib.Path(directory))
            try:
                document = fixture.profile_path.read_text(encoding="utf-8")
                fixture.profile_path.write_text(
                    document.replace(
                        "model-session.profile.v3",
                        "model-session.profile.v2",
                    ),
                    encoding="utf-8",
                )
                fixture.profile_path.chmod(0o600)
                with self.assertRaises(ModelSessionError):
                    load_profile(fixture.profile_root)

                fixture.profile_path.write_text(document, encoding="utf-8")
                fixture.profile_path.chmod(0o600)
                wrong = _private_directory(fixture.lab_root / "profiles" / "wrong-name")
                for name in ("profile.toml", "AGENTS.md", "SYSTEM.md"):
                    source = fixture.profile_root / name
                    target = wrong / name
                    target.write_bytes(source.read_bytes())
                    target.chmod(0o600)
                with self.assertRaises(ModelSessionError) as caught:
                    load_profile(wrong)
                self.assertEqual(
                    caught.exception.code,
                    "invalid_profile_layout",
                )
            finally:
                fixture.close()

    def test_route_does_not_open_mutable_prompt_or_pi_resources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProfileV3Fixture(pathlib.Path(directory))
            try:
                unavailable = fixture.profile_root / "SYSTEM.unavailable"
                (fixture.profile_root / "SYSTEM.md").rename(unavailable)

                route = load_profile_route(fixture.profile_root)

                self.assertEqual(route.profile_id, "chat")
                self.assertEqual(route.project_id, "playground")
                self.assertEqual(route.service_id, "qwen-service")
                self.assertEqual(route.required_input_modalities, ("text",))
                with self.assertRaises(ModelSessionError):
                    load_profile(fixture.profile_root)
            finally:
                fixture.close()

    def test_locked_binding_must_cover_locked_profile_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ProfileV3Fixture(pathlib.Path(directory))
            try:
                profile_document = fixture.profile_path.read_text(
                    encoding="utf-8"
                ).replace(
                    'required_input_modalities = ["text"]',
                    'required_input_modalities = ["text", "image"]',
                )
                fixture.profile_path.write_text(
                    profile_document,
                    encoding="utf-8",
                )
                fixture.profile_path.chmod(0o600)
                fixture.publish(modalities=("text", "image"))
                run = materialize_new_run(
                    load_profile(fixture.profile_root),
                    endpoint_runtime_root=fixture.runtime_root,
                )
                manifest_path = run.snapshot_root / "lock.json"
                manifest = json.loads(manifest_path.read_bytes())
                manifest["service"]["input_modalities"] = ["text"]
                models_path = run.snapshot_root / "pi" / "models.json"
                models = json.loads(models_path.read_bytes())
                provider = next(iter(models["providers"].values()))
                provider["models"][0]["input"] = ["text"]
                models_bytes = (
                    json.dumps(
                        models,
                        indent=2,
                        sort_keys=True,
                        ensure_ascii=False,
                    )
                    + "\n"
                ).encode("utf-8")
                models_path.write_bytes(models_bytes)
                model_resource = next(
                    resource
                    for resource in manifest["resources"]
                    if resource["path"] == "pi/models.json"
                )
                model_resource["sha256"] = hashlib.sha256(models_bytes).hexdigest()
                model_resource["size"] = len(models_bytes)
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
                receipt_path = run.root / "run.json"
                receipt = json.loads(receipt_path.read_bytes())
                receipt["lock_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
                receipt_path.write_text(
                    json.dumps(
                        receipt,
                        indent=2,
                        sort_keys=True,
                        ensure_ascii=False,
                    )
                    + "\n",
                    encoding="utf-8",
                )

                with self.assertRaises(ModelSessionError) as caught:
                    load_run_from_state(
                        fixture.lab_root,
                        "chat",
                        run.session_id,
                    )

                self.assertEqual(
                    caught.exception.code,
                    "immutable_snapshot_changed",
                )
            finally:
                fixture.close()


if __name__ == "__main__":
    unittest.main()
