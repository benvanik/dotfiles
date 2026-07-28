from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import stat
import subprocess
import tempfile
import unittest
from typing import Any
from unittest import mock

from runpod_local.cache import JsonCache
from runpod_local.errors import RunpodLocalError
from runpod_local.model import HUGGING_FACE_BASE
from runpod_local.service_definition import parse_inference_service_toml
from runpod_local.service_huggingface import (
    HUGGINGFACE_CLOSURE_IDENTITY_SCHEMA,
    HuggingFaceClosure,
    HuggingFaceClosureFile,
    default_huggingface_closure_path,
    load_huggingface_closure,
    parse_huggingface_closure,
    resolve_huggingface_closure,
    write_huggingface_closure,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPOSITORY = "fixture-lab/fixture-model"
REVISION = "1" * 40


def service_payload(
    *,
    checkpoint: str | None,
    load_format: str = "safetensors",
) -> bytes:
    checkpoint_line = "" if checkpoint is None else f'checkpoint = "{checkpoint}"\n'
    return f"""\
schema = "runpod.inference-service.v1"
service_id = "fixture-service"
driver = "vllm-openai.v1"
runtime_id = "vllm-cu129-v0.25.1"

[model]
source = "huggingface"
repository = "{REPOSITORY}"
revision = "{REVISION}"
{checkpoint_line}
[endpoint]
input_modalities = ["text"]
reasoning = false

[compatibility]
minimum_compute_capability = "8.0"

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
load_format = "{load_format}"
safetensors_load_strategy = "lazy"
language_model_only = true
mamba_cache_mode = "none"
prefix_caching = true
reasoning_parser = "none"
tool_call_parser = "none"
speculative_method = "none"
speculative_tokens = 0
generation_config = "auto"
""".encode()


def blob(path: str, digit: str, size: int | None) -> dict[str, Any]:
    sibling: dict[str, Any] = {
        "rfilename": path,
        "blobId": digit * 40,
    }
    if size is not None:
        sibling["size"] = size
    return sibling


def lfs(path: str, digit: str, size: int) -> dict[str, Any]:
    return {
        "rfilename": path,
        "blobId": "f" * 40,
        "size": size,
        "lfs": {
            "sha256": digit * 64,
            "size": size,
            "pointerSize": 134,
        },
    }


class FakeMetadataClient:
    def __init__(
        self,
        siblings: list[dict[str, Any]],
        *,
        resolved_revision: str = REVISION,
        json_files: dict[str, dict[str, Any]] | None = None,
        fallback_sizes: dict[str, int] | None = None,
    ) -> None:
        self.info = {
            "sha": resolved_revision,
            "siblings": siblings,
        }
        self.json_files = json_files or {}
        self.fallback_sizes = fallback_sizes or {}
        self.calls: list[tuple[Any, ...]] = []

    def model_info(
        self,
        repository: str,
        revision: str,
    ) -> dict[str, Any]:
        self.calls.append(("model_info", repository, revision))
        return self.info

    def json_file(
        self,
        repository: str,
        resolved_revision: str,
        path: str,
        *,
        optional: bool = False,
    ) -> dict[str, Any] | None:
        self.calls.append(
            (
                "json_file",
                repository,
                resolved_revision,
                path,
                optional,
            )
        )
        return self.json_files.get(path)

    def file_size(
        self,
        repository: str,
        resolved_revision: str,
        path: str,
    ) -> int:
        self.calls.append(("file_size", repository, resolved_revision, path))
        return self.fallback_sizes[path]


def indexed_siblings() -> list[dict[str, Any]]:
    return [
        lfs("weights/model-00002-of-00002.safetensors", "b", 2_000),
        blob("README.md", "2", None),
        lfs("pytorch_model.bin", "d", 7_000),
        blob("configuration_fixture.py", "4", 400),
        blob("weights/model.safetensors.index.json", "5", 500),
        blob("config.json", "3", 300),
        lfs("weights/model-00001-of-00002.safetensors", "a", 1_000),
        lfs("alternate.safetensors", "e", 8_000),
        lfs("tokenizer.json", "c", 600),
    ]


def indexed_client() -> FakeMetadataClient:
    return FakeMetadataClient(
        indexed_siblings(),
        json_files={
            "weights/model.safetensors.index.json": {
                "metadata": {"total_size": 3_000},
                "weight_map": {
                    "layer.1": "model-00002-of-00002.safetensors",
                    "layer.0": "model-00001-of-00002.safetensors",
                },
            }
        },
        fallback_sizes={"README.md": 200},
    )


def recalculate_closure_digest(document: dict[str, Any]) -> None:
    identity = {
        "schema_version": HUGGINGFACE_CLOSURE_IDENTITY_SCHEMA,
        "source": document["source"],
        "checkpoint": document["checkpoint"],
        "files": document["files"],
    }
    encoded = (
        json.dumps(
            identity,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("ascii")
    document["closure_sha256"] = hashlib.sha256(encoded).hexdigest()


class HuggingFaceClosureResolutionTest(unittest.TestCase):
    def definition(
        self,
        *,
        checkpoint: str | None,
        load_format: str = "safetensors",
    ):
        return parse_inference_service_toml(
            service_payload(
                checkpoint=checkpoint,
                load_format=load_format,
            )
        )

    def test_indexed_resolution_preserves_exact_runtime_closure(self):
        client = indexed_client()

        closure = resolve_huggingface_closure(
            self.definition(checkpoint="weights/model.safetensors.index.json"),
            client=client,
        )
        document = closure.as_dict()

        self.assertEqual(
            document["source"],
            {
                "kind": "huggingface",
                "repository": REPOSITORY,
                "revision": REVISION,
            },
        )
        self.assertEqual(
            document["checkpoint"],
            {
                "requested_selector": ("weights/model.safetensors.index.json"),
                "resolved_index": ("weights/model.safetensors.index.json"),
                "weight_files": [
                    "weights/model-00001-of-00002.safetensors",
                    "weights/model-00002-of-00002.safetensors",
                ],
            },
        )
        self.assertEqual(
            [member["path"] for member in document["files"]],
            [
                "README.md",
                "config.json",
                "configuration_fixture.py",
                "tokenizer.json",
                "weights/model-00001-of-00002.safetensors",
                "weights/model-00002-of-00002.safetensors",
                "weights/model.safetensors.index.json",
            ],
        )
        self.assertNotIn(
            "alternate.safetensors",
            [member["path"] for member in document["files"]],
        )
        self.assertNotIn(
            "pytorch_model.bin",
            [member["path"] for member in document["files"]],
        )
        by_path = {member["path"]: member for member in document["files"]}
        self.assertEqual(
            by_path["config.json"]["identity"],
            {"algorithm": "git-blob-sha1", "digest": "3" * 40},
        )
        self.assertEqual(
            by_path["weights/model-00001-of-00002.safetensors"]["identity"],
            {"algorithm": "sha256", "digest": "a" * 64},
        )
        self.assertEqual(by_path["README.md"]["bytes"], 200)
        self.assertEqual(document["file_count"], 7)
        self.assertEqual(document["total_bytes"], 5_000)
        self.assertRegex(document["closure_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            client.calls,
            [
                ("model_info", REPOSITORY, REVISION),
                (
                    "json_file",
                    REPOSITORY,
                    REVISION,
                    "weights/model.safetensors.index.json",
                    False,
                ),
                ("file_size", REPOSITORY, REVISION, "README.md"),
            ],
        )

    def test_resolution_is_independent_of_hub_sibling_order(self):
        first_client = indexed_client()
        second_client = indexed_client()
        second_client.info["siblings"] = list(reversed(second_client.info["siblings"]))
        definition = self.definition(checkpoint="weights/model.safetensors.index.json")

        first = resolve_huggingface_closure(
            definition,
            client=first_client,
        )
        second = resolve_huggingface_closure(
            definition,
            client=second_client,
        )

        self.assertEqual(first.as_dict(), second.as_dict())
        identity = {
            "schema_version": HUGGINGFACE_CLOSURE_IDENTITY_SCHEMA,
            "source": first.as_dict()["source"],
            "checkpoint": first.as_dict()["checkpoint"],
            "files": first.as_dict()["files"],
        }
        expected = hashlib.sha256(
            (
                json.dumps(
                    identity,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
                + "\n"
            ).encode("ascii")
        ).hexdigest()
        self.assertEqual(first.closure_sha256, expected)

    def test_load_format_controls_automatic_checkpoint_admission(self):
        siblings = [
            blob("config.json", "2", 100),
            blob("model.safetensors.index.json", "3", 200),
            lfs("model-00001-of-00001.safetensors", "a", 1_000),
            blob("pytorch_model.bin.index.json", "4", 300),
            lfs("pytorch_model-00001-of-00001.bin", "b", 1_100),
        ]
        indexes = {
            "model.safetensors.index.json": {
                "weight_map": {"tensor": "model-00001-of-00001.safetensors"}
            },
            "pytorch_model.bin.index.json": {
                "weight_map": {"tensor": "pytorch_model-00001-of-00001.bin"}
            },
        }

        safetensors = resolve_huggingface_closure(
            self.definition(checkpoint=None, load_format="safetensors"),
            client=FakeMetadataClient(
                siblings,
                json_files=indexes,
            ),
        )
        self.assertEqual(
            safetensors.resolved_index,
            "model.safetensors.index.json",
        )
        self.assertEqual(
            safetensors.weight_files,
            ("model-00001-of-00001.safetensors",),
        )

        with self.assertRaises(RunpodLocalError) as caught:
            resolve_huggingface_closure(
                self.definition(checkpoint=None, load_format="auto"),
                client=FakeMetadataClient(
                    siblings,
                    json_files=indexes,
                ),
            )
        self.assertEqual(
            caught.exception.code,
            "invalid_huggingface_closure",
        )
        self.assertIn("more than one", str(caught.exception))

    def test_explicit_pytorch_checkpoint_requires_and_uses_auto(self):
        client = FakeMetadataClient(
            [
                blob("config.json", "2", 100),
                lfs("pytorch_model.bin", "a", 1_000),
                lfs("model.safetensors", "b", 900),
            ]
        )

        closure = resolve_huggingface_closure(
            self.definition(
                checkpoint="pytorch_model.bin",
                load_format="auto",
            ),
            client=client,
        )

        self.assertEqual(closure.weight_files, ("pytorch_model.bin",))
        self.assertEqual(
            [member.path for member in closure.files],
            ["config.json", "pytorch_model.bin"],
        )

    def test_revision_identity_and_metadata_fail_closed(self):
        definition = self.definition(checkpoint="model.safetensors")
        invalid_clients = (
            (
                FakeMetadataClient(
                    [lfs("model.safetensors", "a", 1_000)],
                    resolved_revision="2" * 40,
                ),
                "huggingface_revision_mismatch",
            ),
            (
                FakeMetadataClient(
                    [
                        {
                            "rfilename": "model.safetensors",
                            "size": 1_000,
                            "lfs": {"size": 1_000},
                        }
                    ]
                ),
                "invalid_huggingface_closure",
            ),
            (
                FakeMetadataClient(
                    [
                        {
                            "rfilename": "model.safetensors",
                            "size": 1_000,
                            "lfs": "malformed",
                            "blobId": "a" * 40,
                        }
                    ]
                ),
                "invalid_huggingface_closure",
            ),
            (
                FakeMetadataClient(
                    [
                        {
                            **lfs("model.safetensors", "a", 1_000),
                            "size": 999,
                        }
                    ]
                ),
                "invalid_huggingface_closure",
            ),
        )
        for client, expected_code in invalid_clients:
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(RunpodLocalError) as caught:
                    resolve_huggingface_closure(
                        definition,
                        client=client,
                    )
                self.assertEqual(caught.exception.code, expected_code)

    def test_index_requires_every_shard_to_resolve_uniquely(self):
        client = FakeMetadataClient(
            [
                blob("weights/model.safetensors.index.json", "2", 200),
                lfs("model-00001.safetensors", "a", 1_000),
                lfs("weights/model-00001.safetensors", "b", 1_000),
            ],
            json_files={
                "weights/model.safetensors.index.json": {
                    "weight_map": {
                        "tensor": "model-00001.safetensors",
                    }
                }
            },
        )

        with self.assertRaises(RunpodLocalError) as caught:
            resolve_huggingface_closure(
                self.definition(checkpoint="weights/model.safetensors.index.json"),
                client=client,
            )

        self.assertEqual(
            caught.exception.code,
            "invalid_huggingface_closure",
        )
        self.assertIn("uniquely", str(caught.exception))

    def test_index_rejects_shards_from_another_checkpoint_family(self):
        malformed_indexes = (
            (
                "model.safetensors.index.json",
                "model-00001-of-00001.bin",
                "safetensors",
            ),
            (
                "pytorch_model.bin.index.json",
                "model-00001-of-00001.safetensors",
                "auto",
            ),
        )
        for index_path, shard_path, load_format in malformed_indexes:
            with self.subTest(index_path=index_path):
                client = FakeMetadataClient(
                    [
                        blob(index_path, "2", 200),
                        lfs(shard_path, "a", 1_000),
                    ],
                    json_files={
                        index_path: {
                            "weight_map": {"tensor": shard_path},
                        }
                    },
                )

                with self.assertRaises(RunpodLocalError) as caught:
                    resolve_huggingface_closure(
                        self.definition(
                            checkpoint=index_path,
                            load_format=load_format,
                        ),
                        client=client,
                    )

                self.assertEqual(
                    caught.exception.code,
                    "invalid_huggingface_closure",
                )
                self.assertIn(
                    "another checkpoint format",
                    str(caught.exception),
                )

    def test_manifest_parser_rejects_semantic_and_digest_tampering(self):
        closure = resolve_huggingface_closure(
            self.definition(checkpoint="weights/model.safetensors.index.json"),
            client=indexed_client(),
        )

        wrong_role = copy.deepcopy(closure.as_dict())
        weight = next(
            member
            for member in wrong_role["files"]
            if member["role"] == "checkpoint-weight"
        )
        weight["role"] = "snapshot"
        recalculate_closure_digest(wrong_role)
        wrong_total = copy.deepcopy(closure.as_dict())
        wrong_total["total_bytes"] += 1
        wrong_digest = copy.deepcopy(closure.as_dict())
        wrong_digest["closure_sha256"] = "0" * 64
        mixed_index = copy.deepcopy(closure.as_dict())
        original_path = "weights/model-00001-of-00002.safetensors"
        mixed_path = "weights/model-00001-of-00002.bin"
        mixed_index["checkpoint"]["weight_files"][0] = mixed_path
        for member in mixed_index["files"]:
            if member["path"] == original_path:
                member["path"] = mixed_path
        mixed_index["files"].sort(key=lambda member: member["path"])
        recalculate_closure_digest(mixed_index)

        for document in (
            wrong_role,
            wrong_total,
            wrong_digest,
            mixed_index,
        ):
            with self.subTest(document=document):
                with self.assertRaises(RunpodLocalError) as caught:
                    parse_huggingface_closure(document)
                self.assertEqual(
                    caught.exception.code,
                    "invalid_huggingface_closure",
                )


class HuggingFaceClosureFileTest(unittest.TestCase):
    def closure(self, *, repository: str = REPOSITORY) -> HuggingFaceClosure:
        return HuggingFaceClosure(
            repository=repository,
            revision=REVISION,
            requested_selector="model.safetensors",
            resolved_index=None,
            weight_files=("model.safetensors",),
            files=(
                HuggingFaceClosureFile(
                    path="config.json",
                    bytes=100,
                    role="snapshot",
                    identity_algorithm="git-blob-sha1",
                    identity_digest="2" * 40,
                ),
                HuggingFaceClosureFile(
                    path="model.safetensors",
                    bytes=1_000,
                    role="checkpoint-weight",
                    identity_algorithm="sha256",
                    identity_digest="a" * 64,
                ),
            ),
        )

    def test_canonical_writer_and_safe_loader_are_idempotent(self):
        closure = self.closure()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            output = default_huggingface_closure_path(root, closure)

            installed = write_huggingface_closure(output, closure)
            first_metadata = output.stat()
            repeated = write_huggingface_closure(output, closure)

            self.assertEqual(installed, output)
            self.assertEqual(repeated, output)
            self.assertEqual(
                stat.S_IMODE(output.stat().st_mode),
                0o600,
            )
            self.assertEqual(
                output.stat().st_mtime_ns,
                first_metadata.st_mtime_ns,
            )
            self.assertEqual(
                load_huggingface_closure(output).as_dict(),
                closure.as_dict(),
            )
            expected = (
                json.dumps(
                    closure.as_dict(),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
                + "\n"
            ).encode("ascii")
            self.assertEqual(output.read_bytes(), expected)

    def test_writer_refuses_an_existing_different_identity(self):
        first = self.closure()
        second = self.closure(repository="fixture-lab/another-model")
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "closure.json"
            write_huggingface_closure(output, first)

            with self.assertRaises(RunpodLocalError) as caught:
                write_huggingface_closure(output, second)

            self.assertEqual(
                caught.exception.code,
                "huggingface_closure_output_collision",
            )
            self.assertEqual(
                load_huggingface_closure(output).as_dict(),
                first.as_dict(),
            )

    def test_writer_does_not_clobber_a_competing_install(self):
        requested = self.closure()
        competing = self.closure(repository="fixture-lab/competing-model")
        competing_payload = (
            json.dumps(
                competing.as_dict(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            + "\n"
        ).encode("ascii")
        real_link = os.link
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "closure.json"

            def install_competitor(
                source: os.PathLike[str] | str,
                destination: os.PathLike[str] | str,
                *,
                follow_symlinks: bool,
            ) -> None:
                descriptor = os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                try:
                    offset = 0
                    while offset < len(competing_payload):
                        offset += os.write(
                            descriptor,
                            competing_payload[offset:],
                        )
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                real_link(
                    source,
                    destination,
                    follow_symlinks=follow_symlinks,
                )

            with (
                mock.patch(
                    "runpod_local.service_huggingface.os.link",
                    side_effect=install_competitor,
                ),
                self.assertRaises(RunpodLocalError) as caught,
            ):
                write_huggingface_closure(output, requested)

            self.assertEqual(
                caught.exception.code,
                "huggingface_closure_output_collision",
            )
            self.assertEqual(
                load_huggingface_closure(output).as_dict(),
                competing.as_dict(),
            )

    def test_writer_rejects_even_a_broken_symlink_destination(self):
        closure = self.closure()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            output = root / "closure.json"
            output.symlink_to(root / "absent.json")

            with self.assertRaises(RunpodLocalError) as caught:
                write_huggingface_closure(output, closure)

            self.assertEqual(
                caught.exception.code,
                "unsafe_huggingface_closure",
            )
            self.assertTrue(output.is_symlink())

    def test_loader_rejects_symlink_writable_and_duplicate_field_inputs(self):
        closure = self.closure()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            valid = root / "valid.json"
            write_huggingface_closure(valid, closure)

            symlink = root / "closure-link.json"
            symlink.symlink_to(valid)
            writable = root / "writable.json"
            writable.write_bytes(valid.read_bytes())
            writable.chmod(0o622)
            duplicate = root / "duplicate.json"
            duplicate.write_text(
                '{"schema_version":"runpod.huggingface-closure.v1",'
                '"schema_version":"runpod.huggingface-closure.v1"}\n',
                encoding="utf-8",
            )
            duplicate.chmod(0o600)

            for path, code in (
                (symlink, "unsafe_huggingface_closure"),
                (writable, "unsafe_huggingface_closure"),
                (duplicate, "invalid_huggingface_closure"),
            ):
                with self.subTest(path=path):
                    with self.assertRaises(RunpodLocalError) as caught:
                        load_huggingface_closure(path)
                    self.assertEqual(caught.exception.code, code)


class HuggingFaceClosureCliTest(unittest.TestCase):
    def test_offline_cli_resolves_cached_metadata_without_model_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state_root = root / "state"
            config_root = root / "config"
            config_root.mkdir(mode=0o700)
            config = root / "service.toml"
            config.write_bytes(service_payload(checkpoint="model.safetensors"))
            config.chmod(0o600)
            model_info_url = (
                f"{HUGGING_FACE_BASE}/api/models/{REPOSITORY}"
                f"/revision/{REVISION}?blobs=true"
            )
            JsonCache(
                state_root / "cache" / "huggingface",
            ).put(
                model_info_url,
                {
                    "sha": REVISION,
                    "siblings": [
                        blob("config.json", "2", 100),
                        lfs("model.safetensors", "a", 1_000),
                    ],
                },
            )
            environment = {
                "HOME": str(root),
                "PATH": os.environ["PATH"],
                "PYTHONDONTWRITEBYTECODE": "1",
                "XDG_CONFIG_HOME": str(config_root),
            }

            completed = subprocess.run(
                [
                    str(ROOT / "bin" / "runpod-service"),
                    "resolve",
                    str(config),
                    "--offline",
                    "--state-root",
                    str(state_root),
                    "--json",
                ],
                cwd=ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            result = json.loads(completed.stdout)
            output = pathlib.Path(result["output_path"])

            self.assertEqual(
                result["schema_version"],
                "runpod.huggingface-closure-resolution.v1",
            )
            self.assertIs(result["metadata_only"], True)
            self.assertEqual(result["model_bytes_downloaded"], 0)
            self.assertEqual(result["closure"]["file_count"], 2)
            self.assertEqual(result["closure"]["total_bytes"], 1_100)
            self.assertTrue(output.is_file())
            self.assertEqual(
                load_huggingface_closure(output).as_dict(),
                result["closure"],
            )
            self.assertEqual(
                output,
                default_huggingface_closure_path(
                    state_root,
                    load_huggingface_closure(output),
                ),
            )


if __name__ == "__main__":
    unittest.main()
