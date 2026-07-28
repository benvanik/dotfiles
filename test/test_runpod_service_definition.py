from __future__ import annotations

import hashlib
import pathlib
import tempfile
import unittest

from runpod_local.errors import RunpodLocalError
from runpod_local.service_definition import (
    SERVICE_PLAN_SCHEMA,
    load_inference_service,
    parse_inference_service_toml,
)

FIXTURE_REPOSITORY = "fixture-org/fixture-chat-nvfp4"
FIXTURE_REVISION = "1" * 40


def service_toml(
    *,
    service_id: str = "fixture-chat-nvfp4",
    repository: str = FIXTURE_REPOSITORY,
    revision: str = FIXTURE_REVISION,
    checkpoint_line: str = 'checkpoint = "model.safetensors"',
    modalities: str = '"text"',
    reasoning: str = "true",
    minimum_compute_capability: str = "12.0",
    model_implementation: str = "vllm",
    dtype: str = "bfloat16",
    quantization: str = "modelopt_fp4",
    max_model_len: int = 65536,
    max_num_sequences: int = 8,
    max_num_batched_tokens: int = 8192,
    load_format: str = "safetensors",
    language_model_only: str = "true",
    reasoning_parser: str = "fixture_reasoning",
    tool_call_parser: str = "fixture_tools",
    speculative_method: str = "mtp",
    speculative_tokens: int = 1,
) -> bytes:
    return f"""\
schema = "runpod.inference-service.v1"
service_id = "{service_id}"
driver = "vllm-openai.v1"
runtime_id = "vllm-cu129-v0.25.1"

[model]
source = "huggingface"
repository = "{repository}"
revision = "{revision}"
{checkpoint_line}

[endpoint]
input_modalities = [{modalities}]
reasoning = {reasoning}

[compatibility]
minimum_compute_capability = "{minimum_compute_capability}"

[vllm]
model_implementation = "{model_implementation}"
dtype = "{dtype}"
quantization = "{quantization}"
tensor_parallel_size = 1
max_model_len = {max_model_len}
max_num_sequences = {max_num_sequences}
max_num_batched_tokens = {max_num_batched_tokens}
kv_cache_dtype = "bfloat16"
gpu_memory_utilization = 0.90
chunked_prefill = true
load_format = "{load_format}"
safetensors_load_strategy = "lazy"
language_model_only = {language_model_only}
mamba_cache_mode = "none"
prefix_caching = false
reasoning_parser = "{reasoning_parser}"
tool_call_parser = "{tool_call_parser}"
speculative_method = "{speculative_method}"
speculative_tokens = {speculative_tokens}
generation_config = "auto"
""".encode()


