from __future__ import annotations

import hashlib
import inspect
import json
import os
import pathlib
import socket
import tempfile
import unittest

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
