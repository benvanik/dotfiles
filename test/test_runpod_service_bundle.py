from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from runpod_local.errors import RunpodLocalError
from runpod_local.runtime_catalog import load_runtime
from runpod_local.service_bundle import (
    BUNDLE_PLAN_SCHEMA,
    DEPLOYMENT_MANIFEST_SCHEMA,
    ENTRYPOINT_ACTIONS,
    IMPLEMENTATION_BUNDLE_SCHEMA,
    IMPLEMENTATION_MEMBERS,
    RELATIVE_ENTRYPOINT,
    ImplementationMember,
    build_implementation_bundle,
    build_service_bundle_plan,
)
from runpod_local.service_definition import (
    InferenceServiceDefinition,
    load_inference_service,
    parse_inference_service_toml,
)
from runpod_local.service_huggingface import (
    HuggingFaceClosure,
    HuggingFaceClosureFile,
)

from runpod.service_runtime.document import parse_deployment_manifest
from runpod_service_test_fixture import SERVICE_FIXTURE

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = SERVICE_FIXTURE
RUNTIME = load_runtime("vllm-cu129-v0.25.1")


def closure_for(
    definition: InferenceServiceDefinition,
    *,
    identity_digit: str,
) -> HuggingFaceClosure:
    model = definition.normalized_plan()["model"]
    checkpoint = model["checkpoint"]
    if not isinstance(checkpoint, str):
        raise TypeError("bundle fixture requires an exact checkpoint")
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
                identity_digest=identity_digit * 40,
            ),
            HuggingFaceClosureFile(
                path=checkpoint,
                bytes=4096,
                role="checkpoint-weight",
                identity_algorithm="sha256",
                identity_digest=identity_digit * 64,
            ),
        ),
    )


def comparison_definition() -> InferenceServiceDefinition:
    payload = (
        FIXTURE.read_bytes()
        .replace(
            b'service_id = "fixture-dense-text"',
            b'service_id = "fixture-dense-chat"',
            1,
        )
        .replace(
            b'repository = "fixture-org/fixture-dense-text-7b"',
            b'repository = "fixture-org/fixture-dense-chat-13b"',
            1,
        )
        .replace(
            b'revision = "2222222222222222222222222222222222222222"',
            b'revision = "3333333333333333333333333333333333333333"',
            1,
        )
        .replace(b"max_model_len = 8192", b"max_model_len = 4096", 1)
        .replace(
            b"prefix_caching = true",
            b"prefix_caching = false",
            1,
        )
    )
    return parse_inference_service_toml(
        payload,
        source="<comparison-service>",
    )


