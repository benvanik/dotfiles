"""CLI wrapper for read-only Runpod diagnostics."""

from __future__ import annotations

import argparse
import pathlib
from typing import Any

from .auth import CredentialStore
from .doctor import run_doctor
from .output import print_json
from .paths import credentials_file, state_root
from .state import StateStore


DOCTOR_COMMANDS = ("doctor",)


def add_doctor_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "doctor", help="Validate local policy and optionally live provider state."
    )
    parser.add_argument(
        "--state-root",
        metavar="PATH",
        help="Override RUNPOD_HOME (default: ~/.local/runpod).",
    )
    parser.add_argument(
        "--credentials-file",
        metavar="PATH",
        help="Override the mode-0600 Runpod credential file.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Add read-only Pods, volumes, stock, and reconciliation checks.",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--agents-md",
        action="store_true",
        help="Print the agent operating contract and exit.",
    )


def run_doctor_command(args: argparse.Namespace) -> int:
    credential_path = (
        pathlib.Path(args.credentials_file).expanduser().absolute()
        if args.credentials_file
        else credentials_file()
    )
    result = run_doctor(
        state=StateStore(state_root(args.state_root)),
        credential_store=CredentialStore(credential_path),
        live=args.live,
    )
    if args.json:
        print_json(result)
    else:
        for check in result["checks"]:
            print(
                f"{check['status'].upper():<7} "
                f"{check['id']}: {check['message']}"
            )
        print(f"overall: {result['status']}")
    return 1 if result["status"] == "error" else 0
