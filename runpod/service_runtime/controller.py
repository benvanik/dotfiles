"""Inspectable setup/start/status/stop operations for one deployment manifest."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import sys
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from runpod_local.errors import RunpodLocalError

from .collaborators import (
    CompileCacheStage,
    compile_cache_contract,
    verify_compile_cache_stage,
)
from .document import DeploymentManifest, load_deployment_manifest
from .layout import (
    REMOTE_SERVICES_ROOT,
    REMOTE_SESSION_ROOT,
    REMOTE_SNAPSHOTS_ROOT,
    RuntimeLayout,
)
from .platform import ProcessObservation, SystemPlatform
from .snapshot_stage import SnapshotStage, verify_snapshot_stage
from .state import (
    SETUP_RECEIPT_SCHEMA,
    ProcessState,
    atomic_write_private_json,
    ensure_private_directory,
    lifecycle_lock,
    open_advisory_lock,
    read_private_json,
    read_process_state,
    remove_private_file,
    require_owned_directory,
)
from .vllm import (
    FORBIDDEN_INHERITED_ENVIRONMENT,
    build_vllm_argv,
    build_vllm_environment,
)


TERM_GRACE_SECONDS = 30.0
KILL_GRACE_SECONDS = 10.0
_SETUP_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "service_id",
        "service_plan_sha256",
        "manifest_sha256",
        "boot_id",
        "runtime",
        "runtime_verification",
        "observed_gpu",
        "snapshot_stage",
        "compile_cache_contract",
        "compile_cache_stage",
    }
)


def _fail(message: str, *, code: str) -> None:
    raise RunpodLocalError(message, code=code)


@dataclass(frozen=True)
class RuntimeCollaborators:
    """Injectable content-stage boundaries; production implementations are fixed."""

    snapshot_verifier: Callable[..., SnapshotStage] = verify_snapshot_stage
    cache_contract_builder: Callable[..., dict[str, Any]] = compile_cache_contract
    cache_verifier: Callable[..., CompileCacheStage] = verify_compile_cache_stage


class ServiceRuntimeController:
    """Own one service instance's process and generated receipts."""

    def __init__(
        self,
        *,
        layout: RuntimeLayout | None = None,
        platform: Any | None = None,
        collaborators: RuntimeCollaborators | None = None,
        invoked_entrypoint: pathlib.Path | None = None,
    ) -> None:
        self.layout = layout or RuntimeLayout()
        self.platform = platform or SystemPlatform()
        self.collaborators = collaborators or RuntimeCollaborators()
        self.invoked_entrypoint = invoked_entrypoint

    def _load(
        self,
        manifest_path: pathlib.Path,
    ) -> tuple[DeploymentManifest, Any, dict[str, pathlib.Path]]:
        self.platform.require_runtime_account()
        manifest = load_deployment_manifest(manifest_path)
        canonical_paths, local_paths = self.layout.service_paths(
            service_id=manifest.service_id,
            closure_sha256=manifest.closure_sha256,
        )
        expected_manifest_path = local_paths["manifest"]
        if manifest_path != expected_manifest_path:
            _fail(
                "manifest argument does not name the deployment-owned path",
                code="invalid_service_deployment_path",
            )
        if manifest.value["deployment"]["manifest_path"] != str(
            canonical_paths.manifest
        ):
            _fail(
                "manifest path binding changed after validation",
                code="invalid_service_deployment_path",
            )
        if self.invoked_entrypoint is not None:
            canonical_entrypoint = pathlib.PurePosixPath(
                manifest.value["implementation"]["entrypoint"]
            )
            expected_entrypoint = self.layout.localize(canonical_entrypoint)
            if self.invoked_entrypoint != expected_entrypoint:
                _fail(
                    "runtime entrypoint does not match the content-bound bundle",
                    code="invalid_service_runtime_implementation",
                )
        return manifest, canonical_paths, local_paths

    def _require_roots(
        self,
        *,
        local_paths: dict[str, pathlib.Path],
    ) -> None:
        ensure_private_directory(self.layout.session_root, create=False)
        ensure_private_directory(
            self.layout.localize(REMOTE_SERVICES_ROOT),
            create=False,
        )
        ensure_private_directory(
            self.layout.localize(REMOTE_SNAPSHOTS_ROOT),
            create=False,
        )
        ensure_private_directory(local_paths["service_root"], create=False)
        require_owned_directory(self.layout.workspace_root)

    def _validate_gpu_compatibility(
        self,
        *,
        manifest: DeploymentManifest,
        observed_gpu: dict[str, Any],
    ) -> None:
        observed = observed_gpu.get("compute_capability")
        minimum = manifest.service["compatibility"]["minimum_compute_capability"]
        if (
            not isinstance(observed, list)
            or len(observed) != 2
            or tuple(observed) < tuple(minimum)
        ):
            _fail(
                "observed GPU does not satisfy minimum compute capability",
                code="incompatible_service_gpu",
            )
        if manifest.service["vllm"]["tensor_parallel_size"] != 1:
            _fail(
                "this single-GPU runtime slice cannot satisfy tensor parallelism",
                code="unsupported_service_gpu_topology",
            )

    def _verify_stages(
        self,
        *,
        manifest: DeploymentManifest,
        canonical_paths: Any,
        local_paths: dict[str, pathlib.Path],
        boot_id: str,
        observed_gpu: dict[str, Any],
    ) -> tuple[SnapshotStage, dict[str, Any], CompileCacheStage]:
        snapshot = self.collaborators.snapshot_verifier(
            closure=manifest.closure,
            canonical_snapshot_root=canonical_paths.snapshot_root,
            local_snapshot_root=local_paths["snapshot_root"],
            receipt_path=local_paths["snapshot_receipt"],
            boot_id=boot_id,
        )
        contract = self.collaborators.cache_contract_builder(
            manifest=manifest,
            observed_gpu=observed_gpu,
        )
        cache = self.collaborators.cache_verifier(
            contract=contract,
            layout=self.layout,
            boot_id=boot_id,
        )
        return snapshot, contract, cache

    def _setup_receipt(
        self,
        *,
        manifest: DeploymentManifest,
        boot_id: str,
        runtime_verification: dict[str, Any],
        observed_gpu: dict[str, Any],
        snapshot: SnapshotStage,
        contract: dict[str, Any],
        cache: CompileCacheStage,
    ) -> dict[str, Any]:
        return {
            "schema_version": SETUP_RECEIPT_SCHEMA,
            "service_id": manifest.service_id,
            "service_plan_sha256": manifest.service_plan_sha256,
            "manifest_sha256": manifest.manifest_sha256,
            "boot_id": boot_id,
            "runtime": manifest.runtime,
            "runtime_verification": runtime_verification,
            "observed_gpu": observed_gpu,
            "snapshot_stage": snapshot.summary(),
            "compile_cache_contract": contract,
            "compile_cache_stage": cache.summary(),
        }

    def _read_setup_receipt(
        self,
        *,
        path: pathlib.Path,
        manifest: DeploymentManifest,
        boot_id: str,
    ) -> dict[str, Any]:
        try:
            receipt, _ = read_private_json(path)
        except RunpodLocalError as error:
            raise RunpodLocalError(
                "service setup receipt is absent or unsafe; run setup first",
                code="service_setup_required",
            ) from error
        if (
            set(receipt) != _SETUP_RECEIPT_FIELDS
            or receipt["schema_version"] != SETUP_RECEIPT_SCHEMA
            or receipt["service_id"] != manifest.service_id
            or receipt["service_plan_sha256"] != manifest.service_plan_sha256
            or receipt["manifest_sha256"] != manifest.manifest_sha256
            or receipt["boot_id"] != boot_id
            or receipt["runtime"] != manifest.runtime
        ):
            _fail(
                "service setup receipt does not match this deployment and boot",
                code="service_setup_required",
            )
        return receipt

    def _ensure_no_process_state(
        self,
        *,
        manifest: DeploymentManifest,
        state_path: pathlib.Path,
    ) -> None:
        if os.path.lexists(state_path):
            state = read_process_state(state_path)
            if self.platform.process_is_owned(state):
                _fail(
                    f"service is already running as PID {state.pid}",
                    code="service_already_running",
                )
            _fail(
                "service process state exists without its exact live process; "
                "run stop to audit it",
                code="ambiguous_service_process_state",
            )
        processes = self.platform.list_service_processes(service_id=manifest.service_id)
        if processes:
            _fail(
                "service process exists without process state",
                code="ambiguous_service_process_state",
            )

    def setup(self, manifest_path: pathlib.Path) -> dict[str, Any]:
        manifest, canonical_paths, local_paths = self._load(manifest_path)
        self._require_roots(local_paths=local_paths)
        with lifecycle_lock(
            local_paths["lifecycle_lock"],
            create=True,
            exclusive=True,
        ):
            with open_advisory_lock(
                local_paths["serving_lock"],
                create=True,
            ) as serving_lock:
                if not serving_lock.exclusive(nonblocking=True):
                    _fail(
                        "a serving process still holds the service lease",
                        code="service_already_running",
                    )
                self._ensure_no_process_state(
                    manifest=manifest,
                    state_path=local_paths["process_state"],
                )
                boot_id = self.platform.boot_id()
                observed_gpu = self.platform.observe_gpu()
                self._validate_gpu_compatibility(
                    manifest=manifest,
                    observed_gpu=observed_gpu,
                )
                runtime_verification = self.platform.verify_runtime(
                    expected_runtime=manifest.runtime,
                    layout=self.layout,
                )
                runtime_gpu = runtime_verification["gpu"]
                if (
                    runtime_gpu["name"] != observed_gpu["name"]
                    or runtime_gpu["capability"] != observed_gpu["compute_capability"]
                ):
                    _fail(
                        "runtime verifier and NVIDIA inventory disagree",
                        code="runtime_verification_failed",
                    )
                snapshot, contract, cache = self._verify_stages(
                    manifest=manifest,
                    canonical_paths=canonical_paths,
                    local_paths=local_paths,
                    boot_id=boot_id,
                    observed_gpu=observed_gpu,
                )
                receipt = self._setup_receipt(
                    manifest=manifest,
                    boot_id=boot_id,
                    runtime_verification=runtime_verification,
                    observed_gpu=observed_gpu,
                    snapshot=snapshot,
                    contract=contract,
                    cache=cache,
                )
                atomic_write_private_json(local_paths["setup_receipt"], receipt)
        return {
            "schema_version": "runpod.inference-service-operation.v1",
            "action": "setup",
            "service_id": manifest.service_id,
            "manifest_sha256": manifest.manifest_sha256,
            "status": "ready-to-start",
            "observed_gpu": observed_gpu,
            "snapshot_stage": snapshot.summary(),
            "compile_cache_stage": cache.summary(),
        }

    def start(self, manifest_path: pathlib.Path) -> dict[str, Any]:
        manifest, canonical_paths, local_paths = self._load(manifest_path)
        self._require_roots(local_paths=local_paths)
        spawned: ProcessObservation | None = None
        state_written = False
        with lifecycle_lock(
            local_paths["lifecycle_lock"],
            create=False,
            exclusive=True,
        ):
            with open_advisory_lock(
                local_paths["serving_lock"],
                create=False,
            ) as serving_lock:
                if not serving_lock.exclusive(nonblocking=True):
                    _fail(
                        "a serving process already holds the service lease",
                        code="service_already_running",
                    )
                self._ensure_no_process_state(
                    manifest=manifest,
                    state_path=local_paths["process_state"],
                )
                boot_id = self.platform.boot_id()
                setup_receipt = self._read_setup_receipt(
                    path=local_paths["setup_receipt"],
                    manifest=manifest,
                    boot_id=boot_id,
                )
                observed_gpu = self.platform.observe_gpu()
                self._validate_gpu_compatibility(
                    manifest=manifest,
                    observed_gpu=observed_gpu,
                )
                if observed_gpu != setup_receipt["observed_gpu"]:
                    _fail(
                        "observed GPU changed after setup",
                        code="service_setup_required",
                    )
                snapshot, contract, cache = self._verify_stages(
                    manifest=manifest,
                    canonical_paths=canonical_paths,
                    local_paths=local_paths,
                    boot_id=boot_id,
                    observed_gpu=observed_gpu,
                )
                if (
                    snapshot.summary() != setup_receipt["snapshot_stage"]
                    or contract != setup_receipt["compile_cache_contract"]
                    or cache.summary() != setup_receipt["compile_cache_stage"]
                ):
                    _fail(
                        "a staged prerequisite changed after setup",
                        code="service_setup_required",
                    )
                nonce = self.platform.process_nonce()
                environment = build_vllm_environment(
                    session_root=REMOTE_SESSION_ROOT,
                    compile_root=pathlib.PurePosixPath(contract["local_root"]),
                    service_id=manifest.service_id,
                    process_nonce=nonce,
                    manifest_sha256=manifest.manifest_sha256,
                )
                if any(name in environment for name in FORBIDDEN_INHERITED_ENVIRONMENT):
                    _fail(
                        "typed vLLM environment contains a forbidden secret path",
                        code="invalid_service_launch_environment",
                    )
                if not serving_lock.shared():
                    _fail(
                        "cannot establish serving process lease",
                        code="service_start_failed",
                    )
                try:
                    result = self.platform.spawn(
                        argv=build_vllm_argv(
                            manifest.service,
                            snapshot_root=canonical_paths.snapshot_root,
                            port=manifest.port,
                        ),
                        environment_additions=environment,
                        log_path=local_paths["service_log"],
                        serving_lease_descriptor=serving_lock.descriptor,
                    )
                    spawned = result.observation
                    state = ProcessState(
                        service_id=manifest.service_id,
                        service_plan_sha256=manifest.service_plan_sha256,
                        manifest_sha256=manifest.manifest_sha256,
                        boot_id=boot_id,
                        pid=result.pid,
                        process_nonce=nonce,
                        process_start_ticks=result.observation.start_ticks,
                        compile_cache_id=contract["cache_id"],
                    )
                    if not self.platform.process_is_owned(state):
                        _fail(
                            "started process does not match its typed identity",
                            code="service_start_failed",
                        )
                    atomic_write_private_json(
                        local_paths["process_state"],
                        state.normalized(),
                    )
                    state_written = True
                except BaseException as start_error:
                    if spawned is not None and not state_written:
                        self.platform.signal_processes(
                            processes=[spawned],
                            signal_number=signal.SIGTERM,
                        )
                        exited = self.platform.wait_for_exit(
                            processes=[spawned],
                            timeout_seconds=KILL_GRACE_SECONDS,
                        )
                        if not exited:
                            self.platform.signal_processes(
                                processes=[spawned],
                                signal_number=signal.SIGKILL,
                            )
                            exited = self.platform.wait_for_exit(
                                processes=[spawned],
                                timeout_seconds=KILL_GRACE_SECONDS,
                            )
                        remaining = self.platform.list_service_processes(
                            service_id=manifest.service_id
                        )
                        if not exited or remaining:
                            raise RunpodLocalError(
                                "failed start left a service process whose "
                                "ownership cannot be safely committed",
                                code="ambiguous_failed_service_start",
                            ) from start_error
                    raise
        return {
            "schema_version": "runpod.inference-service-operation.v1",
            "action": "start",
            "service_id": manifest.service_id,
            "manifest_sha256": manifest.manifest_sha256,
            "status": "starting",
            "pid": state.pid,
            "process_nonce": state.process_nonce,
            "compile_cache_id": state.compile_cache_id,
            "endpoint": {
                "host": "127.0.0.1",
                "port": manifest.port,
                "authentication": "controller-owned-local-nonsecret",
            },
            "log_path": str(canonical_paths.service_log),
        }

    def _validate_process_state(
        self,
        *,
        state: ProcessState,
        manifest: DeploymentManifest,
        boot_id: str,
    ) -> None:
        if (
            state.service_id != manifest.service_id
            or state.service_plan_sha256 != manifest.service_plan_sha256
            or state.manifest_sha256 != manifest.manifest_sha256
            or state.boot_id != boot_id
        ):
            _fail(
                "service process state does not match this deployment and boot",
                code="ambiguous_service_process_state",
            )

    def status(self, manifest_path: pathlib.Path) -> dict[str, Any]:
        manifest, canonical_paths, local_paths = self._load(manifest_path)
        self._require_roots(local_paths=local_paths)
        if not os.path.lexists(local_paths["lifecycle_lock"]):
            processes = self.platform.list_service_processes(
                service_id=manifest.service_id
            )
            if processes:
                _fail(
                    "service processes exist before lifecycle setup",
                    code="ambiguous_service_process_state",
                )
            return {
                "schema_version": "runpod.inference-service-status.v1",
                "service_id": manifest.service_id,
                "manifest_sha256": manifest.manifest_sha256,
                "phase": "unconfigured",
                "ready": False,
            }
        with lifecycle_lock(
            local_paths["lifecycle_lock"],
            create=False,
            exclusive=False,
        ):
            if not os.path.lexists(local_paths["process_state"]):
                processes = self.platform.list_service_processes(
                    service_id=manifest.service_id
                )
                if processes:
                    _fail(
                        "service processes exist without process state",
                        code="ambiguous_service_process_state",
                    )
                return {
                    "schema_version": "runpod.inference-service-status.v1",
                    "service_id": manifest.service_id,
                    "manifest_sha256": manifest.manifest_sha256,
                    "phase": "stopped",
                    "ready": False,
                    "setup": os.path.lexists(local_paths["setup_receipt"]),
                }
            state = read_process_state(local_paths["process_state"])
            self._validate_process_state(
                state=state,
                manifest=manifest,
                boot_id=self.platform.boot_id(),
            )
            if not self.platform.process_is_owned(state):
                return {
                    "schema_version": "runpod.inference-service-status.v1",
                    "service_id": manifest.service_id,
                    "manifest_sha256": manifest.manifest_sha256,
                    "phase": "exited",
                    "ready": False,
                    "pid": state.pid,
                    "process_state_requires_stop": True,
                    "log_path": str(canonical_paths.service_log),
                }
            probe = self.platform.probe(
                port=manifest.port,
                expected_service_id=manifest.service_id,
            )
            return {
                "schema_version": "runpod.inference-service-status.v1",
                "service_id": manifest.service_id,
                "manifest_sha256": manifest.manifest_sha256,
                "phase": "ready" if probe.ready else "starting",
                "ready": probe.ready,
                "pid": state.pid,
                "compile_cache_id": state.compile_cache_id,
                "probe": {
                    "health_status": probe.health_status,
                    "models_status": probe.models_status,
                    "served_model_ids": list(probe.served_model_ids),
                    "detail": probe.detail,
                },
                "endpoint": {
                    "host": "127.0.0.1",
                    "port": manifest.port,
                    "authentication": "controller-owned-local-nonsecret",
                },
                "log_path": str(canonical_paths.service_log),
            }

    @staticmethod
    def _process_keys(
        processes: Sequence[ProcessObservation],
    ) -> set[tuple[int, int]]:
        return {(item.pid, item.start_ticks) for item in processes}

    def stop(self, manifest_path: pathlib.Path) -> dict[str, Any]:
        manifest, canonical_paths, local_paths = self._load(manifest_path)
        self._require_roots(local_paths=local_paths)
        if not os.path.lexists(local_paths["lifecycle_lock"]):
            processes = self.platform.list_service_processes(
                service_id=manifest.service_id
            )
            if processes:
                _fail(
                    "service processes exist before lifecycle setup",
                    code="ambiguous_service_process_state",
                )
            return {
                "schema_version": "runpod.inference-service-operation.v1",
                "action": "stop",
                "service_id": manifest.service_id,
                "manifest_sha256": manifest.manifest_sha256,
                "status": "already-stopped",
            }
        with lifecycle_lock(
            local_paths["lifecycle_lock"],
            create=False,
            exclusive=True,
        ):
            if not os.path.lexists(local_paths["process_state"]):
                processes = self.platform.list_service_processes(
                    service_id=manifest.service_id
                )
                if processes:
                    _fail(
                        "service processes exist without process state",
                        code="ambiguous_service_process_state",
                    )
                with open_advisory_lock(
                    local_paths["serving_lock"],
                    create=False,
                ) as serving_lock:
                    if not serving_lock.exclusive(nonblocking=True):
                        _fail(
                            "service lease remains without process state",
                            code="ambiguous_service_process_state",
                        )
                return {
                    "schema_version": "runpod.inference-service-operation.v1",
                    "action": "stop",
                    "service_id": manifest.service_id,
                    "manifest_sha256": manifest.manifest_sha256,
                    "status": "already-stopped",
                }
            state = read_process_state(local_paths["process_state"])
            boot_id = self.platform.boot_id()
            if state.boot_id != boot_id:
                processes = self.platform.list_service_processes(
                    service_id=manifest.service_id
                )
                if processes:
                    _fail(
                        "old-boot process state conflicts with live processes",
                        code="ambiguous_service_process_state",
                    )
                remove_private_file(local_paths["process_state"])
                return {
                    "schema_version": "runpod.inference-service-operation.v1",
                    "action": "stop",
                    "service_id": manifest.service_id,
                    "manifest_sha256": manifest.manifest_sha256,
                    "status": "stopped",
                    "stale_boot_state_removed": True,
                }
            self._validate_process_state(
                state=state,
                manifest=manifest,
                boot_id=boot_id,
            )
            all_processes = self.platform.list_service_processes(
                service_id=manifest.service_id
            )
            owned_processes = self.platform.list_service_processes(
                service_id=manifest.service_id,
                process_nonce=state.process_nonce,
                manifest_sha256=state.manifest_sha256,
            )
            if not self._process_keys(all_processes).issubset(
                self._process_keys(owned_processes)
            ):
                _fail(
                    "service ID has processes outside the recorded ownership",
                    code="ambiguous_service_process_state",
                )
            if owned_processes:
                self.platform.signal_processes(
                    processes=owned_processes,
                    signal_number=signal.SIGTERM,
                )
                self.platform.wait_for_exit(
                    processes=owned_processes,
                    timeout_seconds=TERM_GRACE_SECONDS,
                )
                remaining = self.platform.list_service_processes(
                    service_id=manifest.service_id,
                    process_nonce=state.process_nonce,
                    manifest_sha256=state.manifest_sha256,
                )
                if remaining:
                    self.platform.signal_processes(
                        processes=remaining,
                        signal_number=signal.SIGKILL,
                    )
                    self.platform.wait_for_exit(
                        processes=remaining,
                        timeout_seconds=KILL_GRACE_SECONDS,
                    )
            remaining = self.platform.list_service_processes(
                service_id=manifest.service_id
            )
            if remaining:
                _fail(
                    "owned service processes did not stop",
                    code="service_stop_failed",
                )
            with open_advisory_lock(
                local_paths["serving_lock"],
                create=False,
            ) as serving_lock:
                if not serving_lock.exclusive(nonblocking=True):
                    _fail(
                        "a serving child retained the service lease",
                        code="service_stop_failed",
                    )
            setup_receipt = self._read_setup_receipt(
                path=local_paths["setup_receipt"],
                manifest=manifest,
                boot_id=boot_id,
            )
            audit_error: RunpodLocalError | None = None
            try:
                observed_gpu = self.platform.observe_gpu()
                snapshot, contract, cache = self._verify_stages(
                    manifest=manifest,
                    canonical_paths=canonical_paths,
                    local_paths=local_paths,
                    boot_id=boot_id,
                    observed_gpu=observed_gpu,
                )
                if (
                    snapshot.summary() != setup_receipt["snapshot_stage"]
                    or contract != setup_receipt["compile_cache_contract"]
                    or cache.summary() != setup_receipt["compile_cache_stage"]
                ):
                    _fail(
                        "staged prerequisite changed while the service ran",
                        code="service_stop_audit_failed",
                    )
            except RunpodLocalError as error:
                audit_error = error
            remove_private_file(local_paths["process_state"])
            if audit_error is not None:
                raise RunpodLocalError(
                    f"service stopped, but staged prerequisite audit failed: "
                    f"{audit_error}",
                    code="service_stop_audit_failed",
                ) from audit_error
        return {
            "schema_version": "runpod.inference-service-operation.v1",
            "action": "stop",
            "service_id": manifest.service_id,
            "manifest_sha256": manifest.manifest_sha256,
            "status": "stopped",
            "compile_cache_stage_receipt": "still-bound",
            "compile_cache_publication": "external-collaborator",
        }

    def execute(
        self,
        *,
        action: str,
        manifest_path: pathlib.Path,
    ) -> dict[str, Any]:
        operations = {
            "setup": self.setup,
            "start": self.start,
            "status": self.status,
            "stop": self.stop,
        }
        try:
            operation = operations[action]
        except KeyError as error:
            raise RunpodLocalError(
                f"unsupported service runtime action: {action}",
                code="invalid_service_runtime_action",
            ) from error
        return operation(manifest_path)


def _absolute_normalized_path(value: str) -> pathlib.Path:
    path = pathlib.Path(value)
    if (
        not path.is_absolute()
        or str(path) != os.path.normpath(str(path))
        or "\x00" in value
    ):
        raise argparse.ArgumentTypeError("manifest must be an absolute normalized path")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="runpod-service-runtime",
        description=("Operate one generated RunPod inference-service deployment."),
    )
    parser.add_argument(
        "action",
        choices=("setup", "start", "status", "stop"),
    )
    parser.add_argument(
        "--manifest",
        required=True,
        type=_absolute_normalized_path,
        help="absolute deployment.json path",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    invoked_entrypoint: pathlib.Path | None = None,
) -> int:
    arguments = build_parser().parse_args(argv)
    controller = ServiceRuntimeController(
        invoked_entrypoint=invoked_entrypoint,
    )
    try:
        result = controller.execute(
            action=arguments.action,
            manifest_path=arguments.manifest,
        )
    except RunpodLocalError as error:
        print(
            json.dumps(
                {
                    "schema_version": "runpod.inference-service-error.v1",
                    "error": error.code,
                    "message": str(error),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0
