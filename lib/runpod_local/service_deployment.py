"""Plan and execute exact service installation over an existing SSH endpoint."""

from __future__ import annotations

import contextlib
import hashlib
import os
import pathlib
import re
import secrets
import stat
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, BinaryIO

from .errors import RunpodLocalError
from .instances import InstanceStore
from .remote import (
    SshEndpoint,
    build_copy_argv,
    build_ssh_argv,
    ensure_known_hosts_file,
    run_with_activity,
)
from .service_materialization import (
    MaterializedService,
    load_service_materialization,
)

PUSH_PLAN_SCHEMA = "runpod.inference-service-push-plan.v1"
PUSH_RESULT_SCHEMA = "runpod.inference-service-push.v1"
REMOTE_INCOMING_PARENT = "/root/runpod-session/incoming/service-materializations"
MAX_INSTALLER_BYTES = 1024 * 1024
TRANSFER_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _fail(message: str, *, code: str) -> None:
    raise RunpodLocalError(message, code=code)


@contextlib.contextmanager
def _installer_stream(
    path: pathlib.Path,
    *,
    expected: dict[str, Any],
) -> Iterator[BinaryIO]:
    """Yield one verified descriptor positioned for SSH standard input."""

    try:
        before = path.lstat()
    except OSError as error:
        raise RunpodLocalError(
            f"cannot inspect service installer: {path}",
            code="unsafe_service_installer",
        ) from error
    if (
        path.is_symlink()
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size < 1
        or before.st_size > MAX_INSTALLER_BYTES
        or before.st_mode & 0o002
        or (hasattr(os, "getuid") and before.st_uid != os.getuid())
    ):
        _fail(
            f"service installer has an unsafe identity: {path}",
            code="unsafe_service_installer",
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RunpodLocalError(
            f"cannot safely open service installer: {path}",
            code="unsafe_service_installer",
        ) from error
    stream: BinaryIO | None = None
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != before.st_uid
            or opened.st_nlink != before.st_nlink
            or opened.st_size != before.st_size
            or stat.S_IMODE(opened.st_mode) != stat.S_IMODE(before.st_mode)
        ):
            _fail(
                "service installer changed while opening",
                code="service_installer_drift",
            )
        digest = hashlib.sha256()
        observed_bytes = 0
        while observed_bytes <= MAX_INSTALLER_BYTES:
            chunk = os.read(
                descriptor,
                min(
                    64 * 1024,
                    MAX_INSTALLER_BYTES + 1 - observed_bytes,
                ),
            )
            if not chunk:
                break
            digest.update(chunk)
            observed_bytes += len(chunk)
        after = os.fstat(descriptor)
        if (
            observed_bytes != opened.st_size
            or observed_bytes != expected.get("bytes")
            or digest.hexdigest() != expected.get("sha256")
            or after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or not stat.S_ISREG(after.st_mode)
            or after.st_uid != opened.st_uid
            or after.st_nlink != opened.st_nlink
            or stat.S_IMODE(after.st_mode) != stat.S_IMODE(opened.st_mode)
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or after.st_ctime_ns != opened.st_ctime_ns
        ):
            _fail(
                "service installer does not match the materialization",
                code="service_installer_drift",
            )
        os.lseek(descriptor, 0, os.SEEK_SET)
        stream = os.fdopen(descriptor, "rb", closefd=True)
        descriptor = -1
        yield stream
    finally:
        if stream is not None:
            stream.close()
        elif descriptor >= 0:
            os.close(descriptor)


@dataclass(frozen=True)
class ServicePushPlan:
    """Four exact remote-client invocations and no provider mutation."""

    materialization: MaterializedService
    endpoint: SshEndpoint
    installer_path: pathlib.Path
    transfer_id: str
    incoming_path: str
    steps: tuple[dict[str, Any], ...]

    def safe_summary(self) -> dict[str, Any]:
        return {
            "schema_version": PUSH_PLAN_SCHEMA,
            "executed": False,
            "provider_mutation": False,
            "materialization_sha256": (self.materialization.materialization_sha256),
            "instance": {
                "name": self.endpoint.instance_name,
                "operation_id": self.endpoint.operation_id,
                "pod_id": self.endpoint.pod_id,
            },
            "transfer_id": self.transfer_id,
            "incoming_path": self.incoming_path,
            "steps": [
                {
                    **step,
                    "argv": list(step["argv"]),
                }
                for step in self.steps
            ],
        }


