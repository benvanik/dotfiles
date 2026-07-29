"""Remote model-service controller tests."""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import os
import pathlib
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "model-lab"))

from model_lab.errors import ModelLabError  # noqa: E402
from model_lab.service_definition import (  # noqa: E402
    parse_service_toml,
)
from service_runtime.controller import (  # noqa: E402
    ServiceRuntimeController,
    _execute_with_startup_alarm,
)
from service_runtime.document import parse_deployment_manifest  # noqa: E402
from service_runtime.execution_environment import (  # noqa: E402
    runtime_execution_environment,
)
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
schema = "model-lab.service.v1"
service_id = "fixture-service"
driver = "vllm-openai.v1"
runtime_id = "vllm-cu129-v0.25.1"

[model]
source = "huggingface"
repository = "example/Model-7B"
revision = "1111111111111111111111111111111111111111"
checkpoint = "model.safetensors"
weight_format = "native"

[endpoint]
input_modalities = ["text"]
reasoning = false
max_output_tokens = 4096

[compatibility]
minimum_compute_capability = "8.0"

[resources]
gpu_count = 1
gpu_memory_gib = 16
cpu_count = 4
memory_gib = 32
ephemeral_disk_gib = 20
claim_mode = "gpu-exclusive"

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
    "schema_version": "model-lab.runtime-selection.v1",
    "runtime_id": "vllm-cu129-v0.25.1",
    "image": "vllm/vllm-openai@sha256:" + "1" * 64,
    "manifest": {
        "path": "model-lab/runtimes/vllm-cu129/runtime-manifest.json",
        "remote_path": (
            "/root/runpod-session/control/runtime-verifier/runtime-manifest.json"
        ),
        "sha256": "2" * 64,
        "bytes": 456,
    },
    "verifier": {
        "path": "model-lab/runtimes/vllm-cu129/verify-runtime.py",
        "remote_path": (
            "/root/runpod-session/control/runtime-verifier/verify-runtime.py"
        ),
        "sha256": "5" * 64,
        "bytes": 789,
    },
}
GPU = {
    "name": "Fixture NVIDIA GPU",
    "compute_capability": [12, 0],
    "memory_mib": 97887,
    "driver_version": "580.126.09",
}
BOOT_ID = "11111111-2222-4333-8444-555555555555"
PROOF_BOOT_ID = "22222222-3333-4444-8555-666666666666"
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
        "schema_version": "model-lab.huggingface-closure-identity.v1",
        "source": source,
        "checkpoint": checkpoint_record,
        "files": [record],
    }
    return {
        "schema_version": "model-lab.huggingface-closure.v1",
        "source": source,
        "checkpoint": checkpoint_record,
        "files": [record],
        "file_count": 1,
        "total_bytes": 7,
        "closure_sha256": canonical_sha256(identity, newline=True),
    }


def recalculate_closure_identity(closure: dict[str, object]) -> None:
    closure["file_count"] = len(closure["files"])
    closure["total_bytes"] = sum(record["bytes"] for record in closure["files"])
    closure["closure_sha256"] = canonical_sha256(
        {
            "schema_version": "model-lab.huggingface-closure-identity.v1",
            "source": closure["source"],
            "checkpoint": closure["checkpoint"],
            "files": closure["files"],
        },
        newline=True,
    )


