from __future__ import annotations

import copy
import pathlib
import unittest

from runpod_local.errors import RunpodLocalError
from runpod_local.service_compile_cache import (
    COMPILE_CACHE_SCHEMA,
    build_compile_cache_contract,
)


RUNTIME = {
    "runtime_id": "vllm-cu129-v0.25.1",
    "image": "vllm/vllm-openai@sha256:" + "1" * 64,
    "manifest": {"sha256": "2" * 64},
}
GPU = {
    "name": "Fixture RTX",
    "compute_capability": [12, 0],
    "memory_mib": 97887,
    "driver_version": "580.126.09",
}
CLOSURE_SHA256 = "3" * 64
LAUNCH_SHA256 = "4" * 64


def contract(
    *,
    runtime: dict[str, object] | None = None,
    closure_sha256: str = CLOSURE_SHA256,
    launch_sha256: str = LAUNCH_SHA256,
    gpu: dict[str, object] | None = None,
) -> dict[str, object]:
    return build_compile_cache_contract(
        driver="vllm-openai.v1",
        runtime=runtime or copy.deepcopy(RUNTIME),
        huggingface_closure_sha256=closure_sha256,
        compile_affecting_launch_sha256=launch_sha256,
        observed_gpu=gpu or copy.deepcopy(GPU),
    )


class ServiceCompileCacheTest(unittest.TestCase):
    def test_contract_is_driver_first_and_product_reusable(self):
        value = contract()

        self.assertEqual(value["schema_version"], COMPILE_CACHE_SCHEMA)
        self.assertRegex(value["cache_id"], r"^[0-9a-f]{64}$")
        identity = value["identity"]
        self.assertEqual(identity["gpu"]["driver_version"], "580.126.09")
        self.assertEqual(
            identity["gpu"]["compute_capability"],
            [12, 0],
        )
        self.assertNotIn("uuid", identity["gpu"])
        persistent = pathlib.PurePosixPath(value["persistent_root"])
        self.assertEqual(
            persistent.parts[:6],
            (
                "/",
                "workspace",
                ".cache",
                "compiled",
                "vllm-openai",
                "v1",
            ),
        )
        self.assertEqual(persistent.parts[6], "driver-580.126.09")
        self.assertEqual(persistent.parts[7], "sm120")
        self.assertEqual(persistent.name, value["cache_id"])
        self.assertTrue(
            value["local_root"].startswith(
                "/root/runpod-session/cache/compiled/vllm-openai/v1/"
            )
        )

    def test_every_compile_input_changes_the_cache_identity(self):
        baseline = contract()["cache_id"]
        variants: list[dict[str, object]] = []

        variants.append(contract(closure_sha256="5" * 64))
        variants.append(contract(launch_sha256="6" * 64))
        changed_runtime = copy.deepcopy(RUNTIME)
        changed_runtime["manifest"]["sha256"] = "7" * 64
        variants.append(contract(runtime=changed_runtime))
        changed_image = copy.deepcopy(RUNTIME)
        changed_image["image"] = "vllm/vllm-openai@sha256:" + "8" * 64
        variants.append(contract(runtime=changed_image))
        for field, value in (
            ("name", "Different Fixture RTX"),
            ("compute_capability", [10, 0]),
            ("memory_mib", 100000),
            ("driver_version", "581.1.0"),
        ):
            changed_gpu = copy.deepcopy(GPU)
            changed_gpu[field] = value
            variants.append(contract(gpu=changed_gpu))

        for variant in variants:
            with self.subTest(identity=variant["identity"]):
                self.assertNotEqual(variant["cache_id"], baseline)

    def test_physical_gpu_uuid_is_deliberately_not_an_input(self):
        unexpected = copy.deepcopy(GPU)
        unexpected["uuid"] = "GPU-physical-instance"

        with self.assertRaises(RunpodLocalError) as caught:
            contract(gpu=unexpected)

        self.assertEqual(
            caught.exception.code,
            "invalid_compile_cache_identity",
        )

    def test_invalid_or_mutable_identities_fail_closed(self):
        invalid_calls = (
            lambda: build_compile_cache_contract(
                driver="other.v1",
                runtime=copy.deepcopy(RUNTIME),
                huggingface_closure_sha256=CLOSURE_SHA256,
                compile_affecting_launch_sha256=LAUNCH_SHA256,
                observed_gpu=copy.deepcopy(GPU),
            ),
            lambda: contract(closure_sha256="not-a-hash"),
            lambda: contract(
                runtime={
                    **copy.deepcopy(RUNTIME),
                    "image": "vllm/vllm-openai:latest",
                }
            ),
            lambda: contract(gpu={**copy.deepcopy(GPU), "driver_version": "../580"}),
            lambda: contract(gpu={**copy.deepcopy(GPU), "compute_capability": [0, 0]}),
        )

        for operation in invalid_calls:
            with self.subTest(operation=operation):
                with self.assertRaises(RunpodLocalError) as caught:
                    operation()
                self.assertEqual(
                    caught.exception.code,
                    "invalid_compile_cache_identity",
                )


if __name__ == "__main__":
    unittest.main()
