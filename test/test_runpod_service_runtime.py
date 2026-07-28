from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import signal
import stat
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runpod"))

from runpod_local.errors import RunpodLocalError  # noqa: E402
from runpod_local.service_compile_cache import (  # noqa: E402
    build_compile_cache_contract,
)
from runpod_local.service_definition import (  # noqa: E402
    parse_inference_service_toml,
)
from runpod_local.template import docker_arguments_summary  # noqa: E402
from service_runtime.collaborators import (  # noqa: E402
    COMPILE_CACHE_STAGE_SCHEMA,
    COMPILE_CACHE_SUBDIRECTORIES,
    compile_cache_receipt_path,
)
from service_runtime.controller import ServiceRuntimeController  # noqa: E402
from service_runtime.document import parse_deployment_manifest  # noqa: E402
from service_runtime.layout import (  # noqa: E402
    RuntimeLayout,
    canonical_service_paths,
)
from service_runtime.platform import (  # noqa: E402
    ProbeResult,
    ProcessObservation,
    SpawnedProcess,
)
from service_runtime.snapshot_stage import (  # noqa: E402
    SNAPSHOT_STAGE_SCHEMA,
    verify_snapshot_stage,
)
from service_runtime.vllm import (  # noqa: E402
    build_vllm_argv,
    compile_affecting_sha256,
)


CONFIG = b"""\
schema = "runpod.inference-service.v1"
service_id = "fixture-service"
driver = "vllm-openai.v1"
runtime_id = "vllm-cu129-v0.25.1"

[model]
source = "huggingface"
repository = "example/Model-7B"
revision = "1111111111111111111111111111111111111111"
checkpoint = "model.safetensors"

[endpoint]
input_modalities = ["text"]
reasoning = false

[compatibility]
minimum_compute_capability = "8.0"

[vllm]
model_implementation = "vllm"
dtype = "bfloat16"
quantization = "none"
tensor_parallel_size = 1
max_model_len = 8192
max_num_sequences = 2
max_num_batched_tokens = 4096
kv_cache_dtype = "bfloat16"
gpu_memory_utilization = 0.75
chunked_prefill = true
load_format = "safetensors"
safetensors_load_strategy = "lazy"
language_model_only = true
mamba_cache_mode = "none"
prefix_caching = true
reasoning_parser = "none"
tool_call_parser = "none"
speculative_method = "none"
speculative_tokens = 0
generation_config = "auto"
"""
RUNTIME = {
    "schema_version": "runpod.runtime-selection.v1",
    "runtime_id": "vllm-cu129-v0.25.1",
    "image": "vllm/vllm-openai@sha256:" + "1" * 64,
    "manifest": {
        "path": "runpod/runtimes/vllm-cu129/runtime-manifest.json",
        "sha256": "2" * 64,
    },
    "launch_overlay": {
        "bootstrap_id": "fixture-bootstrap-v1",
        "bootstrap_path": "runpod/bootstrap/ssh/bootstrap.sh",
        "bootstrap_sha256": "3" * 64,
        "bootstrap_bytes": 123,
        "docker_entrypoint_summary": docker_arguments_summary(["/bin/bash", "-c"]),
        "docker_start_cmd_summary": docker_arguments_summary(["fixture"]),
    },
    "container_disk_gb": 50,
    "volume_in_gb": 0,
    "volume_mount_path": "/workspace",
}
GPU = {
    "name": "Fixture NVIDIA GPU",
    "compute_capability": [12, 0],
    "memory_mib": 97887,
    "driver_version": "580.126.09",
}
BOOT_ID = "11111111-2222-4333-8444-555555555555"
NONCE = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def canonical_sha256(value: object, *, newline: bool = False) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    if newline:
        payload += "\n"
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def closure_for(checkpoint: str) -> dict[str, object]:
    record = {
        "path": checkpoint,
        "bytes": 7,
        "role": "checkpoint-weight",
        "identity": {"algorithm": "sha256", "digest": "7" * 64},
    }
    source = {
        "kind": "huggingface",
        "repository": "example/Model-7B",
        "revision": "1" * 40,
    }
    checkpoint_record = {
        "requested_selector": checkpoint,
        "resolved_index": None,
        "weight_files": [checkpoint],
    }
    identity = {
        "schema_version": "runpod.huggingface-closure-identity.v1",
        "source": source,
        "checkpoint": checkpoint_record,
        "files": [record],
    }
    return {
        "schema_version": "runpod.huggingface-closure.v1",
        "source": source,
        "checkpoint": checkpoint_record,
        "files": [record],
        "file_count": 1,
        "total_bytes": 7,
        "closure_sha256": canonical_sha256(identity, newline=True),
    }


