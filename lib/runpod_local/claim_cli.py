"""Operator CLI for generic host claims and empty-host retirement."""

from __future__ import annotations

import argparse
import pathlib
from typing import Any

from .api import RunpodApi
from .auth import CredentialStore
from .claims import HostClaimRequest
from .errors import RunpodLocalError
from .host_control import HostControl
from .lifecycle import LifecycleManager
from .output import print_json
from .paths import credentials_file, runpod_root, state_root
from .profile import ProfileStore
from .state import StateStore
from .timeutil import parse_duration


CLAIM_COMMANDS = ("claim",)
CLAIM_AGENT_DOCS = """\
# `runpod claim`

Generic host claims reserve opaque GPU/CPU/RAM/disk capacity and loopback
endpoint ports. They never contain consumer workload or serving policy.

Read-only inspection:

    runpod claim list [HOST] --json
    runpod claim show HOST CLAIM_ID --json

Mutating operations emit a plan unless `--execute` is present:

    runpod claim acquire --owner-system SYSTEM --owner-instance INSTANCE \\
      --operation-id OPERATION --profile PROFILE [RESOURCE OPTIONS] --execute
    runpod claim renew HOST CLAIM_ID --generation N --ttl 2m --execute
    runpod claim release HOST CLAIM_ID --generation N [--now] --execute
    runpod claim enforce [--execute]

The owner operation ID is the acquire idempotency key. Reusing it with a
different request fails. Claim generations are compare-and-swap guards.
Normal final release starts a `while-claimed` host's configured empty grace;
`--now` makes that host exact-retirement-due immediately. Neither form can
retire a manually retained host; that requires the separate operator `runpod
down` authority. Retirement enforcement is read-only unless `--execute` is
explicit. Claim expiry instead quarantines the exact host because opaque
consumer cleanup is unproven. It blocks every new admission, gives
idempotent acquisition, and renewal; gives `while-claimed` hosts zero final
grace; and requires explicit exact-host retirement for manually retained
hosts. Exact terminal/replacement evidence permanently closes the old
operation's claims instead of blocking unrelated fleet placement.
"""


def _add_common(
    parser: argparse.ArgumentParser,
    *,
    credentials: bool,
) -> None:
    parser.add_argument(
        "--state-root",
        metavar="PATH",
        help="Override RUNPOD_STATE_HOME for machine receipts and locks.",
    )
    parser.add_argument(
        "--runpod-root",
        metavar="PATH",
        help="Override RUNPOD_ROOT (default: /mnt/dev/runpod).",
    )
    if credentials:
        parser.add_argument(
            "--credentials-file",
            metavar="PATH",
            help="Override the mode-0600 Runpod credential file.",
        )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a versioned machine-readable result.",
    )
    parser.add_argument(
        "--agents-md",
        action="store_true",
        help="Print the generic claim operating contract and exit.",
    )


