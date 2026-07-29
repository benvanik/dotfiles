from __future__ import annotations

import dataclasses
import multiprocessing
import os
import pathlib
import socket
import subprocess
import tempfile
import threading
import unittest
from typing import Any

from model_lab.configuration import parse_lab_toml
from model_lab.controller import ModelLabController
from model_lab.errors import ModelLabError
from model_lab.lifecycle import Deployment, DeploymentStore
from model_lab.profile_binding import ProfileBindingStore
from model_lab.runpod_backend import (
    ClaimReleaseResult,
    HostClaim,
    HostClaimRequest,
)
from model_lab.service_definition import ServiceDefinition
from model_lab.supervisor import ModelLabSupervisor
from model_session.attachment import ServiceEndpoint
from model_session.service_endpoint import (
    publish_service_endpoint,
    revoke_service_endpoint,
    service_endpoint_socket_path,
)


DOTFILES_ROOT = pathlib.Path(__file__).resolve().parents[1]
MODEL_LAB = DOTFILES_ROOT / "bin" / "model-lab"


def _private_directory(path: pathlib.Path) -> pathlib.Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _write_private_text(path: pathlib.Path, payload: str) -> pathlib.Path:
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o600)
    return path


class _HostControl:
    def __init__(self, events: Any) -> None:
        self.events = events
        self.claim: HostClaim | None = None
        self.acquisition_count = 0

    def acquire(
        self,
        request: HostClaimRequest,
        *,
        startup_deadline: float,
        cleanup_deadline_factory=None,
    ) -> HostClaim:
        del startup_deadline, cleanup_deadline_factory
        if self.claim is None:
            self.acquisition_count += 1
            self.claim = HostClaim(
                host_name="fixture-host",
                claim_id="claim-fixture",
                generation=1,
                operation_id="host-operation-fixture",
                provider_resource_id="pod-fixture",
                profile_name="fixture-pro6000",
                remote_root="/root/runpod-session/claims/claim-fixture",
                endpoints={"openai": 18000},
                hard_expires_at="2099-01-01T00:00:00Z",
            )
            self.events.put(
                {
                    "event": "acquire",
                    "owner": request.owner_instance,
                    "operation_id": request.operation_id,
                }
            )
        return self.claim

    def wait_ready(
        self,
        claim: HostClaim,
        *,
        renewal_ttl_seconds: int,
        startup_deadline: float,
    ) -> HostClaim:
        del renewal_ttl_seconds, startup_deadline
        return self.get(claim.host_name, claim.claim_id)

    def find(self, request: HostClaimRequest) -> HostClaim | None:
        return self.claim

    def cancel(
        self,
        request: HostClaimRequest,
        *,
        cleanup_deadline: float | None = None,
    ) -> None:
        del cleanup_deadline
        claim = self.find(request)
        if claim is not None:
            self.release(
                claim.host_name,
                claim.claim_id,
                claim.generation,
                now=True,
            )

    def renew(
        self,
        host_name: str,
        claim_id: str,
        expected_generation: int,
        renewal_ttl_seconds: int,
        *,
        startup_deadline: float | None = None,
        cancel_event=None,
    ) -> HostClaim:
        if cancel_event is not None and cancel_event.is_set():
            raise ModelLabError(
                "controlled renewal cancellation",
                code="state_lock_cancelled",
            )
        claim = self.get(
            host_name,
            claim_id,
            startup_deadline=startup_deadline,
        )
        if claim.generation != expected_generation:
            raise AssertionError("fixture claim generation changed")
        self.claim = dataclasses.replace(
            claim,
            generation=claim.generation + 1,
        )
        return self.claim

    def release(
        self,
        host_name: str,
        claim_id: str,
        expected_generation: int,
        *,
        now: bool = False,
        cleanup_deadline: float | None = None,
    ) -> ClaimReleaseResult:
        del cleanup_deadline
        claim = self.get(host_name, claim_id)
        if claim.generation != expected_generation:
            raise AssertionError("fixture claim generation changed")
        self.events.put(
            {
                "event": "release",
                "host_name": host_name,
                "claim_id": claim_id,
                "now": now,
            }
        )
        self.claim = None
        return ClaimReleaseResult(
            host_name=host_name,
            claim_id=claim_id,
            released=True,
            final_claim=True,
            retirement="terminated" if now else "grace",
            empty_deadline=None if now else "2099-01-01T00:05:00Z",
        )

    def get(
        self,
        host_name: str,
        claim_id: str,
        *,
        startup_deadline: float | None = None,
    ) -> HostClaim:
        del startup_deadline
        if (
            self.claim is None
            or self.claim.host_name != host_name
            or self.claim.claim_id != claim_id
        ):
            raise AssertionError("fixture claim is absent")
        return self.claim

    def list(self, host_name: str | None = None) -> tuple[HostClaim, ...]:
        if self.claim is None:
            return ()
        if host_name is not None and host_name != self.claim.host_name:
            return ()
        return (self.claim,)

    def status(self, host_name: str) -> dict[str, Any]:
        return {"host_name": host_name, "active": self.claim is not None}

    def enforce_retirement(self, *, execute: bool) -> dict[str, Any]:
        return {"executed": execute, "actions": []}


