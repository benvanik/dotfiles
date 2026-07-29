"""Model-service SSH installation tests."""

from __future__ import annotations

import pathlib
import tempfile
import unittest
from typing import Any
from unittest import mock

from model_lab.errors import ModelLabError
from runpod_local.remote import SshEndpoint
from model_lab.runtime_catalog import load_runtime
from model_lab.service_definition import (
    ServiceDefinition,
    load_service,
)
from model_lab.service_deployment import (
    build_service_push_plan,
    push_service_materialization,
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
INSTALLER = ROOT / "model-lab/service_deploy/install-service.py"


def closure_for(definition: ServiceDefinition) -> HuggingFaceClosure:
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
                identity_digest="5" * 64,
            ),
        ),
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
    def __init__(
        self,
        *,
        return_code: int,
        stdin: object,
        streamed_payloads: list[bytes],
    ) -> None:
        self.return_code = return_code
        if stdin is not None:
            streamed_payloads.append(stdin.read())

    def wait(self, timeout: int) -> int:
        del timeout
        return self.return_code

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


class ProcessFactory:
    def __init__(self, return_codes: list[int]) -> None:
        self.return_codes = iter(return_codes)
        self.argv: list[list[str]] = []
        self.streamed_payloads: list[bytes] = []

    def __call__(self, argv: list[str], **kwargs: object) -> FixtureProcess:
        self.argv.append(argv)
        return FixtureProcess(
            return_code=next(self.return_codes),
            stdin=kwargs.get("stdin"),
            streamed_payloads=self.streamed_payloads,
        )


