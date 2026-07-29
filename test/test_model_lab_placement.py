"""Model placement policy owned by model-lab."""

from __future__ import annotations

import unittest

from model_lab.huggingface_model import GIB
from model_lab.placement import load_hardware_catalog, place_model


def estimate(weight_gib, kv_gib=8, *, kv_available=True, weight_format="native"):
    kv_cache = {
        "available": kv_available,
        "reason": "fixture unsupported layout" if not kv_available else None,
        "bytes": int(kv_gib * GIB) if kv_available else None,
        "context_tokens": 32768,
        "sequences": 1,
        "dtype": "bf16",
    }
    return {
        "repository": {
            "id": "example/model",
            "resolved_revision": "0123456789abcdef",
        },
        "runtime_estimate": {
            "weight_format": weight_format,
            "weight_bytes": int(weight_gib * GIB),
            "kv_cache": kv_cache,
        },
    }


class PlacementTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = load_hardware_catalog()

    def test_32b_bf16_is_candidate_on_pro6000(self):
        report = place_model(
            estimate(61, 8),
            catalog=self.catalog,
            requested_gpus=["pro6000"],
        )
        placement = report["placements"][0]
        self.assertEqual(
            placement["gpu_id"],
            "NVIDIA RTX PRO 6000 Blackwell Server Edition",
        )
        self.assertEqual(placement["status"], "candidate")
        self.assertGreater(placement["headroom_bytes_per_gpu"], 0)

    def test_70b_bf16_boundary_separates_pro_h200_and_b200(self):
        report = place_model(
            estimate(130, 10),
            catalog=self.catalog,
            requested_gpus=["pro6000", "h200", "b200"],
        )
        placements = {
            placement["display_name"]: placement
            for placement in report["placements"]
        }
        self.assertEqual(placements["RTX PRO 6000 Server"]["status"], "impossible")
        self.assertEqual(placements["H200 SXM"]["status"], "tight")
        self.assertEqual(placements["B200"]["status"], "candidate")

    def test_unknown_cache_stays_indeterminate(self):
        report = place_model(
            estimate(20, kv_available=False),
            catalog=self.catalog,
            requested_gpus=["pro6000"],
        )
        self.assertEqual(report["placements"][0]["status"], "indeterminate")

    def test_multi_gpu_static_fit_stays_indeterminate(self):
        report = place_model(
            estimate(150, 8),
            catalog=self.catalog,
            requested_gpus=["h200"],
            gpu_count=2,
        )
        self.assertEqual(report["placements"][0]["status"], "indeterminate")
        self.assertIn("multi-GPU", report["placements"][0]["reasons"][0])

    def test_catalog_distinguishes_all_pro6000_variants(self):
        identifiers = {gpu["id"] for gpu in self.catalog["gpus"]}
        self.assertIn(
            "NVIDIA RTX PRO 6000 Blackwell Server Edition", identifiers
        )
        self.assertIn(
            "NVIDIA RTX PRO 6000 Blackwell Workstation Edition", identifiers
        )
        self.assertIn(
            "NVIDIA RTX PRO 6000 Blackwell Max-Q Workstation Edition",
            identifiers,
        )


if __name__ == "__main__":
    unittest.main()
