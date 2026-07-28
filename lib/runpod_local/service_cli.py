"""CLI surface for generic, config-only inference services."""

from __future__ import annotations

import argparse
import pathlib
from typing import Any

from .api import RunpodApi
from .auth import CredentialStore
from .cache import JsonCache
from .errors import RunpodLocalError
from .http import JsonHttpTransport
from .huggingface_credentials import configured_huggingface_token
from .instances import InstanceStore
from .model import HuggingFaceClient
from .output import print_json
from .paths import credentials_file, dotfiles_root, state_root
from .remote import SshEndpoint, resolve_endpoint
from .runtime_catalog import load_runtime
from .service_bundle import build_service_bundle_plan
from .service_controller import (
    build_service_deployment_plan,
    build_service_validation,
)
from .service_definition import load_inference_service
from .service_deployment import (
    build_service_push_plan,
    push_service_materialization,
)
from .service_execution import (
    CACHE_ACTIONS,
    CACHE_MODES,
    RUNTIME_ACTIONS,
    build_service_runtime_plan,
    execute_service_runtime,
)
from .service_huggingface import (
    default_huggingface_closure_path,
    load_huggingface_closure,
    resolve_huggingface_closure,
    write_huggingface_closure,
)
from .service_installation import (
    InstalledService,
    ServiceInstallationStore,
    build_service_deployment_request,
    require_current_instance,
)
from .service_materialization import (
    build_service_materialization_plan,
    materialize_service,
)
from .service_vllm import DEFAULT_REMOTE_PORT
from .state import StateStore

SERVICE_COMMANDS = ("service",)
INSTALL_PLAN_SCHEMA = "runpod.inference-service-install-plan.v1"
LOCAL_ACTIONS = ("validate", "plan", "resolve", "bundle", "materialize")
REMOTE_ACTIONS = ("install", *RUNTIME_ACTIONS)
DRIFT_SENSITIVE_RUNTIME_ACTIONS = frozenset(RUNTIME_ACTIONS) - {
    "status",
    "stop",
}


def _add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "config",
        nargs="?",
        metavar="CONFIG",
        help="Path to the sole authored model-service TOML file.",
    )


def _add_json_argument(
    parser: argparse.ArgumentParser,
    *,
    help_text: str,
) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        help=help_text,
    )


def _add_closure_argument(
    parser: argparse.ArgumentParser,
    *,
    optional_inspection: bool,
) -> None:
    parser.add_argument(
        "--closure",
        metavar="PATH",
        help=(
            "Optional desired-request comparison closure; the installed "
            "deployment remains authoritative."
            if optional_inspection
            else (
                "Required generated, content-identified Hugging Face closure document."
            )
        ),
    )


def _add_remote_port_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--remote-port",
        type=int,
        default=DEFAULT_REMOTE_PORT,
        metavar="PORT",
        help=(
            f"Deployment-owned remote loopback port (default: {DEFAULT_REMOTE_PORT})."
        ),
    )


def _add_state_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-root",
        metavar="PATH",
        help="Override RUNPOD_HOME (default: ~/.local/runpod).",
    )


def _add_existing_instance_arguments(
    parser: argparse.ArgumentParser,
) -> None:
    parser.add_argument(
        "name",
        nargs="?",
        metavar="NAME",
        help="Already-active local Runpod instance name.",
    )
    parser.add_argument(
        "--credentials-file",
        metavar="PATH",
        help="Override the mode-0600 Runpod credential file.",
    )


