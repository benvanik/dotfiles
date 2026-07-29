"""One-command model service and isolated Pi user surface."""

from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import sys
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from .agents import AGENTS_MD
from .cache import JsonCache
from .catalog import (
    list_profile_ids,
    list_service_ids,
    load_profile_route,
    load_service_id,
)
from .configuration import load_lab_configuration
from .controller import build_claim_request
from .dependencies import Dependencies, build_dependencies
from .errors import ModelLabError
from .hf_auth import manage_huggingface_credential
from .huggingface_credentials import configured_huggingface_token
from .huggingface_model import HuggingFaceClient, ModelInspector
from .lifecycle import DeploymentStore
from .migration import MigrationPolicy, migrate_legacy_profile
from .paths import authored_root, profile_path, runtime_root, state_root
from .placement import load_hardware_catalog, place_model
from model_session.attachment import ServiceEndpointBinding
from model_session.errors import ModelSessionError
from model_session.profile import load_profile
from model_session.service_endpoint import service_workload_identity

VERSION = "0.1.0"


def _add_model_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "repository",
        nargs="?",
        help="Hugging Face repository in exact namespace/name form",
    )
    parser.add_argument("--revision", default="main")
    parser.add_argument("--index-file")
    parser.add_argument("--context", type=int, default=32768, metavar="TOKENS")
    parser.add_argument("--sequences", type=int, default=1)
    parser.add_argument(
        "--kv-dtype",
        choices=("bf16", "fp16", "fp8"),
        default="bf16",
    )
    parser.add_argument(
        "--weight-format",
        choices=("native", "bf16", "fp8", "int8", "q8"),
        default="native",
    )
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--json", action="store_true")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="model-lab",
        description=(
            "Run exact private model services and isolated Pi sessions above "
            "generic RunPod host claims."
        ),
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument(
        "--agents-md",
        action="store_true",
        help="print the complete agent operating contract and exit",
    )
    parser.add_argument(
        "--root",
        metavar="PATH",
        help="override MODEL_LAB_ROOT (default: /mnt/dev/model-lab)",
    )
    parser.add_argument(
        "--state-root",
        metavar="PATH",
        help=("override MODEL_LAB_STATE_HOME (default: ~/.local/state/model-lab)"),
    )
    subparsers = parser.add_subparsers(dest="command")

    model = subparsers.add_parser(
        "model",
        help="inspect an exact Hugging Face checkpoint and estimate memory",
        allow_abbrev=False,
    )
    _add_model_arguments(model)

    place = subparsers.add_parser(
        "place",
        help="compare a model estimate with the static GPU catalog",
        allow_abbrev=False,
    )
    _add_model_arguments(place)
    place.add_argument("--gpu", action="append", default=[])
    place.add_argument("--gpu-count", type=int, default=1)
    place.add_argument("--gpu-memory-utilization", type=float, default=0.90)
    place.add_argument("--weight-slack", type=float, default=1.03)
    place.add_argument("--framework-reserve-gib", type=float, default=4.0)
    place.add_argument("--list-gpus", action="store_true")

    hf_auth = subparsers.add_parser(
        "hf-auth",
        help="manage a model-owned ephemeral HF credential on one active host",
        allow_abbrev=False,
    )
    hf_auth_actions = hf_auth.add_subparsers(dest="hf_auth_action")
    for action in ("push", "status", "clear"):
        action_parser = hf_auth_actions.add_parser(action, allow_abbrev=False)
        action_parser.add_argument("host_name")
        action_parser.add_argument("--runpod-state-root")
        action_parser.add_argument("--credentials-file")
        action_parser.add_argument("--json", action="store_true")
        if action == "push":
            action_parser.add_argument("--token-file")

    migrate = subparsers.add_parser(
        "migrate",
        help="provider-free migration of one quiesced legacy profile",
        allow_abbrev=False,
    )
    migrate.add_argument("source_profile_root")
    migrate.add_argument("--service", required=True)
    migrate.add_argument("--target-profile-id")
    migrate.add_argument("--target-project-id")
    migrate.add_argument("--session", action="append", dest="session_ids")
    migrate.add_argument(
        "--v1-policy-profile",
        help="current profile whose explicit storage/sandbox policy admits v1",
    )
    migrate.add_argument("--json", action="store_true")

    plan = subparsers.add_parser(
        "plan",
        help="show the generic host claim needed by one service",
        allow_abbrev=False,
    )
    plan.add_argument("service")
    plan.add_argument("--host")
    plan.add_argument("--json", action="store_true")

    up = subparsers.add_parser(
        "up",
        help="ensure one service and start its idle TTL",
        allow_abbrev=False,
    )
    up.add_argument("service")
    up.add_argument("--host")
    up.add_argument("--json", action="store_true")

    status = subparsers.add_parser(
        "status",
        help="show local service deployments without provider access",
        allow_abbrev=False,
    )
    status.add_argument("service", nargs="?")
    status.add_argument("--json", action="store_true")

    down = subparsers.add_parser(
        "down",
        help="start service idle grace or stop immediately",
        allow_abbrev=False,
    )
    down.add_argument("service")
    down.add_argument(
        "--now",
        action="store_true",
        help="stop the service and release its RunPod claim immediately",
    )
    down.add_argument("--json", action="store_true")

    pi = subparsers.add_parser(
        "pi",
        help="ensure a service and run an isolated Pi session",
        allow_abbrev=False,
    )
    pi.add_argument("profile")
    pi.add_argument("session_action", nargs="?", choices=("resume",))
    pi.add_argument("session_id", nargs="?")
    pi.add_argument("--host")
    pi.add_argument(
        "--now",
        action="store_true",
        help=(
            "after the final Pi user exits, stop the model service and "
            "release its RunPod claim without the model idle grace"
        ),
    )

    service = subparsers.add_parser(
        "service",
        help="inspect authored model service definitions",
        allow_abbrev=False,
    )
    service_actions = service.add_subparsers(dest="service_action")
    service_actions.add_parser("list", allow_abbrev=False).add_argument(
        "--json", action="store_true"
    )
    service_show = service_actions.add_parser("show", allow_abbrev=False)
    service_show.add_argument("service")
    service_show.add_argument("--json", action="store_true")
    service_validate = service_actions.add_parser("validate", allow_abbrev=False)
    service_validate.add_argument("service")
    service_validate.add_argument("--json", action="store_true")

    profile = subparsers.add_parser(
        "profile",
        help="inspect model-session-owned profiles",
        allow_abbrev=False,
    )
    profile_actions = profile.add_subparsers(dest="profile_action")
    profile_actions.add_parser("list", allow_abbrev=False).add_argument(
        "--json", action="store_true"
    )
    profile_show = profile_actions.add_parser("show", allow_abbrev=False)
    profile_show.add_argument("profile")
    profile_show.add_argument("--json", action="store_true")
    return parser


