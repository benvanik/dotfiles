"""CLI surfaces for safe Pod launch, status, termination, and leases."""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys
import time
from typing import Any

from .api import RunpodApi
from .auth import CredentialStore
from .claim_acquisition import ClaimAcquisitionStore
from .claims import ClaimStore
from .errors import RunpodLocalError
from .host_control import HostControl
from .instances import InstanceStore, lease_expiry_reasons
from .lifecycle import TERMINAL_PHASES, LifecycleManager
from .output import print_json
from .paths import credentials_file, runpod_root, state_root
from .profile import MAX_IMPLICIT_HARD_TTL_SECONDS, ProfileStore
from .state import (
    HOST_CONTROLLER_LOCK_SCOPE,
    StateStore,
    validate_record_name,
)
from .template import redact_docker_arguments
from .timeutil import parse_duration, parse_utc_timestamp, utc_timestamp

LIFECYCLE_COMMANDS = ("up", "status", "down", "ttl")


def _add_common(
    parser: argparse.ArgumentParser,
    *,
    credentials: bool = True,
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
        help="Print the agent operating contract and exit.",
    )


def add_lifecycle_parsers(subparsers: Any) -> None:
    up = subparsers.add_parser(
        "up", help="Plan or execute one crash-reconcilable Pod launch."
    )
    _add_common(up)
    up.add_argument("name", nargs="?", help="Stable local instance name.")
    up.add_argument("--profile", help="Local launch profile.")
    up.add_argument(
        "--gpu",
        action="append",
        default=[],
        help="Restrict launch to a profile GPU alias/ID; repeat as needed.",
    )
    up.add_argument(
        "--ttl",
        help=(
            "Provider-enforced hard lifetime; the implicit maximum is 30m. "
            "An explicit longer value increases lost-controller billing "
            "exposure."
        ),
    )
    up.add_argument(
        "--idle-ttl",
        help=(
            "Local-watcher idle timeout after no explicit heartbeat "
            "(minimum: 30s); tunnel traffic is not observed."
        ),
    )
    up.add_argument(
        "--execute",
        action="store_true",
        help="Create or reconcile the Pod; otherwise emit a read-only plan.",
    )

    status = subparsers.add_parser(
        "status", help="Reconcile local receipts with live Pods."
    )
    _add_common(status)
    status.add_argument("name", nargs="?")
    status.add_argument(
        "--local-only",
        action="store_true",
        help="Read private local receipts without contacting Runpod.",
    )

    down = subparsers.add_parser(
        "down", help="Plan or terminate one exact Pod while preserving its volume."
    )
    _add_common(down)
    down.add_argument("name", nargs="?")
    down.add_argument(
        "--execute",
        action="store_true",
        help="Delete the exact reconciled Pod; otherwise emit a read-only plan.",
    )

    ttl = subparsers.add_parser(
        "ttl", help="Inspect, adjust, heartbeat, or enforce local leases."
    )
    ttl.add_argument(
        "--agents-md",
        action="store_true",
        help="Print the agent operating contract and exit.",
    )
    ttl_actions = ttl.add_subparsers(dest="ttl_action")

    ttl_show = ttl_actions.add_parser("show", help="Show local lease state.")
    _add_common(ttl_show, credentials=False)
    ttl_show.add_argument("name", nargs="?")

    ttl_set = ttl_actions.add_parser(
        "set",
        help=(
            "Set local lifetime from launch intent, bounded by the provider deadline."
        ),
    )
    _add_common(ttl_set, credentials=False)
    ttl_set.add_argument("name")
    ttl_set.add_argument("duration")

    ttl_extend = ttl_actions.add_parser(
        "extend",
        help="Extend the local deadline without passing the provider deadline.",
    )
    _add_common(ttl_extend, credentials=False)
    ttl_extend.add_argument("name")
    ttl_extend.add_argument("duration")

    ttl_touch = ttl_actions.add_parser(
        "touch", help="Record an explicit idle heartbeat without extending hard TTL."
    )
    _add_common(ttl_touch, credentials=False)
    ttl_touch.add_argument("name")
    ttl_touch.add_argument(
        "--source",
        default="manual_heartbeat",
        help="Short non-secret heartbeat source label.",
    )

    ttl_enforce = ttl_actions.add_parser(
        "enforce", help="Plan or terminate every locally expired instance."
    )
    _add_common(ttl_enforce)
    ttl_enforce.add_argument(
        "--execute",
        action="store_true",
        help="Delete expired reconciled Pods; otherwise emit a local plan.",
    )

    ttl_watch = ttl_actions.add_parser(
        "watch", help="Run foreground TTL enforcement at a bounded interval."
    )
    _add_common(ttl_watch)
    ttl_watch.add_argument(
        "--interval",
        default="30s",
        help="Enforcement interval from 5s through 5m (default: 30s).",
    )
    ttl_watch.add_argument(
        "--execute",
        action="store_true",
        help="Required acknowledgement for repeated provider deletion checks.",
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


def _manager(args: argparse.Namespace, *, provider_required: bool) -> LifecycleManager:
    return LifecycleManager(_api(args) if provider_required else None, _state(args))


def _claim_ledger_protects_current_host(
    instances: InstanceStore,
    ledger: dict[str, Any],
) -> bool:
    """Scope one ledger's safety authority to its exact host operation."""

    try:
        current_instance = instances.load(
            ledger["host_name"],
            required=False,
        )
    except RunpodLocalError:
        return True
    if current_instance is None:
        return ledger["operation_end"] is None
    if current_instance["operation_id"] != ledger["host_operation_id"]:
        return False
    return current_instance["phase"] not in TERMINAL_PHASES


def _active_claim_host_names(
    state: StateStore,
    *,
    now: datetime.datetime,
    expire: bool,
    errors: list[dict[str, Any]] | None = None,
) -> set[str]:
    claims = ClaimStore(state)
    acquisitions = ClaimAcquisitionStore(state)
    instances = InstanceStore(state)
    active_hosts = set()
    for scanned in claims.scan():
        if scanned.error is not None:
            try:
                validate_record_name(scanned.name)
            except RunpodLocalError:
                pass
            else:
                active_hosts.add(scanned.name)
            if errors is not None:
                errors.append(
                    {
                        "host_name": scanned.name,
                        "host_operation_id": None,
                        "protects_current_host": True,
                        "record_namespace": "hostclaims",
                        "record_name": scanned.name,
                        "error": {
                            "code": scanned.error.code,
                            "message": str(scanned.error),
                        },
                    }
                )
            continue
        ledger = scanned.value
        if ledger is None:
            continue
        protects_current_host = _claim_ledger_protects_current_host(
            instances,
            ledger,
        )
        if expire:
            try:
                ledger, _ = claims.expire_claims(
                    ledger,
                    now=now,
                )
                acquisitions.reconcile_closed_claims(
                    [ledger],
                    now=now,
                )
            except RunpodLocalError as error:
                if protects_current_host:
                    active_hosts.add(ledger["host_name"])
                if errors is not None:
                    errors.append(
                        {
                            "host_name": ledger["host_name"],
                            "host_operation_id": ledger[
                                "host_operation_id"
                            ],
                            "protects_current_host": (
                                protects_current_host
                            ),
                            "record_namespace": "hostclaims",
                            "record_name": scanned.name,
                            "error": {
                                "code": error.code,
                                "message": str(error),
                            },
                        }
                    )
                continue
            active_claims = ledger["claims"]
        else:
            active_claims = [
                claim
                for claim in ledger["claims"]
                if now < parse_utc_timestamp(claim["renewal_deadline"])
            ]
        if active_claims and protects_current_host:
            active_hosts.add(ledger["host_name"])
    return active_hosts


def _claim_scan_error_actions(
    errors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    actions = []
    for failure in errors:
        host_name = failure["host_name"]
        cause = failure["error"]
        actions.append(
            {
                "instance_name": host_name,
                "phase": None,
                "reasons": ["claim_state_ambiguous"],
                "executed": False,
                "blocked_by_active_claims": failure[
                    "protects_current_host"
                ],
                "host_operation_id": failure["host_operation_id"],
                "state_record": {
                    "namespace": failure["record_namespace"],
                    "name": failure["record_name"],
                },
                "error": {
                    "code": "host_claim_state_ambiguous",
                    "message": (
                        f"claim state for instance {host_name} is ambiguous: "
                        f"{cause['message']}"
                    ),
                    "cause_code": cause["code"],
                },
            }
        )
    return actions


def _with_claim_scan_errors(
    result: dict[str, Any],
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    if not errors:
        return result
    combined = dict(result)
    combined["actions"] = [
        *result["actions"],
        *_claim_scan_error_actions(errors),
    ]
    combined["actions"].sort(
        key=lambda action: (
            action["instance_name"],
            action["reasons"],
        )
    )
    return combined


def _run_ttl_watch_cycle(
    *,
    state: StateStore,
    lifecycle: LifecycleManager,
    hosts: HostControl,
    now: datetime.datetime,
) -> dict[str, Any]:
    """Enforce claim quarantine retirement and host TTL in one watch cycle."""

    claim_retirement = hosts.enforce_retirement(execute=True)
    claim_scan_errors: list[dict[str, Any]] = []
    with state.locked(HOST_CONTROLLER_LOCK_SCOPE):
        protected_names = _active_claim_host_names(
            state,
            now=now,
            expire=True,
            errors=claim_scan_errors,
        )
        ttl_enforcement = lifecycle.enforce_ttl(
            execute=True,
            protected_instance_names=protected_names,
        )
        ttl_enforcement = _with_claim_scan_errors(
            ttl_enforcement,
            claim_scan_errors,
        )
    claim_actions = [
        action
        for action in claim_retirement["actions"]
        if (
            action["expired_claim_ids"]
            or action["due"]
            or action["executed"]
            or action.get("manual_action_required", False)
            or "error" in action
        )
    ]
    return {
        "schema_version": "runpod.ttl-watch-cycle.v1",
        "evaluated_at": utc_timestamp(now),
        "claim_retirement": claim_retirement,
        "ttl_enforcement": ttl_enforcement,
        "actions": [
            *[
                {
                    "controller": "claim-retirement",
                    "action": action,
                }
                for action in claim_actions
            ],
            *[
                {
                    "controller": "host-ttl",
                    "action": action,
                }
                for action in ttl_enforcement["actions"]
            ],
        ],
    }


def _guard_unclaimed_host(
    state: StateStore,
    *,
    name: str,
    now: datetime.datetime,
    expire: bool = True,
) -> None:
    claim_scan_errors: list[dict[str, Any]] = []
    active_host_names = _active_claim_host_names(
        state,
        now=now,
        expire=expire,
        errors=claim_scan_errors,
    )
    exact_errors = [
        failure
        for failure in claim_scan_errors
        if failure["host_name"] == name
        and failure["protects_current_host"]
    ]
    if exact_errors:
        cause = exact_errors[0]["error"]
        raise RunpodLocalError(
            f"claim state for instance {name} is ambiguous: "
            f"{cause['message']}",
            code="host_claim_state_ambiguous",
        )
    if name in active_host_names:
        raise RunpodLocalError(
            f"instance {name} has active host claims; release them before "
            "direct termination",
            code="host_has_active_claims",
        )


def _allowed_gpu_ids(
    args: argparse.Namespace,
    profile: dict[str, Any],
) -> set[str] | None:
    if not args.gpu:
        return None
    requested = set(args.gpu)
    allowed = set(profile["pod"]["gpu_type_ids"])
    unknown = sorted(requested.difference(allowed))
    if unknown:
        raise RunpodLocalError(
            "requested GPU is not admitted by the host profile: " + ", ".join(unknown),
            code="invalid_gpu_restriction",
        )
    return requested


def _print(value: Any, *, as_json: bool) -> None:
    value = redact_docker_arguments(value)
    if as_json:
        print_json(value)
        return
    if isinstance(value, dict) and value.get("schema_version") == (
        "runpod.launch-plan.v1"
    ):
        print(f"{value['action']}: {'ready' if value.get('ready') else 'blocked'}")
        placement = value.get("placement", {})
        for evaluation in placement.get("evaluations", []):
            status = "eligible" if evaluation["eligible"] else "blocked"
            price = evaluation.get("total_price_per_hour")
            price_text = "unquoted" if price is None else f"${price:.3f}/h"
            print(f"  {status:<8} {price_text:<12} {evaluation['gpu_id']}")
            for reason in evaluation["reasons"]:
                print(f"    {reason}")
        return
    if isinstance(value, dict) and value.get("schema_version") == (
        "runpod.launch-result.v1"
    ):
        instance = value["instance"]
        print(
            f"{instance['name']}: {instance['phase']} "
            f"(Pod {instance.get('pod_id') or 'not allocated'})"
        )
        return
    print_json(value)


def _resolve_launch_ttl_seconds(
    requested_ttl: str | None,
    profile_default_ttl_seconds: int,
) -> int:
    if requested_ttl is not None:
        return parse_duration(requested_ttl)
    return min(
        profile_default_ttl_seconds,
        MAX_IMPLICIT_HARD_TTL_SECONDS,
    )


def _resolve_idle_timeout_seconds(requested_idle_ttl: str | None) -> int | None:
    if requested_idle_ttl is None:
        return None
    return parse_duration(requested_idle_ttl)


def _run_up(args: argparse.Namespace) -> int:
    if not args.name or not args.profile:
        raise RunpodLocalError(
            "up requires NAME and --profile",
            code="missing_launch_target",
        )
    state = _state(args)
    profile = ProfileStore(runpod_root(args.runpod_root)).load(args.profile)
    ttl_seconds = _resolve_launch_ttl_seconds(
        args.ttl,
        profile["lease"]["default_ttl_seconds"],
    )
    idle_timeout_seconds = _resolve_idle_timeout_seconds(args.idle_ttl)
    admitted_ids = _allowed_gpu_ids(args, profile)
    manager = LifecycleManager(_api(args), state)
    if args.execute:
        with state.locked(HOST_CONTROLLER_LOCK_SCOPE):
            instance = manager.launch(
                args.name,
                profile,
                ttl_seconds=ttl_seconds,
                idle_timeout_seconds=idle_timeout_seconds,
                allowed_gpu_ids=admitted_ids,
            )
            result = {
                "schema_version": "runpod.launch-result.v1",
                "executed": True,
                "instance": instance,
            }
    else:
        result = manager.plan_launch(
            args.name,
            profile,
            ttl_seconds=ttl_seconds,
            idle_timeout_seconds=idle_timeout_seconds,
            allowed_gpu_ids=admitted_ids,
        )
    _print(result, as_json=args.json)
    return 0


def _run_status(args: argparse.Namespace) -> int:
    result = _manager(args, provider_required=not args.local_only).status(
        args.name, live=not args.local_only
    )
    _print(result, as_json=args.json)
    return 0


def _run_down(args: argparse.Namespace) -> int:
    if not args.name:
        raise RunpodLocalError(
            "down requires NAME",
            code="missing_instance_name",
        )
    state = _state(args)
    manager = LifecycleManager(_api(args), state)
    if args.execute:
        ClaimAcquisitionStore(state).migrate_v1_records()
        with state.locked(HOST_CONTROLLER_LOCK_SCOPE):
            _guard_unclaimed_host(
                state,
                name=args.name,
                now=datetime.datetime.now(datetime.timezone.utc),
            )
            result = manager.terminate(
                args.name,
                execute=True,
                reason="operator_request",
            )
    else:
        _guard_unclaimed_host(
            state,
            name=args.name,
            now=datetime.datetime.now(datetime.timezone.utc),
            expire=False,
        )
        result = manager.terminate(
            args.name,
            execute=False,
            reason="operator_request",
        )
    _print(result, as_json=args.json)
    return 0


def _local_lease_status(
    store: InstanceStore,
    *,
    name: str | None,
    now: datetime.datetime,
) -> dict[str, Any]:
    records = [store.load(name)] if name else store.list()
    leases = []
    for record in records:
        if record is None:
            continue
        leases.append(
            {
                "name": record["name"],
                "phase": record["phase"],
                "pod_id": record.get("pod_id"),
                "lease": record.get("lease"),
                "expiry_reasons": lease_expiry_reasons(record, now=now),
            }
        )
    return {
        "schema_version": "runpod.ttl-status.v1",
        "evaluated_at": utc_timestamp(now),
        "leases": leases,
    }


def _run_ttl(args: argparse.Namespace) -> int:
    if not args.ttl_action:
        raise RunpodLocalError(
            "ttl action required: show, set, extend, touch, enforce, or watch",
            code="missing_action",
        )
    state = _state(args)
    store = InstanceStore(state)
    now = datetime.datetime.now(datetime.timezone.utc)
    if args.ttl_action == "show":
        result = _local_lease_status(store, name=args.name, now=now)
    elif args.ttl_action == "set":
        with state.locked(HOST_CONTROLLER_LOCK_SCOPE):
            result = store.set_ttl(
                args.name,
                ttl_seconds=parse_duration(args.duration),
                now=now,
            )
    elif args.ttl_action == "extend":
        with state.locked(HOST_CONTROLLER_LOCK_SCOPE):
            result = store.extend_ttl(
                args.name,
                extension_seconds=parse_duration(args.duration),
                now=now,
            )
    elif args.ttl_action == "touch":
        with state.locked(HOST_CONTROLLER_LOCK_SCOPE):
            result = store.touch(args.name, now=now, source=args.source)
    elif args.ttl_action == "enforce":
        manager = LifecycleManager(
            _api(args) if args.execute else None,
            state,
        )
        if args.execute:
            ClaimAcquisitionStore(state).migrate_v1_records()
            claim_scan_errors: list[dict[str, Any]] = []
            with state.locked(HOST_CONTROLLER_LOCK_SCOPE):
                protected_names = _active_claim_host_names(
                    state,
                    now=now,
                    expire=True,
                    errors=claim_scan_errors,
                )
                result = manager.enforce_ttl(
                    execute=True,
                    protected_instance_names=protected_names,
                )
                result = _with_claim_scan_errors(
                    result,
                    claim_scan_errors,
                )
        else:
            claim_scan_errors = []
            protected_names = _active_claim_host_names(
                state,
                now=now,
                expire=False,
                errors=claim_scan_errors,
            )
            result = manager.enforce_ttl(
                execute=False,
                protected_instance_names=protected_names,
            )
            result = _with_claim_scan_errors(
                result,
                claim_scan_errors,
            )
    else:
        if not args.execute:
            raise RunpodLocalError(
                "ttl watch requires --execute",
                code="execute_required",
            )
        interval_seconds = parse_duration(args.interval)
        if not 5 <= interval_seconds <= 5 * 60:
            raise RunpodLocalError(
                "watch interval must be between 5 seconds and 5 minutes",
                code="invalid_watch_interval",
            )
        manager = _manager(args, provider_required=True)
        hosts = HostControl(
            state=state,
            lifecycle=manager,
            profiles=ProfileStore(
                runpod_root(getattr(args, "runpod_root", None))
            ),
        )
        try:
            while True:
                result = _run_ttl_watch_cycle(
                    state=state,
                    lifecycle=manager,
                    hosts=hosts,
                    now=datetime.datetime.now(datetime.timezone.utc),
                )
                if args.json:
                    print(
                        json.dumps(
                            result,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                    sys.stdout.flush()
                elif result["actions"]:
                    _print(result, as_json=args.json)
                time.sleep(interval_seconds)
        except KeyboardInterrupt:
            return 130
    _print(result, as_json=args.json)
    if args.ttl_action == "enforce" and any(
        "error" in action for action in result["actions"]
    ):
        return 1
    return 0


def run_lifecycle_command(args: argparse.Namespace) -> int:
    if args.command == "up":
        return _run_up(args)
    if args.command == "status":
        return _run_status(args)
    if args.command == "down":
        return _run_down(args)
    if args.command == "ttl":
        return _run_ttl(args)
    raise RunpodLocalError(
        f"unsupported lifecycle command: {args.command}",
        code="unsupported_command",
    )