def deployment_manifest(
    *,
    checkpoint: str = "model.safetensors",
    load_format: str = "safetensors",
) -> dict[str, object]:
    definition = parse_inference_service_toml(CONFIG)
    service = copy.deepcopy(definition.normalized_plan())
    service["model"]["checkpoint"] = checkpoint
    service["vllm"]["load_format"] = load_format
    closure = closure_for(checkpoint)
    paths = canonical_service_paths(
        service_id=service["service_id"],
        closure_sha256=closure["closure_sha256"],
    )
    port = 8000
    launch_sha256 = compile_affecting_sha256(service)
    bundle_sha256 = "4" * 64
    implementation_root = (
        "/root/runpod-session/control/inference-service-runtime/" + bundle_sha256
    )
    return {
        "schema_version": "runpod.inference-service-deployment-manifest.v1",
        "definition": {
            "source_sha256": hashlib.sha256(CONFIG).hexdigest(),
            "source_bytes": len(CONFIG),
            "service_plan_sha256": canonical_sha256(service, newline=True),
            "service": service,
        },
        "runtime": copy.deepcopy(RUNTIME),
        "huggingface_closure": closure,
        "implementation": {
            "implementation_id": "runpod-inference-service-runtime-v1",
            "bundle_sha256": bundle_sha256,
            "remote_root": implementation_root,
            "entrypoint": implementation_root + "/bin/runpod-service-runtime",
        },
        "deployment": {
            "service_root": str(paths.service_root),
            "manifest_path": str(paths.manifest),
            "process": {
                "state_path": str(paths.process_state),
                "log_path": str(paths.service_log),
                "lifecycle_lock_path": str(paths.lifecycle_lock),
                "serving_lock_path": str(paths.serving_lock),
            },
            "model_snapshot": {
                "root": str(paths.snapshot_root),
                "closure_sha256": closure["closure_sha256"],
            },
            "launch": {
                "argv": list(
                    build_vllm_argv(
                        service,
                        snapshot_root=paths.snapshot_root,
                        port=port,
                    )
                ),
                "snapshot_argument_index": 2,
                "compile_affecting_sha256": launch_sha256,
                "host": "127.0.0.1",
                "port": port,
            },
        },
        "compile_cache": {
            "status": "requires-observed-gpu",
            "contract_schema_version": "runpod.vllm-compile-cache.v1",
            "inputs": {
                "driver": "vllm-openai.v1",
                "runtime": copy.deepcopy(RUNTIME),
                "huggingface_closure_sha256": closure["closure_sha256"],
                "compile_affecting_launch_sha256": launch_sha256,
            },
            "observed_gpu": None,
        },
    }


def write_private_json(path: pathlib.Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)


def stat_identity(path: pathlib.Path) -> dict[str, int]:
    value = path.lstat()
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
        "mode": stat.S_IMODE(value.st_mode),
    }


class FixturePlatform:
    def __init__(self) -> None:
        self.processes: list[ProcessObservation] = []
        self.spawned_argv: tuple[str, ...] | None = None
        self.spawned_environment: dict[str, str] | None = None
        self.signals: list[signal.Signals] = []
        self.fail_process_identity = False
        self.ignore_term = False
        self.ignore_kill = False

    def require_runtime_account(self) -> None:
        pass

    def boot_id(self) -> str:
        return BOOT_ID

    def process_nonce(self) -> str:
        return NONCE

    def observe_gpu(self) -> dict[str, object]:
        return copy.deepcopy(GPU)

    def verify_runtime(self, **_: object) -> dict[str, object]:
        return {
            "schema_version": "runpod.upstream-runtime-verification.v1",
            "requested_image": RUNTIME["image"],
            "runtime_id": RUNTIME["runtime_id"],
            "versions": {},
            "executables": {},
            "gpu_verified": True,
            "gpu": {
                "name": GPU["name"],
                "capability": GPU["compute_capability"],
                "bytes": GPU["memory_mib"] * 1024 * 1024,
            },
        }

    def spawn(
        self,
        *,
        argv: tuple[str, ...],
        environment_additions: dict[str, str],
        **_: object,
    ) -> SpawnedProcess:
        self.spawned_argv = argv
        self.spawned_environment = environment_additions
        observation = ProcessObservation(
            pid=4321,
            process_group_id=4321,
            session_id=4321,
            start_ticks=98765,
        )
        self.processes = [observation]
        return SpawnedProcess(pid=4321, observation=observation)

    def process_is_owned(self, state: object) -> bool:
        return (
            not self.fail_process_identity
            and bool(self.processes)
            and getattr(state, "pid", None) == self.processes[0].pid
        )

    def list_service_processes(self, **_: object) -> list[ProcessObservation]:
        return list(self.processes)

    def signal_processes(
        self,
        *,
        signal_number: signal.Signals,
        **_: object,
    ) -> None:
        self.signals.append(signal_number)
        if signal_number == signal.SIGTERM and not self.ignore_term:
            self.processes = []
        if signal_number == signal.SIGKILL and not self.ignore_kill:
            self.processes = []

    def wait_for_exit(self, **_: object) -> bool:
        return not self.processes

    def probe(self, **_: object) -> ProbeResult:
        return ProbeResult(
            ready=True,
            health_status=200,
            models_status=200,
            served_model_ids=("fixture-service",),
            detail="ready",
        )