def add_claim_parser(subparsers: Any) -> None:
    claim = subparsers.add_parser(
        "claim",
        help="Inspect and control opaque claims over generic Runpod hosts.",
    )
    claim.add_argument(
        "--agents-md",
        action="store_true",
        help="Print the generic claim operating contract and exit.",
    )
    actions = claim.add_subparsers(dest="claim_action")

    list_parser = actions.add_parser(
        "list",
        help="List current claims, expiring stale claims before display.",
    )
    _add_common(list_parser, credentials=False)
    list_parser.add_argument("host_name", nargs="?")

    show = actions.add_parser(
        "show",
        help="Show one current claim by exact host and claim identity.",
    )
    _add_common(show, credentials=False)
    show.add_argument("host_name")
    show.add_argument("claim_id")

    acquire = actions.add_parser(
        "acquire",
        help="Plan or acquire one idempotent generic host claim.",
    )
    _add_common(acquire, credentials=True)
    acquire.add_argument("--owner-system", required=True)
    acquire.add_argument("--owner-instance", required=True)
    acquire.add_argument(
        "--operation-id",
        required=True,
        help="Stable caller idempotency key for this exact request.",
    )
    acquire.add_argument(
        "--profile",
        action="append",
        required=True,
        dest="profiles",
        help="Allowed generic host profile; repeat in placement order.",
    )
    acquire.add_argument("--host", dest="host_name")
    acquire.add_argument(
        "--no-create",
        action="store_true",
        help="Fail unless a compatible active host already exists.",
    )
    acquire.add_argument(
        "--mode",
        choices=("shared", "gpu-exclusive", "host-exclusive"),
        default="shared",
    )
    acquire.add_argument(
        "--gpu-device",
        action="append",
        type=int,
        default=[],
        dest="gpu_devices",
        help="Zero-based GPU device to reserve; repeat in ascending order.",
    )
    acquire.add_argument("--gpu-memory-gib", type=float, default=0.0)
    acquire.add_argument("--cpu-count", type=int, default=0)
    acquire.add_argument("--memory-gib", type=int, default=0)
    acquire.add_argument("--ephemeral-disk-gib", type=int, default=0)
    acquire.add_argument(
        "--endpoint",
        action="append",
        default=[],
        dest="endpoint_names",
        help="Logical loopback endpoint name; repeat as needed.",
    )
    acquire.add_argument("--minimum-remaining", default="0s")
    acquire.add_argument("--renewal-ttl", default="2m")
    acquire.add_argument("--new-host-ttl", default="2h")
    acquire.add_argument(
        "--new-host-retention",
        choices=("manual", "while-claimed"),
        default="while-claimed",
    )
    acquire.add_argument(
        "--execute",
        action="store_true",
        help="Admit the claim and create a Pod if required.",
    )

    renew = actions.add_parser(
        "renew",
        help="Plan or renew one exact claim generation.",
    )
    _add_common(renew, credentials=False)
    renew.add_argument("host_name")
    renew.add_argument("claim_id")
    renew.add_argument("--generation", type=int, required=True)
    renew.add_argument("--ttl", required=True)
    renew.add_argument("--execute", action="store_true")

    release = actions.add_parser(
        "release",
        help="Plan or release one exact claim generation.",
    )
    _add_common(release, credentials=True)
    release.add_argument("host_name")
    release.add_argument("claim_id")
    release.add_argument("--generation", type=int, required=True)
    release.add_argument(
        "--now",
        action="store_true",
        help=(
            "Bypass final-claim grace only on a while-claimed host; manual "
            "host retirement requires runpod down."
        ),
    )
    release.add_argument("--execute", action="store_true")

    enforce = actions.add_parser(
        "enforce",
        help="Expire stale claims and plan or execute due host retirement.",
    )
    _add_common(enforce, credentials=True)
    enforce.add_argument(
        "--execute",
        action="store_true",
        help="Terminate exact hosts whose grace or quarantine is due.",
    )


def _state(args: argparse.Namespace) -> StateStore:
    return StateStore(state_root(getattr(args, "state_root", None)))


def _api(args: argparse.Namespace) -> RunpodApi:
    configured = getattr(args, "credentials_file", None)
    path = (
        pathlib.Path(configured).expanduser().absolute()
        if configured
        else credentials_file()
    )
    credential = CredentialStore(path).load(required=True)
    if credential is None:
        raise AssertionError("required credential unexpectedly absent")
    return RunpodApi(credential)


def _control(
    args: argparse.Namespace,
    *,
    provider_required: bool,
) -> HostControl:
    state = _state(args)
    lifecycle = LifecycleManager(
        _api(args) if provider_required else None,
        state,
    )
    return HostControl(
        state=state,
        lifecycle=lifecycle,
        profiles=ProfileStore(
            runpod_root(getattr(args, "runpod_root", None))
        ),
    )


