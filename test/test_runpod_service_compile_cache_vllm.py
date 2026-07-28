from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runpod"))

from runpod_local.errors import RunpodLocalError  # noqa: E402
from service_runtime.compile_cache_files import (  # noqa: E402
    inventory_compile_cache,
)
from service_runtime.execution_environment import (  # noqa: E402
    runtime_execution_environment,
)
from service_runtime.vllm import (  # noqa: E402
    build_vllm_environment,
    read_vllm_cache_evidence,
)


CACHE_ROOT = pathlib.PurePosixPath("/root/runpod-session/cache/compiled/test")
ARTIFACT = "vllm/torch_compile_cache/aot/model"


def environment(mode: str) -> dict[str, str]:
    return build_vllm_environment(
        session_root=pathlib.PurePosixPath("/root/runpod-session"),
        compile_root=CACHE_ROOT,
        service_id="fixture-service",
        process_nonce="fixture-nonce",
        manifest_sha256="1" * 64,
        cache_mode=mode,
    )


class VllmCompileCacheModeTest(unittest.TestCase):
    def test_launch_additions_do_not_shadow_verified_runtime_environment(self):
        self.assertFalse(
            set(environment("ephemeral"))
            & set(runtime_execution_environment({}).values)
        )

    def test_force_aot_load_exists_only_for_require_modes(self):
        for mode in ("ephemeral", "author"):
            with self.subTest(mode=mode):
                self.assertNotIn("VLLM_FORCE_AOT_LOAD", environment(mode))
        for mode in ("candidate-proof", "accepted"):
            with self.subTest(mode=mode):
                self.assertEqual(environment(mode)["VLLM_FORCE_AOT_LOAD"], "1")

    def test_unsupported_mode_fails_loudly(self):
        with self.assertRaises(RunpodLocalError):
            environment("invented")


class VllmCompileCacheEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = pathlib.Path(self.temporary.name)
        self.cache = root / "cache"
        self.cache.mkdir(mode=0o700)
        for name in (
            "cuda",
            "flashinfer",
            "torch",
            "torchinductor",
            "triton",
            "vllm",
            "xdg",
        ):
            (self.cache / name).mkdir(mode=0o700)
        artifact = self.cache.joinpath(*pathlib.PurePosixPath(ARTIFACT).parts)
        artifact.parent.mkdir(parents=True, mode=0o700)
        for parent in artifact.parents:
            if parent == self.cache:
                break
            parent.chmod(0o700)
        artifact.write_bytes(b"aot")
        artifact.chmod(0o600)
        self.inventory = inventory_compile_cache(self.cache)
        self.log = root / "service.log"

    def write_log(self, value: str) -> None:
        self.log.write_text(value, encoding="utf-8")
        self.log.chmod(0o600)

    def test_author_and_require_use_the_proven_save_and_load_markers(self):
        artifact = f"{CACHE_ROOT}/{ARTIFACT}"
        self.write_log(
            "torch.compile and initial profiling/warmup run together took 12 s\n"
            f"saved AOT compiled function to {artifact}\n"
        )
        author = read_vllm_cache_evidence(
            log_path=self.log,
            cache_root=CACHE_ROOT,
            inventory=self.inventory,
            mode="author",
        )
        self.assertEqual(author["produced_artifacts"], [ARTIFACT])
        self.assertTrue(author["cold_compile_observed"])

        self.write_log(f"Directly load AOT compilation from path {artifact}\n")
        required = read_vllm_cache_evidence(
            log_path=self.log,
            cache_root=CACHE_ROOT,
            inventory=self.inventory,
            mode="candidate-proof",
        )
        self.assertEqual(required["loaded_artifacts"], [ARTIFACT])
        self.assertFalse(required["cold_compile_observed"])

    def test_require_rejects_compile_fallback_and_outside_paths(self):
        artifact = f"{CACHE_ROOT}/{ARTIFACT}"
        self.write_log(
            f"Directly load AOT compilation from path {artifact}\n"
            "Compiling model again due to a load failure\n"
        )
        with self.assertRaises(RunpodLocalError):
            read_vllm_cache_evidence(
                log_path=self.log,
                cache_root=CACHE_ROOT,
                inventory=self.inventory,
                mode="candidate-proof",
            )

        self.write_log("Directly load AOT compilation from path /tmp/outside/model\n")
        with self.assertRaises(RunpodLocalError):
            read_vllm_cache_evidence(
                log_path=self.log,
                cache_root=CACHE_ROOT,
                inventory=self.inventory,
                mode="accepted",
            )

    def test_ephemeral_records_cold_output_without_claiming_acceptance(self):
        artifact = f"{CACHE_ROOT}/{ARTIFACT}"
        self.write_log(f"saved AOT compiled function to {artifact}\n")
        evidence = read_vllm_cache_evidence(
            log_path=self.log,
            cache_root=CACHE_ROOT,
            inventory=self.inventory,
            mode="ephemeral",
        )
        self.assertEqual(evidence["produced_artifacts"], [ARTIFACT])
        self.assertTrue(evidence["cold_compile_observed"])


if __name__ == "__main__":
    unittest.main()
