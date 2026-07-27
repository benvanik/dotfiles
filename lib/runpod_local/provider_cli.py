"""CLI surfaces for credentials, inventory, volumes, templates, and profiles."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import math
import pathlib
import re
from typing import Any

from .api import RunpodApi, gpu_stock_is_available
from .auth import ApiCredential, CredentialStore
from .errors import RunpodLocalError
from .output import print_json
from .paths import credentials_file, state_root
from .profile import (
    DEFAULT_PROFILE_HARD_TTL,
    MAX_IMPLICIT_HARD_TTL_SECONDS,
    ProfileStore,
    create_profile,
    load_ssh_public_key_file,
    validate_ssh_identity_file,
    validate_ssh_key_pair,
)
from .state import StateStore
from .timeutil import parse_duration


PROVIDER_COMMANDS = ("auth", "stock", "volume", "template", "profile")
STANDARD_VOLUME_PRICING = {
    "as_of": "2026-07-26",
    "first_tier_gb": 1000,
    "first_tier_usd_per_gb_month": 0.07,
    "additional_usd_per_gb_month": 0.05,
    "source": "https://docs.runpod.io/storage/network-volumes",
}


def standard_volume_monthly_usd(size_gb: int) -> float:
    first_tier = min(size_gb, STANDARD_VOLUME_PRICING["first_tier_gb"])
    additional = max(
        0, size_gb - STANDARD_VOLUME_PRICING["first_tier_gb"]
    )
    return round(
        first_tier
        * STANDARD_VOLUME_PRICING["first_tier_usd_per_gb_month"]
        + additional
        * STANDARD_VOLUME_PRICING["additional_usd_per_gb_month"],
        2,
    )


def volume_lock_scope(name: str) -> str:
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]
    return f"volume-{digest}"


def created_volume_violations(
    volume: dict[str, Any], request: dict[str, Any]
) -> list[str]:
    violations = []
    volume_id = volume.get("id")
    if (
        not isinstance(volume_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,191}", volume_id)
    ):
        violations.append("missing_or_invalid_volume_id")
    for field in ("name", "size_gb", "data_center_id"):
        if volume.get(field) != request[field]:
            violations.append(f"{field}_mismatch")
    return violations


def _add_agents_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--agents-md",
        action="store_true",
        help="Print the agent operating contract and exit.",
    )


def _add_common_provider_arguments(
    parser: argparse.ArgumentParser,
    *,
    state: bool = False,
    credentials: bool = True,
) -> None:
    if state:
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


def add_provider_parsers(subparsers: Any) -> None:
    auth_parser = subparsers.add_parser(
        "auth", help="Configure or inspect the private Runpod API credential."
    )
    _add_agents_argument(auth_parser)
    auth_subparsers = auth_parser.add_subparsers(dest="auth_action")
    auth_login = auth_subparsers.add_parser(
        "login", help="Validate and store an API key from a no-echo prompt."
    )
    _add_common_provider_arguments(auth_login)
    auth_login.add_argument(
        "--replace",
        action="store_true",
        help="Replace an existing credential file after validation.",
    )
    auth_status = auth_subparsers.add_parser(
        "status", help="Show credential source without showing credential bytes."
    )
    _add_common_provider_arguments(auth_status)
    auth_status.add_argument(
        "--check",
        action="store_true",
        help="Make a read-only Pod list request to validate the credential.",
    )
    auth_logout = auth_subparsers.add_parser(
        "logout", help="Remove only the dedicated local credential file."
    )
    _add_common_provider_arguments(auth_logout)
    auth_logout.add_argument(
        "--execute",
        action="store_true",
        help="Perform the removal; otherwise print the plan.",
    )

    stock_parser = subparsers.add_parser(
        "stock", help="Query live Runpod GPU stock and on-demand prices."
    )
    _add_common_provider_arguments(stock_parser)
    stock_parser.add_argument(
        "--gpu-count",
        type=int,
        default=1,
        help="Requested GPU count for price and stock (default: 1).",
    )
    stock_parser.add_argument(
        "--community",
        action="store_true",
        help="Query Community Cloud instead of Secure Cloud.",
    )
    stock_parser.add_argument(
        "--data-centers",
        action="store_true",
        help="Also query per-datacenter stock.",
    )
    stock_parser.add_argument(
        "--gpu",
        action="append",
        default=[],
        help="Case-insensitive GPU ID/display-name substring; repeat to match any.",
    )
    stock_parser.add_argument(
        "--min-memory",
        type=float,
        default=0,
        metavar="GB",
        help="Minimum provider-reported GPU memory.",
    )
    stock_parser.add_argument(
        "--max-hourly",
        type=float,
        metavar="USD",
        help="Maximum total on-demand hourly price.",
    )
    stock_parser.add_argument(
        "--available-only",
        action="store_true",
        help="Hide stockStatus None and unavailable GPU counts.",
    )

    volume_parser = subparsers.add_parser(
        "volume", help="List and create persistent Runpod network volumes."
    )
    _add_agents_argument(volume_parser)
    volume_subparsers = volume_parser.add_subparsers(dest="volume_action")
    volume_list = volume_subparsers.add_parser(
        "list", help="List network volumes."
    )
    _add_common_provider_arguments(volume_list)
    volume_get = volume_subparsers.add_parser(
        "get", help="Get one network volume."
    )
    _add_common_provider_arguments(volume_get)
    volume_get.add_argument("volume_id")
    volume_create = volume_subparsers.add_parser(
        "create", help="Plan or create a network volume."
    )
    _add_common_provider_arguments(volume_create, state=True)
    volume_create.add_argument("name")
    volume_create.add_argument(
        "--size-gb",
        required=True,
        type=int,
        help="Standard network-volume size from 1 through 4000 GB.",
    )
    volume_create.add_argument(
        "--data-center",
        required=True,
        help="Exact live Secure Cloud datacenter ID.",
    )
    volume_create.add_argument(
        "--execute",
        action="store_true",
        help="Create the volume; otherwise print the exact request plan.",
    )

    template_parser = subparsers.add_parser(
        "template", help="Inspect Runpod templates available to the account."
    )
    _add_agents_argument(template_parser)
    template_subparsers = template_parser.add_subparsers(dest="template_action")
    template_list = template_subparsers.add_parser(
        "list", help="List templates without environment values."
    )
    _add_common_provider_arguments(template_list)
    template_list.add_argument(
        "--search", help="Case-insensitive substring over name, ID, and image."
    )

    profile_parser = subparsers.add_parser(
        "profile", help="Create and inspect validated local launch profiles."
    )
    _add_agents_argument(profile_parser)
    profile_subparsers = profile_parser.add_subparsers(dest="profile_action")
    profile_list = profile_subparsers.add_parser("list", help="List profiles.")
    _add_common_provider_arguments(
        profile_list, state=True, credentials=False
    )
    profile_show = profile_subparsers.add_parser("show", help="Show a profile.")
    _add_common_provider_arguments(
        profile_show, state=True, credentials=False
    )
    profile_show.add_argument("name")
    profile_create = profile_subparsers.add_parser(
        "create", help="Create a validated local launch profile."
    )
    _add_common_provider_arguments(
        profile_create, state=True, credentials=False
    )
    profile_create.add_argument("name")
    runtime_source = profile_create.add_mutually_exclusive_group(required=True)
    runtime_source.add_argument(
        "--image",
        help="Explicit immutable NAME@sha256:DIGEST container reference.",
    )
    runtime_source.add_argument(
        "--template-id", help="Exact account-visible Runpod template ID."
    )
    storage = profile_create.add_mutually_exclusive_group(required=True)
    storage.add_argument(
        "--network-volume-id",
        help="Persistent volume ID; this pins the profile datacenter.",
    )
    storage.add_argument(
        "--ephemeral",
        action="store_true",
        help="Explicitly create a profile without persistent model storage.",
    )
    profile_create.add_argument(
        "--gpu",
        action="append",
        required=True,
        help="GPU catalog alias or exact ID; repeat in fallback order.",
    )
    profile_create.add_argument(
        "--gpu-count",
        type=int,
        default=1,
        help="GPU count (default: 1; multi-GPU fit remains indeterminate).",
    )
    profile_create.add_argument(
        "--max-hourly",
        type=float,
        required=True,
        help="Maximum total Pod rate in USD/hour, excluding volume storage.",
    )
    profile_create.add_argument(
        "--ttl",
        default=DEFAULT_PROFILE_HARD_TTL,
        help=(
            "Default provider-enforced hard lifetime (default and maximum: "
            "30m); longer sessions require an explicit runpod-up --ttl "
            "override."
        ),
    )
    profile_create.add_argument(
        "--container-disk-gb",
        type=int,
        default=50,
        help="Ephemeral container disk size (default: 50 GB).",
    )
    profile_create.add_argument(
        "--min-vcpu-per-gpu",
        type=int,
        default=8,
        help="Minimum vCPU per GPU (default: 8).",
    )
    profile_create.add_argument(
        "--min-ram-per-gpu",
        type=int,
        default=32,
        help="Minimum host RAM GB per GPU (default: 32).",
    )
    profile_create.add_argument(
        "--cuda",
        action="append",
        default=[],
        help="Allowed CUDA version such as 12.8; repeat as needed.",
    )
    profile_create.add_argument(
        "--identity-file",
        default="~/.ssh/id_ed25519_runpod",
        help="Dedicated mode-0600 non-interactive private key.",
    )
    profile_create.add_argument(
        "--public-key-file",
        help="Public key to inject; defaults to IDENTITY_FILE.pub.",
    )
    profile_create.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Non-secret environment value; repeat as needed.",
    )
    profile_create.add_argument(
        "--replace",
        action="store_true",
        help="Atomically replace an existing profile.",
    )


def _credential_store(args: argparse.Namespace) -> CredentialStore:
    path = (
        pathlib.Path(args.credentials_file).expanduser().absolute()
        if getattr(args, "credentials_file", None)
        else credentials_file()
    )
    return CredentialStore(path)


def _api(args: argparse.Namespace) -> RunpodApi:
    credential = _credential_store(args).load(required=True)
    if credential is None:
        raise AssertionError("required credential unexpectedly absent")
    return RunpodApi(credential)


def _profile_store(args: argparse.Namespace) -> ProfileStore:
    root = state_root(getattr(args, "state_root", None))
    return ProfileStore(StateStore(root))


def _print_result(value: Any, *, as_json: bool) -> None:
    if as_json:
        print_json(value)
        return
    if isinstance(value, list):
        for item in value:
            print(_human_line(item))
    elif isinstance(value, dict):
        print(_human_line(value))
    else:
        print(value)


def _human_line(value: dict[str, Any]) -> str:
    if "gpu_id" in value:
        price = value.get("on_demand_price_per_gpu_hour")
        price_text = "unquoted" if price is None else f"${price:.3f}/GPU-h"
        return (
            f"{value.get('stock_status', 'None'):<6} "
            f"{value.get('memory_gb', '?'):>4}G  {price_text:<15} "
            f"{value.get('gpu_id')}"
        )
    if "dataCenterId" in value and "size" in value:
        return (
            f"{value.get('id')}  {value.get('size')} GB  "
            f"{value.get('dataCenterId')}  {value.get('name')}"
        )
    if "data_center_id" in value and "size_gb" in value:
        return (
            f"{value.get('id')}  {value.get('size_gb')} GB  "
            f"{value.get('data_center_id')}  {value.get('name')}"
        )
    if value.get("schema_version") == "runpod.profile.v1":
        pod = value["pod"]
        return (
            f"{value['name']}: {pod['gpu_count']}x {', '.join(pod['gpu_type_ids'])}; "
            f"{pod['storage_mode']}; cap ${value['limits']['max_hourly_usd']:.2f}/h"
        )
    return str(value)


def _parse_environment(plain_values: list[str]) -> dict[str, str]:
    environment: dict[str, str] = {}
    for assignment in plain_values:
        if "=" not in assignment:
            raise RunpodLocalError(
                f"environment assignment must be NAME=VALUE: {assignment!r}",
                code="invalid_profile_environment",
            )
        name, value = assignment.split("=", 1)
        if name in environment:
            raise RunpodLocalError(
                f"environment name specified more than once: {name}",
                code="duplicate_profile_environment",
            )
        environment[name] = value
    return environment


def _run_auth(args: argparse.Namespace) -> int:
    if not args.auth_action:
        raise RunpodLocalError(
            "auth action required: login, status, or logout",
            code="missing_action",
        )
    store = _credential_store(args)
    if args.auth_action == "login":
        if store.path.exists() and not args.replace:
            raise RunpodLocalError(
                f"credential file already exists: {store.path}; use --replace",
                code="credential_exists",
            )
        token = getpass.getpass("Runpod API key (input hidden): ")
        candidate = ApiCredential(token, source="prompt")
        pod_count = len(RunpodApi(candidate).list_pods())
        credential = store.store(token)
        result = {
            "schema_version": "runpod.auth.v1",
            "configured": True,
            "validated": True,
            "source": credential.source,
            "path": str(store.path),
            "visible_pod_count": pod_count,
        }
        _print_result(result, as_json=args.json)
        return 0
    if args.auth_action == "status":
        result = {
            "schema_version": "runpod.auth.v1",
            **store.status(),
            "validated": None,
        }
        if args.check:
            credential = store.load(required=True)
            if credential is None:
                raise AssertionError("required credential unexpectedly absent")
            result["visible_pod_count"] = len(
                RunpodApi(credential).list_pods()
            )
            result["validated"] = True
        _print_result(result, as_json=args.json)
        return 0
    plan = {
        "schema_version": "runpod.plan.v1",
        "action": "remove_local_credential",
        "path": str(store.path),
        "executed": args.execute,
    }
    if args.execute:
        plan["removed"] = store.remove()
    _print_result(plan, as_json=args.json)
    return 0


def _run_stock(args: argparse.Namespace) -> int:
    if not math.isfinite(args.min_memory) or args.min_memory < 0:
        raise RunpodLocalError(
            "--min-memory cannot be negative",
            code="invalid_stock_filter",
        )
    if args.max_hourly is not None and args.max_hourly <= 0:
        raise RunpodLocalError(
            "--max-hourly must be positive",
            code="invalid_stock_filter",
        )
    if args.max_hourly is not None and not math.isfinite(args.max_hourly):
        raise RunpodLocalError(
            "--max-hourly must be finite",
            code="invalid_stock_filter",
        )
    result = _api(args).stock(
        gpu_count=args.gpu_count,
        secure_cloud=not args.community,
        include_data_centers=args.data_centers,
    )
    names = [name.casefold() for name in args.gpu]
    filtered = []
    for gpu in result["gpus"]:
        searchable = (
            f"{gpu.get('gpu_id', '')} {gpu.get('display_name', '')}".casefold()
        )
        if names and not any(name in searchable for name in names):
            continue
        memory = gpu.get("memory_gb")
        if not isinstance(memory, (int, float)) or memory < args.min_memory:
            continue
        if args.available_only and not gpu_stock_is_available(
            gpu, gpu_count=args.gpu_count
        ):
            continue
        price = gpu.get("on_demand_price_per_gpu_hour")
        if (
            args.max_hourly is not None
            and (price is None or price * args.gpu_count > args.max_hourly)
        ):
            continue
        filtered.append(gpu)
    result["gpus"] = filtered
    if args.json:
        print_json(result)
    else:
        for gpu in filtered:
            print(_human_line(gpu))
    return 0


def _run_volume(args: argparse.Namespace) -> int:
    if not args.volume_action:
        raise RunpodLocalError(
            "volume action required: list, get, or create",
            code="missing_action",
        )
    if args.volume_action == "list":
        value = _api(args).list_network_volumes()
        if args.json:
            print_json(
                {
                    "schema_version": "runpod.volume-list.v1",
                    "volumes": value,
                }
            )
        else:
            _print_result(value, as_json=False)
        return 0
    if args.volume_action == "get":
        value = _api(args).get_network_volume(args.volume_id)
        _print_result(value, as_json=args.json)
        return 0
    if not 1 <= args.size_gb <= 4000:
        raise RunpodLocalError(
            "network volume size must be between 1 and 4000 GB",
            code="invalid_volume_size",
        )
    if not args.name or len(args.name) > 191:
        raise RunpodLocalError(
            "network volume name must be 1-191 characters",
            code="invalid_volume_name",
        )
    if any(ord(character) < 32 for character in args.name):
        raise RunpodLocalError(
            "network volume name cannot contain control characters",
            code="invalid_volume_name",
        )
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,191}", args.data_center):
        raise RunpodLocalError(
            f"invalid Runpod data center ID: {args.data_center!r}",
            code="invalid_provider_id",
        )
    request = {
        "name": args.name,
        "size_gb": args.size_gb,
        "data_center_id": args.data_center,
    }
    lock = (
        StateStore(state_root(args.state_root)).locked(
            volume_lock_scope(args.name)
        )
        if args.execute
        else contextlib.nullcontext()
    )
    with lock:
        api = _api(args)
        stock = api.stock(
            gpu_count=1,
            secure_cloud=True,
            include_data_centers=True,
        )
        center_matches = [
            center
            for center in stock["data_centers"]
            if center.get("data_center_id") == args.data_center
        ]
        if len(center_matches) != 1:
            raise RunpodLocalError(
                f"Runpod did not return exactly one Secure Cloud data center "
                f"for {args.data_center}",
                code="invalid_provider_id",
            )
        same_name = [
            volume
            for volume in api.list_network_volumes()
            if volume.get("name") == args.name
        ]
        exact_matches = [
            volume
            for volume in same_name
            if volume.get("size_gb") == args.size_gb
            and volume.get("data_center_id") == args.data_center
        ]
        if len(same_name) > 1 or (same_name and not exact_matches):
            raise RunpodLocalError(
                f"network volume name {args.name!r} is ambiguous or belongs to "
                "a different size/datacenter",
                code="volume_name_conflict",
            )
        if exact_matches and created_volume_violations(
            exact_matches[0], request
        ):
            raise RunpodLocalError(
                f"network volume {args.name!r} has no valid durable ID",
                code="invalid_provider_response",
            )
        result = {
            "schema_version": "runpod.plan.v1",
            "action": (
                "reuse_network_volume"
                if exact_matches
                else "create_network_volume"
            ),
            "request": request,
            "data_center": center_matches[0],
            "standard_storage_estimate": {
                "monthly_usd": standard_volume_monthly_usd(args.size_gb),
                "pricing": STANDARD_VOLUME_PRICING,
                "excludes_compute": True,
            },
            "executed": False,
        }
        if exact_matches:
            result["volume"] = exact_matches[0]
            result["reconciled_existing"] = True
        elif args.execute:
            created = api.create_network_volume(**request)
            violations = created_volume_violations(created, request)
            result["volume"] = created
            result["executed"] = True
            result["verification"] = {
                "status": "error" if violations else "verified",
                "violations": violations,
            }
    _print_result(result, as_json=args.json)
    return (
        1
        if result.get("verification", {}).get("status") == "error"
        else 0
    )


def _safe_template(template: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": template.get("id"),
        "name": template.get("name"),
        "image_name": template.get("imageName", template.get("image")),
        "container_disk_gb": template.get("containerDiskInGb"),
        "volume_mount_path": template.get("volumeMountPath"),
        "ports": template.get("ports")
        if isinstance(template.get("ports"), list)
        else [],
        "is_public": template.get("isPublic"),
    }


def _run_template(args: argparse.Namespace) -> int:
    if args.template_action != "list":
        raise RunpodLocalError(
            "template action required: list",
            code="missing_action",
        )
    templates = [_safe_template(value) for value in _api(args).list_templates()]
    if args.search:
        needle = args.search.casefold()
        templates = [
            template
            for template in templates
            if needle
            in (
                f"{template.get('id', '')} {template.get('name', '')} "
                f"{template.get('image_name', '')}"
            ).casefold()
        ]
    if args.json:
        print_json(
            {
                "schema_version": "runpod.template-list.v1",
                "templates": templates,
            }
        )
    else:
        _print_result(templates, as_json=False)
    return 0


def _run_profile(args: argparse.Namespace) -> int:
    if not args.profile_action:
        raise RunpodLocalError(
            "profile action required: list, show, or create",
            code="missing_action",
        )
    store = _profile_store(args)
    if args.profile_action == "list":
        _print_result(store.list(), as_json=args.json)
        return 0
    if args.profile_action == "show":
        _print_result(store.load(args.name), as_json=args.json)
        return 0
    default_ttl_seconds = parse_duration(args.ttl)
    if default_ttl_seconds > MAX_IMPLICIT_HARD_TTL_SECONDS:
        raise RunpodLocalError(
            "profile default hard lifetime cannot exceed 30m; longer sessions "
            "require an explicit runpod-up --ttl override",
            code="profile_ttl_too_long",
        )
    environment = _parse_environment(args.env)
    public_key_path, public_key = load_ssh_public_key_file(
        args.public_key_file or f"{args.identity_file}.pub"
    )
    validate_ssh_identity_file(args.identity_file)
    validate_ssh_key_pair(args.identity_file, public_key)
    profile = create_profile(
        name=args.name,
        gpu_names=args.gpu,
        max_hourly_usd=args.max_hourly,
        default_ttl_seconds=default_ttl_seconds,
        image_name=args.image,
        template_id=args.template_id,
        network_volume_id=args.network_volume_id,
        ephemeral=args.ephemeral,
        container_disk_gb=args.container_disk_gb,
        gpu_count=args.gpu_count,
        allowed_cuda_versions=args.cuda,
        min_vcpu_per_gpu=args.min_vcpu_per_gpu,
        min_ram_per_gpu=args.min_ram_per_gpu,
        identity_file=args.identity_file,
        public_key_file=str(public_key_path),
        ssh_public_key=public_key,
        environment=environment,
    )
    store.save(profile, replace=args.replace)
    _print_result(profile, as_json=args.json)
    return 0


def run_provider_command(args: argparse.Namespace) -> int:
    if args.command == "auth":
        return _run_auth(args)
    if args.command == "stock":
        return _run_stock(args)
    if args.command == "volume":
        return _run_volume(args)
    if args.command == "template":
        return _run_template(args)
    if args.command == "profile":
        return _run_profile(args)
    raise RunpodLocalError(
        f"unsupported provider command: {args.command}",
        code="unsupported_command",
    )
