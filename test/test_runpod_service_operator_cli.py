from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from runpod_local.auth import ApiCredential
from runpod_local.cli import build_parser, parse_arguments
from runpod_local.errors import RunpodLocalError
from runpod_local.instances import InstanceStore
from runpod_local.remote import SshEndpoint
from runpod_local.runtime_catalog import load_runtime
from runpod_local.service_cli import _existing_endpoint, run_service_command
from runpod_local.service_definition import load_inference_service
from runpod_local.service_execution import CACHE_ACTIONS, RUNTIME_ACTIONS
from runpod_local.service_huggingface import (
    HuggingFaceClosure,
    HuggingFaceClosureFile,
)
from runpod_local.service_installation import ServiceInstallationStore
from runpod_local.service_materialization import (
    build_service_materialization_plan,
    materialize_service,
)
from runpod_local.state import StateStore
from runpod_service_test_fixture import SERVICE_FIXTURE

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = SERVICE_FIXTURE
OPERATION_ID = "11111111-1111-4111-8111-111111111111"


def closure(identity_digit: str = "7") -> HuggingFaceClosure:
    definition = load_inference_service(FIXTURE)
    model = definition.normalized_plan()["model"]
    checkpoint = model["checkpoint"]
    if not isinstance(checkpoint, str):
        raise TypeError("fixture requires one exact checkpoint")
    return HuggingFaceClosure(
        repository=model["repository"],
        revision=model["revision"],
        requested_selector=checkpoint,
        resolved_index=None,
        weight_files=(checkpoint,),
        files=(
            HuggingFaceClosureFile(
                path=checkpoint,
                bytes=4096,
                role="checkpoint-weight",
                identity_algorithm="sha256",
                identity_digest=identity_digit * 64,
            ),
        ),
    )


def write_closure(path: pathlib.Path, *, identity_digit: str = "7") -> None:
    path.write_text(
        json.dumps(closure(identity_digit).as_dict(), sort_keys=True),
        encoding="ascii",
    )
    path.chmod(0o600)


def endpoint(
    root: pathlib.Path,
    *,
    operation_id: str = OPERATION_ID,
    pod_id: str = "pod-1",
) -> SshEndpoint:
    return SshEndpoint(
        instance_name="active-instance",
        operation_id=operation_id,
        pod_id=pod_id,
        host="203.0.113.42",
        port=22022,
        user="root",
        identity_file=root / "identity",
        known_hosts_file=root / "known-hosts",
        host_key_alias=f"runpod-{pod_id}",
    )


def materialized_fixture(
    state: StateStore,
    *,
    identity_digit: str = "7",
):
    materialization_plan = build_service_materialization_plan(
        load_inference_service(FIXTURE),
        source_root=ROOT,
        state_root=state.root,
        runtime=load_runtime("vllm-cu129-v0.25.1"),
        closure=closure(identity_digit),
        remote_port=8000,
    )
    return materialize_service(materialization_plan)


def installed_fixture(
    state: StateStore,
    *,
    target: SshEndpoint,
):
    materialized = materialized_fixture(state)
    instances = InstanceStore(state)
    with mock.patch.object(
        instances,
        "locked_active_lease",
        return_value=contextlib.nullcontext({}),
    ):
        installed, _ = ServiceInstallationStore(state).publish(
            materialization=materialized,
            endpoint=target,
            instances=instances,
        )
    return installed