def deployment_manifest(
    *,
    checkpoint: str = "model.safetensors",
    load_format: str = "safetensors",
) -> dict[str, object]:
    definition = parse_service_toml(CONFIG)
    service = copy.deepcopy(definition.normalized_plan())
    service["model"]["checkpoint"] = checkpoint
    service["vllm"]["load_format"] = load_format
    closure = closure_for(checkpoint)
    paths = canonical_service_paths(
        service_id=service["service_id"],
        deployment_id="0" * 64,
        closure_sha256=closure["closure_sha256"],
    )
    port = 8000
    launch_sha256 = compile_affecting_sha256(service)
    bundle_sha256 = "4" * 64
    implementation_root = (
        "/root/runpod-session/control/model-service-runtime/" + bundle_sha256
    )
    manifest = {
        "schema_version": "model-lab.service-deployment-manifest.v1",
        "definition": {
            "source_sha256": hashlib.sha256(CONFIG).hexdigest(),
            "source_bytes": len(CONFIG),
            "service_plan_sha256": canonical_sha256(service, newline=True),
            "service": service,
        },
        "runtime": copy.deepcopy(RUNTIME),
        "huggingface_closure": closure,
        "implementation": {
            "implementation_id": "model-lab-service-runtime-v1",
            "bundle_sha256": bundle_sha256,
            "remote_root": implementation_root,
            "entrypoint": implementation_root + "/bin/model-lab-service-runtime",
            "receipt": {
                "remote_path": implementation_root + "/bundle.json",
                "bytes": 1234,
                "sha256": "6" * 64,
            },
        },
        "deployment": {
            "service_root": str(paths.service_root),
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
            "status": (
                "requires-runtime-execution-environment-and-observed-gpu"
            ),
            "contract_schema_version": "model-lab.vllm-compile-cache.v1",
            "inputs": {
                "driver": "vllm-openai.v1",
                "runtime": copy.deepcopy(RUNTIME),
                "runtime_execution_environment": None,
                "implementation_bundle_sha256": bundle_sha256,
                "huggingface_closure_sha256": closure["closure_sha256"],
                "compile_affecting_launch_sha256": launch_sha256,
            },
            "observed_gpu": None,
        },
    }
    deployment_id = canonical_sha256(
        {
            "schema_version": "model-lab.service-deployment-identity.v1",
            "manifest": manifest,
        },
        newline=True,
    )
    versioned_paths = canonical_service_paths(
        service_id=service["service_id"],
        deployment_id=deployment_id,
        closure_sha256=closure["closure_sha256"],
    )
    manifest["deployment"] = {
        **manifest["deployment"],
        "deployment_id": deployment_id,
        "manifest_path": str(versioned_paths.manifest),
    }
    return manifest


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
    def __init__(self, *, layout: RuntimeLayout | None = None) -> None:
        self.layout = layout
        self.processes: list[ProcessObservation] = []
        self.spawned_argv: tuple[str, ...] | None = None
        self.spawned_environment: dict[str, str] | None = None
        self.signals: list[signal.Signals] = []
        self.fail_process_identity = False
        self.ignore_term = False
        self.ignore_kill = False
        self.ready = True
        self.cache_behavior = "none"
        self.execution_environment_source: dict[str, str] = {}
        self.monotonic_ns_value = 1_000_000_000
        self.probe_count = 0
        self.ready_after_probe_count = 1
        self.spawn_count = 0
        self.boot_id_value = BOOT_ID
        self.spawn_escaped_process = False

    def monotonic_ns(self) -> int:
        return self.monotonic_ns_value

    def sleep(self, seconds: float) -> None:
        self.monotonic_ns_value += int(seconds * 1_000_000_000)

    def require_runtime_account(self) -> None:
        pass

    def boot_id(self, **_: object) -> str:
        return self.boot_id_value

    def process_nonce(self) -> str:
        return NONCE

    def observe_gpu(self) -> dict[str, object]:
        return copy.deepcopy(GPU)

    def execution_environment(self):
        return runtime_execution_environment(self.execution_environment_source)

    def verify_runtime(self, **_: object) -> dict[str, object]:
        return {
            "schema_version": "model-lab.upstream-runtime-verification.v1",
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
        log_path: pathlib.Path,
        **_: object,
    ) -> SpawnedProcess:
        self.spawn_count += 1
        self.spawned_argv = argv
        self.spawned_environment = environment_additions
        log_text = "fixture server ready\n"
        if self.cache_behavior in {"author", "author-missing-evidence"}:
            if self.layout is None:
                raise AssertionError("cache fixture requires a runtime layout")
            canonical_root = pathlib.PurePosixPath(
                environment_additions["VLLM_CACHE_ROOT"]
            ).parent
            local_root = self.layout.localize(canonical_root)
            artifact = local_root / "vllm" / "controller-aot.bin"
            artifact.write_bytes(b"controller-authored-aot")
            artifact.chmod(0o600)
            if self.cache_behavior == "author":
                log_text += (
                    "saved AOT compiled function to "
                    f"{canonical_root}/vllm/controller-aot.bin\n"
                )
        log_path.write_text(log_text, encoding="utf-8")
        log_path.chmod(0o600)
        observation = ProcessObservation(
            pid=4321,
            process_group_id=4321,
            session_id=4321,
            start_ticks=98765,
        )
        self.processes = [observation]
        if self.spawn_escaped_process:
            self.processes.append(
                ProcessObservation(
                    pid=5432,
                    process_group_id=5432,
                    session_id=5432,
                    start_ticks=87654,
                )
            )
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
        targets = {
            (process.pid, process.start_ticks)
            for process in _.get("processes", ())
        }
        if signal_number == signal.SIGTERM and not self.ignore_term:
            self.processes = [
                process
                for process in self.processes
                if (process.pid, process.start_ticks) not in targets
            ]
        if signal_number == signal.SIGKILL and not self.ignore_kill:
            self.processes = [
                process
                for process in self.processes
                if (process.pid, process.start_ticks) not in targets
            ]

    def wait_for_exit(self, **_: object) -> bool:
        targets = {
            (process.pid, process.start_ticks)
            for process in _.get("processes", ())
        }
        live = {
            (process.pid, process.start_ticks)
            for process in self.processes
        }
        return not (targets & live)

    def probe(self, **_: object) -> ProbeResult:
        self.probe_count += 1
        ready = self.ready and self.probe_count >= self.ready_after_probe_count
        return ProbeResult(
            ready=ready,
            health_status=200 if ready else None,
            models_status=200 if ready else None,
            served_model_ids=("fixture-service",) if ready else (),
            detail="ready" if ready else "not ready",
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
            deployment_id=self.manifest.deployment_id,
            closure_sha256=self.manifest.closure_sha256,
        )
        self.canonical = canonical
        self.local = local
        local["service_root"].mkdir(mode=0o700)
        (local["service_root"] / "deployments").mkdir(mode=0o700)
        local["manifest"].parent.mkdir(mode=0o700)
        write_private_json(local["manifest"], self.value)
        self._stage_snapshot()

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