class RuntimeFixture:
    def __init__(self, testcase: unittest.TestCase) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        testcase.addCleanup(self.temporary.cleanup)
        root = pathlib.Path(self.temporary.name)
        self.session = root / "session"
        self.workspace = root / "workspace"
        self.session.mkdir(mode=0o700)
        self.workspace.mkdir(mode=0o700)
        (self.session / "services").mkdir(mode=0o700)
        (self.session / "model-snapshots").mkdir(mode=0o700)
        self.layout = RuntimeLayout(
            session_root=self.session,
            workspace_root=self.workspace,
        )
        self.value = deployment_manifest()
        self.manifest = parse_deployment_manifest(
            json.dumps(self.value).encode("ascii")
        )
        canonical, local = self.layout.service_paths(
            service_id=self.manifest.service_id,
            closure_sha256=self.manifest.closure_sha256,
        )
        self.canonical = canonical
        self.local = local
        local["service_root"].mkdir(mode=0o700)
        write_private_json(local["manifest"], self.value)
        self._stage_snapshot()
        self._stage_compile_cache()

    def _stage_snapshot(self) -> None:
        root = self.local["snapshot_root"]
        root.mkdir(mode=0o700)
        closure_record = self.manifest.closure["files"][0]
        weight = root / closure_record["path"]
        weight.write_bytes(b"fixture")
        weight.chmod(0o400)
        receipt_record = {
            **closure_record,
            **stat_identity(weight),
        }
        receipt = {
            "schema_version": SNAPSHOT_STAGE_SCHEMA,
            "closure_sha256": self.manifest.closure_sha256,
            "source": self.manifest.closure["source"],
            "checkpoint": self.manifest.closure["checkpoint"],
            "snapshot_root": str(self.canonical.snapshot_root),
            "boot_id": BOOT_ID,
            "directory_stat": stat_identity(root),
            "file_count": 1,
            "total_bytes": len(b"fixture"),
            "files": [receipt_record],
        }
        write_private_json(self.local["snapshot_receipt"], receipt)

    def _stage_compile_cache(self) -> None:
        contract = build_compile_cache_contract(
            driver=self.manifest.service["driver"],
            runtime=self.manifest.runtime,
            huggingface_closure_sha256=self.manifest.closure_sha256,
            compile_affecting_launch_sha256=(
                self.manifest.compile_affecting_launch_sha256
            ),
            observed_gpu=GPU,
        )
        root = self.layout.localize(contract["local_root"])
        root.mkdir(parents=True, mode=0o700)
        # parents=True follows only fixture-owned parents beneath TemporaryDirectory.
        for parent in root.parents:
            if parent == self.session.parent:
                break
            if self.session in (parent, *parent.parents):
                parent.chmod(0o700)
        for name in COMPILE_CACHE_SUBDIRECTORIES:
            (root / name).mkdir(mode=0o700)
        receipt = {
            "schema_version": COMPILE_CACHE_STAGE_SCHEMA,
            "cache_id": contract["cache_id"],
            "contract": contract,
            "boot_id": BOOT_ID,
            "persistent_root": contract["persistent_root"],
            "local_root": contract["local_root"],
            "directory_stat": stat_identity(root),
            "file_count": 0,
            "total_bytes": 0,
            "files_sha256": "9" * 64,
        }
        write_private_json(
            self.layout.localize(compile_cache_receipt_path(contract)),
            receipt,
        )


