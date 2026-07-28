from __future__ import annotations

import dataclasses
import pathlib
import tempfile
import unittest
from typing import Any

from runpod_local.errors import RunpodLocalError
from runpod_local.remote import SshEndpoint, build_ssh_argv
from runpod_local.runtime_catalog import load_runtime
from runpod_local.service_definition import (
    InferenceServiceDefinition,
    load_inference_service,
    parse_inference_service_toml,
)
from runpod_local.service_execution import (
    CACHE_ACTIONS,
    RUNTIME_ACTIONS,
    build_service_runtime_plan,
    execute_service_runtime,
)
from runpod_local.service_huggingface import (
    HuggingFaceClosure,
    HuggingFaceClosureFile,
)
from runpod_local.service_materialization import (
    build_service_materialization_plan,
    materialize_service,
)
from runpod_service_test_fixture import SERVICE_FIXTURE

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = SERVICE_FIXTURE


def comparison_definition() -> InferenceServiceDefinition:
    payload = (
        FIXTURE.read_bytes()
        .replace(
            b'service_id = "fixture-dense-text"',
            b'service_id = "independent-chat"',
            1,
        )
        .replace(
            b'repository = "fixture-org/fixture-dense-text-7b"',
            b'repository = "another-org/independent-chat-13b"',
            1,
        )
        .replace(
            b'revision = "2222222222222222222222222222222222222222"',
            b'revision = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"',
            1,
        )
    )
    return parse_inference_service_toml(
        payload,
        source="<independent-service>",
    )


def closure_for(
    definition: InferenceServiceDefinition,
    *,
    identity_digit: str,
) -> HuggingFaceClosure:
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


