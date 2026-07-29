from __future__ import annotations

import io
import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from model_lab.cli import main
from model_lab.dependencies import Dependencies
from model_session.attachment import ServiceWorkload
from model_session.service_endpoint import service_workload_identity


class FakeSupervisor:
    def __init__(self) -> None:
        self.acquisitions = []
        self.down_requests = []

    def acquire_pi(
        self,
        *,
        profile_id,
        host_name=None,
        stop_on_release=False,
    ):
        self.acquisitions.append(
            (profile_id, host_name, stop_on_release)
        )
        return SimpleNamespace(
            pending=SimpleNamespace(
                profile_id=profile_id,
                service_id="fixture-chat",
                workload_sha256="a" * 64,
                deployment_id="deployment-one",
                use_lease_id="use-one",
            ),
            close=lambda: None,
        )

    def request(self, operation, fields):
        if operation == "down":
            self.down_requests.append((fields["service_id"], fields["now"]))
            return {
                "deployment": {
                    "phase": "released" if fields["now"] else "idle",
                    "idle_deadline": (
                        None
                        if fields["now"]
                        else "2026-07-28T12:30:00Z"
                    ),
                }
            }
        return {
            "deployment": {
                "host_name": "host-one",
                "idle_deadline": "2026-07-28T12:30:00Z",
            },
            "endpoint": {},
        }


class ServiceFixture:
    service_id = "fixture-chat"
    workload_sha256 = "a" * 64
    service_sha256 = "b" * 64
    endpoint = SimpleNamespace(input_modalities=("text", "image"))

    def normalized_plan(self):
        return {}

    def service_workload(self):
        return ServiceWorkload(
            repository="fixture/model",
            revision="c" * 40,
            provider="runpod-vllm",
            model_id=self.service_id,
            context_tokens=32768,
            max_output_tokens=4096,
            weight_format="native",
            kv_cache_dtype="bf16",
            runtime_compatibility="fixture-runtime",
            reasoning=False,
        )


class RouteFixture:
    profile_id = "chat"
    project_id = "model-playground"
    service_id = "fixture-chat"
    required_input_modalities = ("text", "image")


class ModelLabCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name) / "model-lab"
        self.root.mkdir(mode=0o700)
        self.route = RouteFixture()
        self.service = ServiceFixture()
        self.supervisor = FakeSupervisor()
        self.output = io.StringIO()
        self.error = io.StringIO()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, arguments, runner):
        dependencies = Dependencies(
            supervisor=self.supervisor,
            run_model_session=runner,
        )
        with (
            mock.patch(
                "model_lab.cli.load_lab_configuration",
                return_value=object(),
            ),
            mock.patch(
                "model_lab.cli.load_profile_route",
                return_value=self.route,
            ),
            mock.patch(
                "model_lab.cli.load_service_id",
                return_value=self.service,
            ),
            mock.patch(
                "model_lab.cli.runtime_root",
                return_value=pathlib.Path(self.temporary.name) / "runtime",
            ),
        ):
            return main(
                ["--root", str(self.root), *arguments],
                dependencies=dependencies,
                output=self.output,
                error=self.error,
            )

    def test_pi_is_one_command_and_supervisor_owns_final_release(self) -> None:
        invocations = []

        def runner(profile_root, arguments, channel):
            invocations.append((profile_root, list(arguments), channel))
            return 17

        result = self._run(["pi", "chat", "--host", "dev96"], runner)

        self.assertEqual(result, 17)
        self.assertEqual(invocations[0][0:2], (self.root / "profiles" / "chat", []))
        self.assertEqual(invocations[0][2].pending.profile_id, "chat")
        self.assertEqual(invocations[0][2].pending.use_lease_id, "use-one")
        self.assertEqual(
            self.supervisor.acquisitions,
            [("chat", "dev96", False)],
        )
        progress = self.error.getvalue()
        self.assertIn(
            "chat: ensuring fixture-chat endpoint",
            progress,
        )
        self.assertIn("chat: endpoint ready", progress)

    def test_pi_resume_passes_now_to_connection_bound_release(self) -> None:
        def runner(profile_root, arguments, channel):
            self.assertEqual(arguments, ["resume", "session-one"])
            self.assertEqual(channel.pending.deployment_id, "deployment-one")
            return 19

        result = self._run(
            ["pi", "chat", "resume", "session-one", "--now"],
            runner,
        )

        self.assertEqual(result, 19)
        self.assertEqual(
            self.supervisor.acquisitions,
            [("chat", None, True)],
        )

    def test_down_defaults_to_model_idle_and_now_is_explicit(self) -> None:
        result = self._run(["down", "fixture-chat"], lambda *_: 0)
        self.assertEqual(result, 0)
        self.assertEqual(
            self.supervisor.down_requests,
            [("fixture-chat", False)],
        )

        self.output = io.StringIO()
        result = self._run(
            ["down", "fixture-chat", "--now"],
            lambda *_: 0,
        )
        self.assertEqual(result, 0)
        self.assertEqual(
            self.supervisor.down_requests,
            [("fixture-chat", False), ("fixture-chat", True)],
        )

    def test_agents_md_requires_no_supervisor_or_authored_state(self) -> None:
        result = main(
            ["--agents-md"],
            dependency_factory=lambda **_: self.fail("factory must not run"),
            output=self.output,
            error=self.error,
        )

        self.assertEqual(result, 0)
        self.assertIn("model-lab pi PROFILE", self.output.getvalue())
        self.assertEqual(self.error.getvalue(), "")

    def test_model_inspection_is_model_owned_and_uses_private_token(self) -> None:
        report = {
            "schema_version": "model-lab.model-estimate.v1",
            "repository": {
                "id": "fixture/model",
                "resolved_revision": "c" * 40,
            },
        }
        inspector = mock.Mock()
        inspector.inspect.return_value = report
        with (
            mock.patch(
                "model_lab.cli.configured_huggingface_token",
                return_value="private-token",
            ),
            mock.patch("model_lab.cli.HuggingFaceClient") as client_class,
            mock.patch(
                "model_lab.cli.ModelInspector",
                return_value=inspector,
            ),
        ):
            result = main(
                [
                    "--state-root",
                    str(self.root / "state"),
                    "model",
                    "fixture/model",
                    "--revision",
                    "candidate",
                    "--context",
                    "4096",
                    "--sequences",
                    "2",
                    "--kv-dtype",
                    "fp8",
                    "--weight-format",
                    "bf16",
                    "--json",
                ],
                output=self.output,
                error=self.error,
            )

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(self.output.getvalue()), report)
        self.assertEqual(client_class.call_args.kwargs["token"], "private-token")
        self.assertEqual(
            client_class.call_args.kwargs["cache"].root,
            self.root / "state" / "cache" / "huggingface-metadata",
        )
        inspector.inspect.assert_called_once_with(
            "fixture/model",
            revision="candidate",
            index_file=None,
            context_tokens=4096,
            sequences=2,
            kv_dtype="fp8",
            weight_format="bf16",
        )

    def test_place_lists_gpus_without_huggingface_access(self) -> None:
        catalog = {
            "schema_version": "runpod.hardware.v1",
            "gpus": [
                {
                    "id": "Fixture GPU",
                    "display_name": "Fixture",
                    "provider_memory_gb": 96,
                    "aliases": ["fixture"],
                }
            ],
        }
        with (
            mock.patch(
                "model_lab.cli.load_hardware_catalog",
                return_value=catalog,
            ),
            mock.patch("model_lab.cli._inspect_model") as inspect_model,
        ):
            result = main(
                ["place", "--list-gpus", "--json"],
                output=self.output,
                error=self.error,
            )

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(self.output.getvalue()), catalog)
        inspect_model.assert_not_called()

    def test_place_forwards_explicit_policy_to_model_owned_planner(self) -> None:
        model = {"schema_version": "model-lab.model-estimate.v1"}
        placement = {
            "schema_version": "model-lab.placement.v1",
            "placements": [],
        }
        catalog = {"schema_version": "fixture", "gpus": []}
        with (
            mock.patch(
                "model_lab.cli.load_hardware_catalog",
                return_value=catalog,
            ),
            mock.patch(
                "model_lab.cli._inspect_model",
                return_value=model,
            ),
            mock.patch(
                "model_lab.cli.place_model",
                return_value=placement,
            ) as place,
        ):
            result = main(
                [
                    "place",
                    "fixture/model",
                    "--gpu",
                    "pro6000",
                    "--gpu-count",
                    "2",
                    "--gpu-memory-utilization",
                    "0.8",
                    "--weight-slack",
                    "1.1",
                    "--framework-reserve-gib",
                    "6",
                    "--json",
                ],
                output=self.output,
                error=self.error,
            )

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(self.output.getvalue()), placement)
        place.assert_called_once_with(
            model,
            catalog=catalog,
            requested_gpus=["pro6000"],
            gpu_count=2,
            gpu_memory_utilization=0.8,
            weight_slack=1.1,
            framework_reserve_gib=6.0,
        )

    def test_hf_auth_keeps_provider_and_token_paths_explicit(self) -> None:
        response = {
            "schema_version": "model-lab.hf-auth.v1",
            "host_name": "compiler",
            "configured": True,
            "changed": True,
        }
        with mock.patch(
            "model_lab.cli.manage_huggingface_credential",
            return_value=response,
        ) as manage:
            result = main(
                [
                    "hf-auth",
                    "push",
                    "compiler",
                    "--token-file",
                    "token",
                    "--runpod-state-root",
                    "runpod-state",
                    "--credentials-file",
                    "runpod-key",
                    "--json",
                ],
                output=self.output,
                error=self.error,
            )

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(self.output.getvalue()), response)
        manage.assert_called_once_with(
            "push",
            "compiler",
            token_file=pathlib.Path("token").absolute(),
            runpod_state_root=pathlib.Path("runpod-state").absolute(),
            credentials_path=pathlib.Path("runpod-key").absolute(),
        )

    def test_migrate_builds_exact_binding_without_provider_access(self) -> None:
        migrated_run = mock.Mock()
        migrated_run.normalized.return_value = {"session_id": "session-one"}
        migration = SimpleNamespace(
            migration_id="migration-one",
            profile_root=self.root / "profiles" / "migrated",
            state_root=self.root,
            profile_id="migrated",
            project_id="project-one",
            service_id=self.service.service_id,
            workload_sha256="d" * 64,
            runs=(migrated_run,),
            receipt_path=self.root / "migrations" / "migration-one.json",
        )
        source = self.root / "legacy-profile"
        with mock.patch(
            "model_lab.cli.migrate_legacy_profile",
            return_value=migration,
        ) as migrate:
            result = self._run(
                [
                    "migrate",
                    str(source),
                    "--service",
                    self.service.service_id,
                    "--target-profile-id",
                    "migrated",
                    "--target-project-id",
                    "project-one",
                    "--session",
                    "session-one",
                    "--json",
                ],
                lambda *_: self.fail("model-session runner must not run"),
            )

        self.assertEqual(result, 0)
        payload = json.loads(self.output.getvalue())
        self.assertEqual(payload["schema"], "model-lab.migration.v1")
        self.assertEqual(payload["runs"], [{"session_id": "session-one"}])
        call = migrate.call_args
        self.assertEqual(call.args, (source, self.root))
        self.assertEqual(call.kwargs["target_profile_id"], "migrated")
        self.assertEqual(call.kwargs["target_project_id"], "project-one")
        self.assertEqual(call.kwargs["session_ids"], ["session-one"])
        binding = call.kwargs["service_binding"]
        workload = self.service.service_workload()
        self.assertEqual(binding.service_id, self.service.service_id)
        self.assertEqual(binding.service_sha256, self.service.service_sha256)
        self.assertEqual(binding.workload, workload)
        self.assertEqual(
            binding.workload_sha256,
            service_workload_identity(workload),
        )
        self.assertEqual(binding.input_modalities, ("text", "image"))


if __name__ == "__main__":
    unittest.main()
