"""Execute one installed model-service runtime action over generic SSH."""

from __future__ import annotations

import datetime
import json
import pathlib
import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .errors import ModelLabError
from runpod_local.instances import InstanceStore
from runpod_local.remote import (
    SshEndpoint,
    build_ssh_argv,
    ensure_known_hosts_file,
    run_with_activity,
    sanitized_subprocess_environment,
)
from .service_materialization import (
    MaterializedService,
    ServiceMaterializationPlan,
    load_service_materialization,
)

RUNTIME_PLAN_SCHEMA = "model-lab.service-runtime-plan.v1"
RUNTIME_RESULT_SCHEMA = "model-lab.service-runtime-operation.v1"
RUNTIME_ACTIONS = (
    "stage-snapshot",
    "cache-status",
    "prepare-cache",
    "setup",
    "start",
    "status",
    "stop",
)
CACHE_ACTIONS = frozenset({"prepare-cache", "setup", "start"})
CACHE_MODES = (
    "ephemeral",
    "author",
    "candidate-proof",
    "accepted",
)
RELATIVE_ENTRYPOINT = pathlib.PurePosixPath("bin/model-lab-service-runtime")
REMOTE_IMPLEMENTATION_PARENT = pathlib.PurePosixPath(
    "/root/runpod-session/control/model-service-runtime"
)
REMOTE_SERVICE_PARENT = pathlib.PurePosixPath("/root/runpod-session/services")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SERVICE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
MAX_RUNTIME_OUTPUT_BYTES = 1024 * 1024


def _fail(message: str, *, code: str) -> None:
    raise ModelLabError(message, code=code)


def _materialization_document(
    materialization: MaterializedService | ServiceMaterializationPlan,
) -> dict[str, Any]:
    if not isinstance(
        materialization,
        (MaterializedService, ServiceMaterializationPlan),
    ):
        _fail(
            "runtime planning requires one service materialization",
            code="invalid_service_runtime_materialization",
        )
    return materialization.install_document


def _installed_runtime_paths(
    document: dict[str, Any],
) -> tuple[str, str]:
    files = document.get("files") if isinstance(document, dict) else None
    if not isinstance(files, list):
        _fail(
            "materialization file closure is malformed",
            code="invalid_service_runtime_materialization",
        )
    receipts = [
        record
        for record in files
        if isinstance(record, dict)
        and record.get("role") == "implementation-receipt"
        and record.get("mode") == "0600"
    ]
    manifests = [
        record
        for record in files
        if isinstance(record, dict)
        and record.get("role") == "deployment-manifest"
        and record.get("mode") == "0600"
    ]
    if len(receipts) != 1 or len(manifests) != 1:
        _fail(
            "materialization does not contain one runtime and deployment",
            code="invalid_service_runtime_materialization",
        )
    receipt_path = receipts[0].get("remote_path")
    manifest_path = manifests[0].get("remote_path")
    if not isinstance(receipt_path, str) or not isinstance(manifest_path, str):
        _fail(
            "materialization runtime paths are malformed",
            code="invalid_service_runtime_materialization",
        )
    receipt = pathlib.PurePosixPath(receipt_path)
    manifest = pathlib.PurePosixPath(manifest_path)
    try:
        implementation_relative = receipt.relative_to(REMOTE_IMPLEMENTATION_PARENT)
        manifest_relative = manifest.relative_to(REMOTE_SERVICE_PARENT)
    except ValueError:
        _fail(
            "materialization runtime paths are outside their fixed roots",
            code="invalid_service_runtime_materialization",
        )
    if (
        len(implementation_relative.parts) != 2
        or SHA256_PATTERN.fullmatch(implementation_relative.parts[0]) is None
        or implementation_relative.parts[1] != "bundle.json"
        or len(manifest_relative.parts) != 4
        or SERVICE_ID_PATTERN.fullmatch(manifest_relative.parts[0]) is None
        or manifest_relative.parts[1] != "deployments"
        or SHA256_PATTERN.fullmatch(manifest_relative.parts[2]) is None
        or manifest_relative.parts[3] != "deployment.json"
    ):
        _fail(
            "materialization runtime paths are malformed",
            code="invalid_service_runtime_materialization",
        )
    implementation_root = receipt.parent
    entrypoint = implementation_root / RELATIVE_ENTRYPOINT
    entrypoint_records = [
        record
        for record in files
        if isinstance(record, dict)
        and record.get("role") == "implementation-member"
        and record.get("remote_path") == str(entrypoint)
        and record.get("mode") == "0755"
    ]
    if len(entrypoint_records) != 1:
        _fail(
            "materialization does not contain its executable runtime entrypoint",
            code="invalid_service_runtime_materialization",
        )
    return str(entrypoint), manifest_path