class DeploymentDocumentTest(unittest.TestCase):
    def test_manifest_accepts_typed_launch_and_rejects_argv_escape(self):
        value = deployment_manifest()
        parsed = parse_deployment_manifest(json.dumps(value).encode("ascii"))
        self.assertEqual(parsed.service_id, "fixture-service")
        self.assertRegex(parsed.deployment_id, r"^[0-9a-f]{64}$")

        changed = copy.deepcopy(value)
        changed["deployment"]["launch"]["argv"].append("/bin/sh")
        with self.assertRaises(ModelLabError):
            parse_deployment_manifest(json.dumps(changed).encode("ascii"))

        changed = copy.deepcopy(value)
        changed["definition"]["source_sha256"] = "f" * 64
        with self.assertRaises(ModelLabError) as identity:
            parse_deployment_manifest(json.dumps(changed).encode("ascii"))
        self.assertIn("exact pre-path inputs", str(identity.exception))

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
        with self.assertRaises(ModelLabError):
            parse_deployment_manifest(json.dumps(rejected).encode("ascii"))

    def test_model_checkpoint_must_name_a_root_loader_file(self):
        value = deployment_manifest()
        value["definition"]["service"]["model"]["checkpoint"] = (
            "weights/model.safetensors"
        )

        with self.assertRaises(ModelLabError) as caught:
            parse_deployment_manifest(json.dumps(value).encode("ascii"))

        self.assertIn("root-level checkpoint", str(caught.exception))

    def test_closure_index_and_weights_must_remain_at_snapshot_root(self):
        for nested_kind in ("index", "weight"):
            with self.subTest(nested_kind=nested_kind):
                value = deployment_manifest()
                service = value["definition"]["service"]
                service["model"]["checkpoint"] = None
                value["definition"]["service_plan_sha256"] = canonical_sha256(
                    service,
                    newline=True,
                )
                closure = value["huggingface_closure"]
                closure["checkpoint"]["requested_selector"] = None
                if nested_kind == "index":
                    closure["checkpoint"]["resolved_index"] = (
                        "weights/model.safetensors.index.json"
                    )
                    closure["files"].append(
                        {
                            "path": "weights/model.safetensors.index.json",
                            "bytes": 2,
                            "role": "checkpoint-index",
                            "identity": {
                                "algorithm": "git-blob-sha1",
                                "digest": "8" * 40,
                            },
                        }
                    )
                    closure["files"].sort(key=lambda record: record["path"])
                else:
                    closure["checkpoint"]["weight_files"] = [
                        "weights/model.safetensors"
                    ]
                    closure["files"][0]["path"] = "weights/model.safetensors"
                recalculate_closure_identity(closure)

                with self.assertRaises(ModelLabError) as caught:
                    parse_deployment_manifest(json.dumps(value).encode("ascii"))

                self.assertIn("root-level checkpoint", str(caught.exception))

    def test_remote_parser_rejects_nonloader_snapshot_members(self):
        value = deployment_manifest()
        closure = value["huggingface_closure"]
        closure["files"].insert(
            0,
            {
                "path": "exports/model.onnx",
                "bytes": 100_000_000_000,
                "role": "snapshot",
                "identity": {
                    "algorithm": "sha256",
                    "digest": "8" * 64,
                },
            },
        )
        recalculate_closure_identity(closure)

        with self.assertRaises(ModelLabError) as caught:
            parse_deployment_manifest(json.dumps(value).encode("ascii"))

        self.assertIn("not an admitted vLLM loader asset", str(caught.exception))


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
        with self.assertRaises(ModelLabError):
            verify_snapshot_stage(
                closure=fixture.manifest.closure,
                canonical_snapshot_root=fixture.canonical.snapshot_root,
                local_snapshot_root=fixture.local["snapshot_root"],
                receipt_path=fixture.local["snapshot_receipt"],
                boot_id=BOOT_ID,
            )


