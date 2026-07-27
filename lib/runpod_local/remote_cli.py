"""CLI surfaces for reconciled SSH, tunnels, and file transfer."""

from __future__ import annotations

import argparse
import pathlib
import shlex
from typing import Any

from .api import RunpodApi
from .auth import CredentialStore
from .errors import RunpodLocalError
from .instances import InstanceStore
from .output import print_json
from .paths import credentials_file, state_root
from .remote import (
    build_copy_argv,
    build_ssh_argv,
    build_tunnel_argv,
    ensure_known_hosts_file,
    prepare_local_tunnel_socket,
    resolve_endpoint,
    run_with_activity,
)
from .state import StateStore


REMOTE_COMMANDS = ("ssh", "tunnel", "copy")


def _add_common(parser: argparse.ArgumentParser) -> None:
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
        "--json",
        action="store_true",
        help="Inspect the resolved endpoint and argv as versioned JSON.",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        dest="print_only",
        help="Print the shell-escaped local argv without executing it.",
    )
    parser.add_argument(
        "--agents-md",
        action="store_true",
        help="Print the agent operating contract and exit.",
    )


def add_remote_parsers(subparsers: Any) -> None:
    ssh = subparsers.add_parser(
        "ssh",
        help="Open a reconciled SSH session or run one remote command.",
        epilog="Remote execution syntax: NAME -- REMOTE_ARG [REMOTE_ARG ...]",
    )
    _add_common(ssh)
    ssh.add_argument("name", nargs="?")
    ssh.set_defaults(remote_command=[])

    tunnel = subparsers.add_parser(
        "tunnel", help="Open a foreground loopback-only SSH tunnel."
    )
    _add_common(tunnel)
    tunnel.add_argument("name", nargs="?")
    local_listener = tunnel.add_mutually_exclusive_group()
    local_listener.add_argument(
        "--local-port",
        required=False,
        type=int,
        help="Local loopback port from 1 through 65535.",
    )
    local_listener.add_argument(
        "--local-socket",
        required=False,
        metavar="PATH",
        help="Private absolute local AF_UNIX socket path.",
    )
    tunnel.add_argument(
        "--remote-port",
        required=False,
        type=int,
        help="Remote loopback port from 1 through 65535.",
    )

    copy = subparsers.add_parser(
        "copy",
        help="Copy persistent /workspace or ephemeral /root/runpod-session data.",
    )
    copy.add_argument(
        "--agents-md",
        action="store_true",
        help="Print the agent operating contract and exit.",
    )
    copy_actions = copy.add_subparsers(dest="copy_action")
    for action in ("push", "pull"):
        parser = copy_actions.add_parser(
            action,
            help=(
                "Copy local to remote."
                if action == "push"
                else "Copy remote to local."
            ),
        )
        _add_common(parser)
        parser.add_argument("name", nargs="?")
        parser.add_argument(
            "source",
            nargs="?",
            help="Local path for push; allowed absolute remote path for pull.",
        )
        parser.add_argument(
            "destination",
            nargs="?",
            help="Allowed absolute remote path for push; local path for pull.",
        )
        parser.add_argument(
            "--recursive",
            action="store_true",
            help="Copy a directory tree.",
        )


def _state(args: argparse.Namespace) -> StateStore:
    return StateStore(state_root(args.state_root))


def _api(args: argparse.Namespace) -> RunpodApi:
    path = (
        pathlib.Path(args.credentials_file).expanduser().absolute()
        if args.credentials_file
        else credentials_file()
    )
    credential = CredentialStore(path).load(required=True)
    if credential is None:
        raise AssertionError("required credential unexpectedly absent")
    return RunpodApi(credential)


def _endpoint(
    args: argparse.Namespace,
) -> tuple[StateStore, InstanceStore, Any]:
    if not args.name:
        raise RunpodLocalError(
            f"{args.command} requires NAME",
            code="missing_instance_name",
        )
    state = _state(args)
    instances = InstanceStore(state)
    endpoint = resolve_endpoint(
        args.name,
        instances=instances,
        api=_api(args),
        state=state,
    )
    return state, instances, endpoint


