"""Model-service remote execution tests."""

from __future__ import annotations

import dataclasses
import io
import json
import pathlib
import subprocess
import tempfile
import unittest
from typing import Any
from unittest import mock

from model_lab.errors import ModelLabError
from runpod_local.remote import SshEndpoint, build_ssh_argv
from model_lab.runtime_catalog import load_runtime
from model_lab.service_definition import (
    ServiceDefinition,
    load_service,
    parse_service_toml,
)
from model_lab.service_execution import (
    CACHE_ACTIONS,
    MAX_RUNTIME_OUTPUT_BYTES,
    RUNTIME_ACTIONS,
    build_service_runtime_plan,
    execute_service_runtime,
    execute_service_runtime_capture,
)
from model_lab.service_huggingface import (
    HuggingFaceClosure,
    HuggingFaceClosureFile,
)
from model_lab.service_materialization import (
    build_service_materialization_plan,
    materialize_service,
)
from model_lab_service_test_fixture import SERVICE_FIXTURE

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = SERVICE_FIXTURE


def comparison_definition() -> ServiceDefinition:
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
    return parse_service_toml(
        payload,
        source="<independent-service>",
    )


def closure_for(
    definition: ServiceDefinition,
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


class CaptureProcess:
    def __init__(
        self,
        *,
        stdout: bytes,
        stderr: bytes = b"",
        return_code: int = 0,
    ) -> None:
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.return_code = return_code
        self.terminated = False
        self.killed = False

    def wait(self, timeout: int | None = None) -> int:
        del timeout
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class CaptureProcessFactory:
    def __init__(self, process: CaptureProcess) -> None:
        self.process = process
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> CaptureProcess:
        self.calls.append((argv, kwargs))
        return self.process


class ServiceExecutionTest(unittest.TestCase):
    def materialization_plan(
        self,
        *,
        definition: ServiceDefinition,
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

    def captured_plan(self, root: pathlib.Path):
        materialized = materialize_service(
            self.materialization_plan(
                definition=load_service(FIXTURE),
                state_root=root / "state",
                identity_digit="7",
            )
        )
        return build_service_runtime_plan(
            materialized,
            endpoint=endpoint(root),
            action="status",
        )

    def test_two_models_share_exact_runtime_and_action_machinery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            first = self.materialization_plan(
                definition=load_service(FIXTURE),
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
                    r"model-service-runtime/[0-9a-f]{64}/"
                    r"bin/model-lab-service-runtime$"
                ),
            )

    def test_cache_mode_contract_is_exact_for_every_action(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            materialization = self.materialization_plan(
                definition=load_service(FIXTURE),
                state_root=root / "state",
                identity_digit="7",
            )
            target = endpoint(root)
            for action in RUNTIME_ACTIONS:
                with self.subTest(action=action, missing=True):
                    if action in CACHE_ACTIONS:
                        with self.assertRaises(ModelLabError) as caught:
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
                        with self.assertRaises(ModelLabError) as caught:
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
                    definition=load_service(FIXTURE),
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
                    definition=load_service(FIXTURE),
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

            with self.assertRaises(ModelLabError) as caught:
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
                    definition=load_service(FIXTURE),
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

            with self.assertRaises(ModelLabError) as caught:
                execute_service_runtime(
                    changed,
                    resolved_endpoint=resolved,
                    instances=FixtureInstances(),  # type: ignore[arg-type]
                    popen_factory=processes,
                )

        self.assertEqual(caught.exception.code, "invalid_service_runtime_plan")
        self.assertEqual(processes.calls, [])

    def test_captured_execution_returns_one_bounded_json_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            plan = self.captured_plan(root)
            document = {
                "schema_version": "model-lab.service-status.v1",
                "service_id": "fixture-dense-text",
                "state": "ready",
            }
            process = CaptureProcess(
                stdout=json.dumps(document).encode("utf-8")
            )
            factory = CaptureProcessFactory(process)
            instances = FixtureInstances()

            result = execute_service_runtime_capture(
                plan,
                resolved_endpoint=plan.endpoint,
                instances=instances,  # type: ignore[arg-type]
                popen_factory=factory,
            )

        self.assertEqual(result, document)
        self.assertEqual(
            instances.sources,
            ["service-runtime-status", "service-runtime-status"],
        )
        self.assertEqual(len(factory.calls), 1)
        argv, options = factory.calls[0]
        self.assertEqual(argv, list(plan.argv))
        self.assertIs(options["shell"], False)
        self.assertIs(options["stdout"], subprocess.PIPE)
        self.assertIs(options["stderr"], subprocess.PIPE)
        self.assertNotIn("RUNPOD_API_KEY", options["env"])
        self.assertNotIn("HF_TOKEN", options["env"])

    def test_captured_execution_preserves_typed_remote_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            plan = self.captured_plan(root)
            remote_error = {
                "schema_version": "model-lab.service-error.v1",
                "error": "cache_identity_mismatch",
                "message": "the exact compiled cache does not match",
            }
            process = CaptureProcess(
                stdout=b"",
                stderr=json.dumps(remote_error).encode("utf-8"),
                return_code=17,
            )

            with self.assertRaises(ModelLabError) as caught:
                execute_service_runtime_capture(
                    plan,
                    resolved_endpoint=plan.endpoint,
                    instances=FixtureInstances(),  # type: ignore[arg-type]
                    popen_factory=CaptureProcessFactory(process),
                )

        self.assertEqual(caught.exception.code, "cache_identity_mismatch")
        self.assertEqual(
            str(caught.exception),
            "the exact compiled cache does not match",
        )

    def test_captured_execution_terminates_reaps_and_drains_at_deadline(self):
        class ObservedPipe(io.BytesIO):
            def __init__(self, payload: bytes) -> None:
                super().__init__(payload)
                self.drained = False

            def read(self, size: int = -1) -> bytes:
                payload = super().read(size)
                if not payload:
                    self.drained = True
                return payload

        class DeadlineProcess(CaptureProcess):
            def __init__(self) -> None:
                super().__init__(
                    stdout=b'{"service_id":"fixture-dense-text"}',
                    stderr=b"",
                )
                self.stdout = ObservedPipe(
                    b'{"service_id":"fixture-dense-text"}'
                )
                self.stderr = ObservedPipe(b"")
                self.reaped = False
                self.wait_timeouts: list[float | int | None] = []

            def wait(self, timeout: float | None = None) -> int:
                self.wait_timeouts.append(timeout)
                if not self.terminated:
                    raise subprocess.TimeoutExpired(["ssh"], timeout)
                self.reaped = True
                return -15

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            plan = self.captured_plan(root)
            process = DeadlineProcess()
            times = iter((10.0, 10.0, 12.0))

            with self.assertRaises(ModelLabError) as caught:
                execute_service_runtime_capture(
                    plan,
                    resolved_endpoint=plan.endpoint,
                    instances=FixtureInstances(),  # type: ignore[arg-type]
                    popen_factory=CaptureProcessFactory(process),
                    deadline=12.0,
                    monotonic=lambda: next(times),
                )

        self.assertEqual(caught.exception.code, "service_startup_timeout")
        self.assertTrue(process.terminated)
        self.assertTrue(process.reaped)
        self.assertFalse(process.killed)
        self.assertTrue(process.stdout.drained)
        self.assertTrue(process.stderr.drained)
        self.assertEqual(process.wait_timeouts, [2.0, 0.0])

    def test_expired_startup_deadline_does_not_start_remote_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            plan = self.captured_plan(root)
            factory = mock.Mock()

            with self.assertRaises(ModelLabError) as caught:
                execute_service_runtime_capture(
                    plan,
                    resolved_endpoint=plan.endpoint,
                    instances=FixtureInstances(),  # type: ignore[arg-type]
                    popen_factory=factory,
                    deadline=12.0,
                    monotonic=lambda: 12.0,
                )

        self.assertEqual(caught.exception.code, "service_startup_timeout")
        factory.assert_not_called()

    def test_captured_execution_rejects_output_over_one_mib(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            plan = self.captured_plan(root)
            process = CaptureProcess(
                stdout=b"x" * (MAX_RUNTIME_OUTPUT_BYTES + 1)
            )

            with self.assertRaises(ModelLabError) as caught:
                execute_service_runtime_capture(
                    plan,
                    resolved_endpoint=plan.endpoint,
                    instances=FixtureInstances(),  # type: ignore[arg-type]
                    popen_factory=CaptureProcessFactory(process),
                )

        self.assertEqual(
            caught.exception.code,
            "oversized_service_runtime_output",
        )

    def test_captured_execution_terminates_client_on_lease_drift(self):
        class DriftedInstances(FixtureInstances):
            def touch(self, *_: object, **__: object) -> dict[str, Any]:
                raise ModelLabError(
                    "active host lease changed",
                    code="instance_lease_mismatch",
                )

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            plan = self.captured_plan(root)
            process = CaptureProcess(
                stdout=b'{"service_id":"fixture-dense-text"}'
            )

            with self.assertRaises(ModelLabError) as caught:
                execute_service_runtime_capture(
                    plan,
                    resolved_endpoint=plan.endpoint,
                    instances=DriftedInstances(),  # type: ignore[arg-type]
                    popen_factory=CaptureProcessFactory(process),
                )

        self.assertEqual(caught.exception.code, "instance_lease_mismatch")
        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)


if __name__ == "__main__":
    unittest.main()