def _print_json(value: Any, *, output: Any) -> None:
    print(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ),
        file=output,
    )


def _dependency(
    dependencies: Dependencies | None,
    factory: Callable[..., Dependencies],
    *,
    root: pathlib.Path,
    machine_state: pathlib.Path,
    boot_runtime: pathlib.Path,
) -> Dependencies:
    return (
        dependencies
        if dependencies is not None
        else factory(
            authored_root=root,
            state_root=machine_state,
            runtime_root=boot_runtime,
        )
    )


def _status_documents(
    store: DeploymentStore,
    service_id: str | None,
    *,
    root: pathlib.Path,
) -> list[dict[str, Any]]:
    identifiers = (
        (service_id,) if service_id is not None else list_service_ids(root=root)
    )
    documents = []
    for identifier in identifiers:
        deployment = store.load(identifier)
        documents.append(
            {
                "service_id": identifier,
                "deployment": (None if deployment is None else deployment.normalized()),
            }
        )
    return documents


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    return f"{value / (1024**3):.2f} GiB ({value:,} bytes)"


def _inspect_model(
    parsed: argparse.Namespace,
    *,
    machine_state: pathlib.Path,
) -> dict[str, Any]:
    if parsed.repository is None:
        raise ModelLabError(
            "a Hugging Face repository is required",
            code="missing_repository",
        )
    client = HuggingFaceClient(
        cache=JsonCache(machine_state / "cache" / "huggingface-metadata"),
        token=configured_huggingface_token(),
        offline=parsed.offline,
        refresh=parsed.refresh,
    )
    return ModelInspector(client).inspect(
        parsed.repository,
        revision=parsed.revision,
        index_file=parsed.index_file,
        context_tokens=parsed.context,
        sequences=parsed.sequences,
        kv_dtype=parsed.kv_dtype,
        weight_format=parsed.weight_format,
    )