class StartupDeadlineTest(unittest.TestCase):
    def test_alarm_restores_prior_handler_and_disarms_timer(self):
        controller = mock.Mock()
        controller.execute.return_value = {"status": "completed"}
        expiration = datetime.datetime.fromtimestamp(
            125.0,
            tz=datetime.timezone.utc,
        )
        prior_handler = object()

        with (
            mock.patch(
                "service_runtime.controller.time.time",
                return_value=100.0,
            ),
            mock.patch(
                "service_runtime.controller.signal.getsignal",
                return_value=prior_handler,
            ),
            mock.patch(
                "service_runtime.controller.signal.signal"
            ) as install_handler,
            mock.patch(
                "service_runtime.controller.signal.setitimer"
            ) as set_timer,
        ):
            result = _execute_with_startup_alarm(
                controller,
                action="setup",
                manifest_path=pathlib.Path("/fixture/manifest.json"),
                cache_mode="accepted",
                startup_expires_at=expiration,
            )

        self.assertEqual(result, {"status": "completed"})
        self.assertEqual(
            set_timer.call_args_list,
            [
                mock.call(signal.ITIMER_REAL, 25.0),
                mock.call(signal.ITIMER_REAL, 0),
            ],
        )
        self.assertEqual(install_handler.call_count, 2)
        self.assertEqual(
            install_handler.call_args_list[-1],
            mock.call(signal.SIGALRM, prior_handler),
        )

    def test_expired_child_group_cleanup_never_waits_past_deadline(self):
        class StubbornProcess:
            pid = 4242

            def __init__(self) -> None:
                self.wait_timeouts = []

            def wait(self, timeout=None):
                self.wait_timeouts.append(timeout)
                raise subprocess.TimeoutExpired(["fixture"], timeout)

        process = StubbornProcess()
        controller = ServiceRuntimeController(
            startup_expires_at=datetime.datetime.fromtimestamp(
                100.0,
                tz=datetime.timezone.utc,
            )
        )

        with (
            mock.patch(
                "service_runtime.controller.time.time",
                return_value=100.0,
            ),
            mock.patch(
                "service_runtime.controller.os.killpg"
            ) as kill_group,
            mock.patch.object(
                controller,
                "_background_reap",
            ) as background_reap,
        ):
            controller._reap_command_group(process)

        self.assertEqual(process.wait_timeouts, [0.0, 0])
        self.assertEqual(
            kill_group.call_args_list,
            [
                mock.call(4242, signal.SIGTERM),
                mock.call(4242, signal.SIGKILL),
            ],
        )
        background_reap.assert_called_once_with(process)


