#!/usr/bin/env python3
"""Verify the packages and GPU exposed by the pinned upstream vLLM image."""

from __future__ import annotations

import argparse
import importlib.metadata
import inspect
import json
import pathlib
import shutil
import sys


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError as exception:
        raise SystemExit(f"required distribution is absent: {name}") from exception


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a pinned upstream Runpod vLLM runtime."
    )
    parser.add_argument(
        "--manifest",
        required=True,
        type=pathlib.Path,
        help="runtime-manifest.json copied from the administration layer",
    )
    parser.add_argument(
        "--gpu",
        action="store_true",
        help="also require a CUDA GPU and import the compiled runtime",
    )
    arguments = parser.parse_args()

    manifest = json.loads(arguments.manifest.read_text())
    require(
        manifest.get("schema_version") == "runpod.upstream-runtime.v1",
        "runtime manifest schema is unsupported",
    )
    versions = manifest["versions"]
    require(
        f"{sys.version_info.major}.{sys.version_info.minor}" == versions["python"],
        "Python runtime does not match the upstream manifest",
    )

    observed_versions = {
        "vllm": distribution_version("vllm"),
        "torch": distribution_version("torch"),
        "flashinfer": distribution_version("flashinfer-python"),
        "flashinfer_jit_cache": distribution_version("flashinfer-jit-cache"),
    }
    for name, observed in observed_versions.items():
        require(
            observed == versions[name],
            f"{name} version {observed!r} does not match {versions[name]!r}",
        )

    executables = {
        name: shutil.which(name) for name in ("hf", "sshd", "vllm")
    }
    require(all(executables.values()), "required runtime executable is absent")

    report: dict[str, object] = {
        "schema_version": "runpod.upstream-runtime-verification.v1",
        "requested_image": manifest["image"],
        "runtime_id": manifest["runtime_id"],
        "versions": observed_versions,
        "executables": executables,
        "gpu_verified": False,
    }

    if arguments.gpu:
        import flashinfer
        import flashinfer_jit_cache
        import torch
        import vllm
        import vllm._C_stable_libtorch
        import vllm._moe_C_stable_libtorch
        from vllm.model_executor.models import qwen3_5_mtp

        require(vllm.__version__ == versions["vllm"], "vLLM import drift")
        require(
            flashinfer.__version__ == versions["flashinfer"],
            "FlashInfer import drift",
        )
        require(
            flashinfer_jit_cache.__version__ == versions["flashinfer_jit_cache"],
            "FlashInfer JIT-cache import drift",
        )
        require(torch.cuda.is_available(), "CUDA is unavailable")
        require(
            torch.version.cuda == versions["cuda"],
            "Torch CUDA runtime does not match the upstream manifest",
        )
        require(torch.cuda.is_bf16_supported(), "GPU lacks BF16 support")
        mtp_source = inspect.getsource(qwen3_5_mtp.Qwen3_5MultiTokenPredictor)
        require(
            'quant_config.get_name() == "modelopt_fp4"' in mtp_source,
            "ModelOpt NVFP4 MTP BF16 workaround is absent",
        )
        report["gpu_verified"] = True
        report["gpu"] = {
            "name": torch.cuda.get_device_name(0),
            "capability": list(torch.cuda.get_device_capability(0)),
            "bytes": torch.cuda.get_device_properties(0).total_memory,
        }

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