def _print_model_human(report: dict[str, Any], *, output: Any) -> None:
    repository = report["repository"]
    checkpoint = report["checkpoint"]
    architecture = report["architecture"]
    runtime = report["runtime_estimate"]
    kv_cache = runtime["kv_cache"]
    print(
        f"{repository['id']} @ {repository['resolved_revision']}",
        file=output,
    )
    print(
        f"  checkpoint: {checkpoint['file_count']} files, "
        f"{_format_bytes(checkpoint['download_bytes'])} download",
        file=output,
    )
    parameter_count = checkpoint["parameter_count"]
    print(
        f"  tensors:    {_format_bytes(checkpoint['tensor_bytes'])}, "
        f"{parameter_count if parameter_count is not None else 'unknown'} parameters",
        file=output,
    )
    family = "MoE" if architecture["is_moe"] else "dense/unspecified"
    layer_count = architecture["layer_count"]
    print(
        f"  model:      {architecture['model_type'] or 'unknown'} ({family}), "
        f"{layer_count if layer_count is not None else 'unknown'} layers",
        file=output,
    )
    print(
        f"  weights:    {_format_bytes(runtime['weight_bytes'])} "
        f"({runtime['weight_format']}, {runtime['weight_source']})",
        file=output,
    )
    if kv_cache["available"]:
        print(
            f"  KV cache:   {_format_bytes(kv_cache['bytes'])} for "
            f"{kv_cache['sequences']} × {kv_cache['context_tokens']:,} tokens "
            f"at {kv_cache['dtype']}",
            file=output,
        )
    else:
        print(f"  KV cache:   unknown ({kv_cache['reason']})", file=output)
    for warning in report["warnings"]:
        print(f"  warning:    {warning}", file=output)


def _print_gpu_catalog(
    catalog: dict[str, Any],
    *,
    as_json: bool,
    output: Any,
) -> None:
    if as_json:
        _print_json(catalog, output=output)
        return
    print(
        "GPU ID                                                   VRAM  ALIASES",
        file=output,
    )
    for gpu in catalog["gpus"]:
        print(
            f"{gpu['id'][:56]:<56} "
            f"{gpu['provider_memory_gb']:>4g}G  "
            f"{', '.join(gpu.get('aliases', []))}",
            file=output,
        )


def _print_placement_human(report: dict[str, Any], *, output: Any) -> None:
    model = report["model"]
    print(
        f"{model['repository']} @ {model['resolved_revision']} "
        f"({model['weight_format']})",
        file=output,
    )
    print(
        "STATUS         GPU                              VRAM   REQUIRED  HEADROOM",
        file=output,
    )
    for placement in report["placements"]:
        print(
            f"{placement['status']:<14} "
            f"{placement['display_name'][:31]:<31} "
            f"{placement['provider_memory_gb']:>5g}G "
            f"{placement['required_gib_per_gpu']:>8.2f}G "
            f"{placement['headroom_gib_per_gpu']:>8.2f}G",
            file=output,
        )
        if placement["status"] != "candidate":
            print(f"  {'; '.join(placement['reasons'])}", file=output)