def add_service_parser(subparsers: Any) -> None:
    service = subparsers.add_parser(
        "service",
        help=(
            "Validate, materialize, install, or operate one config-only "
            "inference service."
        ),
    )
    service.add_argument(
        "--agents-md",
        action="store_true",
        help="Print the inference-service operating contract and exit.",
    )
    service.set_defaults(service_action=None, json=False)
    actions = service.add_subparsers(dest="service_action")
    action_help = {
        "validate": "Validate one service definition.",
        "plan": "Build a non-executing remote deployment plan.",
        "resolve": (
            "Resolve the generated Hugging Face closure without "
            "downloading model bytes."
        ),
        "bundle": (
            "Bind a generated Hugging Face closure into a non-executing "
            "implementation bundle and manifest."
        ),
        "materialize": (
            "Publish the exact generic implementation and generated "
            "deployment transfer closure locally."
        ),
        "install": ("Install the exact materialization on an already-active instance."),
        "stage-snapshot": (
            "Stage the generated Hugging Face closure on an installed service."
        ),
        "prepare-cache": (
            "Prepare one explicit compiled-cache lifecycle mode remotely."
        ),
        "setup": "Verify and record the installed service prerequisites.",
        "start": "Start the installed service runtime.",
        "status": "Inspect the installed service runtime.",
        "stop": "Stop and audit the installed service runtime.",
    }
    for action, help_text in action_help.items():
        parser = actions.add_parser(
            action,
            help=help_text,
            allow_abbrev=False,
        )
        _add_config_argument(parser)
        _add_json_argument(
            parser,
            help_text=(
                "Format the completed metadata resolution as versioned JSON."
                if action == "resolve"
                else (
                    "Plan without local publication or remote execution and "
                    "emit the versioned result."
                    if action in {"materialize", *REMOTE_ACTIONS}
                    else "Emit the complete versioned machine-readable result."
                )
            ),
        )
        if action in {
            "plan",
            "bundle",
            "materialize",
            *REMOTE_ACTIONS,
        }:
            _add_remote_port_argument(parser)
        if action in {"bundle", "materialize", *REMOTE_ACTIONS}:
            _add_closure_argument(
                parser,
                optional_inspection=action in {"status", "stop"},
            )
        if action in {"materialize", *REMOTE_ACTIONS}:
            _add_state_argument(parser)
        if action in REMOTE_ACTIONS:
            _add_existing_instance_arguments(parser)
        if action in RUNTIME_ACTIONS:
            parser.add_argument(
                "--installed-materialization",
                metavar="SHA256_OR_PATH",
                help=(
                    "Operate one exact generated RUNPOD_HOME materialization "
                    "when its receipt is unavailable; never publishes state."
                ),
            )
        if action in CACHE_ACTIONS:
            parser.add_argument(
                "--cache-mode",
                required=True,
                choices=CACHE_MODES,
                help="Explicit compiled-cache lifecycle mode.",
            )
        if action == "resolve":
            parser.add_argument(
                "--offline",
                action="store_true",
                help="Require all Hugging Face metadata to be cached.",
            )
            parser.add_argument(
                "--refresh",
                action="store_true",
                help="Refresh Hugging Face metadata instead of reusing cache.",
            )
            parser.add_argument(
                "--state-root",
                metavar="PATH",
                help="Override the private Runpod state root.",
            )


def _required_closure(args: argparse.Namespace) -> pathlib.Path:
    if not getattr(args, "closure", None):
        raise RunpodLocalError(
            f"service {args.service_action} requires --closure",
            code="missing_service_huggingface_closure",
        )
    return pathlib.Path(args.closure).expanduser().absolute()


def _existing_endpoint(
    args: argparse.Namespace,
) -> tuple[StateStore, InstanceStore, SshEndpoint]:
    if not getattr(args, "name", None):
        raise RunpodLocalError(
            f"service {args.service_action} requires NAME",
            code="missing_instance_name",
        )
    root = state_root(args.state_root)
    state = StateStore(root)
    instances = InstanceStore(state)
    path = (
        pathlib.Path(args.credentials_file).expanduser().absolute()
        if args.credentials_file
        else credentials_file()
    )
    credential = CredentialStore(path).load(required=True)
    if credential is None:
        raise AssertionError("required credential unexpectedly absent")
    endpoint = resolve_endpoint(
        args.name,
        instances=instances,
        api=RunpodApi(credential),
        state=state,
    )
    return state, instances, endpoint


