"""Model-service materialization tests."""

from __future__ import annotations

import hashlib
import os
import pathlib
import stat
import tempfile
import unittest

from model_lab.errors import ModelLabError
from model_lab.runtime_catalog import load_runtime
from model_lab.service_definition import (
    ServiceDefinition,
    load_service,
)
from model_lab.service_huggingface import (
    HuggingFaceClosure,
    HuggingFaceClosureFile,
)
from model_lab.service_materialization import (
    build_service_materialization_plan,
    load_service_materialization,
    materialize_service,
)
from model_lab_service_test_fixture import SERVICE_FIXTURE

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = SERVICE_FIXTURE


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
                path="config.json",
                bytes=512,
                role="snapshot",
                identity_algorithm="git-blob-sha1",
                identity_digest="4" * 40,
            ),
            HuggingFaceClosureFile(
                path=checkpoint,
                bytes=4096,
                role="checkpoint-weight",
                identity_algorithm="sha256",
                identity_digest="5" * 64,
            ),
        ),
    )


class ServiceMaterializationTest(unittest.TestCase):
    def plan(self, state_root: pathlib.Path):
        definition = load_service(FIXTURE)
        return build_service_materialization_plan(
            definition,
            source_root=ROOT,
            state_root=state_root,
            runtime=load_runtime("vllm-cu129-v0.25.1"),
            closure=closure_for(definition),
            remote_port=8123,
        )

    def test_plan_contains_generic_code_and_one_generated_service_input(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = self.plan(pathlib.Path(directory) / "state")

        summary = plan.safe_summary()
        self.assertIs(summary["executed"], False)
        self.assertRegex(
            summary["materialization_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertIn(
            {
                "path": "/root/runpod-session/model-snapshots",
                "mode": "0700",
            },
            summary["directories"],
        )
        service_files = [
            record
            for record in summary["files"]
            if record["remote_path"].startswith("/root/runpod-session/services/")
        ]
        deployment_id = plan.bundle_plan["deployment_manifest"]["document"][
            "deployment"
        ]["deployment_id"]
        self.assertEqual(
            service_files,
            [
                {
                    "local_path": "payload/service/deployment.json",
                    "remote_path": (
                        "/root/runpod-session/services/"
                        "fixture-dense-text/deployments/"
                        f"{deployment_id}/deployment.json"
                    ),
                    "mode": "0600",
                    "bytes": service_files[0]["bytes"],
                    "sha256": service_files[0]["sha256"],
                    "role": "deployment-manifest",
                    "publish_order": service_files[0]["publish_order"],
                }
            ],
        )
        self.assertRegex(deployment_id, r"^[0-9a-f]{64}$")
        self.assertTrue(
            any(record["role"] == "runtime-manifest" for record in summary["files"])
        )
        self.assertTrue(
            any(record["role"] == "runtime-verifier" for record in summary["files"])
        )
        self.assertTrue(
            any(
                record["role"] == "implementation-receipt"
                for record in summary["files"]
            )
        )
        encoded_paths = "\n".join(record["remote_path"] for record in summary["files"])
        self.assertNotIn("service.toml", encoded_paths)
        self.assertNotIn("model.safetensors", encoded_paths)

    def test_materialization_is_exact_idempotent_and_content_addressed(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = self.plan(pathlib.Path(directory) / "state")
            first = materialize_service(plan)
            second = materialize_service(plan)
            loaded = load_service_materialization(first.root)

            self.assertEqual(first, second)
            self.assertEqual(first, loaded)
            self.assertEqual(first.root.name, first.materialization_sha256)
            self.assertEqual(
                stat.S_IMODE(first.install_path.lstat().st_mode),
                0o600,
            )
            for record in first.install_document["files"]:
                path = first.root.joinpath(
                    *pathlib.PurePosixPath(record["local_path"]).parts
                )
                payload = path.read_bytes()
                with self.subTest(local_path=record["local_path"]):
                    self.assertEqual(
                        stat.S_IMODE(path.lstat().st_mode),
                        int(record["mode"], 8),
                    )
                    self.assertEqual(len(payload), record["bytes"])
                    self.assertEqual(
                        hashlib.sha256(payload).hexdigest(),
                        record["sha256"],
                    )
                    self.assertEqual(path.lstat().st_nlink, 1)

    def test_complete_materialization_rejects_tamper_and_extra_files(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = self.plan(pathlib.Path(directory) / "state")
            materialized = materialize_service(plan)
            deployment = materialized.root / "payload/service/deployment.json"
            deployment.write_bytes(b"{}\n")

            with self.assertRaises(ModelLabError) as tampered:
                load_service_materialization(materialized.root)
            self.assertEqual(
                tampered.exception.code,
                "unsafe_service_materialization",
            )

        with tempfile.TemporaryDirectory() as directory:
            plan = self.plan(pathlib.Path(directory) / "state")
            materialized = materialize_service(plan)
            extra = materialized.root / "payload/service/service.toml"
            extra.write_text("forbidden = true\n", encoding="utf-8")
            extra.chmod(0o600)

            with self.assertRaises(ModelLabError) as unexpected:
                load_service_materialization(materialized.root)
            self.assertEqual(
                unexpected.exception.code,
                "unsafe_service_materialization",
            )

    def test_target_permissions_are_private_even_under_process_umask(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = os.umask(0)
            try:
                materialized = materialize_service(
                    self.plan(pathlib.Path(directory) / "state")
                )
            finally:
                os.umask(previous)

            for parent in (
                materialized.root,
                materialized.root / "payload",
                materialized.root / "payload/service",
            ):
                self.assertEqual(stat.S_IMODE(parent.lstat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
