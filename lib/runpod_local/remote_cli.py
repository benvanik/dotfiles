"""CLI surfaces for reconciled SSH, tunnels, and file transfer."""

from __future__ import annotations

import argparse
import pathlib
import shlex
from typing import Any, BinaryIO

from .api import RunpodApi
from .auth import CredentialStore
from .errors import RunpodLocalError
from .huggingface_credentials import (
    REMOTE_HF_CREDENTIAL_ABSENT,
    REMOTE_HF_CREDENTIAL_UNSAFE,
    REMOTE_HF_TOKEN_PATH,
    build_remote_hf_credential_argv,
    build_remote_hf_probe_argv,
    huggingface_token_path,
    open_huggingface_token_file,
)
from .instances import InstanceStore
from .output import print_json
from .paths import credentials_file, state_root
from .profile import validate_image_digest
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


REMOTE_COMMANDS = ("ssh", "tunnel", "copy", "hf-auth")


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


def _add_hf_auth_common(parser: argparse.ArgumentParser) -> None:
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
        help="Format the completed credential result as versioned JSON.",
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

    hf_auth = subparsers.add_parser(
        "hf-auth",
        help="Manage one ephemeral Hugging Face token on an active Pod.",
    )
    hf_auth.add_argument(
        "--agents-md",
        action="store_true",
        help="Print the agent operating contract and exit.",
    )
    hf_auth_actions = hf_auth.add_subparsers(dest="hf_auth_action")

    hf_auth_push = hf_auth_actions.add_parser(
        "push",
        help="Stream the local active token over SSH into ephemeral storage.",
    )
    _add_hf_auth_common(hf_auth_push)
    hf_auth_push.add_argument("name", nargs="?")
    hf_auth_push.add_argument(
        "--token-file",
        metavar="PATH",
        help="Override HF_TOKEN_PATH without placing token bytes in argv.",
    )

    for action, help_text in (
        ("status", "Check only remote presence and private permissions."),
        ("clear", "Remove only the ephemeral remote token file."),
    ):
        parser = hf_auth_actions.add_parser(action, help=help_text)
        _add_hf_auth_common(parser)
        parser.add_argument("name", nargs="?")


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


def _execute_hf_auth_remote(
    args: argparse.Namespace,
    *,
    endpoint: Any,
    instances: InstanceStore,
    remote_arguments: list[str],
    source: str,
    stdin: BinaryIO | None = None,
) -> int:
    ensure_known_hosts_file(endpoint.known_hosts_file)
    return run_with_activity(
        build_ssh_argv(endpoint, remote_arguments),
        instances=instances,
        name=args.name,
        expected_operation_id=endpoint.operation_id,
        expected_pod_id=endpoint.pod_id,
        source=source,
        stdin=stdin,
    )


def _hf_auth_result(
    args: argparse.Namespace,
    *,
    action: str,
    configured: bool,
    changed: bool,
) -> None:
    result = {
        "schema_version": "runpod.hf-auth.v1",
        "action": action,
        "instance": args.name,
        "configured": configured,
        "changed": changed,
        "remote_token_path": REMOTE_HF_TOKEN_PATH,
        "storage": "ephemeral_container",
    }
    if args.json:
        print_json(result)
        return
    if action == "push":
        print(
            f"installed ephemeral Hugging Face credential for {args.name}"
        )
    elif action == "status":
        state = "configured" if configured else "not configured"
        print(f"{args.name}: Hugging Face credential {state}")
    elif changed:
        print(f"removed ephemeral Hugging Face credential from {args.name}")
    else:
        print(f"{args.name}: Hugging Face credential already absent")


def _require_hf_auth_success(action: str, return_code: int) -> None:
    if return_code == 0:
        return
    if return_code == REMOTE_HF_CREDENTIAL_UNSAFE:
        raise RunpodLocalError(
            "remote Hugging Face credential path or permissions are unsafe",
            code="unsafe_remote_hf_credential",
        )
    raise RunpodLocalError(
        f"remote Hugging Face credential {action} failed with exit status "
        f"{return_code}",
        code="remote_hf_credential_failed",
    )


def _require_hf_auth_image(instances: InstanceStore, name: str) -> None:
    record = instances.load(name)
    payload = record.get("pod_payload")
    image_name = (
        payload.get("imageName") if isinstance(payload, dict) else None
    )
    try:
        validate_image_digest(image_name)
    except RunpodLocalError as error:
        raise RunpodLocalError(
            "Hugging Face credential push requires an explicit "
            "digest-pinned image rather than a tag or template",
            code="hf_auth_unpinned_image",
        ) from error


def _run_hf_auth(args: argparse.Namespace) -> int:
    action = args.hf_auth_action
    if action not in {"push", "status", "clear"}:
        raise RunpodLocalError(
            "hf-auth action required: push, status, or clear",
            code="missing_action",
        )
    _, instances, endpoint = _endpoint(args)

    if action == "push":
        _require_hf_auth_image(instances, args.name)
        token_path = (
            pathlib.Path(args.token_file).expanduser().absolute()
            if args.token_file
            else huggingface_token_path()
        )
        with open_huggingface_token_file(token_path) as token_file:
            probe_return_code = _execute_hf_auth_remote(
                args,
                endpoint=endpoint,
                instances=instances,
                remote_arguments=build_remote_hf_probe_argv(),
                source="hf_auth_host_probe",
            )
            if probe_return_code != 0:
                raise RunpodLocalError(
                    "remote Hugging Face credential host probe failed with "
                    f"exit status {probe_return_code}",
                    code="remote_hf_credential_probe_failed",
                )
            token_file.seek(0)
            return_code = _execute_hf_auth_remote(
                args,
                endpoint=endpoint,
                instances=instances,
                remote_arguments=build_remote_hf_credential_argv("push"),
                source="hf_auth_push",
                stdin=token_file,
            )
        _require_hf_auth_success(action, return_code)
        _hf_auth_result(
            args,
            action=action,
            configured=True,
            changed=True,
        )
        return 0

    return_code = _execute_hf_auth_remote(
        args,
        endpoint=endpoint,
        instances=instances,
        remote_arguments=build_remote_hf_credential_argv(action),
        source=f"hf_auth_{action}",
    )
    if return_code == REMOTE_HF_CREDENTIAL_ABSENT:
        _hf_auth_result(
            args,
            action=action,
            configured=False,
            changed=False,
        )
        return 0
    _require_hf_auth_success(action, return_code)
    _hf_auth_result(
        args,
        action=action,
        configured=action == "status",
        changed=action == "clear",
    )
    return 0


def run_remote_command(args: argparse.Namespace) -> int:
    if args.command == "ssh":
        return _run_ssh(args)
    if args.command == "tunnel":
        return _run_tunnel(args)
    if args.command == "copy":
        return _run_copy(args)
    if args.command == "hf-auth":
        return _run_hf_auth(args)
    raise RunpodLocalError(
        f"unsupported remote command: {args.command}",
        code="unsupported_command",
    )