class ServiceBundlePlanTest(unittest.TestCase):
    def test_bundle_is_the_exact_generic_content_closure(self):
        bundle = build_implementation_bundle(source_root=ROOT)

        self.assertEqual(
            bundle["schema_version"],
            IMPLEMENTATION_BUNDLE_SCHEMA,
        )
        self.assertEqual(
            [entry["source_path"] for entry in bundle["files"]],
            [member.source_path for member in IMPLEMENTATION_MEMBERS],
        )
        self.assertEqual(
            [entry["bundle_path"] for entry in bundle["files"]],
            [member.bundle_path for member in IMPLEMENTATION_MEMBERS],
        )
        self.assertEqual(
            [entry["mode"] for entry in bundle["files"]],
            [member.mode for member in IMPLEMENTATION_MEMBERS],
        )
        self.assertEqual(bundle["file_count"], len(IMPLEMENTATION_MEMBERS))
        self.assertEqual(
            bundle["total_bytes"],
            sum(entry["bytes"] for entry in bundle["files"]),
        )
        content_identity = {
            "schema_version": IMPLEMENTATION_BUNDLE_SCHEMA,
            "implementation_id": bundle["implementation_id"],
            "relative_entrypoint": str(RELATIVE_ENTRYPOINT),
            "entrypoint_contract": {
                "actions": list(ENTRYPOINT_ACTIONS),
                "manifest_argument": "--manifest",
                "instantiation_input_count": 1,
                "instantiation_schema_version": (DEPLOYMENT_MANIFEST_SCHEMA),
            },
            "files": [
                {key: value for key, value in entry.items() if key != "source_path"}
                for entry in bundle["files"]
            ],
        }
        canonical_identity = (
            json.dumps(
                content_identity,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            + "\n"
        ).encode("ascii")
        self.assertEqual(
            bundle["bundle_sha256"],
            hashlib.sha256(canonical_identity).hexdigest(),
        )
        self.assertEqual(
            pathlib.PurePosixPath(bundle["entrypoint"]).relative_to(
                bundle["remote_root"]
            ),
            RELATIVE_ENTRYPOINT,
        )
        self.assertEqual(
            bundle["entrypoint_contract"],
            {
                "actions": list(ENTRYPOINT_ACTIONS),
                "manifest_argument": "--manifest",
                "instantiation_input_count": 1,
                "instantiation_schema_version": (DEPLOYMENT_MANIFEST_SCHEMA),
            },
        )
        for entry in bundle["files"]:
            payload = ROOT.joinpath(entry["source_path"]).read_bytes()
            with self.subTest(source_path=entry["source_path"]):
                self.assertEqual(entry["bytes"], len(payload))
                self.assertEqual(
                    entry["sha256"],
                    hashlib.sha256(payload).hexdigest(),
                )

    def test_two_services_share_only_the_generic_implementation(self):
        first_definition = load_inference_service(FIXTURE)
        second_definition = comparison_definition()

        first = build_service_bundle_plan(
            first_definition,
            source_root=ROOT,
            runtime=RUNTIME,
            closure=closure_for(
                first_definition,
                identity_digit="4",
            ),
            remote_port=8123,
        )
        second = build_service_bundle_plan(
            second_definition,
            source_root=ROOT,
            runtime=RUNTIME,
            closure=closure_for(
                second_definition,
                identity_digit="5",
            ),
            remote_port=8123,
        )

        self.assertEqual(first["schema_version"], BUNDLE_PLAN_SCHEMA)
        self.assertIs(first["executed"], False)
        self.assertEqual(
            first["implementation_bundle"],
            second["implementation_bundle"],
        )
        self.assertNotEqual(
            first["deployment_manifest"],
            second["deployment_manifest"],
        )
        self.assertNotEqual(first["plan_sha256"], second["plan_sha256"])
        generic_payload = json.dumps(
            first["implementation_bundle"],
            sort_keys=True,
        )
        generic_source_bytes = b"".join(
            ROOT.joinpath(entry["source_path"]).read_bytes()
            for entry in first["implementation_bundle"]["files"]
        )
        for definition in (first_definition, second_definition):
            service = definition.normalized_plan()
            for value in (
                service["service_id"],
                service["model"]["repository"],
                service["model"]["revision"],
            ):
                with self.subTest(value=value):
                    self.assertNotIn(value, generic_payload)
                    self.assertNotIn(value.encode("utf-8"), generic_source_bytes)

    def test_content_bundle_has_a_real_relocated_entrypoint(self):
        bundle = build_implementation_bundle(source_root=ROOT)
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = pathlib.Path(directory)
            bundle_root = temporary_root / bundle["bundle_sha256"]
            bundle_root.mkdir(mode=0o700)
            for entry in bundle["files"]:
                source = ROOT / entry["source_path"]
                payload = source.read_bytes()
                self.assertEqual(
                    hashlib.sha256(payload).hexdigest(),
                    entry["sha256"],
                )
                target = bundle_root.joinpath(
                    *pathlib.PurePosixPath(entry["bundle_path"]).parts
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                for parent in target.parents:
                    if parent == temporary_root:
                        break
                    parent.chmod(0o700)
                target.write_bytes(payload)
                target.chmod(int(entry["mode"], 8))
            receipt = bundle["receipt"]
            receipt_path = bundle_root / "bundle.json"
            receipt_path.write_bytes(
                (
                    json.dumps(
                        receipt["document"],
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    )
                    + "\n"
                ).encode("ascii")
            )
            receipt_path.chmod(0o600)
            entrypoint = bundle_root.joinpath(*RELATIVE_ENTRYPOINT.parts)
            manifest_path = temporary_root / "deployment.json"
            manifest = {
                "implementation": {
                    "bundle_sha256": bundle["bundle_sha256"],
                    "remote_root": str(bundle_root),
                    "entrypoint": str(entrypoint),
                    "receipt": {
                        "remote_path": str(receipt_path),
                        "bytes": receipt["bytes"],
                        "sha256": receipt["sha256"],
                    },
                }
            }
            manifest_path.write_text(
                json.dumps(
                    manifest,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="ascii",
            )
            manifest_path.chmod(0o600)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(entrypoint),
                    "--help",
                    "--manifest",
                    str(manifest_path),
                ],
                cwd=bundle_root,
                env={
                    "PATH": os.environ["PATH"],
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                check=False,
                capture_output=True,
                text=True,
            )
            rejected_invocations = (
                ["--help", f"--manifest={manifest_path}"],
                [
                    "--help",
                    "--manifest",
                    str(manifest_path),
                    "--manifest",
                    str(manifest_path),
                ],
                [
                    "status",
                    "--manifest",
                    str(manifest_path),
                    "--man",
                    str(manifest_path),
                ],
                [
                    "status",
                    "--manifest",
                    str(manifest_path),
                    "--cache",
                    "ephemeral",
                ],
            )
            for arguments in rejected_invocations:
                rejected = subprocess.run(
                    [sys.executable, str(entrypoint), *arguments],
                    cwd=bundle_root,
                    env={
                        "PATH": os.environ["PATH"],
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                    check=False,
                    capture_output=True,
                    text=True,
                )
                with self.subTest(arguments=arguments):
                    self.assertNotEqual(rejected.returncode, 0)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(
            "{" + ",".join(ENTRYPOINT_ACTIONS) + "}",
            completed.stdout,
        )
        self.assertIn("--manifest", completed.stdout)

    def test_manifest_is_the_only_generated_instantiation_input(self):
        definition = load_inference_service(FIXTURE)
        closure = closure_for(definition, identity_digit="6")
        plan = build_service_bundle_plan(
            definition,
            source_root=ROOT,
            runtime=RUNTIME,
            closure=closure,
            remote_port=8123,
        )
        descriptor = plan["deployment_manifest"]
        manifest = descriptor["document"]

        self.assertEqual(
            descriptor["schema_version"],
            DEPLOYMENT_MANIFEST_SCHEMA,
        )
        self.assertEqual(descriptor["mode"], "0600")
        canonical_payload = (
            json.dumps(
                manifest,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            + "\n"
        ).encode("ascii")
        self.assertEqual(descriptor["bytes"], len(canonical_payload))
        self.assertEqual(
            descriptor["sha256"],
            hashlib.sha256(canonical_payload).hexdigest(),
        )
        parsed = parse_deployment_manifest(canonical_payload)
        self.assertEqual(parsed.value, manifest)
        self.assertEqual(parsed.manifest_sha256, descriptor["sha256"])
        self.assertEqual(
            manifest["huggingface_closure"],
            closure.as_dict(),
        )
        self.assertNotIn("definition_path", manifest["deployment"])
        snapshot_root = manifest["deployment"]["model_snapshot"]["root"]
        self.assertEqual(
            pathlib.PurePosixPath(snapshot_root).name,
            closure.closure_sha256,
        )
        self.assertEqual(
            manifest["deployment"]["launch"]["argv"][2],
            snapshot_root,
        )
        self.assertNotIn(
            definition.model.checkpoint,
            manifest["deployment"]["launch"]["argv"],
        )
        self.assertEqual(
            manifest["implementation"]["bundle_sha256"],
            plan["implementation_bundle"]["bundle_sha256"],
        )
        requirement = manifest["compile_cache"]
        self.assertEqual(
            requirement["status"],
            "requires-runtime-execution-environment-and-observed-gpu",
        )
        self.assertIsNone(requirement["observed_gpu"])
        self.assertEqual(
            requirement["inputs"]["implementation_bundle_sha256"],
            plan["implementation_bundle"]["bundle_sha256"],
        )
        self.assertIsNone(
            requirement["inputs"]["runtime_execution_environment"]
        )
        self.assertEqual(
            requirement["inputs"]["huggingface_closure_sha256"],
            closure.closure_sha256,
        )
        self.assertEqual(
            requirement["inputs"]["compile_affecting_launch_sha256"],
            manifest["deployment"]["launch"]["compile_affecting_sha256"],
        )

    def test_mismatched_generated_closure_fails_before_manifest(self):
        definition = load_inference_service(FIXTURE)
        mismatched = HuggingFaceClosure(
            repository="fixture-org/different-model",
            revision=definition.model.revision,
            requested_selector=definition.model.checkpoint,
            resolved_index=None,
            weight_files=("model.safetensors",),
            files=(
                HuggingFaceClosureFile(
                    path="model.safetensors",
                    bytes=1,
                    role="checkpoint-weight",
                    identity_algorithm="sha256",
                    identity_digest="7" * 64,
                ),
            ),
        )

        with self.assertRaises(RunpodLocalError) as caught:
            build_service_bundle_plan(
                definition,
                source_root=ROOT,
                runtime=RUNTIME,
                closure=mismatched,
            )

        self.assertEqual(
            caught.exception.code,
            "mismatched_service_huggingface_closure",
        )

    def test_bundle_rejects_an_unreviewed_runtime_summary(self):
        definition = load_inference_service(FIXTURE)

        with self.assertRaises(RunpodLocalError) as caught:
            build_service_bundle_plan(
                definition,
                source_root=ROOT,
                runtime=RUNTIME.safe_summary(),  # type: ignore[arg-type]
                closure=closure_for(definition, identity_digit="8"),
            )

        self.assertEqual(
            caught.exception.code,
            "invalid_service_bundle_runtime",
        )

    def test_safetensors_service_rejects_auto_resolved_bin_closure(self):
        definition = parse_inference_service_toml(
            FIXTURE.read_bytes().replace(
                b'checkpoint = "model.safetensors"\n',
                b"",
                1,
            )
        )
        incompatible = HuggingFaceClosure(
            repository=definition.model.repository,
            revision=definition.model.revision,
            requested_selector=None,
            resolved_index=None,
            weight_files=("model.bin",),
            files=(
                HuggingFaceClosureFile(
                    path="model.bin",
                    bytes=1,
                    role="checkpoint-weight",
                    identity_algorithm="sha256",
                    identity_digest="7" * 64,
                ),
            ),
        )

        with self.assertRaises(RunpodLocalError) as caught:
            build_service_bundle_plan(
                definition,
                source_root=ROOT,
                runtime=RUNTIME,
                closure=incompatible,
            )

        self.assertEqual(
            caught.exception.code,
            "incompatible_service_huggingface_closure",
        )

    def test_private_group_write_is_accepted_but_world_write_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            member = root / "member.py"
            member.write_text("print('generic')\n", encoding="utf-8")
            allowlist = (
                ImplementationMember(
                    "member.py",
                    str(RELATIVE_ENTRYPOINT),
                    "0755",
                ),
            )

            member.chmod(0o664)
            with mock.patch(
                "runpod_local.service_bundle.IMPLEMENTATION_MEMBERS",
                allowlist,
            ):
                bundle = build_implementation_bundle(source_root=root)
            self.assertEqual(bundle["files"][0]["mode"], "0755")

            member.chmod(0o666)
            with (
                mock.patch(
                    "runpod_local.service_bundle.IMPLEMENTATION_MEMBERS",
                    allowlist,
                ),
                self.assertRaises(RunpodLocalError) as caught,
            ):
                build_implementation_bundle(source_root=root)

            self.assertEqual(
                caught.exception.code,
                "unsafe_service_implementation_member",
            )

            member.chmod(0o644)
            (root / "second-link.py").hardlink_to(member)
            with (
                mock.patch(
                    "runpod_local.service_bundle.IMPLEMENTATION_MEMBERS",
                    allowlist,
                ),
                self.assertRaises(RunpodLocalError) as hardlinked,
            ):
                build_implementation_bundle(source_root=root)
            self.assertEqual(
                hardlinked.exception.code,
                "unsafe_service_implementation_member",
            )

    def test_implementation_allowlist_rejects_unsafe_or_duplicate_targets(self):
        invalid_allowlists = (
            (
                ImplementationMember(
                    "member.py",
                    "../member.py",
                    "0644",
                ),
            ),
            (
                ImplementationMember(
                    "entrypoint",
                    str(RELATIVE_ENTRYPOINT),
                    "0644",
                ),
            ),
            (
                ImplementationMember(
                    "first.py",
                    str(RELATIVE_ENTRYPOINT),
                    "0755",
                ),
                ImplementationMember(
                    "second.py",
                    str(RELATIVE_ENTRYPOINT),
                    "0755",
                ),
            ),
        )
        for allowlist in invalid_allowlists:
            with (
                self.subTest(allowlist=allowlist),
                mock.patch(
                    "runpod_local.service_bundle.IMPLEMENTATION_MEMBERS",
                    allowlist,
                ),
                self.assertRaises(RunpodLocalError) as caught,
            ):
                build_implementation_bundle(source_root=ROOT)

            self.assertEqual(
                caught.exception.code,
                "invalid_service_implementation_allowlist",
            )


if __name__ == "__main__":
    unittest.main()