class _ServiceRuntime:
    def __init__(
        self,
        *,
        runtime_root: pathlib.Path,
        events: Any,
    ) -> None:
        self.runtime_root = runtime_root
        self.events = events
        self.listener: socket.socket | None = None
        self.endpoint: ServiceEndpoint | None = None
        self.ensure_count = 0
        self.attest_count = 0

    def ensure_ready(
        self,
        service: ServiceDefinition,
        claim: HostClaim,
        *,
        deployment_id: str,
        startup_deadline: float,
        cleanup_budget=None,
    ) -> ServiceEndpoint:
        del startup_deadline, cleanup_budget
        if self.endpoint is not None:
            raise AssertionError("fixture service was prepared twice")
        _private_directory(self.runtime_root / "services")
        socket_path = service_endpoint_socket_path(
            service.service_id,
            runtime_root=self.runtime_root,
        )
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(os.fspath(socket_path))
        socket_path.chmod(0o600)
        listener.listen(8)
        self.listener = listener
        self.endpoint = publish_service_endpoint(
            service.service_id,
            service_sha256=service.service_sha256,
            workload=service.service_workload(),
            input_modalities=service.endpoint.input_modalities,
            ttl_seconds=3600,
            socket_path=socket_path,
            runtime_root=self.runtime_root,
        )
        self.ensure_count += 1
        self.events.put(
            {
                "event": "service-ready",
                "deployment_id": deployment_id,
                "claim_id": claim.claim_id,
            }
        )
        return self.endpoint

    def attest_ready(
        self,
        service: ServiceDefinition,
        claim: HostClaim,
        deployment: Deployment,
        *,
        startup_deadline: float | None = None,
    ) -> ServiceEndpoint:
        del startup_deadline
        if self.endpoint is None:
            raise AssertionError("fixture endpoint is absent")
        self.attest_count += 1
        self.events.put(
            {
                "event": "service-reused",
                "deployment_id": deployment.deployment_id,
                "claim_id": claim.claim_id,
            }
        )
        return self.endpoint

    def stop(
        self,
        service: ServiceDefinition,
        claim: HostClaim,
        deployment: Deployment,
        *,
        cleanup_deadline: float | None = None,
    ) -> None:
        del cleanup_deadline
        if self.endpoint is not None:
            revoke_service_endpoint(
                service.service_id,
                self.endpoint.publication_id,
                runtime_root=self.runtime_root,
            )
            self.endpoint = None
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        socket_path = service_endpoint_socket_path(
            service.service_id,
            runtime_root=self.runtime_root,
        )
        socket_path.unlink(missing_ok=True)
        self.events.put(
            {
                "event": "service-stopped",
                "deployment_id": deployment.deployment_id,
                "claim_id": claim.claim_id,
            }
        )

    def cleanup_lost_claim(
        self,
        service: ServiceDefinition,
        deployment: Deployment,
        *,
        cleanup_deadline: float | None = None,
    ) -> None:
        del cleanup_deadline
        if self.endpoint is not None:
            revoke_service_endpoint(
                service.service_id,
                self.endpoint.publication_id,
                runtime_root=self.runtime_root,
            )
            self.endpoint = None
        if self.listener is not None:
            self.listener.close()
            self.listener = None