class InferenceServiceDefinitionTest(unittest.TestCase):
    def test_valid_definition_normalizes_authored_contract(self):
        payload = service_toml()
        definition = parse_inference_service_toml(payload)
        plan = definition.normalized_plan()

        self.assertEqual(plan["schema"], SERVICE_PLAN_SCHEMA)
        self.assertEqual(plan["service_id"], "fixture-chat-nvfp4")
        self.assertEqual(
            plan["model"],
            {
                "source": "huggingface",
                "repository": FIXTURE_REPOSITORY,
                "revision": FIXTURE_REVISION,
                "checkpoint": "model.safetensors",
            },
        )
        self.assertNotIn("closure_sha256", plan["model"])
        self.assertEqual(
            plan["compatibility"]["minimum_compute_capability"],
            [12, 0],
        )
        self.assertEqual(
            plan["vllm"]["speculative_config"],
            {"method": "mtp", "num_speculative_tokens": 1},
        )
        self.assertEqual(definition.source_bytes, payload)
        self.assertEqual(
            definition.source_sha256,
            hashlib.sha256(payload).hexdigest(),
        )
        self.assertEqual(definition.source_size, len(payload))
        self.assertEqual(len(definition.plan_sha256), 64)

    def test_semantically_equal_source_has_one_plan_identity(self):
        payload = service_toml()
        reformatted = payload.replace(
            b"gpu_memory_utilization = 0.90",
            b"gpu_memory_utilization=0.9",
        )

        first = parse_inference_service_toml(payload)
        second = parse_inference_service_toml(reformatted)

        self.assertNotEqual(first.source_sha256, second.source_sha256)
        self.assertEqual(first.normalized_plan(), second.normalized_plan())
        self.assertEqual(first.plan_sha256, second.plan_sha256)

    def test_plain_bf16_service_omits_optional_vllm_features(self):
        definition = parse_inference_service_toml(
            service_toml(
                service_id="fixture-bf16",
                repository="fixture-org/fixture-bf16",
                revision="2" * 40,
                checkpoint_line=('checkpoint = "model.safetensors.index.json"'),
                minimum_compute_capability="8.0",
                quantization="none",
                reasoning="false",
                reasoning_parser="none",
                tool_call_parser="none",
                speculative_method="none",
                speculative_tokens=0,
            )
        )

        plan = definition.normalized_plan()
        self.assertIsNone(plan["vllm"]["quantization"])
        self.assertIsNone(plan["vllm"]["reasoning_parser"])
        self.assertIsNone(plan["vllm"]["tool_call_parser"])
        self.assertIsNone(plan["vllm"]["speculative_config"])
        self.assertFalse(plan["vllm"]["auto_tool_choice"])

    def test_runtime_compatibility_selectors_are_data_not_code(self):
        definition = parse_inference_service_toml(
            service_toml(
                model_implementation="transformers",
                quantization="compressed-tensors",
                load_format="auto",
            )
        )

        plan = definition.normalized_plan()["vllm"]
        self.assertEqual(plan["model_implementation"], "transformers")
        self.assertEqual(plan["quantization"], "compressed-tensors")
        self.assertEqual(plan["load_format"], "auto")

    def test_checkpoint_selector_is_optional_authored_input(self):
        definition = parse_inference_service_toml(service_toml(checkpoint_line=""))

        self.assertIsNone(definition.model.checkpoint)
        self.assertIsNone(definition.normalized_plan()["model"]["checkpoint"])

    def test_pytorch_checkpoint_requires_explicit_auto_load_format(self):
        for checkpoint in (
            "pytorch_model.bin",
            "pytorch_model.bin.index.json",
        ):
            with self.subTest(checkpoint=checkpoint):
                definition = parse_inference_service_toml(
                    service_toml(
                        checkpoint_line=f'checkpoint = "{checkpoint}"',
                        load_format="auto",
                    )
                )
                self.assertEqual(definition.model.checkpoint, checkpoint)

                with self.assertRaises(RunpodLocalError) as caught:
                    parse_inference_service_toml(
                        service_toml(
                            checkpoint_line=f'checkpoint = "{checkpoint}"',
                            load_format="safetensors",
                        )
                    )
                self.assertEqual(
                    caught.exception.code,
                    "invalid_service_definition",
                )
                self.assertIn(
                    "requires vllm.load_format = auto",
                    str(caught.exception),
                )

    def test_unknown_fields_fail_closed_in_every_table(self):
        mutations = {
            "top": b'\nunknown = "value"\n',
            "model": b'\nclosure_sha256 = "' + b"0" * 64 + b'"\n',
            "endpoint": b"\nremote_port = 8000\n",
            "compatibility": b'\nexact_gpu_name = "some GPU"\n',
            "vllm": b'\napi_key = "forbidden"\n',
        }
        anchors = {
            "top": b"[model]",
            "model": b"[endpoint]",
            "endpoint": b"[compatibility]",
            "compatibility": b"[vllm]",
            "vllm": b"",
        }
        payload = service_toml()
        for table, mutation in mutations.items():
            with self.subTest(table=table):
                anchor = anchors[table]
                changed = (
                    payload + mutation
                    if not anchor
                    else payload.replace(anchor, mutation + anchor, 1)
                )
                with self.assertRaises(RunpodLocalError) as caught:
                    parse_inference_service_toml(changed)
                self.assertEqual(
                    caught.exception.code,
                    "invalid_service_definition",
                )
                self.assertIn("unknown fields", str(caught.exception))

    def test_model_source_requires_an_exact_commit_and_safe_selector(self):
        invalid_definitions = (
            service_toml(revision="main"),
            service_toml(revision="A" * 40),
            service_toml(checkpoint_line='checkpoint = "../model.safetensors"'),
            service_toml(checkpoint_line='checkpoint = "model.gguf"'),
        )
        for payload in invalid_definitions:
            with self.subTest(payload=payload):
                with self.assertRaises(RunpodLocalError) as caught:
                    parse_inference_service_toml(payload)
                self.assertEqual(
                    caught.exception.code,
                    "invalid_service_definition",
                )

    def test_cross_field_invariants_fail_closed(self):
        invalid_definitions = (
            service_toml(reasoning="false"),
            service_toml(
                speculative_method="none",
                speculative_tokens=1,
            ),
            service_toml(
                modalities='"text", "image"',
                language_model_only="true",
            ),
            service_toml(
                max_num_sequences=8,
                max_num_batched_tokens=4,
            ),
        )
        for payload in invalid_definitions:
            with self.subTest(payload=payload):
                with self.assertRaises(RunpodLocalError) as caught:
                    parse_inference_service_toml(payload)
                self.assertEqual(
                    caught.exception.code,
                    "invalid_service_definition",
                )

    def test_file_loader_retains_bytes_from_one_safe_source(self):
        payload = service_toml()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = root / "service.toml"
            path.write_bytes(payload)
            path.chmod(0o600)

            definition = load_inference_service(path)

            self.assertEqual(definition.source_bytes, payload)
            self.assertEqual(
                definition.source_sha256,
                hashlib.sha256(payload).hexdigest(),
            )
            symlink = root / "service-link.toml"
            symlink.symlink_to(path)
            with self.assertRaises(RunpodLocalError) as caught:
                load_inference_service(symlink)
            self.assertEqual(
                caught.exception.code,
                "unsafe_service_definition",
            )

    def test_group_or_world_writable_file_is_not_a_trusted_definition(self):
        for mode in (0o620, 0o602, 0o666):
            with (
                self.subTest(mode=oct(mode)),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = pathlib.Path(directory) / "service.toml"
                path.write_bytes(service_toml())
                path.chmod(mode)

                with self.assertRaises(RunpodLocalError) as caught:
                    load_inference_service(path)

                self.assertEqual(
                    caught.exception.code,
                    "unsafe_service_definition",
                )


if __name__ == "__main__":
    unittest.main()
