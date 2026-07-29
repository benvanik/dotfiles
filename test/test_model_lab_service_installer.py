"""Remote model-service installer tests."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import importlib.util
import os
import pathlib
import shutil
import stat
import tempfile
import unittest
from types import ModuleType

from model_lab.runtime_catalog import load_runtime
from model_lab.service_definition import load_service
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
TRANSFER_ID = "1" * 64


def load_installer() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "model_lab_service_installer_test",
        INSTALLER,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("cannot load service installer")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def materialized_fixture(
    state_root: pathlib.Path,
    *,
    remote_port: int = 8000,
):
    definition = load_service(FIXTURE)
    model = definition.normalized_plan()["model"]
    checkpoint = model["checkpoint"]
    if not isinstance(checkpoint, str):
        raise TypeError("fixture requires one exact checkpoint")
    closure = HuggingFaceClosure(
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
    return materialize_service(
        build_service_materialization_plan(
            definition,
            source_root=ROOT,
            state_root=state_root,
            runtime=load_runtime("vllm-cu129-v0.25.1"),
            closure=closure,
            remote_port=remote_port,
        )
    )


def configure_test_root(
    module: ModuleType,
    root: pathlib.Path,
) -> None:
    module.SESSION_ROOT = root / "runpod-session"
    module.INCOMING_ROOT = module.SESSION_ROOT / "incoming" / "service-materializations"
    module.IMPLEMENTATION_ROOT = (
        module.SESSION_ROOT / "control" / "model-service-runtime"
    )
    module.RUNTIME_CONTROL_ROOT = module.SESSION_ROOT / "control" / "runtime-verifier"
    module.SERVICES_ROOT = module.SESSION_ROOT / "services"


def relocated_document(
    module: ModuleType,
    document: dict[str, object],
) -> dict[str, object]:
    relocated = copy.deepcopy(document)
    files = relocated["files"]
    if not isinstance(files, list):
        raise TypeError("invalid fixture")
    for record in files:
        if not isinstance(record, dict):
            raise TypeError("invalid fixture")
        path = record["remote_path"]
        if not isinstance(path, str):
            raise TypeError("invalid fixture")
        record["remote_path"] = path.replace(
            "/root/runpod-session",
            str(module.SESSION_ROOT),
            1,
        )
    relocated["directories"] = [
        {"path": path, "mode": "0700"}
        for path in module.expected_final_directories(files)
    ]
    identity = {
        "schema_version": module.INSTALL_IDENTITY_SCHEMA,
        "installer": relocated["installer"],
        "directories": relocated["directories"],
        "files": files,
    }
    relocated["materialization_sha256"] = hashlib.sha256(
        module.canonical_bytes(identity)
    ).hexdigest()
    return relocated


def copy_incoming(
    module: ModuleType,
    materialized: object,
    document: dict[str, object],
    *,
    transfer_id: str = TRANSFER_ID,
) -> pathlib.Path:
    identity = document["materialization_sha256"]
    if not isinstance(identity, str):
        raise TypeError("invalid fixture")
    incoming = module.incoming_path(identity, transfer_id)
    install_path = incoming / "install.json"
    install_path.write_bytes(module.canonical_bytes(document))
    install_path.chmod(0o600)
    shutil.copytree(
        materialized.payload_root,
        incoming / "payload",
        copy_function=shutil.copy2,
    )
    for directory, directory_names, _ in __import__("os").walk(incoming / "payload"):
        pathlib.Path(directory).chmod(0o700)
        for name in directory_names:
            (pathlib.Path(directory) / name).chmod(0o700)
    return incoming


class ServiceInstallerTest(unittest.TestCase):
    def test_changed_stopped_deployment_is_versioned_and_lifecycle_guarded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            module = load_installer()
            configure_test_root(module, root)
            first = materialized_fixture(root / "local", remote_port=8000)
            second = materialized_fixture(root / "local", remote_port=8001)
            first_document = relocated_document(
                module,
                first.install_document,
            )
            second_document = relocated_document(
                module,
                second.install_document,
            )
            first_identity = first_document["materialization_sha256"]
            second_identity = second_document["materialization_sha256"]
            second_transfer = "2" * 64
            module.prepare(first_identity, TRANSFER_ID)
            copy_incoming(module, first, first_document)
            module.install(first_identity, TRANSFER_ID)
            module.prepare(second_identity, second_transfer)
            copy_incoming(
                module,
                second,
                second_document,
                transfer_id=second_transfer,
            )
            first_manifest = next(
                pathlib.Path(record["remote_path"])
                for record in first_document["files"]
                if record["role"] == "deployment-manifest"
            )
            second_manifest = next(
                pathlib.Path(record["remote_path"])
                for record in second_document["files"]
                if record["role"] == "deployment-manifest"
            )
            service_root = first_manifest.parents[2]

            serving_descriptor = os.open(
                service_root / "serving.lock",
                os.O_RDWR,
            )
            try:
                fcntl.flock(serving_descriptor, fcntl.LOCK_SH)
                with self.assertRaises(module.InstallError) as running:
                    module.install(second_identity, second_transfer)
                self.assertIn("service is running", str(running.exception))
            finally:
                os.close(serving_descriptor)

            process_state = service_root / "process.json"
            process_state.write_text("{}\n", encoding="ascii")
            process_state.chmod(0o600)
            with self.assertRaises(module.InstallError) as retained:
                module.install(second_identity, second_transfer)
            self.assertIn("process state is retained", str(retained.exception))
            process_state.unlink()

            changed = module.install(second_identity, second_transfer)

            self.assertEqual(changed["status"], "installed")
            self.assertNotEqual(first_manifest, second_manifest)
            self.assertTrue(first_manifest.is_file())
            self.assertTrue(second_manifest.is_file())
            self.assertEqual(first_manifest.parents[2], second_manifest.parents[2])
            self.assertEqual(first_manifest.parents[1].name, "deployments")
            self.assertRegex(first_manifest.parent.name, r"^[0-9a-f]{64}$")
            self.assertRegex(second_manifest.parent.name, r"^[0-9a-f]{64}$")

    def test_prepare_install_and_identical_retry_are_no_clobber(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            module = load_installer()
            configure_test_root(module, root)
            materialized = materialized_fixture(root / "local")
            document = relocated_document(
                module,
                materialized.install_document,
            )
            identity = document["materialization_sha256"]

            prepared = module.prepare(identity, TRANSFER_ID)
            self.assertEqual(prepared["status"], "ready-for-copy")
            copy_incoming(module, materialized, document)
            installed = module.install(identity, TRANSFER_ID)
            repeated = module.install(identity, TRANSFER_ID)

            self.assertEqual(installed["status"], "installed")
            self.assertEqual(repeated, installed)
            for record in document["files"]:
                destination = pathlib.Path(record["remote_path"])
                payload = destination.read_bytes()
                with self.subTest(remote_path=record["remote_path"]):
                    self.assertEqual(
                        stat.S_IMODE(destination.lstat().st_mode),
                        int(record["mode"], 8),
                    )
                    self.assertEqual(len(payload), record["bytes"])
                    self.assertEqual(
                        hashlib.sha256(payload).hexdigest(),
                        record["sha256"],
                    )
                    self.assertEqual(destination.lstat().st_nlink, 1)

    def test_partial_resume_validates_existing_bytes_and_rejects_extras(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            module = load_installer()
            configure_test_root(module, root)
            materialized = materialized_fixture(root / "local")
            document = relocated_document(
                module,
                materialized.install_document,
            )
            identity = document["materialization_sha256"]
            module.prepare(identity, TRANSFER_ID)
            incoming = copy_incoming(module, materialized, document)

            self.assertEqual(
                module.prepare(identity, TRANSFER_ID)["status"],
                "ready-for-copy",
            )
            extra = incoming / "payload/undeclared-empty"
            extra.mkdir(mode=0o700)
            with self.assertRaises(module.InstallError):
                module.install(identity, TRANSFER_ID)

    def test_installed_conflict_and_uppercase_identity_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            module = load_installer()
            configure_test_root(module, root)
            materialized = materialized_fixture(root / "local")
            document = relocated_document(
                module,
                materialized.install_document,
            )
            identity = document["materialization_sha256"]
            module.prepare(identity, TRANSFER_ID)
            copy_incoming(module, materialized, document)
            module.install(identity, TRANSFER_ID)

            member = next(
                record
                for record in document["files"]
                if record["role"] == "implementation-member"
            )
            pathlib.Path(member["remote_path"]).write_bytes(b"changed\n")
            with self.assertRaises(module.InstallError):
                module.install(identity, TRANSFER_ID)
            with self.assertRaises(module.InstallError):
                module.incoming_path("A" * 64, TRANSFER_ID)

    def test_final_generic_closure_rejects_an_extra_empty_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            module = load_installer()
            configure_test_root(module, root)
            materialized = materialized_fixture(root / "local")
            document = relocated_document(
                module,
                materialized.install_document,
            )
            identity = document["materialization_sha256"]
            module.prepare(identity, TRANSFER_ID)
            copy_incoming(module, materialized, document)
            module.install(identity, TRANSFER_ID)
            implementation_root = next(
                pathlib.Path(record["remote_path"]).parents[1]
                for record in document["files"]
                if record["role"] == "implementation-member"
            )
            (implementation_root / "undeclared-empty").mkdir(mode=0o700)

            with self.assertRaises(module.InstallError):
                module.install(identity, TRANSFER_ID)

    def test_interrupted_transfer_does_not_poison_a_new_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            module = load_installer()
            configure_test_root(module, root)
            materialized = materialized_fixture(root / "local")
            document = relocated_document(
                module,
                materialized.install_document,
            )
            identity = document["materialization_sha256"]
            first_transfer = "2" * 64
            second_transfer = "3" * 64
            module.prepare(identity, first_transfer)
            interrupted = module.incoming_path(identity, first_transfer)
            (interrupted / "install.json").write_bytes(b'{"truncated":')
            (interrupted / "install.json").chmod(0o600)

            with self.assertRaises(module.InstallError):
                module.prepare(identity, first_transfer)

            prepared = module.prepare(identity, second_transfer)
            copy_incoming(
                module,
                materialized,
                document,
                transfer_id=second_transfer,
            )
            installed = module.install(identity, second_transfer)

            self.assertEqual(prepared["transfer_id"], second_transfer)
            self.assertEqual(installed["status"], "installed")


if __name__ == "__main__":
    unittest.main()