def _claim_request(args: argparse.Namespace) -> HostClaimRequest:
    minimum_remaining_seconds = (
        0
        if args.minimum_remaining in {"0", "0s"}
        else parse_duration(args.minimum_remaining)
    )
    return HostClaimRequest(
        owner_system=args.owner_system,
        owner_instance=args.owner_instance,
        owner_operation_id=args.operation_id,
        allowed_profile_names=tuple(args.profiles),
        mode=args.mode,
        host_name=args.host_name,
        create_if_missing=not args.no_create,
        gpu_devices=tuple(args.gpu_devices),
        gpu_memory_gb=args.gpu_memory_gib,
        cpu_count=args.cpu_count,
        ram_gb=args.memory_gib,
        ephemeral_disk_gb=args.ephemeral_disk_gib,
        endpoint_names=tuple(sorted(args.endpoint_names)),
        minimum_remaining_seconds=minimum_remaining_seconds,
        renewal_ttl_seconds=parse_duration(args.renewal_ttl),
        new_host_hard_ttl_seconds=parse_duration(args.new_host_ttl),
        new_host_retention=args.new_host_retention,
    ).validated()


def _mutation_plan(
    action: str,
    *,
    target: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "runpod.host-claim-operation-plan.v1",
        "action": action,
        "executed": False,
        "target": target,
    }


def _print(value: Any, *, as_json: bool) -> None:
    if as_json:
        print_json(value)
        return
    if isinstance(value, list):
        for item in value:
            print(
                f"{item['host_name']} {item['claim_id']} "
                f"generation={item['generation']} "
                f"renewal={item['renewal_deadline']}"
            )
        return
    print_json(value)


def run_claim_command(
    args: argparse.Namespace,
    *,
    control: HostControl | None = None,
) -> int:
    if getattr(args, "agents_md", False):
        print(CLAIM_AGENT_DOCS.rstrip())
        return 0
    action = getattr(args, "claim_action", None)
    if action is None:
        raise RunpodLocalError(
            "claim action required: list, show, acquire, renew, release, or enforce",
            code="missing_action",
        )
    if action == "list":
        active_control = control or _control(args, provider_required=False)
        result = {
            "schema_version": "runpod.host-claim-list.v1",
            "host_name": args.host_name,
            "claims": [
                claim.to_document()
                for claim in active_control.list(args.host_name)
            ],
        }
    elif action == "show":
        active_control = control or _control(args, provider_required=False)
        result = active_control.get(
            args.host_name,
            args.claim_id,
        ).to_document()
    elif action == "acquire":
        request = _claim_request(args)
        if not args.execute:
            result = _mutation_plan(
                "acquire",
                target=request.identity_document(),
            )
        else:
            active_control = control or _control(
                args,
                provider_required=True,
            )
            result = active_control.acquire(request).to_document()
    elif action == "renew":
        renewal_ttl_seconds = parse_duration(args.ttl)
        if not args.execute:
            result = _mutation_plan(
                "renew",
                target={
                    "host_name": args.host_name,
                    "claim_id": args.claim_id,
                    "expected_generation": args.generation,
                    "renewal_ttl_seconds": renewal_ttl_seconds,
                },
            )
        else:
            active_control = control or _control(
                args,
                provider_required=False,
            )
            result = active_control.renew(
                args.host_name,
                args.claim_id,
                args.generation,
                renewal_ttl_seconds,
            ).to_document()
    elif action == "release":
        if not args.execute:
            result = _mutation_plan(
                "release",
                target={
                    "host_name": args.host_name,
                    "claim_id": args.claim_id,
                    "expected_generation": args.generation,
                    "retire_now": args.now,
                },
            )
        else:
            active_control = control or _control(
                args,
                provider_required=True,
            )
            result = active_control.release(
                args.host_name,
                args.claim_id,
                args.generation,
                now=args.now,
            ).to_document()
    else:
        active_control = control or _control(
            args,
            provider_required=args.execute,
        )
        result = active_control.enforce_retirement(execute=args.execute)
    _print(result, as_json=args.json)
    return 0
