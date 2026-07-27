from __future__ import annotations

import pathlib
import tempfile
import unittest

from runpod_local.cache import JsonCache
from runpod_local.errors import RunpodLocalError
from runpod_local.model import GIB, ModelInspector


class FakeHuggingFaceClient:
    def __init__(self, model_info, files):
        self._model_info = model_info
        self._files = files
        self.file_requests = []

    def model_info(self, repository, revision):
        self.requested_repository = repository
        self.requested_revision = revision
        return self._model_info

    def json_file(self, repository, resolved_revision, path, optional=False):
        self.file_requests.append((repository, resolved_revision, path))
        if path not in self._files:
            if optional:
                return None
            raise AssertionError(f"unexpected missing fixture {path}")
        return self._files[path]

    def file_size(self, repository, resolved_revision, path):
        raise AssertionError(f"fixture omitted sibling size for {path}")


def sibling(path, size):
    return {
        "rfilename": path,
        "size": size,
        "lfs": {"size": size, "sha256": f"sha-{path}"},
    }


def model_info(*siblings, parameter_count=32_000_000_000, dtypes=None):
    dtypes = dtypes or {"BF16": parameter_count}
    return {
        "sha": "0123456789abcdef",
        "gated": False,
        "private": False,
        "siblings": list(siblings),
        "safetensors": {
            "total": parameter_count,
            "parameters": dtypes,
        },
        "usedStorage": 999_999_999_999,
    }