def _checked_action(
    action: str,
    *,
    cache_mode: str | None,
) -> None:
    if action not in RUNTIME_ACTIONS:
        _fail(
            f"unsupported service runtime action: {action}",
            code="invalid_service_runtime_action",
        )
    if action in CACHE_ACTIONS:
        if cache_mode not in CACHE_MODES:
            _fail(
                f"{action} requires an explicit supported cache mode",
                code="service_cache_mode_required",
            )
    elif cache_mode is not None:
        _fail(
            f"{action} does not accept a cache mode",
            code="unexpected_service_cache_mode",
        )


@dataclass(frozen=True)
class ServiceRuntimePlan:
    """One exact SSH invocation of an installed generated deployment."""

    materialization_root: pathlib.Path
    materialization_sha256: str
    endpoint: SshEndpoint
    action: str
    cache_mode: str | None
    entrypoint: str
    manifest: str
    argv: tuple[str, ...]

    def safe_summary(self) -> dict[str, Any]:
        return {
            "schema_version": RUNTIME_PLAN_SCHEMA,
            "executed": False,
            "provider_mutation": False,
            "materialization_sha256": self.materialization_sha256,
            "instance": {
                "name": self.endpoint.instance_name,
                "operation_id": self.endpoint.operation_id,
                "pod_id": self.endpoint.pod_id,
            },
            "action": self.action,
            "cache_mode": self.cache_mode,
            "entrypoint": self.entrypoint,
            "manifest": self.manifest,
            "argv": list(self.argv),
        }


def build_service_runtime_plan(
    materialization: MaterializedService | ServiceMaterializationPlan,
    *,
    endpoint: SshEndpoint,
    action: str,
    cache_mode: str | None = None,
) -> ServiceRuntimePlan:
    """Build a shell-free runtime invocation without remote execution."""

    _checked_action(action, cache_mode=cache_mode)
    document = _materialization_document(materialization)
    entrypoint, manifest = _installed_runtime_paths(document)
    materialization_sha256 = document.get("materialization_sha256")
    if not isinstance(materialization_sha256, str):
        _fail(
            "materialization identity is malformed",
            code="invalid_service_runtime_materialization",
        )
    remote_argv = [
        entrypoint,
        action,
        "--manifest",
        manifest,
    ]
    if cache_mode is not None:
        remote_argv.extend(["--cache-mode", cache_mode])
    return ServiceRuntimePlan(
        materialization_root=materialization.local_root
        if isinstance(materialization, ServiceMaterializationPlan)
        else materialization.root,
        materialization_sha256=materialization_sha256,
        endpoint=endpoint,
        action=action,
        cache_mode=cache_mode,
        entrypoint=entrypoint,
        manifest=manifest,
        argv=tuple(build_ssh_argv(endpoint, remote_argv)),
    )