def _inspect_or_execute(
    args: argparse.Namespace,
    *,
    endpoint: Any,
    instances: InstanceStore,
    argv: list[str],
    source: str,
    includes_remote_command: bool = False,
    maintain_activity: bool = True,
) -> int:
    if args.json and args.print_only:
        raise RunpodLocalError(
            "--json and --print are mutually exclusive",
            code="conflicting_output_mode",
        )
    if includes_remote_command and (args.json or args.print_only):
        raise RunpodLocalError(
            "remote commands cannot be combined with endpoint/argv printing",
            code="unsafe_command_display",
        )
    if args.json:
        print_json(
            {
                "schema_version": "runpod.remote-plan.v1",
                "endpoint": endpoint.safe_dict(),
                "argv": argv,
                "executed": False,
            }
        )
        return 0
    if args.print_only:
        print(shlex.join(argv))
        return 0
    ensure_known_hosts_file(endpoint.known_hosts_file)
    return run_with_activity(
        argv,
        instances=instances,
        name=args.name,
        expected_operation_id=endpoint.operation_id,
        expected_pod_id=endpoint.pod_id,
        source=source,
        maintain_activity=maintain_activity,
    )


def _run_ssh(args: argparse.Namespace) -> int:
    _, instances, endpoint = _endpoint(args)
    remote_command = list(args.remote_command)
    if remote_command and remote_command[0] == "--":
        remote_command.pop(0)
    argv = build_ssh_argv(endpoint, remote_command or None)
    return _inspect_or_execute(
        args,
        endpoint=endpoint,
        instances=instances,
        argv=argv,
        source="ssh_command" if remote_command else "ssh_session",
        includes_remote_command=bool(remote_command),
    )


def _run_tunnel(args: argparse.Namespace) -> int:
    if args.remote_port is None:
        raise RunpodLocalError(
            "tunnel requires --remote-port",
            code="missing_tunnel_port",
        )
    if args.local_port is None and args.local_socket is None:
        raise RunpodLocalError(
            "tunnel requires --local-port or --local-socket",
            code="missing_tunnel_listener",
        )
    _, instances, endpoint = _endpoint(args)
    argv = build_tunnel_argv(
        endpoint,
        local_port=args.local_port,
        local_socket=args.local_socket,
        remote_port=args.remote_port,
    )
    if args.local_socket is not None and not (
        args.json or args.print_only
    ):
        prepare_local_tunnel_socket(args.local_socket)
    return _inspect_or_execute(
        args,
        endpoint=endpoint,
        instances=instances,
        argv=argv,
        source="ssh_tunnel",
        maintain_activity=False,
    )


def _run_copy(args: argparse.Namespace) -> int:
    if args.copy_action not in {"push", "pull"}:
        raise RunpodLocalError(
            "copy action required: push or pull",
            code="missing_action",
        )
    if not args.name or args.source is None or args.destination is None:
        raise RunpodLocalError(
            f"copy {args.copy_action} requires NAME SOURCE DESTINATION",
            code="missing_copy_operand",
        )
    _, instances, endpoint = _endpoint(args)
    argv = build_copy_argv(
        endpoint,
        direction=args.copy_action,
        source=args.source,
        destination=args.destination,
        recursive=args.recursive,
    )
    return _inspect_or_execute(
        args,
        endpoint=endpoint,
        instances=instances,
        argv=argv,
        source=f"copy_{args.copy_action}",
    )


def run_remote_command(args: argparse.Namespace) -> int:
    if args.command == "ssh":
        return _run_ssh(args)
    if args.command == "tunnel":
        return _run_tunnel(args)
    if args.command == "copy":
        return _run_copy(args)
    raise RunpodLocalError(
        f"unsupported remote command: {args.command}",
        code="unsupported_command",
    )