class DeploymentDocumentTest(unittest.TestCase):
    def test_manifest_accepts_typed_launch_and_rejects_argv_escape(self):
        value = deployment_manifest()
        parsed = parse_deployment_manifest(json.dumps(value).encode("ascii"))
        self.assertEqual(parsed.service_id, "fixture-service")

        changed = copy.deepcopy(value)
        changed["deployment"]["launch"]["argv"].append("/bin/sh")
        with self.assertRaises(RunpodLocalError):
            parse_deployment_manifest(json.dumps(changed).encode("ascii"))

    def test_pytorch_bin_requires_auto_load_format(self):
        accepted = deployment_manifest(
            checkpoint="pytorch_model.bin",
            load_format="auto",
        )
        self.assertEqual(
            parse_deployment_manifest(json.dumps(accepted).encode("ascii")).closure[
                "checkpoint"
            ]["weight_files"],
            ["pytorch_model.bin"],
        )

        rejected = deployment_manifest(
            checkpoint="pytorch_model.bin",
            load_format="safetensors",
        )
        with self.assertRaises(RunpodLocalError):
            parse_deployment_manifest(json.dumps(rejected).encode("ascii"))


class SnapshotStageTest(unittest.TestCase):
    def test_fast_verifier_binds_stats_and_exact_tree(self):
        fixture = RuntimeFixture(self)
        verified = verify_snapshot_stage(
            closure=fixture.manifest.closure,
            canonical_snapshot_root=fixture.canonical.snapshot_root,
            local_snapshot_root=fixture.local["snapshot_root"],
            receipt_path=fixture.local["snapshot_receipt"],
            boot_id=BOOT_ID,
        )
        self.assertEqual(
            verified.receipt["closure_sha256"],
            fixture.manifest.closure_sha256,
        )

        unexpected = fixture.local["snapshot_root"] / "extra.json"
        unexpected.write_text("{}", encoding="ascii")
        unexpected.chmod(0o400)
        with self.assertRaises(RunpodLocalError):
            verify_snapshot_stage(
                closure=fixture.manifest.closure,
                canonical_snapshot_root=fixture.canonical.snapshot_root,
                local_snapshot_root=fixture.local["snapshot_root"],
                receipt_path=fixture.local["snapshot_receipt"],
                boot_id=BOOT_ID,
            )


class ServiceRuntimeLifecycleTest(unittest.TestCase):
    def test_setup_start_status_stop_uses_only_generated_manifest(self):
        fixture = RuntimeFixture(self)
        platform = FixturePlatform()
        controller = ServiceRuntimeController(
            layout=fixture.layout,
            platform=platform,
        )

        setup = controller.setup(fixture.local["manifest"])
        started = controller.start(fixture.local["manifest"])
        status = controller.status(fixture.local["manifest"])
        stopped = controller.stop(fixture.local["manifest"])

        self.assertEqual(setup["status"], "ready-to-start")
        self.assertEqual(started["status"], "starting")
        self.assertEqual(status["phase"], "ready")
        self.assertEqual(stopped["status"], "stopped")
        self.assertFalse(os.path.lexists(fixture.local["process_state"]))
        self.assertEqual(
            platform.spawned_argv,
            tuple(fixture.value["deployment"]["launch"]["argv"]),
        )
        self.assertNotIn("HF_TOKEN", platform.spawned_environment)
        self.assertEqual(
            platform.spawned_environment["RUNPOD_SERVICE_ID"],
            "fixture-service",
        )

    def test_failed_start_escalates_and_reports_ambiguous_survivor(self):
        fixture = RuntimeFixture(self)
        platform = FixturePlatform()
        platform.fail_process_identity = True
        platform.ignore_term = True
        platform.ignore_kill = True
        controller = ServiceRuntimeController(
            layout=fixture.layout,
            platform=platform,
        )
        controller.setup(fixture.local["manifest"])

        with self.assertRaises(RunpodLocalError) as caught:
            controller.start(fixture.local["manifest"])

        self.assertEqual(caught.exception.code, "ambiguous_failed_service_start")
        self.assertEqual(platform.signals, [signal.SIGTERM, signal.SIGKILL])
        self.assertFalse(os.path.lexists(fixture.local["process_state"]))


if __name__ == "__main__":
    unittest.main()