def _path_argument(value: str | None) -> pathlib.Path | None:
    if value is None:
        return None
    return pathlib.Path(value).expanduser().absolute()


def _migration_policy(path: str | None) -> MigrationPolicy | None:
    if path is None:
        return None
    profile_root = pathlib.Path(path).expanduser().absolute()
    try:
        profile = load_profile(profile_root)
    except ModelSessionError as error:
        raise ModelLabError(
            f"cannot load v1 migration policy profile {profile_root}: {error}",
            code=error.code,
        ) from error
    storage = profile.contract.storage
    sandbox = profile.contract.sandbox
    if storage is None or sandbox is None:
        raise ModelLabError(
            "v1 migration policy profile has no explicit storage or sandbox "
            "contract",
            code="invalid_legacy_migration_policy",
        )
    return MigrationPolicy(storage=storage, sandbox=sandbox)


def _migration_document(result: Any) -> dict[str, Any]:
    return {
        "schema": "model-lab.migration.v1",
        "migration_id": result.migration_id,
        "profile_id": result.profile_id,
        "project_id": result.project_id,
        "service_id": result.service_id,
        "workload_sha256": result.workload_sha256,
        "profile_root": str(result.profile_root),
        "state_root": str(result.state_root),
        "receipt_path": str(result.receipt_path),
        "runs": [run.normalized() for run in result.runs],
    }


