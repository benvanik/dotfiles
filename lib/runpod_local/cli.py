"""Command-line entry point for the local Runpod control plane."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .agents import AGENT_DOCS
from .claim_cli import (
    CLAIM_AGENT_DOCS,
    CLAIM_COMMANDS,
    add_claim_parser,
    run_claim_command,
)
from .doctor_cli import (
    DOCTOR_COMMANDS,
    add_doctor_parser,
    run_doctor_command,
)
from .errors import RunpodLocalError
from .lifecycle_cli import (
    LIFECYCLE_COMMANDS,
    add_lifecycle_parsers,
    run_lifecycle_command,
)
from .output import print_json
from .provider_cli import (
    PROVIDER_COMMANDS,
    add_provider_parsers,
    run_provider_command,
)
from .remote_cli import (
    REMOTE_COMMANDS,
    add_remote_parsers,
    run_remote_command,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="runpod",
        description="Manage generic Runpod hosts, claims, storage, and SSH access.",
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

    add_provider_parsers(subparsers)
    add_lifecycle_parsers(subparsers)
    add_remote_parsers(subparsers)
    add_claim_parser(subparsers)
    add_doctor_parser(subparsers)
    return parser


def run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.agents_md:
        document = (
            CLAIM_AGENT_DOCS
            if args.command in CLAIM_COMMANDS
            else AGENT_DOCS.get(
                args.command or "root",
                AGENT_DOCS["root"],
            )
        )
        print(document.rstrip())
        return 0
    if args.command is None:
        parser.print_help()
        return 0
    if args.command in PROVIDER_COMMANDS:
        return run_provider_command(args)
    if args.command in LIFECYCLE_COMMANDS:
        return run_lifecycle_command(args)
    if args.command in REMOTE_COMMANDS:
        return run_remote_command(args)
    if args.command in CLAIM_COMMANDS:
        return run_claim_command(args)
    if args.command in DOCTOR_COMMANDS:
        return run_doctor_command(args)
    raise RunpodLocalError(
        f"unsupported command: {args.command}",
        code="unsupported_command",
    )


def parse_arguments(
    parser: argparse.ArgumentParser, arguments: list[str]
) -> argparse.Namespace:
    remote_command: list[str] | None = None
    if arguments and arguments[0] == "ssh" and "--" in arguments[1:]:
        delimiter = arguments.index("--", 1)
        remote_command = arguments[delimiter + 1 :]
        arguments = arguments[:delimiter]
    args = parser.parse_args(arguments)
    if remote_command is not None:
        args.remote_command = remote_command
    return args


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = parse_arguments(parser, arguments)
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
