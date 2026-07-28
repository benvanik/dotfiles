from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess
import tempfile
import unittest

from runpod_local.service_definition import (
    load_inference_service,
    parse_inference_service_toml,
)
from runpod_local.service_vllm import build_vllm_argv

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "test" / "fixtures" / "runpod-services"
SECOND_SERVICE = FIXTURE_ROOT / "dense-text-second-service.toml"
SERVICE_ID = "fixture-dense-text"
MODEL_REPOSITORY = "fixture-org/fixture-dense-text-7b"
MODEL_REVISION = "2222222222222222222222222222222222222222"
COMPARISON_SERVICE_ID = "fixture-dense-chat"
COMPARISON_MODEL_REPOSITORY = "fixture-org/fixture-dense-chat-13b"
COMPARISON_MODEL_REVISION = "3333333333333333333333333333333333333333"


def flag_value(arguments: list[str], flag: str) -> str:
    position = arguments.index(flag)
    return arguments[position + 1]


def run_service_command(*arguments: str) -> dict[str, object]:
    environment = {
        "PATH": os.environ["PATH"],
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    completed = subprocess.run(
        [str(ROOT / "bin" / "runpod-service"), *arguments],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise TypeError("service command did not emit a JSON object")
    return value


class ConfigOnlySecondServiceAcceptanceTest(unittest.TestCase):
    def test_fixture_is_one_nonexecutable_configuration_file(self):
        entries = list(FIXTURE_ROOT.iterdir())

        self.assertEqual(entries, [SECOND_SERVICE])
        metadata = SECOND_SERVICE.lstat()
        self.assertTrue(stat.S_ISREG(metadata.st_mode))
        self.assertFalse(stat.S_ISLNK(metadata.st_mode))
        self.assertEqual(stat.S_IMODE(metadata.st_mode) & 0o111, 0)
        self.assertEqual(SECOND_SERVICE.suffix, ".toml")

    def test_config_alone_defines_a_distinct_vllm_launch(self):
        fixture_root_before = FIXTURE_ROOT.lstat()
        fixture_entries_before = {
            path.name: path.lstat() for path in FIXTURE_ROOT.iterdir()
        }
        definition = load_inference_service(SECOND_SERVICE)
        plan = definition.normalized_plan()

        self.assertEqual(plan["schema"], "runpod.inference-service-plan.v1")
        self.assertEqual(plan["service_id"], SERVICE_ID)
        self.assertEqual(plan["driver"], "vllm-openai.v1")
        self.assertEqual(plan["runtime_id"], "vllm-cu129-v0.25.1")
        self.assertEqual(plan["model"]["repository"], MODEL_REPOSITORY)
        self.assertEqual(plan["model"]["revision"], MODEL_REVISION)
        self.assertEqual(
            plan["model"]["checkpoint"],
            "weights/model.safetensors",
        )
        self.assertNotIn("closure_sha256", plan["model"])
        self.assertFalse(plan["endpoint"]["reasoning"])
        self.assertEqual(
            plan["compatibility"]["minimum_compute_capability"],
            [8, 0],
        )
        self.assertIsNone(plan["vllm"]["quantization"])
        self.assertIsNone(plan["vllm"]["reasoning_parser"])
        self.assertIsNone(plan["vllm"]["tool_call_parser"])
        self.assertIsNone(plan["vllm"]["speculative_config"])
        self.assertIs(plan["vllm"]["auto_tool_choice"], False)
        self.assertEqual(plan["vllm"]["model_implementation"], "vllm")
        self.assertEqual(plan["vllm"]["load_format"], "safetensors")

        model_path = "/root/runpod-session/models/resolved-fixture-snapshot"
        arguments = list(
            build_vllm_argv(
                definition,
                model_path=model_path,
                remote_port=8123,
            )
        )
        self.assertEqual(
            arguments[:3],
            ["/usr/local/bin/vllm", "serve", model_path],
        )
        self.assertNotIn("weights/model.safetensors", arguments)
        self.assertEqual(
            flag_value(arguments, "--served-model-name"),
            SERVICE_ID,
        )
        self.assertEqual(flag_value(arguments, "--host"), "127.0.0.1")
        self.assertEqual(flag_value(arguments, "--port"), "8123")
        self.assertEqual(
            flag_value(arguments, "--api-key"),
            "model-session-local-no-secret",
        )
        self.assertEqual(flag_value(arguments, "--max-model-len"), "8192")
        self.assertEqual(flag_value(arguments, "--max-num-seqs"), "2")
        self.assertEqual(flag_value(arguments, "--model-impl"), "vllm")
        self.assertEqual(
            flag_value(arguments, "--load-format"),
            "safetensors",
        )
        self.assertIn("--enable-prefix-caching", arguments)
        for omitted_flag in (
            "--quantization",
            "--reasoning-parser",
            "--tool-call-parser",
            "--enable-auto-tool-choice",
            "--speculative-config",
        ):
            with self.subTest(omitted_flag=omitted_flag):
                self.assertNotIn(omitted_flag, arguments)
        self.assertFalse(
            any(argument.endswith((".py", ".sh")) for argument in arguments)
        )
        fixture_entries_after = {
            path.name: path.lstat() for path in FIXTURE_ROOT.iterdir()
        }
        fixture_root_after = FIXTURE_ROOT.lstat()
        self.assertEqual(
            (
                fixture_root_after.st_dev,
                fixture_root_after.st_ino,
                fixture_root_after.st_mtime_ns,
                fixture_root_after.st_ctime_ns,
            ),
            (
                fixture_root_before.st_dev,
                fixture_root_before.st_ino,
                fixture_root_before.st_mtime_ns,
                fixture_root_before.st_ctime_ns,
            ),
        )
        self.assertEqual(
            set(fixture_entries_after),
            set(fixture_entries_before),
        )
        for name, before in fixture_entries_before.items():
            after = fixture_entries_after[name]
            self.assertEqual(
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
                (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                ),
            )

    def test_definition_identity_depends_on_config_not_its_path(self):
        payload = SECOND_SERVICE.read_bytes()
        from_file = load_inference_service(SECOND_SERVICE)
        from_memory = parse_inference_service_toml(
            payload,
            source="<second-service-fixture>",
        )
        changed = parse_inference_service_toml(
            payload.replace(
                b'service_id = "fixture-dense-text"',
                b'service_id = "fixture-dense-chat"',
                1,
            ),
            source="<changed-service-fixture>",
        )

        self.assertEqual(
            from_file.normalized_plan(),
            from_memory.normalized_plan(),
        )
        self.assertEqual(from_file.plan_sha256, from_memory.plan_sha256)
        self.assertNotEqual(from_file.plan_sha256, changed.plan_sha256)

    def test_cli_plans_second_service_with_the_same_generic_planner(self):
        validation = run_service_command(
            "validate",
            str(SECOND_SERVICE),
            "--json",
        )
        self.assertEqual(
            validation["schema_version"],
            "runpod.inference-service-validation.v1",
        )
        self.assertIs(validation["valid"], True)
        self.assertEqual(validation["service"]["service_id"], SERVICE_ID)

        payload = SECOND_SERVICE.read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            comparison_path = pathlib.Path(directory) / "comparison.toml"
            comparison_payload = (
                payload.replace(
                    b'service_id = "fixture-dense-text"',
                    b'service_id = "fixture-dense-chat"',
                    1,
                )
                .replace(
                    b'repository = "fixture-org/fixture-dense-text-7b"',
                    b'repository = "fixture-org/fixture-dense-chat-13b"',
                    1,
                )
                .replace(
                    b'revision = "2222222222222222222222222222222222222222"',
                    b'revision = "3333333333333333333333333333333333333333"',
                    1,
                )
                .replace(
                    b"max_model_len = 8192",
                    b"max_model_len = 4096",
                    1,
                )
                .replace(
                    b"prefix_caching = true",
                    b"prefix_caching = false",
                    1,
                )
            )
            comparison_path.write_bytes(comparison_payload)
            comparison_path.chmod(0o644)
            second_plan = run_service_command(
                "plan",
                str(SECOND_SERVICE),
                "--remote-port",
                "8123",
                "--json",
            )
            comparison_plan = run_service_command(
                "plan",
                str(comparison_path),
                "--remote-port",
                "8123",
                "--json",
            )

        self.assertEqual(
            second_plan["schema_version"],
            "runpod.inference-service-deployment-plan.v1",
        )
        self.assertIs(second_plan["executed"], False)
        self.assertEqual(
            second_plan["planning_source_closure"]["schema_version"],
            "runpod.inference-service-planning-source.v1",
        )
        self.assertEqual(
            second_plan["deployment"]["schema_version"],
            "runpod.vllm-openai-deployment-plan.v1",
        )
        self.assertEqual(
            second_plan["planning_source_closure"],
            comparison_plan["planning_source_closure"],
        )
        self.assertNotIn("controller_bundle", second_plan)
        self.assertEqual(
            second_plan["remote_controller_requirement"]["status"],
            "unresolved",
        )
        self.assertIs(
            second_plan["remote_controller_requirement"][
                "generic_implementation_required"
            ],
            True,
        )
        self.assertEqual(
            second_plan["remote_controller_requirement"]["config_input_count"],
            1,
        )
        self.assertEqual(second_plan["config_input"]["companion_inputs"], 0)
        self.assertNotEqual(
            second_plan["config_input"]["sha256"],
            comparison_plan["config_input"]["sha256"],
        )
        self.assertNotEqual(
            second_plan["config_input"]["remote_path"],
            comparison_plan["config_input"]["remote_path"],
        )
        self.assertNotEqual(
            second_plan["deployment"]["service_root"],
            comparison_plan["deployment"]["service_root"],
        )
        self.assertNotEqual(
            second_plan["deployment"]["launch"]["compile_affecting_sha256"],
            comparison_plan["deployment"]["launch"]["compile_affecting_sha256"],
        )
        self.assertEqual(
            second_plan["deployment"]["launch"]["compile_affecting_sha256"],
            second_plan["deployment"]["compile_cache_identity_inputs"][
                "compile_affecting_launch_sha256"
            ],
        )
        generic_planner = json.dumps(
            second_plan["planning_source_closure"],
            sort_keys=True,
        )
        for service_specific_value in (
            SERVICE_ID,
            MODEL_REPOSITORY,
            MODEL_REVISION,
            COMPARISON_SERVICE_ID,
            COMPARISON_MODEL_REPOSITORY,
            COMPARISON_MODEL_REVISION,
        ):
            with self.subTest(service_specific_value=service_specific_value):
                self.assertNotIn(service_specific_value, generic_planner)

    def test_fixture_identity_is_absent_from_reusable_executable_sources(self):
        production_sources = [
            *sorted((ROOT / "lib" / "runpod_local").glob("*.py")),
            *sorted((ROOT / "bin").glob("runpod*")),
            *sorted((ROOT / "runpod").rglob("*.py")),
            *sorted((ROOT / "runpod").rglob("*.sh")),
        ]
        needles = (SERVICE_ID, MODEL_REPOSITORY, MODEL_REVISION)

        for path in production_sources:
            if not path.is_file():
                continue
            source = path.read_text(encoding="utf-8")
            for needle in needles:
                with self.subTest(path=path, needle=needle):
                    self.assertNotIn(needle, source)


if __name__ == "__main__":
    unittest.main()
