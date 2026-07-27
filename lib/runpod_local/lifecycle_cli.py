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
from .cache import JsonCache
from .errors import RunpodLocalError
from .huggingface_credentials import configured_huggingface_token
from .instances import InstanceStore, lease_expiry_reasons
from .lifecycle import LifecycleManager
from .output import print_json
from .paths import credentials_file, state_root
from .profile import MAX_IMPLICIT_HARD_TTL_SECONDS, ProfileStore
from .state import StateStore
from .template import redact_docker_arguments
from .timeutil import parse_duration, utc_timestamp
from .workload import (
    HuggingFaceWorkload,
    WorkloadPlacementRequest,
    plan_workload,
)

LIFECYCLE_COMMANDS = ("up", "status", "down", "ttl")


def _add_common(
    parser: argparse.ArgumentParser,
    *,
    credentials: bool = True,
) -> None:
    parser.add_argument(
        "--state-root",
        metavar="PATH",
        help="Override RUNPOD_HOME (default: ~/.local/runpod).",
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


def _add_model_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model",
        metavar="NAMESPACE/REPOSITORY",
        help="Inspect this exact Hugging Face model and restrict GPU placement.",
    )
    parser.add_argument("--revision", default="main")
    parser.add_argument("--index-file")
    parser.add_argument("--context", type=int, default=32768)
    parser.add_argument("--sequences", type=int, default=1)
    parser.add_argument(
        "--kv-dtype", choices=("bf16", "fp16", "fp8"), default="bf16"
    )
    parser.add_argument(
        "--weight-format",
        choices=("native", "bf16", "fp8", "int8", "q8"),
        default="native",
    )
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument(
        "--allow-indeterminate-fit",
        action="store_true",
        help="Admit indeterminate static placement; never admits tight/impossible.",
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
            "(minimum: 30s); Pi/vLLM tunnel traffic is not observed."
        ),
    )
    _add_model_options(up)
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
            "Set local lifetime from launch intent, bounded by the provider "
            "deadline."
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


def _manager(
    args: argparse.Namespace, *, provider_required: bool
) -> LifecycleManager:
    return LifecycleManager(_api(args) if provider_required else None, _state(args))


def _model_placement(
    args: argparse.Namespace,
    profile: dict[str, Any],
) -> tuple[set[str] | None, dict[str, Any] | None]:
    root = state_root(args.state_root)
    model = (
        HuggingFaceWorkload(
            repository=args.model,
            revision=args.revision,
            index_file=args.index_file,
            context_tokens=args.context,
            sequences=args.sequences,
            kv_dtype=args.kv_dtype,
            weight_format=args.weight_format,
            offline=args.offline,
            refresh=args.refresh,
        )
        if args.model is not None
        else None
    )
    placement = plan_workload(
        WorkloadPlacementRequest(
            allowed_gpu_ids=tuple(profile["pod"]["gpu_type_ids"]),
            requested_gpus=tuple(args.gpu),
            model=model,
            allow_indeterminate_fit=args.allow_indeterminate_fit,
            gpu_count=profile["pod"]["gpu_count"],
        ),
        cache=JsonCache(root / "cache" / "huggingface"),
        token=configured_huggingface_token(),
    )
    return (
        placement.admitted_gpu_ids,
        placement.model_summary,
    )


def _print(value: Any, *, as_json: bool) -> None:
    value = redact_docker_arguments(value)
    if as_json:
        print_json(value)
        return
    if isinstance(value, dict) and value.get("schema_version") == (
        "runpod.launch-plan.v1"
    ):
        print(
            f"{value['action']}: "
            f"{'ready' if value.get('ready') else 'blocked'}"
        )
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
    profile = ProfileStore(state).load(args.profile)
    ttl_seconds = _resolve_launch_ttl_seconds(
        args.ttl,
        profile["lease"]["default_ttl_seconds"],
    )
    idle_timeout_seconds = _resolve_idle_timeout_seconds(args.idle_ttl)
    admitted_ids, model = _model_placement(args, profile)
    manager = LifecycleManager(_api(args), state)
    if args.execute:
        instance = manager.launch(
            args.name,
            profile,
            ttl_seconds=ttl_seconds,
            idle_timeout_seconds=idle_timeout_seconds,
            allowed_gpu_ids=admitted_ids,
            model=model,
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
            model=model,
        )
    _print(result, as_json=args.json)
    return 0


def _run_status(args: argparse.Namespace) -> int:
    result = _manager(
        args, provider_required=not args.local_only
    ).status(args.name, live=not args.local_only)
    _print(result, as_json=args.json)
    return 0


def _run_down(args: argparse.Namespace) -> int:
    if not args.name:
        raise RunpodLocalError(
            "down requires NAME",
            code="missing_instance_name",
        )
    result = _manager(args, provider_required=True).terminate(
        args.name,
        execute=args.execute,
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
        result = store.set_ttl(
            args.name,
            ttl_seconds=parse_duration(args.duration),
            now=now,
        )
    elif args.ttl_action == "extend":
        result = store.extend_ttl(
            args.name,
            extension_seconds=parse_duration(args.duration),
            now=now,
        )
    elif args.ttl_action == "touch":
        result = store.touch(args.name, now=now, source=args.source)
    elif args.ttl_action == "enforce":
        result = _manager(
            args, provider_required=args.execute
        ).enforce_ttl(execute=args.execute)
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
        try:
            while True:
                result = manager.enforce_ttl(execute=True)
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
    if (
        args.ttl_action == "enforce"
        and any("error" in action for action in result["actions"])
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