def execute_service_runtime(
    plan: ServiceRuntimePlan,
    *,
    resolved_endpoint: SshEndpoint,
    instances: InstanceStore,
    popen_factory: Callable[..., Any] = subprocess.Popen,
) -> dict[str, Any]:
    """Revalidate and execute one exact runtime plan on its active Pod."""

    current = load_service_materialization(plan.materialization_root)
    expected = build_service_runtime_plan(
        current,
        endpoint=resolved_endpoint,
        action=plan.action,
        cache_mode=plan.cache_mode,
    )
    if plan != expected:
        _fail(
            "service runtime plan changed after validation",
            code="invalid_service_runtime_plan",
        )
    ensure_known_hosts_file(resolved_endpoint.known_hosts_file)
    return_code = run_with_activity(
        list(plan.argv),
        instances=instances,
        name=resolved_endpoint.instance_name,
        expected_operation_id=resolved_endpoint.operation_id,
        expected_pod_id=resolved_endpoint.pod_id,
        source=f"service-runtime-{plan.action}",
        popen_factory=popen_factory,
    )
    if return_code != 0:
        raise ModelLabError(
            f"service runtime action {plan.action} exited {return_code}",
            code="service_runtime_action_failed",
        )
    return {
        "schema_version": RUNTIME_RESULT_SCHEMA,
        "executed": True,
        "provider_mutation": False,
        "materialization_sha256": plan.materialization_sha256,
        "instance": {
            "name": resolved_endpoint.instance_name,
            "operation_id": resolved_endpoint.operation_id,
            "pod_id": resolved_endpoint.pod_id,
        },
        "action": plan.action,
        "cache_mode": plan.cache_mode,
        "status": "completed",
    }


class _BoundedPipeCapture:
    """Drain one child pipe without allowing remote output to grow memory."""

    def __init__(self, stream: Any, *, maximum_bytes: int) -> None:
        self.stream = stream
        self.maximum_bytes = maximum_bytes
        self.payload = bytearray()
        self.overflow = False
        self.failure: BaseException | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def join(self) -> None:
        self.thread.join()
        if self.failure is not None:
            raise ModelLabError(
                f"cannot read remote service runtime output: {self.failure}",
                code="service_runtime_output_failed",
            ) from self.failure

    def _run(self) -> None:
        try:
            while True:
                chunk = self.stream.read(64 * 1024)
                if not chunk:
                    return
                remaining = self.maximum_bytes + 1 - len(self.payload)
                if remaining > 0:
                    self.payload.extend(chunk[:remaining])
                if len(chunk) > remaining or len(self.payload) > self.maximum_bytes:
                    self.overflow = True
        except BaseException as error:
            self.failure = error