def _installed_runtime(
    args: argparse.Namespace,
    *,
    store: ServiceInstallationStore,
    endpoint: SshEndpoint,
    service_id: str,
) -> tuple[InstalledService, str]:
    selector = getattr(args, "installed_materialization", None)
    if selector:
        materialization = store.load_selector(selector)
        installed = store.inspect(
            materialization=materialization,
            endpoint=endpoint,
        )
        if installed.request.service_id != service_id:
            raise RunpodLocalError(
                "selected materialization belongs to another service",
                code="mismatched_service_materialization_selector",
            )
        return installed, "explicit-materialization"
    installed = store.load(
        instance_name=endpoint.instance_name,
        service_id=service_id,
    )
    if installed is None:
        raise AssertionError("required installation unexpectedly absent")
    require_current_instance(installed, endpoint=endpoint)
    return installed, "installation-receipt"


def run_service_command(args: argparse.Namespace) -> int:
    if args.service_action not in {*LOCAL_ACTIONS, *REMOTE_ACTIONS}:
        raise RunpodLocalError(
            "service requires validate, plan, resolve, bundle, materialize, "
            "install, or a runtime action",
            code="missing_service_action",
        )
    if not args.config:
        raise RunpodLocalError(
            f"service {args.service_action} requires CONFIG",
            code="missing_service_config",
        )
    source_path = pathlib.Path(args.config).expanduser().absolute()
    definition = load_inference_service(source_path)
    service = definition.normalized_plan()
    runtime_id = service["runtime_id"]
    service_id = service["service_id"]
    runtime_definition = None
    runtime = None
    if args.service_action in {
        "validate",
        "plan",
        "bundle",
        "materialize",
        "install",
    }:
        runtime_definition = load_runtime(runtime_id)
        runtime = runtime_definition.safe_summary()
    if args.service_action == "resolve":
        root = state_root(args.state_root)
        client = HuggingFaceClient(
            cache=JsonCache(root / "cache" / "huggingface"),
            transport=JsonHttpTransport(),
            token=configured_huggingface_token(),
            offline=args.offline,
            refresh=args.refresh,
        )
        closure = resolve_huggingface_closure(
            definition,
            client=client,
        )
        output_path = default_huggingface_closure_path(root, closure)
        write_huggingface_closure(output_path, closure)
        result = {
            "schema_version": "runpod.huggingface-closure-resolution.v1",
            "metadata_only": True,
            "model_bytes_downloaded": 0,
            "service_id": definition.normalized_plan()["service_id"],
            "service_plan_sha256": definition.plan_sha256,
            "output_path": str(output_path),
            "closure": closure.as_dict(),
        }
    elif args.service_action == "validate":
        if runtime is None:
            raise AssertionError("validation runtime unexpectedly absent")
        result = build_service_validation(
            definition,
            source_path=source_path,
            runtime=runtime,
        )
    elif args.service_action == "plan":
        if runtime is None:
            raise AssertionError("deployment runtime unexpectedly absent")
        result = build_service_deployment_plan(
            definition,
            source_path=source_path,
            source_root=dotfiles_root(),
            runtime=runtime,
            remote_port=args.remote_port,
        )
    elif args.service_action == "bundle":
        if runtime_definition is None:
            raise AssertionError("bundle runtime unexpectedly absent")
        closure_path = _required_closure(args)
        result = build_service_bundle_plan(
            definition,
            source_root=dotfiles_root(),
            runtime=runtime_definition,
            closure=load_huggingface_closure(closure_path),
            remote_port=args.remote_port,
        )
    else:
        closure = None
        desired_request = None
        if args.service_action not in {"status", "stop"} or getattr(
            args, "closure", None
        ):
            closure_path = _required_closure(args)
            closure = load_huggingface_closure(closure_path)
            desired_request = build_service_deployment_request(
                definition,
                closure=closure,
                remote_port=args.remote_port,
            )
        if args.service_action in {"materialize", "install"}:
            if runtime_definition is None:
                raise AssertionError("materialization runtime unexpectedly absent")
            if closure is None:
                raise AssertionError("materialization closure unexpectedly absent")
            materialization_plan = build_service_materialization_plan(
                definition,
                source_root=dotfiles_root(),
                state_root=state_root(args.state_root),
                runtime=runtime_definition,
                closure=closure,
                remote_port=args.remote_port,
            )
        if args.service_action == "materialize":
            result = (
                materialization_plan.safe_summary()
                if args.json
                else materialize_service(materialization_plan).safe_summary()
            )
        elif args.service_action == "install":
            state, instances, endpoint = _existing_endpoint(args)
            store = ServiceInstallationStore(state)
            if args.json:
                result = {
                    "schema_version": INSTALL_PLAN_SCHEMA,
                    "executed": False,
                    "provider_mutation": False,
                    "instance": endpoint.safe_dict(),
                    "materialization": materialization_plan.safe_summary(),
                    "remote_installation": {
                        "status": "available-after-materialization",
                        "transport_steps": 4,
                    },
                    "installation_receipt": {
                        "status": "available-after-installation",
                        "path": str(
                            store.receipt_path(
                                instance_name=endpoint.instance_name,
                                service_id=service_id,
                            )
                        ),
                    },
                }
            else:
                materialized = materialize_service(materialization_plan)
                push_plan = build_service_push_plan(
                    materialized,
                    endpoint=endpoint,
                    installer_path=materialization_plan.installer_path,
                )
                push_result = push_service_materialization(
                    push_plan,
                    resolved_endpoint=endpoint,
                    instances=instances,
                )
                installed, receipt_changed = store.publish(
                    materialization=materialized,
                    endpoint=endpoint,
                    instances=instances,
                )
                result = {
                    **push_result,
                    "installation_receipt": installed.safe_summary(),
                    "receipt_changed": receipt_changed,
                }
        else:
            state, instances, endpoint = _existing_endpoint(args)
            store = ServiceInstallationStore(state)
            installed, installation_source = _installed_runtime(
                args,
                store=store,
                endpoint=endpoint,
                service_id=service_id,
            )
            request_matches = (
                None
                if desired_request is None
                else installed.request == desired_request
            )
            if args.service_action in DRIFT_SENSITIVE_RUNTIME_ACTIONS:
                if desired_request is None:
                    raise AssertionError(
                        "drift-sensitive service request unexpectedly absent"
                    )
                if not request_matches:
                    raise RunpodLocalError(
                        "requested service config, model closure, or port "
                        "differs from the installed deployment; install it "
                        f"before this {args.service_action} action",
                        code="service_installation_request_drift",
                    )
            cache_mode = getattr(args, "cache_mode", None)
            runtime_plan = build_service_runtime_plan(
                installed.materialization,
                endpoint=endpoint,
                action=args.service_action,
                cache_mode=cache_mode,
            )
            if args.json:
                result = {
                    **runtime_plan.safe_summary(),
                    "installation_source": installation_source,
                    "installation": installed.safe_summary(),
                    "desired_service": (
                        None if desired_request is None else desired_request.as_dict()
                    ),
                    "desired_service_matches_installation": request_matches,
                }
            else:
                execute_service_runtime(
                    runtime_plan,
                    resolved_endpoint=endpoint,
                    instances=instances,
                )
                return 0
    if args.json:
        print_json(result)
    elif args.service_action == "validate":
        print(
            f"validated {result['service']['service_id']} "
            f"({result['service_plan_sha256']})"
        )
    elif args.service_action == "resolve":
        closure = result["closure"]
        print(
            f"resolved {result['service_id']} to {result['output_path']} "
            f"({closure['file_count']} files, "
            f"{closure['total_bytes']} bytes, "
            f"{closure['closure_sha256']})"
        )
    elif args.service_action == "plan":
        print(
            f"planned {result['service']['service_id']} on "
            f"127.0.0.1:{result['deployment']['remote_port']} "
            f"({result['plan_sha256']})"
        )
    elif args.service_action == "bundle":
        manifest = result["deployment_manifest"]
        print(
            f"bundled {definition.normalized_plan()['service_id']} as "
            f"{result['implementation_bundle']['bundle_sha256']} with "
            f"manifest {manifest['sha256']}"
        )
    elif args.service_action == "materialize":
        print(
            f"materialized {definition.normalized_plan()['service_id']} at "
            f"{result['root']} ({result['materialization_sha256']})"
        )
    elif args.service_action == "install":
        print(
            f"installed {definition.normalized_plan()['service_id']} on "
            f"{args.name} ({result['materialization_sha256']})"
        )
    return 0