def endpoint(root: pathlib.Path) -> SshEndpoint:
    return SshEndpoint(
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


class FixtureInstances:
    def __init__(self) -> None:
        self.sources: list[str] = []

    def check_active_lease(self, *_: object, **__: object) -> dict[str, Any]:
        return {"lease": {"idle_timeout_seconds": 1800}}

    def touch(self, *_: object, **kwargs: object) -> dict[str, Any]:
        source = kwargs.get("source")
        if isinstance(source, str):
            self.sources.append(source)
        return {"lease": {"idle_timeout_seconds": 1800}}


class FixtureProcess:
    def wait(self, timeout: int) -> int:
        del timeout
        return 0

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


class ProcessFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> FixtureProcess:
        self.calls.append((argv, kwargs))
        return FixtureProcess()


class ServiceExecutionTest(unittest.TestCase):
    def materialization_plan(
        self,
        *,
        definition: InferenceServiceDefinition,
        state_root: pathlib.Path,
        identity_digit: str,
    ):
        return build_service_materialization_plan(
            definition,
            source_root=ROOT,
            state_root=state_root,
            runtime=load_runtime("vllm-cu129-v0.25.1"),
            closure=closure_for(
                definition,
                identity_digit=identity_digit,
            ),
            remote_port=8123,
        )

    def test_two_models_share_exact_runtime_and_action_machinery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            first = self.materialization_plan(
                definition=load_inference_service(FIXTURE),
                state_root=root / "state",
                identity_digit="7",
            )
            second = self.materialization_plan(
                definition=comparison_definition(),
                state_root=root / "state",
                identity_digit="8",
            )
            target = endpoint(root)
            first_runtime = build_service_runtime_plan(
                first,
                endpoint=target,
                action="status",
            )
            second_runtime = build_service_runtime_plan(
                second,
                endpoint=target,
                action="status",
            )

        self.assertEqual(first_runtime.entrypoint, second_runtime.entrypoint)
        self.assertNotEqual(first_runtime.manifest, second_runtime.manifest)
        expected_remote = [
            first_runtime.entrypoint,
            "status",
            "--manifest",
            first_runtime.manifest,
        ]
        self.assertEqual(
            list(first_runtime.argv),
            build_ssh_argv(target, expected_remote),
        )
        for plan in (first_runtime, second_runtime):
            summary = plan.safe_summary()
            self.assertIs(summary["executed"], False)
            self.assertIs(summary["provider_mutation"], False)
            encoded = "\n".join(plan.argv)
            self.assertNotIn(".toml", encoded)
            self.assertNotIn("HF_TOKEN", encoded)
            self.assertNotIn("RUNPOD_API_KEY", encoded)
            self.assertRegex(
                plan.entrypoint,
                (
                    r"^/root/runpod-session/control/"
                    r"inference-service-runtime/[0-9a-f]{64}/"
                    r"bin/runpod-service-runtime$"
                ),
            )

    def test_cache_mode_contract_is_exact_for_every_action(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            materialization = self.materialization_plan(
                definition=load_inference_service(FIXTURE),
                state_root=root / "state",
                identity_digit="7",
            )
            target = endpoint(root)
            for action in RUNTIME_ACTIONS:
                with self.subTest(action=action, missing=True):
                    if action in CACHE_ACTIONS:
                        with self.assertRaises(RunpodLocalError) as caught:
                            build_service_runtime_plan(
                                materialization,
                                endpoint=target,
                                action=action,
                            )
                        self.assertEqual(
                            caught.exception.code,
                            "service_cache_mode_required",
                        )
                    else:
                        plan = build_service_runtime_plan(
                            materialization,
                            endpoint=target,
                            action=action,
                        )
                        self.assertIsNone(plan.cache_mode)
                with self.subTest(action=action, supplied=True):
                    if action in CACHE_ACTIONS:
                        plan = build_service_runtime_plan(
                            materialization,
                            endpoint=target,
                            action=action,
                            cache_mode="accepted",
                        )
                        self.assertEqual(
                            list(plan.argv),
                            build_ssh_argv(
                                target,
                                [
                                    plan.entrypoint,
                                    action,
                                    "--manifest",
                                    plan.manifest,
                                    "--cache-mode",
                                    "accepted",
                                ],
                            ),
                        )
                    else:
                        with self.assertRaises(RunpodLocalError) as caught:
                            build_service_runtime_plan(
                                materialization,
                                endpoint=target,
                                action=action,
                                cache_mode="accepted",
                            )
                        self.assertEqual(
                            caught.exception.code,
                            "unexpected_service_cache_mode",
                        )

    def test_execution_revalidates_plan_and_records_activity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            materialized = materialize_service(
                self.materialization_plan(
                    definition=load_inference_service(FIXTURE),
                    state_root=root / "state",
                    identity_digit="7",
                )
            )
            plan = build_service_runtime_plan(
                materialized,
                endpoint=endpoint(root),
                action="status",
            )
            instances = FixtureInstances()
            processes = ProcessFactory()

            result = execute_service_runtime(
                plan,
                resolved_endpoint=plan.endpoint,
                instances=instances,  # type: ignore[arg-type]
                popen_factory=processes,
            )

        self.assertIs(result["executed"], True)
        self.assertIs(result["provider_mutation"], False)
        self.assertEqual(
            instances.sources,
            ["service-runtime-status", "service-runtime-status"],
        )
        self.assertEqual(len(processes.calls), 1)
        argv, options = processes.calls[0]
        self.assertEqual(argv, list(plan.argv))
        self.assertIs(options["shell"], False)
        self.assertNotIn("RUNPOD_API_KEY", options["env"])
        self.assertNotIn("HF_TOKEN", options["env"])

    def test_execution_rejects_any_argv_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            materialized = materialize_service(
                self.materialization_plan(
                    definition=load_inference_service(FIXTURE),
                    state_root=root / "state",
                    identity_digit="7",
                )
            )
            plan = build_service_runtime_plan(
                materialized,
                endpoint=endpoint(root),
                action="status",
            )
            changed = dataclasses.replace(
                plan,
                argv=(*plan.argv, "--unexpected"),
            )
            processes = ProcessFactory()

            with self.assertRaises(RunpodLocalError) as caught:
                execute_service_runtime(
                    changed,
                    resolved_endpoint=plan.endpoint,
                    instances=FixtureInstances(),  # type: ignore[arg-type]
                    popen_factory=processes,
                )

        self.assertEqual(caught.exception.code, "invalid_service_runtime_plan")
        self.assertEqual(processes.calls, [])

    def test_execution_rejects_coordinated_endpoint_and_argv_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            materialized = materialize_service(
                self.materialization_plan(
                    definition=load_inference_service(FIXTURE),
                    state_root=root / "state",
                    identity_digit="7",
                )
            )
            resolved = endpoint(root)
            changed_endpoint = dataclasses.replace(
                resolved,
                host="198.51.100.9",
                port=22999,
                host_key_alias="runpod-other-pod",
            )
            changed = build_service_runtime_plan(
                materialized,
                endpoint=changed_endpoint,
                action="status",
            )
            processes = ProcessFactory()

            with self.assertRaises(RunpodLocalError) as caught:
                execute_service_runtime(
                    changed,
                    resolved_endpoint=resolved,
                    instances=FixtureInstances(),  # type: ignore[arg-type]
                    popen_factory=processes,
                )

        self.assertEqual(caught.exception.code, "invalid_service_runtime_plan")
        self.assertEqual(processes.calls, [])


if __name__ == "__main__":
    unittest.main()
