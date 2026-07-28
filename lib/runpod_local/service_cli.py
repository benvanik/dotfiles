"""CLI surface for config-only inference-service validation and planning."""

from __future__ import annotations

import argparse
import pathlib
from typing import Any

from .cache import JsonCache
from .errors import RunpodLocalError
from .http import JsonHttpTransport
from .huggingface_credentials import configured_huggingface_token
from .model import HuggingFaceClient
from .output import print_json
from .paths import dotfiles_root, state_root
from .runtime_catalog import load_runtime
from .service_bundle import build_service_bundle_plan
from .service_controller import (
    build_service_deployment_plan,
    build_service_validation,
)
from .service_definition import load_inference_service
from .service_huggingface import (
    default_huggingface_closure_path,
    load_huggingface_closure,
    resolve_huggingface_closure,
    write_huggingface_closure,
)
from .service_vllm import DEFAULT_REMOTE_PORT

SERVICE_COMMANDS = ("service",)


def add_service_parser(subparsers: Any) -> None:
    service = subparsers.add_parser(
        "service",
        help="Validate, resolve, plan, or bundle one config-only inference service.",
    )
    service.add_argument(
        "--agents-md",
        action="store_true",
        help="Print the inference-service operating contract and exit.",
    )
    service.set_defaults(service_action=None, json=False)
    actions = service.add_subparsers(dest="service_action")
    action_help = {
        "validate": "Validate one service definition.",
        "plan": "Build a non-executing remote deployment plan.",
        "resolve": (
            "Resolve the generated Hugging Face closure without "
            "downloading model bytes."
        ),
        "bundle": (
            "Bind a generated Hugging Face closure into a non-executing "
            "implementation bundle and manifest."
        ),
    }
    for action, help_text in action_help.items():
        parser = actions.add_parser(
            action,
            help=help_text,
        )
        parser.add_argument(
            "config",
            nargs="?",
            metavar="CONFIG",
            help="Path to the sole authored model-service TOML file.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit the complete versioned machine-readable result.",
        )
        if action in {"plan", "bundle"}:
            parser.add_argument(
                "--remote-port",
                type=int,
                default=DEFAULT_REMOTE_PORT,
                metavar="PORT",
                help=(
                    "Deployment-owned remote loopback port "
                    f"(default: {DEFAULT_REMOTE_PORT})."
                ),
            )
        if action == "bundle":
            parser.add_argument(
                "--closure",
                metavar="PATH",
                help=(
                    "Path to a generated, content-identified Hugging Face "
                    "closure document."
                ),
            )
        if action == "resolve":
            parser.add_argument(
                "--output",
                metavar="PATH",
                help=(
                    "Write the generated closure to PATH instead of its "
                    "content-addressed state location."
                ),
            )
            parser.add_argument(
                "--offline",
                action="store_true",
                help="Require all Hugging Face metadata to be cached.",
            )
            parser.add_argument(
                "--refresh",
                action="store_true",
                help="Refresh Hugging Face metadata instead of reusing cache.",
            )
            parser.add_argument(
                "--state-root",
                metavar="PATH",
                help="Override the private Runpod state root.",
            )


def run_service_command(args: argparse.Namespace) -> int:
    if args.service_action not in {
        "validate",
        "plan",
        "resolve",
        "bundle",
    }:
        raise RunpodLocalError(
            "service requires validate, plan, resolve, or bundle",
            code="missing_service_action",
        )
    if not args.config:
        raise RunpodLocalError(
            f"service {args.service_action} requires CONFIG",
            code="missing_service_config",
        )
    if args.service_action == "bundle" and not args.closure:
        raise RunpodLocalError(
            "service bundle requires --closure",
            code="missing_service_huggingface_closure",
        )
    source_path = pathlib.Path(args.config).expanduser().absolute()
    definition = load_inference_service(source_path)
    runtime_id = definition.normalized_plan()["runtime_id"]
    runtime = load_runtime(runtime_id).safe_summary()
    if args.service_action == "resolve":
        root = state_root(args.state_root)
        client = HuggingFaceClient(
            cache=JsonCache(root / "cache" / "huggingface"),
            transport=JsonHttpTransport(),
            token=configured_huggingface_token(),
            offline=args.offline,
            refresh=args.refresh,
        )
        closure = resolve_huggingface_closure(
            definition,
            client=client,
        )
        output_path = (
            pathlib.Path(args.output).expanduser().absolute()
            if args.output
            else default_huggingface_closure_path(root, closure)
        )
        write_huggingface_closure(output_path, closure)
        result = {
            "schema_version": "runpod.huggingface-closure-resolution.v1",
            "metadata_only": True,
            "model_bytes_downloaded": 0,
            "service_id": definition.normalized_plan()["service_id"],
            "service_plan_sha256": definition.plan_sha256,
            "output_path": str(output_path),
            "closure": closure.as_dict(),
        }
    elif args.service_action == "validate":
        result = build_service_validation(
            definition,
            source_path=source_path,
            runtime=runtime,
        )
    elif args.service_action == "plan":
        result = build_service_deployment_plan(
            definition,
            source_path=source_path,
            source_root=dotfiles_root(),
            runtime=runtime,
            remote_port=args.remote_port,
        )
    else:
        closure_path = pathlib.Path(args.closure).expanduser().absolute()
        result = build_service_bundle_plan(
            definition,
            source_root=dotfiles_root(),
            runtime=runtime,
            closure=load_huggingface_closure(closure_path),
            remote_port=args.remote_port,
        )
    if args.json:
        print_json(result)
    elif args.service_action == "validate":
        print(
            f"validated {result['service']['service_id']} "
            f"({result['service_plan_sha256']})"
        )
    elif args.service_action == "resolve":
        closure = result["closure"]
        print(
            f"resolved {result['service_id']} to {result['output_path']} "
            f"({closure['file_count']} files, "
            f"{closure['total_bytes']} bytes, "
            f"{closure['closure_sha256']})"
        )
    elif args.service_action == "plan":
        print(
            f"planned {result['service']['service_id']} on "
            f"127.0.0.1:{result['deployment']['remote_port']} "
            f"({result['plan_sha256']})"
        )
    else:
        manifest = result["deployment_manifest"]
        print(
            f"bundled {definition.normalized_plan()['service_id']} as "
            f"{result['implementation_bundle']['bundle_sha256']} with "
            f"manifest {manifest['sha256']}"
        )
    return 0