def build_service_push_plan(
    materialization: MaterializedService,
    *,
    endpoint: SshEndpoint,
    installer_path: pathlib.Path,
    transfer_id: str | None = None,
) -> ServicePushPlan:
    """Build shell-free SSH/SCP commands without executing or creating a Pod."""

    materialization = load_service_materialization(materialization.root)
    installer_path = installer_path.expanduser().absolute()
    with _installer_stream(
        installer_path,
        expected=materialization.install_document["installer"],
    ):
        pass
    transfer_id = secrets.token_hex(32) if transfer_id is None else transfer_id
    if TRANSFER_ID_PATTERN.fullmatch(transfer_id) is None:
        _fail(
            "service transfer identity must be a lowercase SHA-256-shaped nonce",
            code="invalid_service_transfer_id",
        )
    identity = materialization.materialization_sha256
    incoming = f"{REMOTE_INCOMING_PARENT}/{identity}/{transfer_id}"
    prepare_argv = build_ssh_argv(
        endpoint,
        [
            "/usr/bin/python3.12",
            "-",
            "prepare",
            "--identity",
            identity,
            "--transfer-id",
            transfer_id,
        ],
    )
    install_document_argv = build_copy_argv(
        endpoint,
        direction="push",
        source=str(materialization.install_path),
        destination=f"{incoming}/install.json",
        recursive=False,
    )
    payload_argv = build_copy_argv(
        endpoint,
        direction="push",
        source=str(materialization.payload_root),
        destination=incoming,
        recursive=True,
    )
    install_argv = build_ssh_argv(
        endpoint,
        [
            "/usr/bin/python3.12",
            "-",
            "install",
            "--identity",
            identity,
            "--transfer-id",
            transfer_id,
        ],
    )
    steps = (
        {
            "name": "prepare",
            "transport": "ssh-stdin",
            "argv": prepare_argv,
            "stdin": "content-bound-installer",
        },
        {
            "name": "copy-install-document",
            "transport": "scp",
            "argv": install_document_argv,
            "stdin": None,
        },
        {
            "name": "copy-payload",
            "transport": "scp",
            "argv": payload_argv,
            "stdin": None,
        },
        {
            "name": "install",
            "transport": "ssh-stdin",
            "argv": install_argv,
            "stdin": "content-bound-installer",
        },
    )
    return ServicePushPlan(
        materialization=materialization,
        endpoint=endpoint,
        installer_path=installer_path,
        transfer_id=transfer_id,
        incoming_path=incoming,
        steps=steps,
    )


def push_service_materialization(
    plan: ServicePushPlan,
    *,
    resolved_endpoint: SshEndpoint,
    instances: InstanceStore,
    popen_factory: Callable[..., Any] = subprocess.Popen,
) -> dict[str, Any]:
    """Execute the exact plan against a separately resolved active endpoint."""

    current = load_service_materialization(plan.materialization.root)
    if current.install_document != plan.materialization.install_document:
        _fail(
            "service materialization changed after push planning",
            code="service_materialization_drift",
        )
    expected_plan = build_service_push_plan(
        current,
        endpoint=resolved_endpoint,
        installer_path=plan.installer_path,
        transfer_id=plan.transfer_id,
    )
    if plan != expected_plan:
        _fail(
            "service push plan changed after validation",
            code="invalid_service_push_plan",
        )
    ensure_known_hosts_file(resolved_endpoint.known_hosts_file)
    completed_steps: list[str] = []
    for step in expected_plan.steps:
        current = load_service_materialization(plan.materialization.root)
        if current.install_document != plan.materialization.install_document:
            _fail(
                "service materialization changed during push",
                code="service_materialization_drift",
            )
        source = f"service-push-{step['name']}"
        if step["stdin"] == "content-bound-installer":
            with _installer_stream(
                plan.installer_path,
                expected=current.install_document["installer"],
            ) as installer:
                return_code = run_with_activity(
                    step["argv"],
                    instances=instances,
                    name=resolved_endpoint.instance_name,
                    expected_operation_id=resolved_endpoint.operation_id,
                    expected_pod_id=resolved_endpoint.pod_id,
                    source=source,
                    stdin=installer,
                    popen_factory=popen_factory,
                )
        else:
            return_code = run_with_activity(
                step["argv"],
                instances=instances,
                name=resolved_endpoint.instance_name,
                expected_operation_id=resolved_endpoint.operation_id,
                expected_pod_id=resolved_endpoint.pod_id,
                source=source,
                popen_factory=popen_factory,
            )
        if return_code != 0:
            raise RunpodLocalError(
                f"service deployment step {step['name']} exited {return_code}",
                code="service_deployment_step_failed",
            )
        completed_steps.append(step["name"])
    return {
        "schema_version": PUSH_RESULT_SCHEMA,
        "executed": True,
        "provider_mutation": False,
        "materialization_sha256": current.materialization_sha256,
        "instance": {
            "name": resolved_endpoint.instance_name,
            "operation_id": resolved_endpoint.operation_id,
            "pod_id": resolved_endpoint.pod_id,
        },
        "status": "installed",
        "completed_steps": completed_steps,
    }
