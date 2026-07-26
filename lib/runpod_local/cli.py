"""Command-line entry point for the local Runpod control plane."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from . import __version__
from .agents import AGENT_DOCS
from .cache import JsonCache
from .errors import RunpodLocalError
from .http import JsonHttpTransport
from .lifecycle_cli import (
    LIFECYCLE_COMMANDS,
    add_lifecycle_parsers,
    run_lifecycle_command,
)
from .model import HuggingFaceClient, ModelInspector
from .output import print_json, print_model_human, print_placement_human
from .paths import state_root
from .placement import load_hardware_catalog, place_model
from .provider_cli import (
    PROVIDER_COMMANDS,
    add_provider_parsers,
    run_provider_command,
)


def _add_model_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "repository",
        nargs="?",
        help="Hugging Face repository in exact namespace/name form.",
    )
    parser.add_argument(
        "--revision",
        default="main",
        help="Branch, tag, or commit to resolve once (default: main).",
    )
    parser.add_argument(
        "--index-file",
        help="Exact checkpoint index path when root selection is ambiguous.",
    )
    parser.add_argument(
        "--context",
        type=int,
        default=32768,
        metavar="TOKENS",
        help="Requested context tokens per sequence (default: 32768).",
    )
    parser.add_argument(
        "--sequences",
        type=int,
        default=1,
        help="Concurrent KV-cache sequence count (default: 1).",
    )
    parser.add_argument(
        "--kv-dtype",
        choices=("bf16", "fp16", "fp8"),
        default="bf16",
        help="KV cache dtype for the estimate (default: bf16).",
    )
    parser.add_argument(
        "--weight-format",
        choices=("native", "bf16", "fp8", "int8", "q8"),
        default="native",
        help="Native checkpoint or hypothetical uniform storage projection.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use only private cached Hugging Face metadata.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh mutable Hugging Face model metadata.",
    )
    parser.add_argument(
        "--state-root",
        metavar="PATH",
        help="Override RUNPOD_HOME (default: ~/.local/runpod).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the versioned machine-readable result.",
    )
    parser.add_argument(
        "--agents-md",
        action="store_true",
        help="Print the agent operating contract and exit.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="runpod",
        description=(
            "Inspect models, plan GPU placement, and manage private Runpod sessions."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--agents-md",
        action="store_true",
        help="Print the complete agent operating contract and exit.",
    )
    subparsers = parser.add_subparsers(dest="command")

    model_parser = subparsers.add_parser(
        "model",
        help="Inspect an exact Hugging Face checkpoint and estimate inference memory.",
    )
    _add_model_arguments(model_parser)

    place_parser = subparsers.add_parser(
        "place",
        help="Compare a model estimate with the static Runpod GPU memory catalog.",
    )
    _add_model_arguments(place_parser)
    place_parser.add_argument(
        "--gpu",
        action="append",
        default=[],
        help="GPU ID, display name, or catalog alias; repeat to compare.",
    )
    place_parser.add_argument(
        "--gpu-count",
        type=int,
        default=1,
        help="Tensor-parallel GPU count (multi-GPU remains indeterminate).",
    )
    place_parser.add_argument(
        "--gpu-memory-utilization",
        type=float,
        default=0.90,
        help="Fraction of reported VRAM available to the runtime (default: 0.90).",
    )
    place_parser.add_argument(
        "--weight-slack",
        type=float,
        default=1.03,
        help="Multiplier from serialized tensors to the weight envelope.",
    )
    place_parser.add_argument(
        "--framework-reserve-gib",
        type=float,
        default=4.0,
        help="Fixed per-GPU framework/workspace reserve (default: 4 GiB).",
    )
    place_parser.add_argument(
        "--list-gpus",
        action="store_true",
        help="List catalog IDs and aliases without inspecting a model.",
    )
    add_provider_parsers(subparsers)
    add_lifecycle_parsers(subparsers)
    return parser


def _model_inspector(args: argparse.Namespace) -> ModelInspector:
    root = state_root(args.state_root)
    cache = JsonCache(root / "cache" / "huggingface")
    client = HuggingFaceClient(
        cache=cache,
        transport=JsonHttpTransport(),
        offline=args.offline,
        refresh=args.refresh,
    )
    return ModelInspector(client)


def _inspect(args: argparse.Namespace) -> dict[str, Any]:
    if not args.repository:
        raise RunpodLocalError(
            "a Hugging Face repository is required",
            code="missing_repository",
        )
    return _model_inspector(args).inspect(
        args.repository,
        revision=args.revision,
        index_file=args.index_file,
        context_tokens=args.context,
        sequences=args.sequences,
        kv_dtype=args.kv_dtype,
        weight_format=args.weight_format,
    )


def _print_gpu_catalog(as_json: bool) -> None:
    catalog = load_hardware_catalog()
    if as_json:
        print_json(catalog)
        return
    print("GPU ID                                                   VRAM  ALIASES")
    for gpu in catalog["gpus"]:
        print(
            f"{gpu['id'][:56]:<56} {gpu['provider_memory_gb']:>4g}G  "
            f"{', '.join(gpu.get('aliases', []))}"
        )


def run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.agents_md:
        print(AGENT_DOCS.get(args.command or "root", AGENT_DOCS["root"]).rstrip())
        return 0
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "model":
        report = _inspect(args)
        if args.json:
            print_json(report)
        else:
            print_model_human(report)
        return 0
    if args.command == "place":
        if args.list_gpus:
            _print_gpu_catalog(args.json)
            return 0
        model_report = _inspect(args)
        placement_report = place_model(
            model_report,
            requested_gpus=args.gpu,
            gpu_count=args.gpu_count,
            gpu_memory_utilization=args.gpu_memory_utilization,
            weight_slack=args.weight_slack,
            framework_reserve_gib=args.framework_reserve_gib,
        )
        if args.json:
            print_json(placement_report)
        else:
            print_placement_human(placement_report)
        return 0
    if args.command in PROVIDER_COMMANDS:
        return run_provider_command(args)
    if args.command in LIFECYCLE_COMMANDS:
        return run_lifecycle_command(args)
    raise RunpodLocalError(
        f"unsupported command: {args.command}",
        code="unsupported_command",
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(arguments)
    try:
        return run(args, parser)
    except RunpodLocalError as error:
        wants_json = "--json" in arguments
        if wants_json:
            print_json(
                {
                    "schema_version": "runpod.error.v1",
                    "error": {"code": error.code, "message": str(error)},
                }
            )
        else:
            print(f"runpod: error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