class ServiceOperatorCliTest(unittest.TestCase):
    def test_parser_requires_cache_mode_only_for_cache_actions(self):
        parser = build_parser()
        closure_path = "/private/generated/closure.json"
        for action in RUNTIME_ACTIONS:
            arguments = [
                "service",
                action,
                "/private/model.toml",
                "active-instance",
                "--closure",
                closure_path,
            ]
            with self.subTest(action=action):
                if action in CACHE_ACTIONS:
                    with (
                        contextlib.redirect_stderr(io.StringIO()),
                        self.assertRaises(SystemExit),
                    ):
                        parse_arguments(parser, arguments)
                    parsed = parse_arguments(
                        parser,
                        [*arguments, "--cache-mode", "accepted"],
                    )
                    self.assertEqual(parsed.cache_mode, "accepted")
                else:
                    parsed = parse_arguments(parser, arguments)
                    self.assertEqual(parsed.service_action, action)
                    with (
                        contextlib.redirect_stderr(io.StringIO()),
                        self.assertRaises(SystemExit),
                    ):
                        parse_arguments(
                            parser,
                            [*arguments, "--cache-mode", "accepted"],
                        )

    def test_materialize_json_is_a_nonwriting_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            closure_path = root / "closure.json"
            state = root / "state"
            write_closure(closure_path)
            environment = {
                "PATH": os.environ["PATH"],
                "PYTHONDONTWRITEBYTECODE": "1",
            }

            completed = subprocess.run(
                [
                    str(ROOT / "bin" / "runpod-service"),
                    "materialize",
                    str(FIXTURE),
                    "--closure",
                    str(closure_path),
                    "--state-root",
                    str(state),
                    "--json",
                ],
                cwd=ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            result = json.loads(completed.stdout)

            self.assertIs(result["executed"], False)
            self.assertFalse(state.exists())
            self.assertEqual(
                result["schema_version"],
                "runpod.inference-service-materialization-plan.v1",
            )
            service_files = [
                record
                for record in result["files"]
                if record["role"] == "deployment-manifest"
            ]
            self.assertEqual(len(service_files), 1)
            self.assertNotIn(
                ".toml",
                "\n".join(record["remote_path"] for record in result["files"]),
            )

    def test_install_json_does_not_materialize_or_open_ssh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            closure_path = root / "closure.json"
            state = root / "state"
            write_closure(closure_path)
            target = endpoint(root)
            state_store = StateStore(state)
            arguments = parse_arguments(
                build_parser(),
                [
                    "service",
                    "install",
                    str(FIXTURE),
                    "active-instance",
                    "--closure",
                    str(closure_path),
                    "--state-root",
                    str(state),
                    "--json",
                ],
            )
            output = io.StringIO()
            with (
                mock.patch(
                    "runpod_local.service_cli._existing_endpoint",
                    return_value=(state_store, object(), target),
                ),
                mock.patch(
                    "runpod_local.service_cli.materialize_service",
                    side_effect=AssertionError("local publication attempted"),
                ),
                mock.patch(
                    "runpod_local.service_cli.push_service_materialization",
                    side_effect=AssertionError("SSH deployment attempted"),
                ),
                contextlib.redirect_stdout(output),
            ):
                return_code = run_service_command(arguments)
            result = json.loads(output.getvalue())

            self.assertEqual(return_code, 0)
            self.assertIs(result["executed"], False)
            self.assertIs(result["provider_mutation"], False)
            self.assertEqual(
                result["remote_installation"]["status"],
                "available-after-materialization",
            )
            self.assertEqual(
                result["installation_receipt"]["status"],
                "available-after-installation",
            )
            self.assertFalse(state.exists())

    def test_runtime_json_does_not_materialize_or_open_ssh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            closure_path = root / "closure.json"
            state = root / "state"
            write_closure(closure_path)
            target = endpoint(root)
            state_store = StateStore(state)
            installed = installed_fixture(state_store, target=target)
            receipt_path = ServiceInstallationStore(state_store).receipt_path(
                instance_name=target.instance_name,
                service_id=installed.request.service_id,
            )
            receipt_before = receipt_path.read_bytes()
            arguments = parse_arguments(
                build_parser(),
                [
                    "service",
                    "start",
                    str(FIXTURE),
                    "active-instance",
                    "--closure",
                    str(closure_path),
                    "--state-root",
                    str(state),
                    "--cache-mode",
                    "ephemeral",
                    "--json",
                ],
            )
            output = io.StringIO()
            with (
                mock.patch(
                    "runpod_local.service_cli._existing_endpoint",
                    return_value=(state_store, object(), target),
                ),
                mock.patch(
                    "runpod_local.service_cli.materialize_service",
                    side_effect=AssertionError("local publication attempted"),
                ),
                mock.patch(
                    "runpod_local.service_cli.execute_service_runtime",
                    side_effect=AssertionError("SSH execution attempted"),
                ),
                contextlib.redirect_stdout(output),
            ):
                return_code = run_service_command(arguments)
            result = json.loads(output.getvalue())

            self.assertEqual(return_code, 0)
            self.assertIs(result["executed"], False)
            self.assertIs(result["provider_mutation"], False)
            self.assertEqual(result["action"], "start")
            self.assertEqual(result["cache_mode"], "ephemeral")
            self.assertEqual(
                result["installation_source"],
                "installation-receipt",
            )
            self.assertEqual(receipt_path.read_bytes(), receipt_before)

    def test_install_publishes_receipt_only_after_remote_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            closure_path = root / "closure.json"
            state_store = StateStore(root / "state")
            instances = InstanceStore(state_store)
            target = endpoint(root)
            write_closure(closure_path)
            arguments = parse_arguments(
                build_parser(),
                [
                    "service",
                    "install",
                    str(FIXTURE),
                    "active-instance",
                    "--closure",
                    str(closure_path),
                    "--state-root",
                    str(state_store.root),
                ],
            )

            def push_success(plan, *, resolved_endpoint, instances):
                del instances
                self.assertEqual(resolved_endpoint, target)
                return {
                    "schema_version": "runpod.inference-service-push.v1",
                    "executed": True,
                    "provider_mutation": False,
                    "materialization_sha256": (
                        plan.materialization.materialization_sha256
                    ),
                    "instance": {
                        "name": target.instance_name,
                        "operation_id": target.operation_id,
                        "pod_id": target.pod_id,
                    },
                    "status": "installed",
                    "completed_steps": [
                        "prepare",
                        "copy-install-document",
                        "copy-payload",
                        "install",
                    ],
                }

            with (
                mock.patch(
                    "runpod_local.service_cli._existing_endpoint",
                    return_value=(state_store, instances, target),
                ),
                mock.patch(
                    "runpod_local.service_cli.push_service_materialization",
                    side_effect=RunpodLocalError(
                        "simulated remote failure",
                        code="service_deployment_step_failed",
                    ),
                ),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(RunpodLocalError),
            ):
                run_service_command(arguments)
            self.assertIsNone(
                ServiceInstallationStore(state_store).load(
                    instance_name=target.instance_name,
                    service_id="fixture-dense-text",
                    required=False,
                )
            )

            with (
                mock.patch(
                    "runpod_local.service_cli._existing_endpoint",
                    return_value=(state_store, instances, target),
                ),
                mock.patch.object(
                    instances,
                    "locked_active_lease",
                    return_value=contextlib.nullcontext({}),
                ),
                mock.patch(
                    "runpod_local.service_cli.push_service_materialization",
                    side_effect=push_success,
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                return_code = run_service_command(arguments)

            installed = ServiceInstallationStore(state_store).load(
                instance_name=target.instance_name,
                service_id="fixture-dense-text",
            )
            self.assertEqual(return_code, 0)
            self.assertIsNotNone(installed)

    def test_status_and_stop_survive_source_and_closure_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state_store = StateStore(root / "state")
            target = endpoint(root)
            installed = installed_fixture(state_store, target=target)

            with (
                mock.patch(
                    "runpod_local.service_cli._existing_endpoint",
                    return_value=(state_store, object(), target),
                ),
                mock.patch(
                    "runpod_local.service_cli.load_runtime",
                    side_effect=AssertionError(
                        "installed operation loaded current runtime source"
                    ),
                ),
                mock.patch(
                    "runpod_local.service_cli.build_service_materialization_plan",
                    side_effect=AssertionError(
                        "installed operation rebuilt current materialization"
                    ),
                ),
                mock.patch(
                    "runpod_local.service_cli.load_huggingface_closure",
                    side_effect=AssertionError(
                        "installed inspection loaded an obsolete closure"
                    ),
                ),
                mock.patch(
                    "runpod_local.service_cli.execute_service_runtime",
                    return_value={"executed": True},
                ) as execute,
            ):
                return_codes = []
                for action in ("status", "stop"):
                    arguments = parse_arguments(
                        build_parser(),
                        [
                            "service",
                            action,
                            str(FIXTURE),
                            "active-instance",
                            "--state-root",
                            str(state_store.root),
                        ],
                    )
                    return_codes.append(run_service_command(arguments))

            self.assertEqual(return_codes, [0, 0])
            self.assertEqual(execute.call_count, 2)
            for call in execute.call_args_list:
                runtime_plan = call.args[0]
                self.assertEqual(
                    runtime_plan.materialization_sha256,
                    installed.materialization.materialization_sha256,
                )
                self.assertEqual(
                    call.kwargs["resolved_endpoint"],
                    target,
                )

    def test_runtime_rejects_receipt_from_replaced_pod(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            closure_path = root / "closure.json"
            state_store = StateStore(root / "state")
            original = endpoint(root)
            replacement = endpoint(
                root,
                operation_id="22222222-2222-4222-8222-222222222222",
                pod_id="pod-2",
            )
            write_closure(closure_path)
            installed_fixture(state_store, target=original)
            arguments = parse_arguments(
                build_parser(),
                [
                    "service",
                    "status",
                    str(FIXTURE),
                    "active-instance",
                    "--closure",
                    str(closure_path),
                    "--state-root",
                    str(state_store.root),
                    "--json",
                ],
            )

            with (
                mock.patch(
                    "runpod_local.service_cli._existing_endpoint",
                    return_value=(state_store, object(), replacement),
                ),
                mock.patch(
                    "runpod_local.service_cli.execute_service_runtime",
                    side_effect=AssertionError("SSH execution attempted"),
                ),
                self.assertRaises(RunpodLocalError) as caught,
            ):
                run_service_command(arguments)

            self.assertEqual(
                caught.exception.code,
                "service_installation_instance_changed",
            )

    def test_launch_rejects_config_or_closure_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            closure_path = root / "changed-closure.json"
            state_store = StateStore(root / "state")
            target = endpoint(root)
            write_closure(closure_path, identity_digit="8")
            installed_fixture(state_store, target=target)
            arguments = parse_arguments(
                build_parser(),
                [
                    "service",
                    "start",
                    str(FIXTURE),
                    "active-instance",
                    "--closure",
                    str(closure_path),
                    "--state-root",
                    str(state_store.root),
                    "--cache-mode",
                    "ephemeral",
                    "--json",
                ],
            )

            with (
                mock.patch(
                    "runpod_local.service_cli._existing_endpoint",
                    return_value=(state_store, object(), target),
                ),
                mock.patch(
                    "runpod_local.service_cli.execute_service_runtime",
                    side_effect=AssertionError("SSH execution attempted"),
                ),
                self.assertRaises(RunpodLocalError) as caught,
            ):
                run_service_command(arguments)

            self.assertEqual(
                caught.exception.code,
                "service_installation_request_drift",
            )

    def test_exact_selector_is_nonmutating_for_plan_and_execution(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state_store = StateStore(root / "state")
            target = endpoint(root)
            materialized = materialized_fixture(state_store)
            store = ServiceInstallationStore(state_store)
            receipt_path = store.receipt_path(
                instance_name=target.instance_name,
                service_id="fixture-dense-text",
            )
            common = [
                "service",
                "status",
                str(FIXTURE),
                "active-instance",
                "--state-root",
                str(state_store.root),
                "--installed-materialization",
                materialized.materialization_sha256,
            ]
            json_arguments = parse_arguments(
                build_parser(),
                [*common, "--json"],
            )
            output = io.StringIO()

            with (
                mock.patch(
                    "runpod_local.service_cli._existing_endpoint",
                    return_value=(state_store, object(), target),
                ),
                mock.patch(
                    "runpod_local.service_cli.execute_service_runtime",
                    side_effect=AssertionError("SSH execution attempted"),
                ),
                contextlib.redirect_stdout(output),
            ):
                return_code = run_service_command(json_arguments)

            result = json.loads(output.getvalue())
            self.assertEqual(return_code, 0)
            self.assertEqual(
                result["installation_source"],
                "explicit-materialization",
            )
            self.assertIsNone(result["desired_service"])
            self.assertIsNone(result["desired_service_matches_installation"])
            self.assertFalse(receipt_path.exists())

            execute = mock.Mock(return_value={"executed": True})
            with (
                mock.patch(
                    "runpod_local.service_cli._existing_endpoint",
                    return_value=(state_store, object(), target),
                ),
                mock.patch(
                    "runpod_local.service_cli.execute_service_runtime",
                    execute,
                ),
            ):
                return_code = run_service_command(
                    parse_arguments(build_parser(), common)
                )

            self.assertEqual(return_code, 0)
            self.assertFalse(receipt_path.exists())
            execute.assert_called_once()
            self.assertEqual(
                execute.call_args.kwargs["resolved_endpoint"],
                target,
            )

    def test_endpoint_resolution_has_only_read_provider_authority(self):
        calls: list[tuple[str, str]] = []

        class ReadOnlyApi:
            def __init__(self, credential: ApiCredential) -> None:
                self.credential = credential

            def get_pod(self, pod_id: str) -> dict[str, str]:
                calls.append(("get_pod", pod_id))
                return {"id": pod_id}

        class FixtureCredentialStore:
            def __init__(self, path: pathlib.Path) -> None:
                self.path = path

            def load(self, *, required: bool) -> ApiCredential | None:
                self.assert_required(required)
                return ApiCredential("private-fixture", source="fixture")

            @staticmethod
            def assert_required(required: bool) -> None:
                if required is not True:
                    raise AssertionError("credential was not required")

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            target = SshEndpoint(
                instance_name="active-instance",
                operation_id="operation-1",
                pod_id="pod-1",
                host="203.0.113.42",
                port=22022,
                user="root",
                identity_file=root / "identity",
                known_hosts_file=root / "known-hosts",
                host_key_alias="runpod-pod-1",
            )

            def resolve(
                name: str,
                *,
                instances: object,
                api: ReadOnlyApi,
                state: object,
            ) -> SshEndpoint:
                del instances, state
                self.assertEqual(name, "active-instance")
                self.assertEqual(api.get_pod("pod-1"), {"id": "pod-1"})
                return target

            arguments = argparse.Namespace(
                service_action="install",
                name="active-instance",
                state_root=str(root / "state"),
                credentials_file=str(root / "credential"),
            )
            with (
                mock.patch(
                    "runpod_local.service_cli.CredentialStore",
                    FixtureCredentialStore,
                ),
                mock.patch(
                    "runpod_local.service_cli.RunpodApi",
                    ReadOnlyApi,
                ),
                mock.patch(
                    "runpod_local.service_cli.resolve_endpoint",
                    resolve,
                ),
            ):
                _, _, observed = _existing_endpoint(arguments)

        self.assertEqual(observed, target)
        self.assertEqual(calls, [("get_pod", "pod-1")])


if __name__ == "__main__":
    unittest.main()
