"""Model workload estimates owned by model-lab."""

from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

from model_lab.cache import JsonCache
from model_lab.errors import ModelLabError
from model_lab.huggingface_model import GIB
from model_lab.workload import (
    HuggingFaceWorkload,
    WorkloadPlacementRequest,
    plan_workload,
)


PRO_GPU_ID = "NVIDIA RTX PRO 6000 Blackwell Server Edition"
H200_GPU_ID = "NVIDIA H200"


def hardware_catalog():
    return {
        "schema_version": "runpod.hardware.v1",
        "catalog_as_of": "2026-07-26",
        "source": "test fixture",
        "gpus": [
            {
                "id": PRO_GPU_ID,
                "display_name": "RTX PRO 6000 Server",
                "provider_memory_gb": 96,
                "aliases": ["pro6000"],
                "capabilities": ["bf16", "fp8", "int8"],
            },
            {
                "id": H200_GPU_ID,
                "display_name": "H200 SXM",
                "provider_memory_gb": 141,
                "aliases": ["h200"],
                "capabilities": ["bf16", "fp8", "int8"],
            },
        ],
    }


def model_estimate():
    return {
        "repository": {
            "id": "example/model",
            "requested_revision": "release",
            "resolved_revision": "0123456789abcdef",
        },
        "checkpoint": {
            "kind": "indexed",
            "index_file": "weights/model.index.json",
        },
        "runtime_estimate": {
            "weight_format": "q8",
            "weight_bytes": 40 * GIB,
            "kv_cache": {
                "available": True,
                "reason": None,
                "bytes": 2 * GIB,
                "context_tokens": 65536,
                "sequences": 3,
                "dtype": "fp8",
            },
        },
    }


class WorkloadPlacementTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cache = JsonCache(
            pathlib.Path(self.temporary.name) / "huggingface"
        )

    def request(self, **overrides):
        arguments = {
            "allowed_gpu_ids": (PRO_GPU_ID, H200_GPU_ID),
            "requested_gpus": ("pro6000",),
            "gpu_count": 1,
            "model": HuggingFaceWorkload(
                repository="example/model",
                revision="release",
                index_file="weights/model.index.json",
                context_tokens=65536,
                sequences=3,
                kv_dtype="fp8",
                weight_format="q8",
                offline=True,
                refresh=False,
            ),
        }
        arguments.update(overrides)
        return WorkloadPlacementRequest(**arguments)

    def test_maps_every_inspection_and_placement_input_without_argparse(self):
        transport = mock.Mock()
        client = object()
        inspector = mock.Mock()
        inspector.inspect.return_value = model_estimate()
        with (
            mock.patch(
                "model_lab.workload.HuggingFaceClient",
                return_value=client,
            ) as client_type,
            mock.patch(
                "model_lab.workload.ModelInspector",
                return_value=inspector,
            ) as inspector_type,
        ):
            result = plan_workload(
                self.request(),
                cache=self.cache,
                catalog=hardware_catalog(),
                transport=transport,
            )

        client_type.assert_called_once_with(
            cache=self.cache,
            transport=transport,
            offline=True,
            refresh=False,
        )
        inspector_type.assert_called_once_with(client)
        inspector.inspect.assert_called_once_with(
            "example/model",
            revision="release",
            index_file="weights/model.index.json",
            context_tokens=65536,
            sequences=3,
            kv_dtype="fp8",
            weight_format="q8",
        )
        self.assertEqual(result.admitted_gpu_ids, {PRO_GPU_ID})
        self.assertEqual(
            result.model_summary["admitted_gpu_ids"], [PRO_GPU_ID]
        )
        self.assertEqual(
            result.model_summary["admitted_statuses"], ["candidate"]
        )
        self.assertEqual(
            {
                item["gpu_id"] for item in result.model_summary["placements"]
            },
            {PRO_GPU_ID, H200_GPU_ID},
        )
        self.assertEqual(
            result.model_summary["placement_policy"]["id"],
            "runpod-static-v1",
        )
        self.assertEqual(
            result.model_summary["checkpoint"],
            model_estimate()["checkpoint"],
        )

    def test_indeterminate_fit_requires_explicit_admission(self):
        inspector = mock.Mock()
        inspector.inspect.return_value = model_estimate()
        with mock.patch(
            "model_lab.workload.ModelInspector",
            return_value=inspector,
        ):
            blocked = plan_workload(
                self.request(
                    gpu_count=2,
                    requested_gpus=(),
                    allow_indeterminate_fit=False,
                ),
                cache=self.cache,
                catalog=hardware_catalog(),
            )
            admitted = plan_workload(
                self.request(
                    gpu_count=2,
                    requested_gpus=(),
                    allow_indeterminate_fit=True,
                ),
                cache=self.cache,
                catalog=hardware_catalog(),
            )

        self.assertEqual(blocked.admitted_gpu_ids, set())
        self.assertEqual(
            blocked.model_summary["admitted_statuses"], ["candidate"]
        )
        self.assertEqual(
            admitted.admitted_gpu_ids, {PRO_GPU_ID, H200_GPU_ID}
        )
        self.assertEqual(
            admitted.model_summary["admitted_statuses"],
            ["candidate", "indeterminate"],
        )

    def test_gpu_selection_without_model_is_still_validated(self):
        result = plan_workload(
            self.request(model=None),
            cache=self.cache,
            catalog=hardware_catalog(),
        )
        self.assertEqual(result.admitted_gpu_ids, {PRO_GPU_ID})
        self.assertIsNone(result.model_summary)

        with self.assertRaises(ModelLabError) as caught:
            plan_workload(
                self.request(
                    model=None,
                    allowed_gpu_ids=(PRO_GPU_ID,),
                    requested_gpus=("h200",),
                ),
                cache=self.cache,
                catalog=hardware_catalog(),
            )
        self.assertEqual(caught.exception.code, "gpu_not_allowed")

    def test_unrestricted_non_model_request_needs_no_hardware_catalog(self):
        with mock.patch(
            "model_lab.workload.load_hardware_catalog",
            side_effect=AssertionError("catalog must remain lazy"),
        ):
            result = plan_workload(
                self.request(model=None, requested_gpus=()),
                cache=self.cache,
            )
        self.assertIsNone(result.admitted_gpu_ids)
        self.assertIsNone(result.model_summary)

    def test_empty_allowed_gpu_set_fails_closed(self):
        with self.assertRaises(ModelLabError) as caught:
            plan_workload(
                self.request(
                    allowed_gpu_ids=(),
                    model=None,
                    requested_gpus=(),
                ),
                cache=self.cache,
            )
        self.assertEqual(
            caught.exception.code, "invalid_workload_placement"
        )

if __name__ == "__main__":
    unittest.main()
