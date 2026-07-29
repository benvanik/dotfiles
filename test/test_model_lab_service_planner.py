"""Model-service deployment planner tests."""

from __future__ import annotations

import hashlib
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from model_lab.errors import ModelLabError
from model_lab.service_planner import (
    build_planning_source_closure,
    build_service_deployment_plan,
    build_service_validation,
)
from model_lab.service_definition import parse_service_toml
from model_lab.service_vllm import (
    MODEL_SNAPSHOT_ARGUMENT,
    build_vllm_argv,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_PATH = pathlib.Path("/private/models/fixture-service.toml")
RUNTIME = {
    "runtime_id": "vllm-cu129-v0.25.1",
    "image": "vllm/vllm-openai@sha256:" + "1" * 64,
    "manifest": {"sha256": "2" * 64},
}
CONFIG = b"""\
schema = "model-lab.service.v1"
service_id = "fixture-service"
driver = "vllm-openai.v1"
runtime_id = "vllm-cu129-v0.25.1"

[model]
source = "huggingface"
repository = "example/Model-7B"
revision = "1111111111111111111111111111111111111111"
checkpoint = "model.safetensors"
weight_format = "native"

[endpoint]
input_modalities = ["text"]
reasoning = false
max_output_tokens = 2048

[compatibility]
minimum_compute_capability = "8.0"

[resources]
gpu_count = 1
gpu_memory_gib = 24
cpu_count = 8
memory_gib = 32
ephemeral_disk_gib = 50
claim_mode = "shared"

[vllm]
model_implementation = "vllm"
dtype = "bfloat16"
quantization = "none"
tensor_parallel_size = 1
max_model_len = 8192
max_num_sequences = 2
max_num_batched_tokens = 4096
kv_cache_dtype = "bfloat16"
gpu_memory_utilization = 0.75
chunked_prefill = true
load_format = "safetensors"
safetensors_load_strategy = "lazy"
language_model_only = true
mamba_cache_mode = "none"
prefix_caching = true
reasoning_parser = "none"
tool_call_parser = "none"
speculative_method = "none"
speculative_tokens = 0
generation_config = "auto"
"""


class ServiceControllerPlanTest(unittest.TestCase):
    def definition(self, payload: bytes = CONFIG):
        return parse_service_toml(
            payload,
            source="<controller-test>",
        )

    def test_validation_binds_the_single_exact_config_input(self):
        definition = self.definition()

        validation = build_service_validation(
            definition,
            source_path=SOURCE_PATH,
            runtime=RUNTIME,
        )

        self.assertEqual(
            validation["schema_version"],
            "model-lab.service-validation.v1",
        )
        self.assertIs(validation["valid"], True)
        self.assertEqual(
            validation["service_plan_sha256"],
            definition.plan_sha256,
        )
        self.assertEqual(
            validation["config_input"],
            {
                "source_path": str(SOURCE_PATH),
                "bytes": len(CONFIG),
                "sha256": hashlib.sha256(CONFIG).hexdigest(),
                "scope": "local-planning-only",
                "remote_path": None,
                "companion_inputs": 0,
            },
        )

    def test_plan_separates_service_state_from_shared_model_snapshot(self):
        definition = self.definition()

        plan = build_service_deployment_plan(
            definition,
            source_path=SOURCE_PATH,
            source_root=ROOT,
            runtime=RUNTIME,
            remote_port=8123,
        )

        self.assertEqual(
            plan["schema_version"],
            "model-lab.service-deployment-plan.v1",
        )
        self.assertIs(plan["executed"], False)
        self.assertEqual(
            plan["planning_source_closure"]["schema_version"],
            "model-lab.service-planning-source.v1",
        )
        self.assertEqual(
            plan["remote_controller_requirement"]["status"],
            "available-after-materialization",
        )
        self.assertIs(
            plan["remote_controller_requirement"]["generic_implementation_required"],
            False,
        )
        self.assertEqual(
            plan["remote_controller_requirement"]["authored_remote_input_count"],
            0,
        )
        deployment = plan["deployment"]
        self.assertEqual(
            deployment["service_root"],
            "/root/runpod-session/services/fixture-service",
        )
        self.assertEqual(
            deployment["manifest_path_template"],
            (
                "/root/runpod-session/services/fixture-service/deployments/"
                "{deployment_id}/deployment.json"
            ),
        )
        self.assertNotIn(
            deployment["service_root"],
            deployment["model_snapshot"]["shared_root_template"],
        )
        self.assertEqual(
            deployment["launch"]["argv_template"][2],
            MODEL_SNAPSHOT_ARGUMENT,
        )
        self.assertNotIn(
            "model.safetensors",
            deployment["launch"]["argv_template"],
        )
        self.assertIsNone(deployment["model_snapshot"]["generated_closure_sha256"])
        self.assertIsNone(deployment["compile_cache_identity_inputs"]["observed_gpu"])
        self.assertEqual(
            deployment["compile_cache_identity_inputs"]["status"],
            "requires-materialization-and-remote-observation",
        )
        self.assertIsNone(
            deployment["compile_cache_identity_inputs"][
                "implementation_bundle_sha256"
            ]
        )
        self.assertIsNone(
            deployment["compile_cache_identity_inputs"][
                "runtime_execution_environment"
            ]
        )
        self.assertEqual(
            deployment["compile_cache_identity_inputs"]["persistent_root_prefix"],
            "/workspace/.cache/compiled/vllm-openai/v1",
        )

    def test_compile_affecting_hash_is_known_and_tracks_typed_vllm_plan(self):
        first = build_service_deployment_plan(
            self.definition(),
            source_path=SOURCE_PATH,
            source_root=ROOT,
            runtime=RUNTIME,
        )["deployment"]
        changed_definition = self.definition(
            CONFIG.replace(
                b"max_model_len = 8192",
                b"max_model_len = 16384",
                1,
            )
        )
        second = build_service_deployment_plan(
            changed_definition,
            source_path=SOURCE_PATH,
            source_root=ROOT,
            runtime=RUNTIME,
        )["deployment"]

        first_hash = first["launch"]["compile_affecting_sha256"]
        self.assertRegex(first_hash, r"^[0-9a-f]{64}$")
        self.assertEqual(
            first_hash,
            first["compile_cache_identity_inputs"]["compile_affecting_launch_sha256"],
        )
        self.assertNotEqual(
            first_hash,
            second["launch"]["compile_affecting_sha256"],
        )

    def test_planning_source_closure_is_generic_and_content_bound(self):
        closure = build_planning_source_closure(source_root=ROOT)
        encoded = json.dumps(closure, sort_keys=True)

        self.assertEqual(
            [record["source"] for record in closure["files"]],
            [
                "lib/model_lab/__init__.py",
                "lib/model_lab/errors.py",
                "lib/model_lab/service_definition.py",
                "lib/model_lab/service_vllm.py",
                "lib/model_lab/service_planner.py",
            ],
        )
        self.assertNotIn("fixture-service", encoded)
        self.assertNotIn("example/Model-7B", encoded)
        self.assertRegex(closure["source_sha256"], r"^[0-9a-f]{64}$")

    def test_planning_source_rejects_world_writes_and_hardlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "planner.py"
            source.write_text("GENERIC = True\n", encoding="utf-8")
            with mock.patch(
                "model_lab.service_planner.PLANNING_SOURCE_FILES",
                ("planner.py",),
            ):
                source.chmod(0o666)
                with self.assertRaises(ModelLabError) as writable:
                    build_planning_source_closure(source_root=root)
                self.assertEqual(
                    writable.exception.code,
                    "unsafe_service_planning_source",
                )

                source.chmod(0o644)
                (root / "second-link.py").hardlink_to(source)
                with self.assertRaises(ModelLabError) as hardlinked:
                    build_planning_source_closure(source_root=root)
                self.assertEqual(
                    hardlinked.exception.code,
                    "unsafe_service_planning_source",
                )

    def test_adapter_rejects_nonabsolute_snapshot_and_invalid_port(self):
        definition = self.definition()

        with self.assertRaises(ModelLabError) as path_error:
            build_vllm_argv(
                definition,
                model_path="relative/model",
                remote_port=8000,
            )
        self.assertEqual(
            path_error.exception.code,
            "invalid_service_model_path",
        )
        with self.assertRaises(ModelLabError) as port_error:
            build_vllm_argv(
                definition,
                model_path="/model",
                remote_port=True,
            )
        self.assertEqual(
            port_error.exception.code,
            "invalid_service_remote_port",
        )


if __name__ == "__main__":
    unittest.main()
