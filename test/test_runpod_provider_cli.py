from __future__ import annotations

import argparse
import contextlib
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from runpod_local.cli import build_parser
from runpod_local.errors import RunpodLocalError
from runpod_local.host_template import build_generic_host_template
from runpod_local.profile import (
    DEFAULT_PROFILE_HARD_TTL,
    ProfileStore,
)
from runpod_local.provider_cli import (
    _run_profile,
    _run_template,
    _run_volume,
    created_volume_violations,
    template_lock_scope,
    volume_lock_scope,
)
from runpod_local.template import (
    build_private_template_contract,
    docker_arguments_summary,
)

SSH_PUBLIC_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f "
    "fixture@example"
)
IMAGE = "vllm/vllm-openai@sha256:" + "1" * 64
GPU_ID = "NVIDIA RTX PRO 6000 Blackwell Server Edition"
GPU_MEMORY_GB = 96.0
TEMPLATE_NAME = "generic-ssh"


class FakeVolumeApi:
    def __init__(self, *, volumes=None, created=None):
        self.volumes = list(volumes or [])
        self.created = created or {
            "id": "volume123",
            "name": "model-cache",
            "size_gb": 250,
            "data_center_id": "EUR-IS-2",
        }
        self.create_calls = []

    def stock(self, **_kwargs):
        return {
            "data_centers": [
                {
                    "data_center_id": "EUR-IS-2",
                    "name": "Iceland",
                    "location": "Iceland",
                    "gpu_availability": [],
                }
            ]
        }

    def list_network_volumes(self):
        return list(self.volumes)

    def create_network_volume(self, **request):
        self.create_calls.append(request)
        return dict(self.created)


class FakeTemplateApi:
    def __init__(self, *, templates=None, created=None, gpus=None):
        self.templates = list(templates or [])
        self.created = created
        self.create_calls = []
        self.gpus = (
            [
                {
                    "gpu_id": GPU_ID,
                    "memory_gb": GPU_MEMORY_GB,
                }
            ]
            if gpus is None
            else list(gpus)
        )

    def list_templates(self):
        return [dict(template) for template in self.templates]

    def get_template(self, template_id):
        matches = [
            template
            for template in self.templates
            if template["id"] == template_id
        ]
        if len(matches) != 1:
            raise AssertionError("fixture template identity is not unique")
        return dict(matches[0])

    def create_template(self, contract):
        self.create_calls.append(contract)
        if self.created is not None:
            return dict(self.created)
        return {**contract, "id": "template123"}

    def stock(self, **_kwargs):
        return {"gpus": list(self.gpus)}


def volume_args(root: pathlib.Path, *, execute: bool) -> argparse.Namespace:
    return argparse.Namespace(
        volume_action="create",
        volume_id=None,
        name="model-cache",
        size_gb=250,
        data_center="EUR-IS-2",
        execute=execute,
        state_root=str(root),
        credentials_file=None,
        json=True,
        agents_md=False,
        command="volume",
    )


class ProviderCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.temporary_root = pathlib.Path(self.temporary.name)
        self.root = self.temporary_root / "state"
        self.authored_root = self.temporary_root / "runpod"

    def run_volume(self, api: FakeVolumeApi, *, execute: bool):
        output = io.StringIO()
        with mock.patch(
            "runpod_local.provider_cli._api", return_value=api
        ), contextlib.redirect_stdout(output):
            status = _run_volume(volume_args(self.root, execute=execute))
        return status, output.getvalue()

    def template_contract(self, **overrides):
        contract = build_generic_host_template(
            name=TEMPLATE_NAME,
            image=IMAGE,
            container_disk_gb=50,
            template_id="template123",
        )
        contract.update(overrides)
        return contract

    def run_template(self, api: FakeTemplateApi, *, execute: bool):
        arguments = build_parser().parse_args(
            [
                "template",
                "create",
                TEMPLATE_NAME,
                "--image",
                IMAGE,
                "--state-root",
                str(self.root),
                "--json",
                *(["--execute"] if execute else []),
            ]
        )
        output = io.StringIO()
        with mock.patch(
            "runpod_local.provider_cli._api", return_value=api
        ), contextlib.redirect_stdout(output):
            status = _run_template(arguments)
        return status, json.loads(output.getvalue())

    def test_parser_exposes_the_lock_state_root(self):
        args = build_parser().parse_args(
            [
                "volume",
                "create",
                "model-cache",
                "--size-gb",
                "250",
                "--data-center",
                "EUR-IS-2",
                "--state-root",
                str(self.root),
            ]
        )

        self.assertEqual(args.state_root, str(self.root))

    def test_profile_default_hard_ttl_is_bounded_to_thirty_minutes(self):
        args = build_parser().parse_args(
            [
                "profile",
                "create",
                "bounded-default",
                "--template-id",
                "template123",
                "--ephemeral",
                "--gpu",
                GPU_ID,
                "--max-hourly",
                "2.25",
            ]
        )

        self.assertEqual(DEFAULT_PROFILE_HARD_TTL, "30m")
        self.assertEqual(args.ttl, DEFAULT_PROFILE_HARD_TTL)

    def test_profile_create_stores_the_thirty_minute_default(self):
        args = build_parser().parse_args(
            [
                "profile",
                "create",
                "bounded-default",
                "--template-id",
                "template123",
                "--ephemeral",
                "--gpu",
                GPU_ID,
                "--max-hourly",
                "2.25",
                "--state-root",
                str(self.root),
                "--runpod-root",
                str(self.authored_root),
                "--json",
            ]
        )
        output = io.StringIO()
        api = FakeTemplateApi(templates=[self.template_contract()])
        with (
            mock.patch(
                "runpod_local.provider_cli._api",
                return_value=api,
            ),
            mock.patch(
                "runpod_local.provider_cli.load_ssh_public_key_file",
                return_value=(
                    pathlib.Path("/fixture/id_ed25519_runpod.pub"),
                    SSH_PUBLIC_KEY,
                ),
            ),
            mock.patch(
                "runpod_local.provider_cli.validate_ssh_identity_file"
            ),
            mock.patch(
                "runpod_local.provider_cli.validate_ssh_key_pair"
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(_run_profile(args), 0)

        emitted = json.loads(output.getvalue())
        stored = ProfileStore(self.authored_root).load("bounded-default")
        self.assertEqual(emitted["lease"]["default_ttl_seconds"], 1800)
        self.assertEqual(stored["lease"]["default_ttl_seconds"], 1800)
        self.assertEqual(
            stored["pod"]["gpu_memory_gb_by_type"],
            {GPU_ID: GPU_MEMORY_GB},
        )
        self.assertFalse((self.root / "profiles").exists())

    def test_profile_rejects_unverified_provider_gpu_capacity(self):
        args = build_parser().parse_args(
            [
                "profile",
                "create",
                "capacity-unknown",
                "--template-id",
                "template123",
                "--ephemeral",
                "--gpu",
                GPU_ID,
                "--max-hourly",
                "2.25",
                "--state-root",
                str(self.root),
                "--runpod-root",
                str(self.authored_root),
            ]
        )
        invalid_stock = (
            [{"gpu_id": GPU_ID, "memory_gb": None}],
            [
                {"gpu_id": GPU_ID, "memory_gb": GPU_MEMORY_GB},
                {"gpu_id": GPU_ID, "memory_gb": GPU_MEMORY_GB},
            ],
        )
        for gpus in invalid_stock:
            with (
                self.subTest(gpus=gpus),
                mock.patch(
                    "runpod_local.provider_cli._api",
                    return_value=FakeTemplateApi(
                        templates=[self.template_contract()],
                        gpus=gpus,
                    ),
                ),
                mock.patch(
                    "runpod_local.provider_cli.load_ssh_public_key_file",
                    return_value=(
                        pathlib.Path("/fixture/id_ed25519_runpod.pub"),
                        SSH_PUBLIC_KEY,
                    ),
                ),
                mock.patch(
                    "runpod_local.provider_cli.validate_ssh_identity_file"
                ),
                mock.patch(
                    "runpod_local.provider_cli.validate_ssh_key_pair"
                ),
                self.assertRaises(RunpodLocalError) as caught,
            ):
                _run_profile(args)
            self.assertEqual(caught.exception.code, "invalid_profile_gpu")
        self.assertFalse(self.authored_root.exists())

    def test_template_create_reconciles_exact_private_overlay(self):
        api = FakeTemplateApi()

        status, result = self.run_template(api, execute=True)

        self.assertEqual(status, 0)
        self.assertEqual(result["verification"]["status"], "verified")
        self.assertEqual(len(api.create_calls), 1)
        request = api.create_calls[0]
        expected = build_generic_host_template(
            name=TEMPLATE_NAME,
            image=IMAGE,
            container_disk_gb=50,
        )
        self.assertEqual(request, expected)
        self.assertEqual(request["docker_entrypoint"], ["/bin/bash", "-c"])
        self.assertEqual(request["volume_in_gb"], 0)
        self.assertFalse(request["is_public"])
        self.assertFalse(request["is_serverless"])
        self.assertNotIn(request["docker_start_cmd"][0], json.dumps(result))
        self.assertEqual(
            result["request"]["docker_start_cmd"]["sha256"],
            docker_arguments_summary(request["docker_start_cmd"])["sha256"],
        )
        lock = (
            self.root
            / "locks"
            / f"{template_lock_scope(TEMPLATE_NAME)}.lock"
        )
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)

    def test_template_create_reuses_exact_name_without_post(self):
        api = FakeTemplateApi(templates=[self.template_contract()])

        status, result = self.run_template(api, execute=True)

        self.assertEqual(status, 0)
        self.assertTrue(result["reconciled_existing"])
        self.assertEqual(api.create_calls, [])

    def test_template_create_rejects_same_name_ssh_overlay_drift(self):
        drifted = self.template_contract()
        drifted["docker_start_cmd"] = ["different\n"]
        api = FakeTemplateApi(templates=[drifted])

        with self.assertRaises(RunpodLocalError) as caught:
            self.run_template(api, execute=False)

        self.assertEqual(caught.exception.code, "template_name_conflict")

    def test_template_create_has_no_arbitrary_command_surface(self):
        for forbidden_arguments in (
            ["--entrypoint-json", '["/bin/sh","-c"]'],
            ["--start-cmd-file", "/tmp/operator-command"],
        ):
            with self.subTest(
                arguments=forbidden_arguments
            ), self.assertRaises(SystemExit):
                build_parser().parse_args(
                    [
                        "template",
                        "create",
                        TEMPLATE_NAME,
                        "--image",
                        IMAGE,
                        *forbidden_arguments,
                    ]
                )

    def test_mutable_template_image_fails_before_provider_access(self):
        arguments = build_parser().parse_args(
            [
                "template",
                "create",
                TEMPLATE_NAME,
                "--image",
                "vllm/vllm-openai:latest",
            ]
        )
        with mock.patch(
            "runpod_local.provider_cli._api"
        ) as provider, self.assertRaises(RunpodLocalError) as caught:
            _run_template(arguments)
        self.assertEqual(caught.exception.code, "invalid_image_digest")
        provider.assert_not_called()

    def test_template_list_and_get_redact_provider_docker_arguments(self):
        secret = "PROVIDER_SECRET=must-not-escape\n"
        contract = self.template_contract(docker_start_cmd=[secret])
        api = FakeTemplateApi(templates=[contract])
        for action in ("list", "get"):
            with self.subTest(action=action):
                arguments = build_parser().parse_args(
                    [
                        "template",
                        action,
                        *(["template123"] if action == "get" else []),
                        "--json",
                    ]
                )
                output = io.StringIO()
                with mock.patch(
                    "runpod_local.provider_cli._api",
                    return_value=api,
                ), contextlib.redirect_stdout(output):
                    self.assertEqual(_run_template(arguments), 0)
                emitted = output.getvalue()
                self.assertNotIn("must-not-escape", emitted)
                self.assertIn('"argument_count": 1', emitted)
                self.assertIn('"sha256":', emitted)

    def test_template_profile_snapshots_provider_host_contract_and_capacity(
        self,
    ):
        contract = self.template_contract()
        api = FakeTemplateApi(templates=[contract])
        arguments = build_parser().parse_args(
            [
                "profile",
                "create",
                "template-host",
                "--template-id",
                "template123",
                "--network-volume-id",
                "volume123",
                "--gpu",
                GPU_ID,
                "--max-hourly",
                "2.25",
                "--state-root",
                str(self.root),
                "--runpod-root",
                str(self.authored_root),
                "--json",
            ]
        )
        output = io.StringIO()
        with mock.patch(
            "runpod_local.provider_cli._api", return_value=api
        ), mock.patch(
            "runpod_local.provider_cli.load_ssh_public_key_file",
            return_value=(
                pathlib.Path("/fixture/id_ed25519_runpod.pub"),
                SSH_PUBLIC_KEY,
            ),
        ), mock.patch(
            "runpod_local.provider_cli.validate_ssh_identity_file"
        ), mock.patch(
            "runpod_local.provider_cli.validate_ssh_key_pair"
        ), contextlib.redirect_stdout(output):
            self.assertEqual(_run_profile(arguments), 0)

        stored = ProfileStore(self.authored_root).load("template-host")
        self.assertEqual(stored["pod"]["image_name"], IMAGE)
        self.assertEqual(stored["pod"]["template_contract"], contract)
        self.assertEqual(
            stored["pod"]["gpu_memory_gb_by_type"],
            {GPU_ID: GPU_MEMORY_GB},
        )
        self.assertNotIn("runtime", stored["pod"])

    def test_template_profile_rejects_arbitrary_image_ingress(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                [
                    "profile",
                    "create",
                    "custom-template",
                    "--image",
                    IMAGE,
                    "--template-id",
                    "template123",
                    "--network-volume-id",
                    "volume123",
                    "--gpu",
                    GPU_ID,
                    "--max-hourly",
                    "2.25",
                ]
            )

    def test_profile_parser_has_no_workload_runtime_surface(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                [
                    "profile",
                    "create",
                    "runtime-shaped-host",
                    "--template-id",
                    "template123",
                    "--runtime",
                    "vllm-cu129",
                    "--ephemeral",
                    "--gpu",
                    GPU_ID,
                    "--max-hourly",
                    "2.25",
                ]
            )

    def test_profile_rejects_self_consistent_non_ssh_template(self):
        contract = build_private_template_contract(
            name="arbitrary-command",
            image="operator/image@sha256:" + "2" * 64,
            docker_entrypoint=["/bin/sh", "-c"],
            docker_start_cmd=["exec sleep infinity\n"],
            template_id="template123",
        )
        api = FakeTemplateApi(templates=[contract])
        arguments = build_parser().parse_args(
            [
                "profile",
                "create",
                "custom-template",
                "--template-id",
                "template123",
                "--network-volume-id",
                "volume123",
                "--gpu",
                GPU_ID,
                "--max-hourly",
                "2.25",
                "--state-root",
                str(self.root),
                "--runpod-root",
                str(self.authored_root),
            ]
        )
        with (
            mock.patch(
                "runpod_local.provider_cli._api",
                return_value=api,
            ),
            mock.patch(
                "runpod_local.provider_cli.load_ssh_public_key_file",
                return_value=(
                    pathlib.Path("/fixture/id_ed25519_runpod.pub"),
                    SSH_PUBLIC_KEY,
                ),
            ),
            mock.patch(
                "runpod_local.provider_cli.validate_ssh_identity_file"
            ),
            mock.patch(
                "runpod_local.provider_cli.validate_ssh_key_pair"
            ),
            self.assertRaises(RunpodLocalError) as caught,
        ):
            _run_profile(arguments)
        self.assertEqual(caught.exception.code, "template_contract_drift")

    def test_profile_create_rejects_a_default_above_thirty_minutes(self):
        args = build_parser().parse_args(
            [
                "profile",
                "create",
                "unsafe-default",
                "--template-id",
                "template123",
                "--ephemeral",
                "--gpu",
                GPU_ID,
                "--max-hourly",
                "2.25",
                "--ttl",
                "4h",
                "--state-root",
                str(self.root),
            ]
        )

        with self.assertRaises(RunpodLocalError) as caught:
            _run_profile(args)
        self.assertEqual(caught.exception.code, "profile_ttl_too_long")
        self.assertFalse(self.root.exists())

    def test_plan_is_local_state_read_only(self):
        api = FakeVolumeApi()

        status, output = self.run_volume(api, execute=False)

        self.assertEqual(status, 0)
        self.assertIn('"executed": false', output)
        self.assertEqual(api.create_calls, [])
        self.assertFalse(self.root.exists())

    def test_execute_verifies_created_volume_and_uses_private_lock(self):
        api = FakeVolumeApi()

        status, output = self.run_volume(api, execute=True)

        self.assertEqual(status, 0)
        self.assertIn('"status": "verified"', output)
        self.assertEqual(
            api.create_calls,
            [
                {
                    "name": "model-cache",
                    "size_gb": 250,
                    "data_center_id": "EUR-IS-2",
                }
            ],
        )
        lock = self.root / "locks" / f"{volume_lock_scope('model-cache')}.lock"
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)

    def test_exact_existing_volume_is_reused_without_post(self):
        volume = {
            "id": "volume123",
            "name": "model-cache",
            "size_gb": 250,
            "data_center_id": "EUR-IS-2",
        }
        api = FakeVolumeApi(volumes=[volume])

        status, output = self.run_volume(api, execute=True)

        self.assertEqual(status, 0)
        self.assertIn('"reconciled_existing": true', output)
        self.assertEqual(api.create_calls, [])

    def test_existing_volume_without_durable_id_is_rejected(self):
        api = FakeVolumeApi(
            volumes=[
                {
                    "id": None,
                    "name": "model-cache",
                    "size_gb": 250,
                    "data_center_id": "EUR-IS-2",
                }
            ]
        )

        with self.assertRaises(RunpodLocalError) as caught:
            self.run_volume(api, execute=False)
        self.assertEqual(caught.exception.code, "invalid_provider_response")

    def test_contradictory_created_volume_is_reported_as_executed_error(self):
        api = FakeVolumeApi(
            created={
                "id": None,
                "name": "other-cache",
                "size_gb": 500,
                "data_center_id": "OTHER-DC",
            }
        )

        status, output = self.run_volume(api, execute=True)

        self.assertEqual(status, 1)
        self.assertIn('"executed": true', output)
        self.assertIn('"status": "error"', output)
        self.assertIn('"missing_or_invalid_volume_id"', output)
        self.assertEqual(
            created_volume_violations(
                api.created,
                {
                    "name": "model-cache",
                    "size_gb": 250,
                    "data_center_id": "EUR-IS-2",
                },
            ),
            [
                "missing_or_invalid_volume_id",
                "name_mismatch",
                "size_gb_mismatch",
                "data_center_id_mismatch",
            ],
        )


if __name__ == "__main__":
    unittest.main()