class ModelInspectorTest(unittest.TestCase):
    def test_index_selects_and_deduplicates_only_referenced_shards(self):
        shard_a = sibling("model-00001-of-00002.safetensors", 120)
        shard_b = sibling("model-00002-of-00002.safetensors", 140)
        ignored = sibling("original/model-00001-of-00001.safetensors", 9_000)
        info = model_info(
            sibling("model.safetensors.index.json", 80),
            shard_a,
            shard_b,
            ignored,
            parameter_count=100,
        )
        files = {
            "model.safetensors.index.json": {
                "metadata": {"total_size": 200},
                "weight_map": {
                    "a": shard_a["rfilename"],
                    "b": shard_a["rfilename"],
                    "c": shard_b["rfilename"],
                },
            },
            "config.json": {
                "model_type": "fixture",
                "num_hidden_layers": 2,
                "num_attention_heads": 4,
                "num_key_value_heads": 2,
                "head_dim": 8,
                "max_position_embeddings": 4096,
            },
        }
        report = ModelInspector(FakeHuggingFaceClient(info, files)).inspect(
            "example/dense", context_tokens=1024
        )

        self.assertEqual(report["checkpoint"]["download_bytes"], 260)
        self.assertEqual(report["checkpoint"]["tensor_bytes"], 200)
        self.assertEqual(report["checkpoint"]["file_count"], 2)
        self.assertEqual(
            [entry["path"] for entry in report["checkpoint"]["files"]],
            [shard_a["rfilename"], shard_b["rfilename"]],
        )
        self.assertNotEqual(
            report["checkpoint"]["download_bytes"], info["usedStorage"]
        )

    def test_every_file_read_uses_the_resolved_commit(self):
        info = model_info(sibling("model.safetensors", 200), parameter_count=100)
        client = FakeHuggingFaceClient(
            info,
            {
                "config.json": {
                    "num_hidden_layers": 1,
                    "num_attention_heads": 1,
                    "head_dim": 1,
                }
            },
        )
        ModelInspector(client).inspect("example/model", revision="moving-branch")
        self.assertEqual(client.requested_revision, "moving-branch")
        self.assertTrue(client.file_requests)
        self.assertTrue(
            all(request[1] == info["sha"] for request in client.file_requests)
        )

    def test_standard_gqa_kv_cache(self):
        size = 65_524_246_528
        info = model_info(
            sibling("model.safetensors", size),
            parameter_count=32_762_123_264,
        )
        files = {
            "config.json": {
                "model_type": "qwen3",
                "num_hidden_layers": 64,
                "num_attention_heads": 64,
                "num_key_value_heads": 8,
                "head_dim": 128,
                "max_position_embeddings": 40960,
            }
        }
        report = ModelInspector(FakeHuggingFaceClient(info, files)).inspect(
            "Qwen/Qwen3-32B",
            context_tokens=32768,
            sequences=1,
            kv_dtype="bf16",
        )
        kv_cache = report["runtime_estimate"]["kv_cache"]
        self.assertTrue(kv_cache["available"])
        self.assertEqual(kv_cache["bytes_per_token_per_sequence_full_attention"], 262144)
        self.assertEqual(kv_cache["bytes"], 8 * GIB)

    def test_hybrid_full_and_sliding_attention_kv_cache(self):
        info = model_info(sibling("model.safetensors", 100))
        layer_types = ["full_attention", "sliding_attention"] * 18
        files = {
            "config.json": {
                "model_type": "gpt_oss",
                "num_hidden_layers": 36,
                "num_attention_heads": 64,
                "num_key_value_heads": 8,
                "head_dim": 64,
                "sliding_window": 128,
                "layer_types": layer_types,
            }
        }
        report = ModelInspector(FakeHuggingFaceClient(info, files)).inspect(
            "openai/gpt-oss-120b", context_tokens=131072
        )
        self.assertEqual(
            report["runtime_estimate"]["kv_cache"]["bytes"], 4_836_556_800
        )

    def test_gemma4_uses_distinct_global_and_sliding_kv_geometry(self):
        info = model_info(sibling("model.safetensors", 100))
        layer_types = (["sliding_attention"] * 5 + ["full_attention"]) * 10
        files = {
            "config.json": {
                "model_type": "gemma4",
                "text_config": {
                    "model_type": "gemma4_text",
                    "attention_k_eq_v": True,
                    "num_hidden_layers": 60,
                    "num_attention_heads": 32,
                    "num_key_value_heads": 16,
                    "num_global_key_value_heads": 4,
                    "head_dim": 256,
                    "global_head_dim": 512,
                    "sliding_window": 1024,
                    "layer_types": layer_types,
                },
            }
        }
        expected_bytes = {
            32768: 3_523_215_360,
            65536: 6_207_569_920,
            131072: 11_576_279_040,
            262144: 22_313_697_280,
        }
        for context_tokens, expected in expected_bytes.items():
            with self.subTest(context_tokens=context_tokens):
                report = ModelInspector(
                    FakeHuggingFaceClient(info, files)
                ).inspect(
                    "llmfan46/gemma-4-31B-it-uncensored-heretic",
                    context_tokens=context_tokens,
                    kv_dtype="bf16",
                )
                kv_cache = report["runtime_estimate"]["kv_cache"]
                self.assertEqual(kv_cache["bytes"], expected)
                self.assertEqual(
                    kv_cache["gib"], round(expected / GIB, 3)
                )
                self.assertEqual(
                    kv_cache["attention_layer_geometries"],
                    {
                        "full_attention": {
                            "layer_count": 10,
                            "cache_tokens_per_layer": context_tokens,
                            "key_value_heads": 4,
                            "head_dimension": 512,
                            "bytes_per_token_per_layer": 8192,
                            "bytes_per_sequence": (
                                10 * context_tokens * 8192
                            ),
                        },
                        "sliding_attention": {
                            "layer_count": 50,
                            "cache_tokens_per_layer": 1024,
                            "key_value_heads": 16,
                            "head_dimension": 256,
                            "bytes_per_token_per_layer": 16384,
                            "bytes_per_sequence": 50 * 1024 * 16384,
                        },
                    },
                )

    def test_global_kv_geometry_without_layer_types_is_unmodeled(self):
        info = model_info(sibling("model.safetensors", 100))
        files = {
            "config.json": {
                "num_hidden_layers": 2,
                "num_attention_heads": 8,
                "num_key_value_heads": 2,
                "num_global_key_value_heads": 1,
                "head_dim": 64,
                "global_head_dim": 128,
            }
        }
        report = ModelInspector(FakeHuggingFaceClient(info, files)).inspect(
            "example/ambiguous-global-geometry"
        )
        kv_cache = report["runtime_estimate"]["kv_cache"]
        self.assertFalse(kv_cache["available"])
        self.assertIn("without layer_types", kv_cache["reason"])

    def test_invalid_global_kv_geometry_is_not_silently_ignored(self):
        info = model_info(sibling("model.safetensors", 100))
        files = {
            "config.json": {
                "num_hidden_layers": 2,
                "num_attention_heads": 8,
                "num_key_value_heads": 2,
                "num_global_key_value_heads": "1",
                "head_dim": 64,
                "global_head_dim": 128,
                "sliding_window": 1024,
                "layer_types": ["sliding_attention", "full_attention"],
            }
        }
        report = ModelInspector(FakeHuggingFaceClient(info, files)).inspect(
            "example/invalid-global-geometry"
        )
        kv_cache = report["runtime_estimate"]["kv_cache"]
        self.assertFalse(kv_cache["available"])
        self.assertIn("num_global_key_value_heads", kv_cache["reason"])

    def test_mla_kv_cache(self):
        info = model_info(sibling("model.safetensors", 100))
        files = {
            "config.json": {
                "model_type": "kimi_k2",
                "num_hidden_layers": 61,
                "kv_lora_rank": 512,
                "qk_rope_head_dim": 64,
            }
        }
        report = ModelInspector(FakeHuggingFaceClient(info, files)).inspect(
            "moonshotai/Kimi-K2-Instruct", context_tokens=131072
        )
        self.assertEqual(
            report["runtime_estimate"]["kv_cache"]["bytes"], 9_210_691_584
        )

    def test_unknown_hybrid_cache_layout_is_not_invented(self):
        info = model_info(sibling("model.safetensors", 100))
        files = {
            "config.json": {
                "text_config": {
                    "model_type": "hybrid",
                    "num_hidden_layers": 2,
                    "num_attention_heads": 8,
                    "num_key_value_heads": 2,
                    "head_dim": 64,
                    "layer_types": ["linear_attention", "full_attention"],
                }
            }
        }
        report = ModelInspector(FakeHuggingFaceClient(info, files)).inspect(
            "example/hybrid"
        )
        kv_cache = report["runtime_estimate"]["kv_cache"]
        self.assertFalse(kv_cache["available"])
        self.assertIn("linear_attention", kv_cache["reason"])
        self.assertEqual(report["architecture"]["config_source"], "text_config")

    def test_non_native_weight_format_is_explicit_projection(self):
        info = model_info(
            sibling("model.safetensors", 200),
            parameter_count=100,
            dtypes={"BF16": 90, "F32": 10},
        )
        report = ModelInspector(
            FakeHuggingFaceClient(info, {"config.json": {}})
        ).inspect("example/model", weight_format="fp8")
        runtime = report["runtime_estimate"]
        self.assertEqual(runtime["weight_bytes"], 100)
        self.assertEqual(
            runtime["weight_source"], "hypothetical_uniform_parameter_storage"
        )
        self.assertEqual(runtime["weight_confidence"], "low_projection")
        self.assertTrue(report["warnings"])

    def test_ambiguous_root_checkpoint_fails_closed(self):
        info = model_info(
            sibling("first.safetensors", 100),
            sibling("second.safetensors", 100),
        )
        with self.assertRaisesRegex(
            RunpodLocalError, "multiple root weight files"
        ) as caught:
            ModelInspector(FakeHuggingFaceClient(info, {})).inspect(
                "example/ambiguous"
            )
        self.assertEqual(caught.exception.code, "ambiguous_checkpoint")

    def test_index_path_traversal_is_rejected(self):
        info = model_info(
            sibling("model.safetensors.index.json", 50),
            sibling("model.safetensors", 100),
        )
        files = {
            "model.safetensors.index.json": {
                "weight_map": {"weight": "../model.safetensors"}
            }
        }
        with self.assertRaises(RunpodLocalError) as caught:
            ModelInspector(FakeHuggingFaceClient(info, files)).inspect(
                "example/traversal"
            )
        self.assertEqual(caught.exception.code, "invalid_repository_path")

    def test_conflicting_hub_file_sizes_are_rejected(self):
        bad_sibling = {
            "rfilename": "model.safetensors",
            "size": 100,
            "lfs": {"size": 101},
        }
        with self.assertRaises(RunpodLocalError) as caught:
            ModelInspector(
                FakeHuggingFaceClient(
                    model_info(bad_sibling), {"config.json": {}}
                )
            ).inspect("example/conflict")
        self.assertEqual(caught.exception.code, "conflicting_sibling_size")


class JsonCacheTest(unittest.TestCase):
    def test_cache_files_are_private_and_identity_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "cache"
            cache = JsonCache(root, now=lambda: 10.0)
            cache.put("key", {"value": 1})
            self.assertEqual(
                cache.get("key", maximum_age_seconds=None), {"value": 1}
            )
            cache_file = next(root.iterdir())
            self.assertEqual(cache_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
