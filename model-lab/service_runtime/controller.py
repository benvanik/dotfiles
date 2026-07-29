"""Inspectable model-service operations for one deployment manifest."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from model_lab.errors import ModelLabError

from .collaborators import compile_cache_contract
from .compile_cache_archive import load_persistent_compile_cache
from .compile_cache_document import (
    COMPILE_CACHE_LAUNCH_MEASUREMENT_SCHEMA,
    MAX_CANDIDATE_READY_DURATION_NS,
    CompileCacheMode,
    artifact_records,
    load_measurement,
)
from .compile_cache_files import (
    inventory_compile_cache,
)
from .compile_cache_stage import (
    CompileCachePrerequisite,
    accept_compile_cache_candidate,
    load_compile_cache_prerequisite,
    prepare_compile_cache,
    seal_compile_cache_candidate,
    stage_compile_cache,
)
from .document import DeploymentManifest, load_deployment_manifest
from .execution_environment import validate_runtime_execution_environment
from .layout import (
    REMOTE_SERVICES_ROOT,
    REMOTE_SESSION_ROOT,
    REMOTE_SNAPSHOTS_ROOT,
    RuntimeLayout,
)
from .platform import ProcessObservation, SystemPlatform
from .snapshot_stage import SnapshotStage, verify_snapshot_stage
from .snapshot_stager import (
    SnapshotStagePublication,
    stage_huggingface_snapshot,
)
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
    require_owned_untrusted_directory,
    write_private_json_once,
)
from .vllm import (
    FORBIDDEN_INHERITED_ENVIRONMENT,
    build_vllm_argv,
    build_vllm_environment,
    read_vllm_cache_evidence,
)


TERM_GRACE_SECONDS = 30.0
KILL_GRACE_SECONDS = 10.0
READY_POLL_SECONDS = 1.0
READY_TIMEOUT_NS = MAX_CANDIDATE_READY_DURATION_NS
READY_RECEIPT_SCHEMA = "model-lab.service-ready.v1"
PROOF_ATTEMPT_SCHEMA = "model-lab.service-cache-proof-attempt.v1"
ONE_ATTEMPT_CACHE_MODES = frozenset({"author", "candidate-proof"})
HUGGINGFACE_TOKEN_LEASE = REMOTE_SESSION_ROOT / "secrets" / "huggingface" / "token"
_SETUP_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "service_id",
        "service_plan_sha256",
        "manifest_sha256",
        "boot_id",
        "runtime",
        "runtime_execution_environment",
        "runtime_verification",
        "observed_gpu",
        "snapshot_stage",
        "compile_cache_contract",
        "compile_cache_prerequisite",
    }
)


def _fail(message: str, *, code: str) -> None:
    raise ModelLabError(message, code=code)


def _explicit_cache_mode(value: str | None) -> CompileCacheMode:
    if value not in {"ephemeral", "author", "candidate-proof", "accepted"}:
        _fail(
            "this action requires an explicit supported cache mode",
            code="service_cache_mode_required",
        )
    return value


def _launch_receipt_path(
    *,
    service_root: pathlib.Path,
    kind: str,
    process_nonce: str,
) -> pathlib.Path:
    """Name one launch receipt without trusting a nonce as a path component."""

    nonce_sha256 = hashlib.sha256(process_nonce.encode("utf-8")).hexdigest()
    return service_root / f"{kind}-{nonce_sha256}.json"


def _proof_attempt_path(
    *,
    local_cache_root: pathlib.Path,
    cache_id: str,
) -> pathlib.Path:
    """Name the one attempt allowed to mutate one proof cache on this Pod."""

    if (
        len(cache_id) != 64
        or any(character not in "0123456789abcdef" for character in cache_id)
    ):
        _fail(
            "compile-cache proof attempt has an invalid cache identity",
            code="unsafe_service_runtime_state",
        )
    if local_cache_root.name != cache_id:
        _fail(
            "compile-cache proof root does not match its cache identity",
            code="unsafe_service_runtime_state",
        )
    return local_cache_root.parent / f"{cache_id}.proof-attempt.json"


def _probe_document(probe: Any) -> dict[str, Any]:
    return {
        "health_status": probe.health_status,
        "models_status": probe.models_status,
        "served_model_ids": list(probe.served_model_ids),
        "detail": probe.detail,
    }


@dataclass(frozen=True)
class RuntimeCollaborators:
    """Injectable content-stage boundaries; production implementations are fixed."""

    snapshot_stager: Callable[..., SnapshotStagePublication] = (
        stage_huggingface_snapshot
    )
    snapshot_verifier: Callable[..., SnapshotStage] = verify_snapshot_stage
    cache_contract_builder: Callable[..., dict[str, Any]] = compile_cache_contract
    cache_prerequisite_loader: Callable[..., CompileCachePrerequisite] = (
        load_compile_cache_prerequisite
    )


class ServiceRuntimeController:
    """Own one service instance's process and generated receipts."""

    def __init__(
        self,
        *,
        layout: RuntimeLayout | None = None,
        platform: Any | None = None,
        collaborators: RuntimeCollaborators | None = None,
        invoked_entrypoint: pathlib.Path | None = None,
        startup_expires_at: datetime.datetime | None = None,
    ) -> None:
        self.layout = layout or RuntimeLayout()
        self.platform = platform or SystemPlatform()
        self.collaborators = collaborators or RuntimeCollaborators()
        self.invoked_entrypoint = invoked_entrypoint
        self.startup_expires_at = startup_expires_at

    def _remaining_startup_seconds(self) -> float | None:
        if self.startup_expires_at is None:
            return None
        remaining = self.startup_expires_at.timestamp() - time.time()
        if remaining <= 0:
            _fail(
                "remote service action exceeded its startup deadline",
                code="service_startup_timeout",
            )
        return remaining

    def _cleanup_grace_seconds(self) -> float:
        if self.startup_expires_at is None:
            return float(KILL_GRACE_SECONDS)
        return min(
            float(KILL_GRACE_SECONDS),
            max(
                0.0,
                self.startup_expires_at.timestamp() - time.time(),
            ),
        )

    @staticmethod
    def _background_reap(process: Any) -> None:
        def reap() -> None:
            try:
                process.wait()
            except BaseException:
                return

        threading.Thread(target=reap, daemon=True).start()

    def _reap_command_group(self, process: Any) -> None:
        grace_seconds = self._cleanup_grace_seconds()
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=grace_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=0)
        except subprocess.TimeoutExpired:
            self._background_reap(process)

    def _run_startup_bounded_command(
        self,
        command: Sequence[str],
        **keywords: Any,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run one child group whose lifetime cannot outlive startup."""

        check = keywords.pop("check", False)
        if keywords:
            allowed = {"cwd", "env", "stdin", "stdout", "stderr", "umask"}
            unexpected = set(keywords).difference(allowed)
            if unexpected:
                _fail(
                    "bounded runtime command received unsupported options",
                    code="invalid_service_runtime_command",
                )
        remaining = self._remaining_startup_seconds()
        process = subprocess.Popen(
            command,
            start_new_session=True,
            **keywords,
        )
        try:
            return_code = process.wait(timeout=remaining)
        except BaseException:
            self._reap_command_group(process)
            raise
        result = subprocess.CompletedProcess(
            command,
            return_code,
        )
        if check and return_code != 0:
            raise subprocess.CalledProcessError(
                return_code,
                command,
            )
        return result

    def _load(
        self,
        manifest_path: pathlib.Path,
    ) -> tuple[DeploymentManifest, Any, dict[str, pathlib.Path]]:
        self.platform.require_runtime_account()
        manifest = load_deployment_manifest(manifest_path)
        canonical_paths, local_paths = self.layout.service_paths(
            service_id=manifest.service_id,
            deployment_id=manifest.deployment_id,
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
        require_owned_untrusted_directory(self.layout.workspace_root)

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

    def _verify_prerequisites(
        self,
        *,
        manifest: DeploymentManifest,
        canonical_paths: Any,
        local_paths: dict[str, pathlib.Path],
        boot_id: str,
        observed_gpu: dict[str, Any],
        runtime_execution_environment: dict[str, Any],
        cache_mode: CompileCacheMode,
        verify_cache_inventory: bool = True,
    ) -> tuple[SnapshotStage, dict[str, Any], CompileCachePrerequisite]:
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
            runtime_execution_environment=runtime_execution_environment,
        )
        cache = self.collaborators.cache_prerequisite_loader(
            contract=contract,
            layout=self.layout,
            boot_id=boot_id,
            expected_mode=cache_mode,
            verify_inventory=verify_cache_inventory,
        )
        if cache.mode != cache_mode:
            _fail(
                "explicit cache mode does not match its typed prerequisite",
                code="service_cache_mode_mismatch",
            )
        return snapshot, contract, cache

    def _setup_receipt(
        self,
        *,
        manifest: DeploymentManifest,
        boot_id: str,
        runtime_verification: dict[str, Any],
        runtime_execution_environment: dict[str, Any],
        observed_gpu: dict[str, Any],
        snapshot: SnapshotStage,
        contract: dict[str, Any],
        cache: CompileCachePrerequisite,
    ) -> dict[str, Any]:
        return {
            "schema_version": SETUP_RECEIPT_SCHEMA,
            "service_id": manifest.service_id,
            "service_plan_sha256": manifest.service_plan_sha256,
            "manifest_sha256": manifest.manifest_sha256,
            "boot_id": boot_id,
            "runtime": manifest.runtime,
            "runtime_execution_environment": runtime_execution_environment,
            "runtime_verification": runtime_verification,
            "observed_gpu": observed_gpu,
            "snapshot_stage": snapshot.summary(),
            "compile_cache_contract": contract,
            "compile_cache_prerequisite": cache.summary(),
        }

    def _read_setup_receipt(
        self,
        *,
        path: pathlib.Path,
        manifest: DeploymentManifest,
        boot_id: str,
        cache_mode: CompileCacheMode,
    ) -> dict[str, Any]:
        try:
            receipt, _ = read_private_json(path)
        except ModelLabError as error:
            raise ModelLabError(
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
            or receipt["compile_cache_prerequisite"].get("mode") != cache_mode
        ):
            _fail(
                "service setup receipt does not match this deployment and boot",
                code="service_setup_required",
            )
        validate_runtime_execution_environment(receipt["runtime_execution_environment"])
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

    def setup(
        self,
        manifest_path: pathlib.Path,
        *,
        cache_mode: str | None,
    ) -> dict[str, Any]:
        explicit_mode = _explicit_cache_mode(cache_mode)
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
                boot_id = self.platform.boot_id(layout=self.layout)
                observed_gpu = self.platform.observe_gpu()
                self._validate_gpu_compatibility(
                    manifest=manifest,
                    observed_gpu=observed_gpu,
                )
                execution_environment = self.platform.execution_environment()
                runtime_verification = self.platform.verify_runtime(
                    expected_runtime=manifest.runtime,
                    layout=self.layout,
                    execution_environment=execution_environment,
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
                snapshot, contract, cache = self._verify_prerequisites(
                    manifest=manifest,
                    canonical_paths=canonical_paths,
                    local_paths=local_paths,
                    boot_id=boot_id,
                    observed_gpu=observed_gpu,
                    runtime_execution_environment=(execution_environment.normalized()),
                    cache_mode=explicit_mode,
                )
                receipt = self._setup_receipt(
                    manifest=manifest,
                    boot_id=boot_id,
                    runtime_verification=runtime_verification,
                    runtime_execution_environment=(execution_environment.normalized()),
                    observed_gpu=observed_gpu,
                    snapshot=snapshot,
                    contract=contract,
                    cache=cache,
                )
                atomic_write_private_json(local_paths["setup_receipt"], receipt)
        return {
            "schema_version": "model-lab.service-operation.v1",
            "action": "setup",
            "service_id": manifest.service_id,
            "manifest_sha256": manifest.manifest_sha256,
            "status": "ready-to-start",
            "observed_gpu": observed_gpu,
            "snapshot_stage": snapshot.summary(),
            "compile_cache_prerequisite": cache.summary(),
        }

    def stage_snapshot(self, manifest_path: pathlib.Path) -> dict[str, Any]:
        manifest, canonical_paths, local_paths = self._load(manifest_path)
        self._require_roots(local_paths=local_paths)
        stage_arguments: dict[str, Any] = {}
        if self.startup_expires_at is not None:
            stage_arguments["command_runner"] = (
                self._run_startup_bounded_command
            )
        publication = self.collaborators.snapshot_stager(
            closure=manifest.closure,
            canonical_snapshot_root=canonical_paths.snapshot_root,
            local_snapshot_root=local_paths["snapshot_root"],
            receipt_path=local_paths["snapshot_receipt"],
            layout=self.layout,
            boot_id=self.platform.boot_id(layout=self.layout),
            **stage_arguments,
        )
        return {
            "schema_version": "model-lab.service-operation.v1",
            "action": "stage-snapshot",
            "service_id": manifest.service_id,
            "manifest_sha256": manifest.manifest_sha256,
            "status": "staged",
            "snapshot_stage": publication.summary(),
        }

    def prepare_cache(
        self,
        manifest_path: pathlib.Path,
        *,
        cache_mode: str | None,
    ) -> dict[str, Any]:
        explicit_mode = _explicit_cache_mode(cache_mode)
        manifest, _, local_paths = self._load(manifest_path)
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
                boot_id = self.platform.boot_id(layout=self.layout)
                observed_gpu = self.platform.observe_gpu()
                self._validate_gpu_compatibility(
                    manifest=manifest,
                    observed_gpu=observed_gpu,
                )
                execution_environment = self.platform.execution_environment()
                contract = self.collaborators.cache_contract_builder(
                    manifest=manifest,
                    observed_gpu=observed_gpu,
                    runtime_execution_environment=(execution_environment.normalized()),
                )
                if explicit_mode in {"ephemeral", "author"}:
                    cache = prepare_compile_cache(
                        contract=contract,
                        layout=self.layout,
                        boot_id=boot_id,
                        mode=explicit_mode,
                    )
                else:
                    cache = stage_compile_cache(
                        contract=contract,
                        layout=self.layout,
                        boot_id=boot_id,
                        source=explicit_mode,
                    )
        return {
            "schema_version": "model-lab.service-operation.v1",
            "action": "prepare-cache",
            "service_id": manifest.service_id,
            "manifest_sha256": manifest.manifest_sha256,
            "cache_mode": explicit_mode,
            "status": "prepared",
            "compile_cache": cache,
        }

    def cache_status(
        self,
        manifest_path: pathlib.Path,
    ) -> dict[str, Any]:
        """Inspect the exact persistent generation without staging it."""

        manifest, _, local_paths = self._load(manifest_path)
        self._require_roots(local_paths=local_paths)
        observed_gpu = self.platform.observe_gpu()
        self._validate_gpu_compatibility(
            manifest=manifest,
            observed_gpu=observed_gpu,
        )
        execution_environment = self.platform.execution_environment()
        contract = self.collaborators.cache_contract_builder(
            manifest=manifest,
            observed_gpu=observed_gpu,
            runtime_execution_environment=(
                execution_environment.normalized()
            ),
        )
        persistent_root = self.layout.localize(
            pathlib.PurePosixPath(contract["persistent_root"])
        )
        if not os.path.lexists(persistent_root):
            state = "absent"
        else:
            state = load_persistent_compile_cache(
                contract=contract,
                layout=self.layout,
                require_accepted=False,
                verify_bundle_content=False,
            ).state
        return {
            "schema_version": "model-lab.service-cache-status.v1",
            "service_id": manifest.service_id,
            "manifest_sha256": manifest.manifest_sha256,
            "state": state,
            "cache_id": contract["cache_id"],
        }

    def _reap_failed_start_processes(
        self,
        *,
        manifest: DeploymentManifest,
        process_nonce: str,
        spawned: ProcessObservation | None,
    ) -> bool:
        """Stop the launch group plus every escaped process with its exact tags."""

        grace_seconds = self._cleanup_grace_seconds()
        initial_exited = True
        if spawned is not None:
            self.platform.signal_processes(
                processes=[spawned],
                signal_number=signal.SIGTERM,
            )
            initial_exited = self.platform.wait_for_exit(
                processes=[spawned],
                timeout_seconds=grace_seconds,
            )

        tagged = self.platform.list_service_processes(
            service_id=manifest.service_id,
            process_nonce=process_nonce,
            manifest_sha256=manifest.manifest_sha256,
        )
        initial_key = (
            None
            if spawned is None
            else (spawned.pid, spawned.start_ticks)
        )
        escaped = [
            process
            for process in tagged
            if (process.pid, process.start_ticks) != initial_key
        ]
        if escaped:
            self.platform.signal_processes(
                processes=escaped,
                signal_number=signal.SIGTERM,
            )
            self.platform.wait_for_exit(
                processes=escaped,
                timeout_seconds=grace_seconds,
            )

        remaining = self.platform.list_service_processes(
            service_id=manifest.service_id,
            process_nonce=process_nonce,
            manifest_sha256=manifest.manifest_sha256,
        )
        initial_remains = any(
            (process.pid, process.start_ticks) == initial_key
            for process in remaining
        )
        if spawned is not None and (not initial_exited or initial_remains):
            self.platform.signal_processes(
                processes=[spawned],
                signal_number=signal.SIGKILL,
            )
            initial_exited = self.platform.wait_for_exit(
                processes=[spawned],
                timeout_seconds=grace_seconds,
            )
        escaped_remaining = [
            process
            for process in remaining
            if (process.pid, process.start_ticks) != initial_key
        ]
        if escaped_remaining:
            self.platform.signal_processes(
                processes=escaped_remaining,
                signal_number=signal.SIGKILL,
            )
            self.platform.wait_for_exit(
                processes=escaped_remaining,
                timeout_seconds=grace_seconds,
            )

        survivors = self.platform.list_service_processes(
            service_id=manifest.service_id,
            process_nonce=process_nonce,
            manifest_sha256=manifest.manifest_sha256,
        )
        all_service_processes = self.platform.list_service_processes(
            service_id=manifest.service_id,
        )
        return (
            initial_exited
            and not survivors
            and not all_service_processes
        )

    def start(
        self,
        manifest_path: pathlib.Path,
        *,
        cache_mode: str | None,
    ) -> dict[str, Any]:
        explicit_mode = _explicit_cache_mode(cache_mode)
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
                boot_id = self.platform.boot_id(layout=self.layout)
                setup_receipt = self._read_setup_receipt(
                    path=local_paths["setup_receipt"],
                    manifest=manifest,
                    boot_id=boot_id,
                    cache_mode=explicit_mode,
                )
                execution_environment = self.platform.execution_environment()
                if (
                    execution_environment.normalized()
                    != setup_receipt["runtime_execution_environment"]
                ):
                    _fail(
                        "runtime execution environment changed after setup",
                        code="service_setup_required",
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
                snapshot, contract, cache = self._verify_prerequisites(
                    manifest=manifest,
                    canonical_paths=canonical_paths,
                    local_paths=local_paths,
                    boot_id=boot_id,
                    observed_gpu=observed_gpu,
                    runtime_execution_environment=(execution_environment.normalized()),
                    cache_mode=explicit_mode,
                )
                if (
                    snapshot.summary() != setup_receipt["snapshot_stage"]
                    or contract != setup_receipt["compile_cache_contract"]
                    or cache.summary() != setup_receipt["compile_cache_prerequisite"]
                ):
                    _fail(
                        "a staged prerequisite changed after setup",
                        code="service_setup_required",
                    )
                proof_attempt_path: pathlib.Path | None = None
                if explicit_mode in ONE_ATTEMPT_CACHE_MODES:
                    proof_attempt_path = _proof_attempt_path(
                        local_cache_root=cache.local_root,
                        cache_id=contract["cache_id"],
                    )
                    if os.path.lexists(proof_attempt_path):
                        _fail(
                            "author and candidate-proof cache prerequisites are "
                            "single-attempt; this cache was already exposed to a "
                            "service process on this Pod",
                            code="service_compile_cache_proof_consumed",
                        )
                token_lease = self.layout.localize(HUGGINGFACE_TOKEN_LEASE)
                if os.path.lexists(token_lease):
                    _fail(
                        "Hugging Face token lease remains present after model staging",
                        code="huggingface_token_lease_present",
                    )
                nonce = self.platform.process_nonce()
                environment = build_vllm_environment(
                    session_root=REMOTE_SESSION_ROOT,
                    compile_root=pathlib.PurePosixPath(contract["local_root"]),
                    service_id=manifest.service_id,
                    process_nonce=nonce,
                    manifest_sha256=manifest.manifest_sha256,
                    cache_mode=explicit_mode,
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
                    started_monotonic_ns = self.platform.monotonic_ns()
                    if proof_attempt_path is not None:
                        proof_attempt = {
                            "schema_version": PROOF_ATTEMPT_SCHEMA,
                            "service_id": manifest.service_id,
                            "service_plan_sha256": manifest.service_plan_sha256,
                            "manifest_sha256": manifest.manifest_sha256,
                            "boot_id": boot_id,
                            "compile_cache_id": contract["cache_id"],
                            "compile_cache_mode": explicit_mode,
                            "compile_cache_prerequisite_sha256": (
                                cache.receipt_sha256
                            ),
                            "process_nonce": nonce,
                            "started_monotonic_ns": started_monotonic_ns,
                        }
                        write_private_json_once(
                            proof_attempt_path,
                            proof_attempt,
                        )
                        recorded_attempt, _ = read_private_json(
                            proof_attempt_path,
                            maximum_bytes=64 * 1024,
                        )
                        if recorded_attempt != proof_attempt:
                            _fail(
                                "compile-cache proof attempt receipt changed "
                                "before process spawn",
                                code="unsafe_service_runtime_state",
                            )
                    result = self.platform.spawn(
                        argv=build_vllm_argv(
                            manifest.service,
                            snapshot_root=canonical_paths.snapshot_root,
                            port=manifest.port,
                        ),
                        environment_additions=environment,
                        execution_environment=execution_environment,
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
                        compile_cache_mode=explicit_mode,
                        compile_cache_prerequisite_sha256=(cache.receipt_sha256),
                        started_monotonic_ns=started_monotonic_ns,
                        runtime_execution_environment=(
                            execution_environment.normalized()
                        ),
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
                    ready_deadline_ns = started_monotonic_ns + READY_TIMEOUT_NS
                    while True:
                        if not self.platform.process_is_owned(state):
                            _fail(
                                "started service exited before becoming ready",
                                code="service_start_failed",
                            )
                        probe = self.platform.probe(
                            port=manifest.port,
                            expected_service_id=manifest.service_id,
                        )
                        observed_monotonic_ns = self.platform.monotonic_ns()
                        if probe.ready and observed_monotonic_ns <= ready_deadline_ns:
                            ready = self._ready_receipt(
                                state=state,
                                local_paths=local_paths,
                                probe=probe,
                                observed_monotonic_ns=observed_monotonic_ns,
                            )
                            if ready is None:
                                _fail(
                                    "successful readiness probe produced no receipt",
                                    code="unsafe_service_runtime_state",
                                )
                            break
                        if observed_monotonic_ns >= ready_deadline_ns:
                            _fail(
                                "service did not become ready within five minutes; "
                                f"last probe: {probe.detail}",
                                code="service_start_timeout",
                            )
                        remaining_seconds = (
                            ready_deadline_ns - observed_monotonic_ns
                        ) / 1_000_000_000
                        self.platform.sleep(min(READY_POLL_SECONDS, remaining_seconds))
                except BaseException as start_error:
                    try:
                        reaped = self._reap_failed_start_processes(
                            manifest=manifest,
                            process_nonce=nonce,
                            spawned=spawned,
                        )
                    except BaseException as cleanup_error:
                        raise ModelLabError(
                            "failed start could not audit its owned processes",
                            code="ambiguous_failed_service_start",
                        ) from cleanup_error
                    if not reaped:
                        raise ModelLabError(
                            "failed start left a service process whose "
                            "ownership cannot be safely committed",
                            code="ambiguous_failed_service_start",
                        ) from start_error
                    if state_written:
                        remove_private_file(local_paths["process_state"])
                    raise
        return {
            "schema_version": "model-lab.service-operation.v1",
            "action": "start",
            "service_id": manifest.service_id,
            "manifest_sha256": manifest.manifest_sha256,
            "status": "ready",
            "pid": state.pid,
            "process_nonce": state.process_nonce,
            "compile_cache_id": state.compile_cache_id,
            "compile_cache_mode": state.compile_cache_mode,
            "proof_attempt_receipt": (
                None
                if proof_attempt_path is None
                else str(proof_attempt_path)
            ),
            "ready_receipt": str(ready[1]),
            "startup_duration_ns": (
                ready[0]["ready_monotonic_ns"] - state.started_monotonic_ns
            ),
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

    def _ready_receipt(
        self,
        *,
        state: ProcessState,
        local_paths: dict[str, pathlib.Path],
        probe: Any | None = None,
        observed_monotonic_ns: int | None = None,
    ) -> tuple[dict[str, Any], pathlib.Path] | None:
        """Read or publish the immutable first successful readiness probe."""

        if (probe is None) != (observed_monotonic_ns is None):
            _fail(
                "readiness publication requires one timestamped probe",
                code="unsafe_service_runtime_state",
            )
        path = _launch_receipt_path(
            service_root=local_paths["service_root"],
            kind="ready",
            process_nonce=state.process_nonce,
        )
        if os.path.lexists(path):
            receipt, _ = read_private_json(path)
        elif (
            probe is not None
            and probe.ready
            and isinstance(observed_monotonic_ns, int)
            and not isinstance(observed_monotonic_ns, bool)
        ):
            receipt = {
                "schema_version": READY_RECEIPT_SCHEMA,
                "service_id": state.service_id,
                "service_plan_sha256": state.service_plan_sha256,
                "manifest_sha256": state.manifest_sha256,
                "boot_id": state.boot_id,
                "pid": state.pid,
                "process_nonce": state.process_nonce,
                "process_start_ticks": state.process_start_ticks,
                "compile_cache_id": state.compile_cache_id,
                "compile_cache_mode": state.compile_cache_mode,
                "compile_cache_prerequisite_sha256": (
                    state.compile_cache_prerequisite_sha256
                ),
                "started_monotonic_ns": state.started_monotonic_ns,
                "runtime_execution_environment": (state.runtime_execution_environment),
                "ready_monotonic_ns": observed_monotonic_ns,
                "ready": True,
                "probe": _probe_document(probe),
            }
            atomic_write_private_json(path, receipt)
        else:
            return None
        expected_fields = {
            "schema_version",
            "service_id",
            "service_plan_sha256",
            "manifest_sha256",
            "boot_id",
            "pid",
            "process_nonce",
            "process_start_ticks",
            "compile_cache_id",
            "compile_cache_mode",
            "compile_cache_prerequisite_sha256",
            "started_monotonic_ns",
            "runtime_execution_environment",
            "ready_monotonic_ns",
            "ready",
            "probe",
        }
        expected_identity = {
            "service_id": state.service_id,
            "service_plan_sha256": state.service_plan_sha256,
            "manifest_sha256": state.manifest_sha256,
            "boot_id": state.boot_id,
            "pid": state.pid,
            "process_nonce": state.process_nonce,
            "process_start_ticks": state.process_start_ticks,
            "compile_cache_id": state.compile_cache_id,
            "compile_cache_mode": state.compile_cache_mode,
            "compile_cache_prerequisite_sha256": (
                state.compile_cache_prerequisite_sha256
            ),
            "started_monotonic_ns": state.started_monotonic_ns,
            "runtime_execution_environment": state.runtime_execution_environment,
        }
        recorded_probe = receipt.get("probe")
        if (
            set(receipt) != expected_fields
            or receipt["schema_version"] != READY_RECEIPT_SCHEMA
            or any(
                receipt.get(name) != value for name, value in expected_identity.items()
            )
            or receipt["ready"] is not True
            or isinstance(receipt["ready_monotonic_ns"], bool)
            or not isinstance(receipt["ready_monotonic_ns"], int)
            or receipt["ready_monotonic_ns"] < state.started_monotonic_ns
            or not isinstance(recorded_probe, dict)
            or set(recorded_probe)
            != {
                "health_status",
                "models_status",
                "served_model_ids",
                "detail",
            }
            or recorded_probe["health_status"] != 200
            or recorded_probe["models_status"] != 200
            or recorded_probe["served_model_ids"] != [state.service_id]
            or recorded_probe["detail"] != "ready"
        ):
            _fail(
                "service readiness receipt is malformed or mismatched",
                code="unsafe_service_runtime_state",
            )
        return receipt, path

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
                "schema_version": "model-lab.service-status.v1",
                "service_id": manifest.service_id,
                "manifest_sha256": manifest.manifest_sha256,
                "phase": "unconfigured",
                "ready": False,
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
                return {
                    "schema_version": "model-lab.service-status.v1",
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
                boot_id=self.platform.boot_id(layout=self.layout),
            )
            if not self.platform.process_is_owned(state):
                return {
                    "schema_version": "model-lab.service-status.v1",
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
            ready_receipt = self._ready_receipt(
                state=state,
                local_paths=local_paths,
            )
            if probe.ready and ready_receipt is None:
                _fail(
                    "service is ready without its start-owned readiness receipt",
                    code="unsafe_service_runtime_state",
                )
            return {
                "schema_version": "model-lab.service-status.v1",
                "service_id": manifest.service_id,
                "manifest_sha256": manifest.manifest_sha256,
                "phase": "ready" if probe.ready else "starting",
                "ready": probe.ready,
                "pid": state.pid,
                "compile_cache_id": state.compile_cache_id,
                "compile_cache_mode": state.compile_cache_mode,
                "probe": _probe_document(probe),
                "ready_receipt": (
                    None if ready_receipt is None else str(ready_receipt[1])
                ),
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

    def _launch_measurement(
        self,
        *,
        state: ProcessState,
        manifest: DeploymentManifest,
        contract: dict[str, Any],
        cache: CompileCachePrerequisite,
        ready_receipt: dict[str, Any],
        stopped_monotonic_ns: int,
        post_inventory: dict[str, Any],
        local_paths: dict[str, pathlib.Path],
    ) -> tuple[dict[str, Any], pathlib.Path]:
        if state.compile_cache_mode in ONE_ATTEMPT_CACHE_MODES:
            attempt_path = _proof_attempt_path(
                local_cache_root=cache.local_root,
                cache_id=state.compile_cache_id,
            )
            attempt, _ = read_private_json(
                attempt_path,
                maximum_bytes=64 * 1024,
            )
            if attempt != {
                "schema_version": PROOF_ATTEMPT_SCHEMA,
                "service_id": state.service_id,
                "service_plan_sha256": state.service_plan_sha256,
                "manifest_sha256": state.manifest_sha256,
                "boot_id": state.boot_id,
                "compile_cache_id": state.compile_cache_id,
                "compile_cache_mode": state.compile_cache_mode,
                "compile_cache_prerequisite_sha256": (
                    state.compile_cache_prerequisite_sha256
                ),
                "process_nonce": state.process_nonce,
                "started_monotonic_ns": state.started_monotonic_ns,
            }:
                _fail(
                    "compile-cache proof attempt receipt is malformed or "
                    "mismatched",
                    code="service_stop_audit_failed",
                )
        evidence = read_vllm_cache_evidence(
            log_path=local_paths["service_log"],
            cache_root=pathlib.PurePosixPath(contract["local_root"]),
            inventory=post_inventory,
            mode=state.compile_cache_mode,
        )
        path = _launch_receipt_path(
            service_root=local_paths["service_root"],
            kind="cache-measurement",
            process_nonce=state.process_nonce,
        )
        if os.path.lexists(path):
            measurement, _ = load_measurement(
                path=path,
                contract=contract,
                mode=state.compile_cache_mode,
            )
            expected = {
                "schema_version": COMPILE_CACHE_LAUNCH_MEASUREMENT_SCHEMA,
                "mode": state.compile_cache_mode,
                "cache_id": contract["cache_id"],
                "contract": contract,
                "boot_id": state.boot_id,
                "service_manifest_sha256": manifest.manifest_sha256,
                "prerequisite_receipt_sha256": (
                    state.compile_cache_prerequisite_sha256
                ),
                "runtime_execution_environment": (state.runtime_execution_environment),
                "runtime_execution_environment_sha256": (
                    state.runtime_execution_environment["sha256"]
                ),
                "started_monotonic_ns": state.started_monotonic_ns,
                "ready_monotonic_ns": ready_receipt["ready_monotonic_ns"],
                "ready": True,
                "process_stopped": True,
                "pre_inventory_sha256": cache.pre_inventory_sha256,
                "post_inventory_sha256": post_inventory["sha256"],
                "cache_evidence": evidence,
            }
            if any(measurement.get(name) != value for name, value in expected.items()):
                _fail(
                    "existing launch measurement does not match the stopped run",
                    code="service_stop_audit_failed",
                )
            return measurement, path
        measurement = {
            "schema_version": COMPILE_CACHE_LAUNCH_MEASUREMENT_SCHEMA,
            "mode": state.compile_cache_mode,
            "cache_id": contract["cache_id"],
            "contract": contract,
            "boot_id": state.boot_id,
            "service_manifest_sha256": manifest.manifest_sha256,
            "prerequisite_receipt_sha256": (state.compile_cache_prerequisite_sha256),
            "runtime_execution_environment": (state.runtime_execution_environment),
            "runtime_execution_environment_sha256": (
                state.runtime_execution_environment["sha256"]
            ),
            "started_monotonic_ns": state.started_monotonic_ns,
            "ready_monotonic_ns": ready_receipt["ready_monotonic_ns"],
            "stopped_monotonic_ns": stopped_monotonic_ns,
            "ready": True,
            "process_stopped": True,
            "pre_inventory_sha256": cache.pre_inventory_sha256,
            "post_inventory_sha256": post_inventory["sha256"],
            "cache_evidence": evidence,
        }
        atomic_write_private_json(path, measurement)
        load_measurement(
            path=path,
            contract=contract,
            mode=state.compile_cache_mode,
        )
        return measurement, path

    def _publish_compile_cache(
        self,
        *,
        state: ProcessState,
        contract: dict[str, Any],
        cache: CompileCachePrerequisite,
        measurement_path: pathlib.Path,
        measurement: dict[str, Any],
        post_inventory: dict[str, Any],
    ) -> dict[str, Any]:
        if state.compile_cache_mode == "ephemeral":
            return {
                "action": "audit-ephemeral",
                "state": "measured-ephemeral-cache-not-published",
                "cache_id": contract["cache_id"],
                "runtime_cache_mutated": (
                    post_inventory["sha256"] != cache.pre_inventory_sha256
                ),
            }
        if state.compile_cache_mode == "author":
            return seal_compile_cache_candidate(
                contract=contract,
                layout=self.layout,
                measurement_path=measurement_path,
            )
        if state.compile_cache_mode == "candidate-proof":
            return accept_compile_cache_candidate(
                contract=contract,
                layout=self.layout,
                measurement_path=measurement_path,
            )
        generation = load_persistent_compile_cache(
            contract=contract,
            layout=self.layout,
            require_accepted=True,
            verify_bundle_content=False,
        )
        if (
            generation.inventory["sha256"] != cache.pre_inventory_sha256
            or artifact_records(
                post_inventory,
                measurement["cache_evidence"]["loaded_artifacts"],
            )
            != generation.authored["produced_artifacts"]
        ):
            _fail(
                "accepted launch did not reuse the exact accepted artifacts",
                code="service_stop_audit_failed",
            )
        return {
            "action": "audit-accepted",
            "state": "accepted-cache-reuse-proven",
            "cache_id": contract["cache_id"],
            "persistent_root": contract["persistent_root"],
            "runtime_cache_mutated": (
                post_inventory["sha256"] != cache.pre_inventory_sha256
            ),
        }

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
                "schema_version": "model-lab.service-operation.v1",
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
                    "schema_version": "model-lab.service-operation.v1",
                    "action": "stop",
                    "service_id": manifest.service_id,
                    "manifest_sha256": manifest.manifest_sha256,
                    "status": "already-stopped",
                }
            state = read_process_state(local_paths["process_state"])
            boot_id = self.platform.boot_id(layout=self.layout)
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
                    "schema_version": "model-lab.service-operation.v1",
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
            ready = self._ready_receipt(
                state=state,
                local_paths=local_paths,
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
            stopped_monotonic_ns = self.platform.monotonic_ns()
            if ready is None:
                remove_private_file(local_paths["process_state"])
                return {
                    "schema_version": "model-lab.service-operation.v1",
                    "action": "stop",
                    "service_id": manifest.service_id,
                    "manifest_sha256": manifest.manifest_sha256,
                    "status": "stopped-unready",
                    "compile_cache_mode": state.compile_cache_mode,
                    "compile_cache_measurement": None,
                    "compile_cache_publication": {
                        "action": "none",
                        "state": "stopped-unready-cache-not-published",
                        "cache_id": state.compile_cache_id,
                    },
                }
            audit_error: ModelLabError | None = None
            audit_result: dict[str, Any] | None = None
            try:
                setup_receipt = self._read_setup_receipt(
                    path=local_paths["setup_receipt"],
                    manifest=manifest,
                    boot_id=boot_id,
                    cache_mode=state.compile_cache_mode,
                )
                observed_gpu = self.platform.observe_gpu()
                if observed_gpu != setup_receipt["observed_gpu"]:
                    _fail(
                        "observed GPU changed while the service ran",
                        code="service_stop_audit_failed",
                    )
                if (
                    state.runtime_execution_environment
                    != setup_receipt["runtime_execution_environment"]
                ):
                    _fail(
                        "runtime execution environment binding changed",
                        code="service_stop_audit_failed",
                    )
                snapshot, contract, cache = self._verify_prerequisites(
                    manifest=manifest,
                    canonical_paths=canonical_paths,
                    local_paths=local_paths,
                    boot_id=boot_id,
                    observed_gpu=observed_gpu,
                    runtime_execution_environment=(state.runtime_execution_environment),
                    cache_mode=state.compile_cache_mode,
                    verify_cache_inventory=False,
                )
                if (
                    snapshot.summary() != setup_receipt["snapshot_stage"]
                    or contract != setup_receipt["compile_cache_contract"]
                    or cache.summary() != setup_receipt["compile_cache_prerequisite"]
                    or state.compile_cache_id != contract["cache_id"]
                    or state.compile_cache_prerequisite_sha256 != cache.receipt_sha256
                ):
                    _fail(
                        "staged prerequisite changed while the service ran",
                        code="service_stop_audit_failed",
                    )
                post_inventory = inventory_compile_cache(cache.local_root)
                measurement, measurement_path = self._launch_measurement(
                    state=state,
                    manifest=manifest,
                    contract=contract,
                    cache=cache,
                    ready_receipt=ready[0],
                    stopped_monotonic_ns=stopped_monotonic_ns,
                    post_inventory=post_inventory,
                    local_paths=local_paths,
                )
                publication = self._publish_compile_cache(
                    state=state,
                    contract=contract,
                    cache=cache,
                    measurement_path=measurement_path,
                    measurement=measurement,
                    post_inventory=post_inventory,
                )
                audit_result = {
                    "compile_cache_mode": state.compile_cache_mode,
                    "compile_cache_measurement": str(
                        canonical_paths.service_root / measurement_path.name
                    ),
                    "compile_cache_pre_inventory_sha256": (cache.pre_inventory_sha256),
                    "compile_cache_post_inventory_sha256": (post_inventory["sha256"]),
                    "compile_cache_publication": publication,
                }
            except ModelLabError as error:
                audit_error = error
            if audit_error is not None:
                raise ModelLabError(
                    "service stopped and retained retryable process state, "
                    f"but compiled-cache audit failed: "
                    f"{audit_error}",
                    code="service_stop_audit_failed",
                ) from audit_error
            if audit_result is None:
                _fail(
                    "service stopped without a compile-cache audit result",
                    code="service_stop_audit_failed",
                )
            remove_private_file(local_paths["process_state"])
        return {
            "schema_version": "model-lab.service-operation.v1",
            "action": "stop",
            "service_id": manifest.service_id,
            "manifest_sha256": manifest.manifest_sha256,
            "status": "stopped",
            **audit_result,
        }

    def execute(
        self,
        *,
        action: str,
        manifest_path: pathlib.Path,
        cache_mode: str | None = None,
    ) -> dict[str, Any]:
        cache_actions = {
            "prepare-cache": self.prepare_cache,
            "setup": self.setup,
            "start": self.start,
        }
        if action in cache_actions:
            return cache_actions[action](
                manifest_path,
                cache_mode=cache_mode,
            )
        if cache_mode is not None:
            _fail(
                f"{action} does not accept a cache mode",
                code="unexpected_service_cache_mode",
            )
        if action == "stage-snapshot":
            return self.stage_snapshot(manifest_path)
        if action == "cache-status":
            return self.cache_status(manifest_path)
        if action == "status":
            return self.status(manifest_path)
        if action == "stop":
            return self.stop(manifest_path)
        _fail(
            f"unsupported service runtime action: {action}",
            code="invalid_service_runtime_action",
        )


def _absolute_normalized_path(value: str) -> pathlib.Path:
    path = pathlib.Path(value)
    if (
        not path.is_absolute()
        or str(path) != os.path.normpath(str(path))
        or "\x00" in value
    ):
        raise argparse.ArgumentTypeError("manifest must be an absolute normalized path")
    return path


def _canonical_startup_expiration(value: str) -> datetime.datetime:
    try:
        parsed = datetime.datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "startup expiration must be an RFC3339 timestamp"
        ) from error
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(
            "startup expiration must include a timezone"
        )
    normalized = (
        parsed.astimezone(datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    if normalized != value:
        raise argparse.ArgumentTypeError(
            "startup expiration must be canonical UTC"
        )
    return parsed.astimezone(datetime.timezone.utc)


def _execute_with_startup_alarm(
    controller: ServiceRuntimeController,
    *,
    action: str,
    manifest_path: pathlib.Path,
    cache_mode: str | None,
    startup_expires_at: datetime.datetime | None,
) -> dict[str, Any]:
    if startup_expires_at is None:
        return controller.execute(
            action=action,
            manifest_path=manifest_path,
            cache_mode=cache_mode,
        )
    remaining = startup_expires_at.timestamp() - time.time()
    if remaining <= 0:
        _fail(
            "remote service action cannot start after its startup deadline",
            code="service_startup_timeout",
        )
    previous_handler = signal.getsignal(signal.SIGALRM)

    def expire_action(_signal_number: int, _frame: Any) -> None:
        _fail(
            "remote service action exceeded its startup deadline",
            code="service_startup_timeout",
        )

    signal.signal(signal.SIGALRM, expire_action)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        return controller.execute(
            action=action,
            manifest_path=manifest_path,
            cache_mode=cache_mode,
        )
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="model-lab-service-runtime",
        description=(
            "Operate one generated model-lab service deployment on a RunPod host."
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        "action",
        choices=(
            "stage-snapshot",
            "cache-status",
            "prepare-cache",
            "setup",
            "start",
            "status",
            "stop",
        ),
    )
    parser.add_argument(
        "--manifest",
        required=True,
        type=_absolute_normalized_path,
        help="absolute deployment.json path",
    )
    parser.add_argument(
        "--cache-mode",
        choices=("ephemeral", "author", "candidate-proof", "accepted"),
        help="explicit compiled-cache lifecycle mode",
    )
    parser.add_argument(
        "--startup-expires-at",
        type=_canonical_startup_expiration,
        help=(
            "absolute UTC deadline inherited from the endpoint startup "
            "operation"
        ),
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
        startup_expires_at=arguments.startup_expires_at,
    )
    try:
        result = _execute_with_startup_alarm(
            controller,
            action=arguments.action,
            manifest_path=arguments.manifest,
            cache_mode=arguments.cache_mode,
            startup_expires_at=arguments.startup_expires_at,
        )
    except ModelLabError as error:
        print(
            json.dumps(
                {
                    "schema_version": "model-lab.service-error.v1",
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