class ServiceRuntimeLifecycleTest(unittest.TestCase):
    def test_controller_accepts_forced_0777_untrusted_workspace_root(self):
        fixture = RuntimeFixture(self)
        fixture.workspace.chmod(0o777)
        controller = ServiceRuntimeController(
            layout=fixture.layout,
            platform=FixturePlatform(),
        )

        staged = controller.stage_snapshot(fixture.local["manifest"])

        self.assertEqual(staged["status"], "staged")
        self.assertEqual(
            stat.S_IMODE(fixture.workspace.stat().st_mode),
            0o777,
        )

    def test_controller_still_rejects_a_symlinked_workspace_root(self):
        fixture = RuntimeFixture(self)
        actual_workspace = fixture.workspace.with_name("actual-workspace")
        fixture.workspace.rename(actual_workspace)
        fixture.workspace.symlink_to(actual_workspace, target_is_directory=True)
        controller = ServiceRuntimeController(
            layout=fixture.layout,
            platform=FixturePlatform(),
        )

        with self.assertRaises(ModelLabError) as caught:
            controller.stage_snapshot(fixture.local["manifest"])

        self.assertEqual(
            caught.exception.code,
            "unsafe_service_runtime_state",
        )

    def test_controller_rejects_0777_private_session_state(self):
        fixture = RuntimeFixture(self)
        fixture.workspace.chmod(0o777)
        fixture.session.chmod(0o777)
        controller = ServiceRuntimeController(
            layout=fixture.layout,
            platform=FixturePlatform(),
        )

        with self.assertRaises(ModelLabError) as caught:
            controller.stage_snapshot(fixture.local["manifest"])

        self.assertEqual(
            caught.exception.code,
            "unsafe_service_runtime_state",
        )

    def test_setup_start_status_stop_uses_only_generated_manifest(self):
        fixture = RuntimeFixture(self)
        platform = FixturePlatform()
        controller = ServiceRuntimeController(
            layout=fixture.layout,
            platform=platform,
        )

        staged = controller.stage_snapshot(fixture.local["manifest"])
        prepared = controller.prepare_cache(
            fixture.local["manifest"],
            cache_mode="ephemeral",
        )
        setup = controller.setup(
            fixture.local["manifest"],
            cache_mode="ephemeral",
        )
        started = controller.start(
            fixture.local["manifest"],
            cache_mode="ephemeral",
        )
        status = controller.status(fixture.local["manifest"])
        stopped = controller.stop(fixture.local["manifest"])

        self.assertEqual(staged["action"], "stage-snapshot")
        self.assertEqual(
            staged["snapshot_stage"]["disposition"],
            "reused",
        )
        self.assertEqual(setup["status"], "ready-to-start")
        self.assertEqual(prepared["cache_mode"], "ephemeral")
        self.assertEqual(started["status"], "ready")
        self.assertEqual(started["startup_duration_ns"], 0)
        self.assertEqual(status["phase"], "ready")
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(
            stopped["compile_cache_publication"]["state"],
            "measured-ephemeral-cache-not-published",
        )
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
        controller.prepare_cache(
            fixture.local["manifest"],
            cache_mode="ephemeral",
        )
        controller.setup(
            fixture.local["manifest"],
            cache_mode="ephemeral",
        )

        with self.assertRaises(ModelLabError) as caught:
            controller.start(
                fixture.local["manifest"],
                cache_mode="ephemeral",
            )

        self.assertEqual(caught.exception.code, "ambiguous_failed_service_start")
        self.assertEqual(platform.signals, [signal.SIGTERM, signal.SIGKILL])
        self.assertFalse(os.path.lexists(fixture.local["process_state"]))

    def test_start_timeout_reaps_process_that_escaped_launch_group(self):
        fixture = RuntimeFixture(self)
        platform = FixturePlatform()
        platform.ready = False
        platform.spawn_escaped_process = True
        controller = ServiceRuntimeController(
            layout=fixture.layout,
            platform=platform,
        )
        controller.prepare_cache(
            fixture.local["manifest"],
            cache_mode="ephemeral",
        )
        controller.setup(
            fixture.local["manifest"],
            cache_mode="ephemeral",
        )

        with self.assertRaises(ModelLabError) as caught:
            controller.start(
                fixture.local["manifest"],
                cache_mode="ephemeral",
            )

        self.assertEqual(caught.exception.code, "service_start_timeout")
        self.assertEqual(
            platform.signals,
            [signal.SIGTERM, signal.SIGTERM],
        )
        self.assertEqual(platform.processes, [])
        self.assertFalse(os.path.lexists(fixture.local["process_state"]))

    def test_ready_author_audit_is_retryable_and_seals_candidate(self):
        fixture = RuntimeFixture(self)
        platform = FixturePlatform(layout=fixture.layout)
        platform.cache_behavior = "author-missing-evidence"
        controller = ServiceRuntimeController(
            layout=fixture.layout,
            platform=platform,
        )
        prepared = controller.prepare_cache(
            fixture.local["manifest"],
            cache_mode="author",
        )
        controller.setup(
            fixture.local["manifest"],
            cache_mode="author",
        )
        controller.start(
            fixture.local["manifest"],
            cache_mode="author",
        )
        controller.status(fixture.local["manifest"])

        with self.assertRaises(ModelLabError) as caught:
            controller.stop(fixture.local["manifest"])

        self.assertEqual(caught.exception.code, "service_stop_audit_failed")
        self.assertTrue(os.path.lexists(fixture.local["process_state"]))
        canonical_root = pathlib.PurePosixPath(prepared["compile_cache"]["local_root"])
        fixture.local["service_log"].write_text(
            "saved AOT compiled function to "
            f"{canonical_root}/vllm/controller-aot.bin\n",
            encoding="utf-8",
        )
        fixture.local["service_log"].chmod(0o600)

        stopped = controller.stop(fixture.local["manifest"])

        self.assertEqual(
            stopped["compile_cache_publication"]["state"],
            "candidate-requires-distinct-boot-proof",
        )
        self.assertFalse(os.path.lexists(fixture.local["process_state"]))

    def test_author_timeout_consumes_proof_attempt_and_blocks_warm_retry(self):
        fixture = RuntimeFixture(self)
        platform = FixturePlatform(layout=fixture.layout)
        platform.ready = False
        controller = ServiceRuntimeController(
            layout=fixture.layout,
            platform=platform,
        )
        prepared = controller.prepare_cache(
            fixture.local["manifest"],
            cache_mode="author",
        )
        controller.setup(
            fixture.local["manifest"],
            cache_mode="author",
        )
        with self.assertRaises(ModelLabError) as caught:
            controller.start(
                fixture.local["manifest"],
                cache_mode="author",
            )

        self.assertEqual(caught.exception.code, "service_start_timeout")
        self.assertEqual(platform.signals, [signal.SIGTERM])
        self.assertFalse(os.path.lexists(fixture.local["process_state"]))
        self.assertEqual(platform.monotonic_ns_value, 301_000_000_000)
        cache_root = fixture.layout.localize(
            pathlib.PurePosixPath(
                prepared["compile_cache"]["local_root"]
            )
        )
        proof_attempts = list(
            cache_root.parent.glob("*.proof-attempt.json")
        )
        self.assertEqual(len(proof_attempts), 1)

        platform.ready = True
        with self.assertRaises(ModelLabError) as retry_caught:
            controller.start(
                fixture.local["manifest"],
                cache_mode="author",
            )

        self.assertEqual(
            retry_caught.exception.code,
            "service_compile_cache_proof_consumed",
        )
        self.assertEqual(platform.spawn_count, 1)
        self.assertEqual(list(platform.processes), [])

    def test_candidate_proof_timeout_also_blocks_retry_on_that_pod(self):
        author_fixture = RuntimeFixture(self)
        author_platform = FixturePlatform(layout=author_fixture.layout)
        author_platform.cache_behavior = "author"
        author_controller = ServiceRuntimeController(
            layout=author_fixture.layout,
            platform=author_platform,
        )
        author_controller.prepare_cache(
            author_fixture.local["manifest"],
            cache_mode="author",
        )
        author_controller.setup(
            author_fixture.local["manifest"],
            cache_mode="author",
        )
        author_controller.start(
            author_fixture.local["manifest"],
            cache_mode="author",
        )
        authored = author_controller.stop(author_fixture.local["manifest"])
        self.assertEqual(
            authored["compile_cache_publication"]["state"],
            "candidate-requires-distinct-boot-proof",
        )

        proof_fixture = RuntimeFixture(self)
        proof_layout = RuntimeLayout(
            session_root=proof_fixture.session,
            workspace_root=author_fixture.workspace,
        )
        snapshot_receipt = json.loads(
            proof_fixture.local["snapshot_receipt"].read_text(
                encoding="utf-8"
            )
        )
        snapshot_receipt["boot_id"] = PROOF_BOOT_ID
        write_private_json(
            proof_fixture.local["snapshot_receipt"],
            snapshot_receipt,
        )
        proof_platform = FixturePlatform(layout=proof_layout)
        proof_platform.boot_id_value = PROOF_BOOT_ID
        proof_platform.ready = False
        proof_controller = ServiceRuntimeController(
            layout=proof_layout,
            platform=proof_platform,
        )
        proof_controller.prepare_cache(
            proof_fixture.local["manifest"],
            cache_mode="candidate-proof",
        )
        proof_controller.setup(
            proof_fixture.local["manifest"],
            cache_mode="candidate-proof",
        )

        with self.assertRaises(ModelLabError) as caught:
            proof_controller.start(
                proof_fixture.local["manifest"],
                cache_mode="candidate-proof",
            )

        self.assertEqual(caught.exception.code, "service_start_timeout")
        self.assertEqual(proof_platform.spawn_count, 1)
        self.assertFalse(
            os.path.lexists(proof_fixture.local["process_state"])
        )

        proof_platform.ready = True
        with self.assertRaises(ModelLabError) as retry_caught:
            proof_controller.start(
                proof_fixture.local["manifest"],
                cache_mode="candidate-proof",
            )

        self.assertEqual(
            retry_caught.exception.code,
            "service_compile_cache_proof_consumed",
        )
        self.assertEqual(proof_platform.spawn_count, 1)

    def test_start_owns_first_ready_time_independent_of_later_status(self):
        fixture = RuntimeFixture(self)
        platform = FixturePlatform(layout=fixture.layout)
        platform.ready_after_probe_count = 3
        controller = ServiceRuntimeController(
            layout=fixture.layout,
            platform=platform,
        )
        controller.prepare_cache(
            fixture.local["manifest"],
            cache_mode="ephemeral",
        )
        controller.setup(
            fixture.local["manifest"],
            cache_mode="ephemeral",
        )

        started = controller.start(
            fixture.local["manifest"],
            cache_mode="ephemeral",
        )
        receipt_path = pathlib.Path(started["ready_receipt"])
        receipt_before = receipt_path.read_bytes()
        platform.sleep(60 * 60)
        status = controller.status(fixture.local["manifest"])

        self.assertEqual(started["startup_duration_ns"], 2_000_000_000)
        self.assertEqual(platform.probe_count, 4)
        self.assertEqual(status["phase"], "ready")
        self.assertEqual(receipt_path.read_bytes(), receipt_before)

    def test_status_cannot_create_a_missing_start_readiness_receipt(self):
        fixture = RuntimeFixture(self)
        platform = FixturePlatform(layout=fixture.layout)
        controller = ServiceRuntimeController(
            layout=fixture.layout,
            platform=platform,
        )
        controller.prepare_cache(
            fixture.local["manifest"],
            cache_mode="ephemeral",
        )
        controller.setup(
            fixture.local["manifest"],
            cache_mode="ephemeral",
        )
        started = controller.start(
            fixture.local["manifest"],
            cache_mode="ephemeral",
        )
        receipt_path = pathlib.Path(started["ready_receipt"])
        receipt_path.rename(receipt_path.with_suffix(".withheld"))

        with self.assertRaises(ModelLabError) as caught:
            controller.status(fixture.local["manifest"])

        self.assertEqual(caught.exception.code, "unsafe_service_runtime_state")
        self.assertFalse(os.path.lexists(receipt_path))

    def test_start_rejects_a_remaining_huggingface_token_lease(self):
        fixture = RuntimeFixture(self)
        platform = FixturePlatform()
        controller = ServiceRuntimeController(
            layout=fixture.layout,
            platform=platform,
        )
        controller.prepare_cache(
            fixture.local["manifest"],
            cache_mode="ephemeral",
        )
        controller.setup(
            fixture.local["manifest"],
            cache_mode="ephemeral",
        )
        token = fixture.session / "secrets/huggingface/token"
        token.parent.mkdir(parents=True, mode=0o700)
        token.write_text("must-not-reach-vllm\n", encoding="ascii")
        token.chmod(0o600)

        with self.assertRaises(ModelLabError) as caught:
            controller.start(
                fixture.local["manifest"],
                cache_mode="ephemeral",
            )

        self.assertEqual(
            caught.exception.code,
            "huggingface_token_lease_present",
        )
        self.assertIsNone(platform.spawned_argv)
        self.assertFalse(os.path.lexists(fixture.local["process_state"]))

    def test_start_rejects_runtime_environment_drift(self):
        fixture = RuntimeFixture(self)
        platform = FixturePlatform()
        controller = ServiceRuntimeController(
            layout=fixture.layout,
            platform=platform,
        )
        controller.prepare_cache(
            fixture.local["manifest"],
            cache_mode="ephemeral",
        )
        controller.setup(
            fixture.local["manifest"],
            cache_mode="ephemeral",
        )
        platform.execution_environment_source = {
            "LD_LIBRARY_PATH": "/usr/local/nvidia/lib64"
        }

        with self.assertRaises(ModelLabError) as caught:
            controller.start(
                fixture.local["manifest"],
                cache_mode="ephemeral",
            )

        self.assertEqual(caught.exception.code, "service_setup_required")
        self.assertIsNone(platform.spawned_argv)


if __name__ == "__main__":
    unittest.main()