def _runtime_json_object(
    payload: bytes,
    *,
    label: str,
) -> dict[str, Any]:
    def reject_duplicates(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate field {key!r}")
            value[key] = item
        return value

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ModelLabError(
            f"remote service runtime {label} is not one valid JSON object",
            code="invalid_service_runtime_output",
        ) from error
    if not isinstance(value, dict):
        raise ModelLabError(
            f"remote service runtime {label} is not a JSON object",
            code="invalid_service_runtime_output",
        )
    return value


def _terminate_remote_client(process: Any) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def execute_service_runtime_capture(
    plan: ServiceRuntimePlan,
    *,
    resolved_endpoint: SshEndpoint,
    instances: InstanceStore,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    clock: Callable[[], datetime.datetime] | None = None,
) -> dict[str, Any]:
    """Execute and authenticate one bounded remote runtime JSON result.

    Both output pipes are drained concurrently so a verbose remote failure
    cannot deadlock SSH. The active Pod receipt is checked before launch and
    throughout the action; lease drift terminates the exact client process.
    """

    current = load_service_materialization(plan.materialization_root)
    expected = build_service_runtime_plan(
        current,
        endpoint=resolved_endpoint,
        action=plan.action,
        cache_mode=plan.cache_mode,
    )
    if plan != expected:
        _fail(
            "service runtime plan changed after validation",
            code="invalid_service_runtime_plan",
        )
    ensure_known_hosts_file(resolved_endpoint.known_hosts_file)
    now = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
    record = instances.check_active_lease(
        resolved_endpoint.instance_name,
        now=now(),
        expected_operation_id=resolved_endpoint.operation_id,
        expected_pod_id=resolved_endpoint.pod_id,
    )
    try:
        process = popen_factory(
            list(plan.argv),
            env=sanitized_subprocess_environment(),
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise ModelLabError(
            f"cannot start remote service runtime client: {error}",
            code="remote_client_start_failed",
        ) from error
    if process.stdout is None or process.stderr is None:
        _terminate_remote_client(process)
        raise ModelLabError(
            "remote service runtime client did not expose captured pipes",
            code="service_runtime_output_failed",
        )
    stdout = _BoundedPipeCapture(
        process.stdout,
        maximum_bytes=MAX_RUNTIME_OUTPUT_BYTES,
    )
    stderr = _BoundedPipeCapture(
        process.stderr,
        maximum_bytes=MAX_RUNTIME_OUTPUT_BYTES,
    )
    stdout.start()
    stderr.start()
    source = f"service-runtime-{plan.action}"
    try:
        record = instances.touch(
            resolved_endpoint.instance_name,
            now=now(),
            source=source,
            expected_operation_id=resolved_endpoint.operation_id,
            expected_pod_id=resolved_endpoint.pod_id,
        )
    except BaseException:
        _terminate_remote_client(process)
        stdout.join()
        stderr.join()
        raise
    idle_timeout = record["lease"]["idle_timeout_seconds"]
    heartbeat_seconds = (
        30
        if idle_timeout is None
        else min(30, max(1, idle_timeout // 3))
    )
    while True:
        try:
            return_code = process.wait(timeout=heartbeat_seconds)
            break
        except subprocess.TimeoutExpired:
            try:
                instances.touch(
                    resolved_endpoint.instance_name,
                    now=now(),
                    source=source,
                    expected_operation_id=resolved_endpoint.operation_id,
                    expected_pod_id=resolved_endpoint.pod_id,
                    record_event=False,
                )
            except BaseException:
                _terminate_remote_client(process)
                stdout.join()
                stderr.join()
                raise
    stdout.join()
    stderr.join()
    if stdout.overflow or stderr.overflow:
        raise ModelLabError(
            "remote service runtime output exceeded its one-MiB bound",
            code="oversized_service_runtime_output",
        )
    if return_code != 0:
        try:
            remote_error = _runtime_json_object(
                bytes(stderr.payload),
                label="error",
            )
        except ModelLabError:
            raise ModelLabError(
                f"service runtime action {plan.action} exited {return_code}",
                code="service_runtime_action_failed",
            ) from None
        code = remote_error.get("error")
        message = remote_error.get("message")
        if (
            remote_error.get("schema_version")
            != "model-lab.service-error.v1"
            or set(remote_error) != {"schema_version", "error", "message"}
            or not isinstance(code, str)
            or ERROR_CODE_PATTERN.fullmatch(code) is None
            or not isinstance(message, str)
            or not message
            or len(message.encode("utf-8")) > 64 * 1024
            or any(
                ord(character) < 32 and character not in "\t\n\r"
                for character in message
            )
        ):
            raise ModelLabError(
                f"service runtime action {plan.action} exited {return_code}",
                code="service_runtime_action_failed",
            )
        raise ModelLabError(message, code=code)
    if stderr.payload:
        raise ModelLabError(
            "successful remote service runtime emitted unexpected stderr",
            code="invalid_service_runtime_output",
        )
    result = _runtime_json_object(bytes(stdout.payload), label="result")
    if result.get("service_id") is None:
        raise ModelLabError(
            "remote service runtime result has no service identity",
            code="invalid_service_runtime_output",
        )
    instances.touch(
        resolved_endpoint.instance_name,
        now=now(),
        source=source,
        expected_operation_id=resolved_endpoint.operation_id,
        expected_pod_id=resolved_endpoint.pod_id,
    )
    return result