def _supervisor_process(
    authored_root: pathlib.Path,
    state_root: pathlib.Path,
    runtime_root: pathlib.Path,
    control: Any,
    events: Any,
) -> None:
    try:
        lab = parse_lab_toml((authored_root / "lab.toml").read_bytes())
        hosts = _HostControl(events)
        runtime = _ServiceRuntime(
            runtime_root=runtime_root,
            events=events,
        )
        controller = ModelLabController(
            hosts=hosts,
            runtime=runtime,
            deployments=DeploymentStore(state_root),
            bindings=ProfileBindingStore(authored_root),
            lab=lab,
        )
        supervisor = ModelLabSupervisor(
            controller=controller,
            authored_root=authored_root,
            state_root=state_root,
            runtime_root=runtime_root,
            maintenance_interval_seconds=3600,
        )
        failures: list[BaseException] = []

        def serve() -> None:
            try:
                supervisor.serve_forever()
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=serve)
        thread.start()
        supervisor.ready_event.wait()
        if failures or not thread.is_alive():
            raise AssertionError(f"fixture supervisor failed: {failures!r}")
        control.send({"ready": True})
        if control.recv() != {"stop": True}:
            raise AssertionError("fixture supervisor received an invalid command")
        supervisor.stop()
        thread.join()
        if failures:
            raise AssertionError(f"fixture supervisor failed: {failures!r}")
        control.send(
            {
                "stopped": True,
                "host_acquisitions": hosts.acquisition_count,
                "service_preparations": runtime.ensure_count,
                "service_reuses": runtime.attest_count,
            }
        )
    except BaseException as error:
        try:
            control.send(
                {
                    "error": type(error).__name__,
                    "message": str(error),
                }
            )
        finally:
            raise


class ModelLabEndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="model-lab-e2e.",
            dir=os.environ.get("TEST_TMPDIR", "/tmp"),
        )
        self.root_temporary = pathlib.Path(self.temporary.name)
        self.authored_root = _private_directory(self.root_temporary / "model-lab")
        self.state_root = _private_directory(self.root_temporary / "model-lab-state")
        self.xdg_runtime_root = _private_directory(self.root_temporary / "runtime")
        self.runtime_root = self.xdg_runtime_root / "model-lab"
        self._write_authored_fixture()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_authored_fixture(self) -> None:
        _write_private_text(
            self.authored_root / "lab.toml",
            """\
schema = "model-lab.v1"
allowed_runpod_profiles = ["fixture-pro6000"]

[lease]
hard_ttl_seconds = 600
service_idle_ttl_seconds = 60
renewal_ttl_seconds = 120
minimum_useful_seconds = 60
startup_timeout_seconds = 300
""",
        )
        services = _private_directory(self.authored_root / "services")
        _write_private_text(
            services / "fixture-service.toml",
            """\
schema = "model-lab.service.v1"
service_id = "fixture-service"
driver = "vllm-openai.v1"
runtime_id = "fixture-runtime-v1"

[model]
source = "huggingface"
repository = "fixture/model"
revision = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
checkpoint = "model.safetensors"
weight_format = "bf16"

[endpoint]
input_modalities = ["text"]
reasoning = false
max_output_tokens = 8192

[compatibility]
minimum_compute_capability = "8.0"

[resources]
gpu_count = 1
gpu_memory_gib = 32
cpu_count = 8
memory_gib = 32
ephemeral_disk_gib = 20
claim_mode = "gpu-exclusive"

[vllm]
model_implementation = "vllm"
dtype = "bfloat16"
quantization = "none"
tensor_parallel_size = 1
max_model_len = 65536
max_num_sequences = 1
max_num_batched_tokens = 8192
kv_cache_dtype = "bfloat16"
gpu_memory_utilization = 0.90
chunked_prefill = true
load_format = "safetensors"
safetensors_load_strategy = "lazy"
language_model_only = true
mamba_cache_mode = "none"
prefix_caching = false
reasoning_parser = "none"
tool_call_parser = "none"
speculative_method = "none"
speculative_tokens = 0
generation_config = "auto"
""",
        )
        profile = _private_directory(self.authored_root / "profiles" / "chat")
        _write_private_text(
            profile / "profile.toml",
            """\
schema = "model-session.profile.v3"
profile_id = "chat"
project_id = "fixture-project"
service_id = "fixture-service"

[endpoint]
required_input_modalities = ["text"]

[pi]
version = "0.82.1"
tools = ["read", "write", "edit", "bash"]
system_prompt_file = "SYSTEM.md"

[storage]
max_sessions = 8
work_bytes = 8589934592
work_inodes = 65536
history_bytes = 2147483648
history_inodes = 16384
checkpoint_bytes = 18253611008
max_file_bytes = 4294967296
max_logical_bytes = 17179869184

[sandbox]
memory_bytes = 17179869184
max_tasks = 256
max_runtime_seconds = 86400
idle_timeout_seconds = 3600
shutdown_grace_seconds = 30
""",
        )
        _write_private_text(
            profile / "AGENTS.md",
            "Provider-free end-to-end fixture.\n",
        )
        _write_private_text(
            profile / "SYSTEM.md",
            "Reply tersely.\n",
        )
        _private_directory(self.authored_root / "projects" / "fixture-project")
        pi_root = _private_directory(self.authored_root / "runtimes" / "pi" / "0.82.1")
        pi_bin = _private_directory(pi_root / "bin")
        pi = pi_bin / "pi"
        _write_private_text(
            pi,
            """#!/bin/sh
if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then
  printf '%s\n' '0.82.1'
  exit 0
fi
[ "$MODEL_SESSION_BASE_URL" = "http://127.0.0.1:41111/v1" ] || exit 31
[ "$MODEL_SESSION_INFERENCE_SOCKET" = \
"/run/model-session/inference.sock" ] || exit 32
[ -r /workspace/AGENTS.md ] || exit 33
printf '%s\n' "$@" > /workspace/pi-argv
""",
        )
        pi.chmod(0o700)
        node = pi_bin / "node"
        _write_private_text(node, "#!/bin/sh\nprintf '%s\\n' 'v24.11.1'\n")
        node.chmod(0o700)

    def _run_model_lab(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "XDG_RUNTIME_DIR": os.fspath(self.xdg_runtime_root),
        }
        return subprocess.run(
            [
                os.fspath(MODEL_LAB),
                "--root",
                os.fspath(self.authored_root),
                "--state-root",
                os.fspath(self.state_root),
                *arguments,
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )

    def test_new_then_resume_reuses_service_and_now_releases_final_claim(
        self,
    ) -> None:
        context = multiprocessing.get_context("fork")
        parent_control, child_control = context.Pipe()
        events = context.Queue()
        process: multiprocessing.Process | None = None
        stop_requested = False
        previous_runtime = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = os.fspath(self.xdg_runtime_root)
        try:
            process = context.Process(
                target=_supervisor_process,
                args=(
                    self.authored_root,
                    self.state_root,
                    self.runtime_root,
                    child_control,
                    events,
                ),
            )
            process.start()
            ready = parent_control.recv()
            self.assertEqual(ready, {"ready": True}, ready)

            first = self._run_model_lab("pi", "chat")
            self.assertEqual(first.returncode, 0, first.stderr)
            sessions = tuple(
                sorted((self.authored_root / "sessions" / "chat").iterdir())
            )
            self.assertEqual(len(sessions), 1)
            session_id = sessions[0].name
            self.assertTrue((sessions[0] / "workspace" / "pi-argv").is_file())

            resumed = self._run_model_lab(
                "pi",
                "chat",
                "resume",
                session_id,
                "--now",
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)

            observed = []
            while not any(item["event"] == "release" for item in observed):
                observed.append(events.get())
            release = next(item for item in observed if item["event"] == "release")
            self.assertTrue(release["now"])
            self.assertEqual(
                [item["event"] for item in observed].count("acquire"),
                1,
                observed,
            )
            self.assertEqual(
                [item["event"] for item in observed].count("service-ready"),
                1,
                observed,
            )
            self.assertIn("service-reused", [item["event"] for item in observed])
            self.assertIn("service-stopped", [item["event"] for item in observed])

            parent_control.send({"stop": True})
            stop_requested = True
            summary = parent_control.recv()
            self.assertEqual(
                summary,
                {
                    "stopped": True,
                    "host_acquisitions": 1,
                    "service_preparations": 1,
                    "service_reuses": 1,
                },
            )
            process.join()
            self.assertEqual(process.exitcode, 0)
        finally:
            if previous_runtime is None:
                os.environ.pop("XDG_RUNTIME_DIR", None)
            else:
                os.environ["XDG_RUNTIME_DIR"] = previous_runtime
            if process is not None and process.is_alive():
                if not stop_requested:
                    try:
                        parent_control.send({"stop": True})
                    except (BrokenPipeError, EOFError, OSError):
                        pass
                process.join(timeout=5)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