class ServiceDeploymentTest(unittest.TestCase):
    def fixture(self, root: pathlib.Path):
        definition = load_service(FIXTURE)
        materialization = materialize_service(
            build_service_materialization_plan(
                definition,
                source_root=ROOT,
                state_root=root / "state",
                runtime=load_runtime("vllm-cu129-v0.25.1"),
                closure=closure_for(definition),
            )
        )
        endpoint = SshEndpoint(
            instance_name="fixture-instance",
            operation_id="operation-1",
            pod_id="pod-1",
            host="203.0.113.42",
            port=22022,
            user="root",
            identity_file=root / "identity",
            known_hosts_file=root / "state/ssh/known-hosts/pod-1",
            host_key_alias="runpod-pod-1",
        )
        return materialization, endpoint

    def test_push_plan_is_four_shell_free_steps_on_existing_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            materialization, endpoint = self.fixture(pathlib.Path(directory))
            plan = build_service_push_plan(
                materialization,
                endpoint=endpoint,
                installer_path=INSTALLER,
            )

        summary = plan.safe_summary()
        self.assertIs(summary["executed"], False)
        self.assertIs(summary["provider_mutation"], False)
        self.assertEqual(
            [step["name"] for step in summary["steps"]],
            [
                "prepare",
                "copy-install-document",
                "copy-payload",
                "install",
            ],
        )
        self.assertEqual(
            summary["incoming_path"],
            (
                "/root/runpod-session/incoming/service-materializations/"
                f"{materialization.materialization_sha256}/"
                f"{summary['transfer_id']}"
            ),
        )
        encoded = "\n".join(
            argument for step in summary["steps"] for argument in step["argv"]
        )
        self.assertNotIn("runpodctl", encoded)
        self.assertNotIn("service.toml", encoded)
        self.assertNotIn("HF_TOKEN", encoded)
        self.assertNotIn("api-key", encoded)
        self.assertIn("/usr/bin/python3.12", encoded)

    def test_push_streams_bound_installer_and_tracks_each_activity(self):
        with tempfile.TemporaryDirectory() as directory:
            materialization, endpoint = self.fixture(pathlib.Path(directory))
            plan = build_service_push_plan(
                materialization,
                endpoint=endpoint,
                installer_path=INSTALLER,
            )
            instances = FixtureInstances()
            processes = ProcessFactory([0, 0, 0, 0])

            result = push_service_materialization(
                plan,
                resolved_endpoint=endpoint,
                instances=instances,  # type: ignore[arg-type]
                popen_factory=processes,
            )

            self.assertEqual(result["status"], "installed")
            self.assertEqual(
                result["completed_steps"],
                [
                    "prepare",
                    "copy-install-document",
                    "copy-payload",
                    "install",
                ],
            )
            self.assertEqual(len(processes.argv), 4)
            self.assertEqual(
                processes.streamed_payloads,
                [INSTALLER.read_bytes(), INSTALLER.read_bytes()],
            )
            self.assertEqual(
                instances.sources,
                [
                    "service-push-prepare",
                    "service-push-prepare",
                    "service-push-copy-install-document",
                    "service-push-copy-install-document",
                    "service-push-copy-payload",
                    "service-push-copy-payload",
                    "service-push-install",
                    "service-push-install",
                ],
            )

    def test_push_threads_one_absolute_deadline_through_every_step(self):
        with tempfile.TemporaryDirectory() as directory:
            materialization, endpoint = self.fixture(pathlib.Path(directory))
            plan = build_service_push_plan(
                materialization,
                endpoint=endpoint,
                installer_path=INSTALLER,
            )
            monotonic = mock.Mock(return_value=10.0)
            calls: list[dict[str, Any]] = []

            def capture_run(*_args: object, **kwargs: Any) -> int:
                calls.append(kwargs)
                return 0

            with mock.patch(
                "model_lab.service_deployment.run_with_activity",
                side_effect=capture_run,
            ):
                result = push_service_materialization(
                    plan,
                    resolved_endpoint=endpoint,
                    instances=FixtureInstances(),  # type: ignore[arg-type]
                    deadline=300.0,
                    monotonic=monotonic,
                )

        self.assertEqual(result["status"], "installed")
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(call["deadline"] == 300.0 for call in calls))
        self.assertTrue(
            all(call["monotonic"] is monotonic for call in calls)
        )

    def test_failed_copy_stops_before_later_remote_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            materialization, endpoint = self.fixture(pathlib.Path(directory))
            plan = build_service_push_plan(
                materialization,
                endpoint=endpoint,
                installer_path=INSTALLER,
            )
            processes = ProcessFactory([0, 9])

            with self.assertRaises(ModelLabError) as caught:
                push_service_materialization(
                    plan,
                    resolved_endpoint=endpoint,
                    instances=FixtureInstances(),  # type: ignore[arg-type]
                    popen_factory=processes,
                )

        self.assertEqual(
            caught.exception.code,
            "service_deployment_step_failed",
        )
        self.assertEqual(len(processes.argv), 2)

    def test_execution_rebuilds_and_rejects_a_mutated_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            materialization, endpoint = self.fixture(pathlib.Path(directory))
            plan = build_service_push_plan(
                materialization,
                endpoint=endpoint,
                installer_path=INSTALLER,
            )
            plan.steps[0]["argv"].append("--unexpected")
            processes = ProcessFactory([])

            with self.assertRaises(ModelLabError) as caught:
                push_service_materialization(
                    plan,
                    resolved_endpoint=endpoint,
                    instances=FixtureInstances(),  # type: ignore[arg-type]
                    popen_factory=processes,
                )

        self.assertEqual(caught.exception.code, "invalid_service_push_plan")
        self.assertEqual(processes.argv, [])

    def test_execution_rejects_coordinated_endpoint_and_argv_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            materialization, endpoint = self.fixture(pathlib.Path(directory))
            attacker_endpoint = SshEndpoint(
                instance_name=endpoint.instance_name,
                operation_id=endpoint.operation_id,
                pod_id=endpoint.pod_id,
                host="198.51.100.17",
                port=endpoint.port,
                user=endpoint.user,
                identity_file=endpoint.identity_file,
                known_hosts_file=endpoint.known_hosts_file,
                host_key_alias=endpoint.host_key_alias,
            )
            attacker_plan = build_service_push_plan(
                materialization,
                endpoint=attacker_endpoint,
                installer_path=INSTALLER,
            )
            processes = ProcessFactory([])

            with self.assertRaises(ModelLabError) as caught:
                push_service_materialization(
                    attacker_plan,
                    resolved_endpoint=endpoint,
                    instances=FixtureInstances(),  # type: ignore[arg-type]
                    popen_factory=processes,
                )

        self.assertEqual(caught.exception.code, "invalid_service_push_plan")
        self.assertEqual(processes.argv, [])


if __name__ == "__main__":
    unittest.main()