def main(
    arguments: Sequence[str] | None = None,
    *,
    dependencies: Dependencies | None = None,
    dependency_factory: Callable[..., Dependencies] = build_dependencies,
    output: Any = None,
    error: Any = None,
) -> int:
    stdout = sys.stdout if output is None else output
    stderr = sys.stderr if error is None else error
    parser = _parser()
    parsed = parser.parse_args(list(sys.argv[1:] if arguments is None else arguments))
    if parsed.agents_md:
        print(AGENTS_MD, end="", file=stdout)
        return 0
    root = authored_root(parsed.root)
    machine_state = state_root(parsed.state_root)
    try:
        if parsed.command is None:
            parser.print_help(file=stdout)
            return 0
        if parsed.command == "model":
            report = _inspect_model(parsed, machine_state=machine_state)
            if parsed.json:
                _print_json(report, output=stdout)
            else:
                _print_model_human(report, output=stdout)
            return 0
        if parsed.command == "place":
            catalog = load_hardware_catalog()
            if parsed.list_gpus:
                _print_gpu_catalog(catalog, as_json=parsed.json, output=stdout)
                return 0
            model = _inspect_model(parsed, machine_state=machine_state)
            report = place_model(
                model,
                catalog=catalog,
                requested_gpus=parsed.gpu,
                gpu_count=parsed.gpu_count,
                gpu_memory_utilization=parsed.gpu_memory_utilization,
                weight_slack=parsed.weight_slack,
                framework_reserve_gib=parsed.framework_reserve_gib,
            )
            if parsed.json:
                _print_json(report, output=stdout)
            else:
                _print_placement_human(report, output=stdout)
            return 0
        if parsed.command == "hf-auth":
            if parsed.hf_auth_action is None:
                raise ModelLabError(
                    "hf-auth requires push, status, or clear",
                    code="missing_hf_auth_action",
                )
            result = manage_huggingface_credential(
                parsed.hf_auth_action,
                parsed.host_name,
                token_file=_path_argument(getattr(parsed, "token_file", None)),
                runpod_state_root=_path_argument(parsed.runpod_state_root),
                credentials_path=_path_argument(parsed.credentials_file),
            )
            if parsed.json:
                _print_json(result, output=stdout)
            else:
                credential_state = (
                    "configured" if result["configured"] else "absent"
                )
                print(
                    f"{result['host_name']}: Hugging Face credential "
                    f"{credential_state}",
                    file=stdout,
                )
            return 0
        if parsed.command == "migrate":
            service = load_service_id(parsed.service, root=root)
            workload = service.service_workload()
            binding = ServiceEndpointBinding(
                service_id=service.service_id,
                service_sha256=service.service_sha256,
                workload=workload,
                workload_sha256=service_workload_identity(workload),
                input_modalities=service.endpoint.input_modalities,
            )
            result = migrate_legacy_profile(
                pathlib.Path(parsed.source_profile_root).expanduser().absolute(),
                root,
                service_binding=binding,
                target_profile_id=parsed.target_profile_id,
                target_project_id=parsed.target_project_id,
                session_ids=parsed.session_ids,
                v1_policy=_migration_policy(parsed.v1_policy_profile),
            )
            document = _migration_document(result)
            if parsed.json:
                _print_json(document, output=stdout)
            else:
                print(
                    f"migrated {result.profile_id}: {len(result.runs)} runs; "
                    f"receipt {result.receipt_path}",
                    file=stdout,
                )
            return 0
        if parsed.command == "service":
            if parsed.service_action == "list":
                identifiers = list_service_ids(root=root)
                if parsed.json:
                    _print_json(
                        {
                            "schema": "model-lab.service-list.v1",
                            "services": list(identifiers),
                        },
                        output=stdout,
                    )
                else:
                    print("\n".join(identifiers), file=stdout)
                return 0
            if parsed.service_action in {"show", "validate"}:
                service = load_service_id(parsed.service, root=root)
                document = {
                    "schema": "model-lab.service-validation.v1",
                    "valid": True,
                    "source": service.source_label,
                    "source_sha256": service.source_sha256,
                    "workload_sha256": service.workload_sha256,
                    "service_sha256": service.service_sha256,
                    "service": service.normalized_plan(),
                }
                if parsed.json or parsed.service_action == "show":
                    _print_json(document, output=stdout)
                else:
                    print(
                        f"validated {service.service_id} ({service.service_sha256})",
                        file=stdout,
                    )
                return 0
            raise ModelLabError(
                "service requires list, show, or validate",
                code="missing_service_action",
            )
        if parsed.command == "profile":
            if parsed.profile_action == "list":
                identifiers = list_profile_ids(root=root)
                if parsed.json:
                    _print_json(
                        {
                            "schema": "model-lab.profile-list.v1",
                            "profiles": list(identifiers),
                        },
                        output=stdout,
                    )
                else:
                    print("\n".join(identifiers), file=stdout)
                return 0
            if parsed.profile_action == "show":
                route = load_profile_route(parsed.profile, root=root)
                _print_json(
                    {
                        "schema": "model-lab.profile-route.v1",
                        "profile_id": route.profile_id,
                        "project_id": route.project_id,
                        "service_id": route.service_id,
                        "required_input_modalities": list(
                            route.required_input_modalities
                        ),
                        "root": str(profile_path(route.profile_id, root).parent),
                    },
                    output=stdout,
                )
                return 0
            raise ModelLabError(
                "profile requires list or show",
                code="missing_profile_action",
            )
        if parsed.command == "status":
            if parsed.service is not None:
                load_service_id(parsed.service, root=root)
            documents = _status_documents(
                DeploymentStore(machine_state),
                parsed.service,
                root=root,
            )
            result = {
                "schema": "model-lab.status.v1",
                "services": documents,
            }
            if parsed.json:
                _print_json(result, output=stdout)
            else:
                for item in documents:
                    deployment = item["deployment"]
                    phase = "absent" if deployment is None else deployment["phase"]
                    print(f"{item['service_id']}: {phase}", file=stdout)
            return 0

        lab = load_lab_configuration(root / "lab.toml")
        if parsed.command == "plan":
            service = load_service_id(parsed.service, root=root)
            request = build_claim_request(
                service,
                lab,
                operation_id="available-at-execution",
                host_name=parsed.host,
            )
            result = {
                "schema": "model-lab.plan.v1",
                "service": service.normalized_plan(),
                "workload_sha256": service.workload_sha256,
                "service_sha256": service.service_sha256,
                "runpod_claim": dataclasses.asdict(request),
                "provider_mutation": False,
            }
            _print_json(result, output=stdout)
            return 0
        boot_runtime = runtime_root()
        dependency = _dependency(
            dependencies,
            dependency_factory,
            root=root,
            machine_state=machine_state,
            boot_runtime=boot_runtime,
        )
        if parsed.command == "up":
            service = load_service_id(parsed.service, root=root)
            response = dependency.supervisor.request(
                "up",
                {
                    "service_id": service.service_id,
                    "host_name": parsed.host,
                },
            )
            result = {
                "schema": "model-lab.up.v1",
                "service_id": service.service_id,
                **response,
            }
            if parsed.json:
                _print_json(result, output=stdout)
            else:
                deployment = response["deployment"]
                print(
                    f"{service.service_id}: ready on "
                    f"{deployment['host_name']}; "
                    f"idle deadline {deployment['idle_deadline']}",
                    file=stdout,
                )
            return 0
        if parsed.command == "down":
            service = load_service_id(parsed.service, root=root)
            response = dependency.supervisor.request(
                "down",
                {
                    "service_id": service.service_id,
                    "now": parsed.now,
                },
            )
            deployment = response["deployment"]
            if parsed.json:
                _print_json(
                    {
                        "schema": "model-lab.down.v1",
                        "service_id": service.service_id,
                        "now": parsed.now,
                        "deployment": deployment,
                    },
                    output=stdout,
                )
            else:
                print(
                    f"{service.service_id}: {deployment['phase']}"
                    + (
                        ""
                        if deployment["idle_deadline"] is None
                        else f" until {deployment['idle_deadline']}"
                    ),
                    file=stdout,
                )
            return 0
        if parsed.command == "pi":
            route = load_profile_route(parsed.profile, root=root)
            service = load_service_id(route.service_id, root=root)
            progress_done = threading.Event()
            progress_started = time.monotonic()
            print(
                f"model-lab: {route.profile_id}: ensuring "
                f"{service.service_id} endpoint...",
                file=stderr,
                flush=True,
            )

            def report_progress() -> None:
                while not progress_done.wait(30):
                    elapsed = round(time.monotonic() - progress_started)
                    print(
                        f"model-lab: {route.profile_id}: still ensuring "
                        f"{service.service_id} endpoint ({elapsed}s elapsed)...",
                        file=stderr,
                        flush=True,
                    )

            progress = threading.Thread(
                target=report_progress,
                name="model-lab-cli-progress",
                daemon=True,
            )
            progress.start()
            try:
                channel = dependency.supervisor.acquire_pi(
                    profile_id=route.profile_id,
                    host_name=parsed.host,
                    stop_on_release=parsed.now,
                )
            finally:
                progress_done.set()
                progress.join()
            elapsed = round(time.monotonic() - progress_started)
            print(
                f"model-lab: {route.profile_id}: endpoint ready "
                f"({elapsed}s); launching Pi",
                file=stderr,
                flush=True,
            )
            if (
                channel.pending.service_id != service.service_id
                or channel.pending.workload_sha256 != service.workload_sha256
            ):
                channel.close()
                raise ModelLabError(
                    "supervisor Pi grant differs from the exact profile service",
                    code="supervisor_grant_mismatch",
                )
            session_arguments = (
                []
                if parsed.session_action is None
                else [
                    "resume",
                    *([] if parsed.session_id is None else [parsed.session_id]),
                ]
            )
            return dependency.run_model_session(
                profile_path(route.profile_id, root).parent,
                session_arguments,
                channel,
            )
        raise ModelLabError(
            f"unsupported model-lab command: {parsed.command}",
            code="unsupported_command",
        )
    except ModelLabError as exception:
        print(
            f"model-lab: {exception.code}: {exception}",
            file=stderr,
        )
        return 2
