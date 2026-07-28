"""CLI surface for config-only inference-service validation and planning."""

from __future__ import annotations

import argparse
import pathlib
from typing import Any

from .errors import RunpodLocalError
from .output import print_json
from .paths import dotfiles_root
from .runtime_catalog import load_runtime
from .service_controller import (
    build_service_deployment_plan,
    build_service_validation,
)
from .service_definition import load_inference_service
from .service_vllm import DEFAULT_REMOTE_PORT

SERVICE_COMMANDS = ("service",)


def add_service_parser(subparsers: Any) -> None:
    service = subparsers.add_parser(
        "service",
        help="Validate or plan one config-only inference service.",
    )
    service.add_argument(
        "--agents-md",
        action="store_true",
        help="Print the inference-service operating contract and exit.",
    )
    service.set_defaults(service_action=None, json=False)
    actions = service.add_subparsers(dest="service_action")
    for action in ("validate", "plan"):
        parser = actions.add_parser(
            action,
            help=(
                "Validate one service definition."
                if action == "validate"
                else "Build a non-executing remote deployment plan."
            ),
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
        if action == "plan":
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


def run_service_command(args: argparse.Namespace) -> int:
    if args.service_action not in {"validate", "plan"}:
        raise RunpodLocalError(
            "service requires validate or plan",
            code="missing_service_action",
        )
    if not args.config:
        raise RunpodLocalError(
            f"service {args.service_action} requires CONFIG",
            code="missing_service_config",
        )
    source_path = pathlib.Path(args.config).expanduser().absolute()
    definition = load_inference_service(source_path)
    runtime_id = definition.normalized_plan()["runtime_id"]
    runtime = load_runtime(runtime_id).safe_summary()
    if args.service_action == "validate":
        result = build_service_validation(
            definition,
            source_path=source_path,
            runtime=runtime,
        )
    else:
        result = build_service_deployment_plan(
            definition,
            source_path=source_path,
            source_root=dotfiles_root(),
            runtime=runtime,
            remote_port=args.remote_port,
        )
    if args.json:
        print_json(result)
    elif args.service_action == "validate":
        print(
            f"validated {result['service']['service_id']} "
            f"({result['service_plan_sha256']})"
        )
    else:
        print(
            f"planned {result['service']['service_id']} on "
            f"127.0.0.1:{result['deployment']['remote_port']} "
            f"({result['plan_sha256']})"
        )
    return 0
