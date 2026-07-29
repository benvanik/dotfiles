"""Private model-lab receipts for exact service installations."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .errors import ModelLabError
from runpod_local.errors import RunpodLocalError
from runpod_local.instances import InstanceStore, OPERATION_ID_PATTERN
from runpod_local.remote import SshEndpoint
from .service_definition import ServiceDefinition
from .service_huggingface import HuggingFaceClosure
from .service_materialization import (
    MaterializedService,
    load_service_materialization,
)
from runpod_local.state import StateStore, validate_record_name

INSTALLATION_SCHEMA = "model-lab.service-installation.v1"
INSTALLATION_IDENTITY_SCHEMA = "model-lab.service-installation-identity.v1"
SERVICE_CONTRACT_SCHEMA = "model-lab.service-request.v1"
INSTALLATION_NAMESPACE = "service-installations"
MAX_DEPLOYMENT_DOCUMENT_BYTES = 16 * 1024 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PROVIDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,191}$")
SERVICE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
REMOTE_IMPLEMENTATION_PARENT = pathlib.PurePosixPath(
    "/root/runpod-session/control/model-service-runtime"
)
REMOTE_SERVICE_PARENT = pathlib.PurePosixPath("/root/runpod-session/services")
RELATIVE_ENTRYPOINT = pathlib.PurePosixPath("bin/model-lab-service-runtime")


def _fail(message: str, *, code: str) -> None:
    raise ModelLabError(message, code=code)


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("ascii")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _required_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        _fail(
            f"{label} is not a SHA-256 identity",
            code="invalid_service_installation",
        )
    return value


def _required_service_id(value: Any) -> str:
    if not isinstance(value, str) or SERVICE_ID_PATTERN.fullmatch(value) is None:
        _fail(
            "installation service ID is invalid",
            code="invalid_service_installation",
        )
    return value


def _required_port(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
        _fail(
            "installation service port is invalid",
            code="invalid_service_installation",
        )
    return value


@dataclass(frozen=True)
class ServiceDeploymentRequest:
    """Model/runtime request identity independent of implementation source."""

    service_id: str
    service_plan_sha256: str
    huggingface_closure_sha256: str
    remote_port: int

    def as_dict(self) -> dict[str, Any]:
        value = {
            "schema_version": SERVICE_CONTRACT_SCHEMA,
            "service_id": _required_service_id(self.service_id),
            "service_plan_sha256": _required_sha256(
                self.service_plan_sha256,
                label="service plan",
            ),
            "huggingface_closure_sha256": _required_sha256(
                self.huggingface_closure_sha256,
                label="Hugging Face closure",
            ),
            "remote_port": _required_port(self.remote_port),
        }
        return value

    @property
    def request_sha256(self) -> str:
        return _sha256(self.as_dict())


def build_service_deployment_request(
    definition: ServiceDefinition,
    *,
    closure: HuggingFaceClosure,
    remote_port: int,
) -> ServiceDeploymentRequest:
    """Bind the authored service semantics to one generated model closure."""

    service = definition.normalized_plan()
    model = service["model"]
    closure_document = closure.as_dict()
    expected_source = {
        "kind": model["source"],
        "repository": model["repository"],
        "revision": model["revision"],
    }
    if (
        closure_document["source"] != expected_source
        or closure_document["checkpoint"]["requested_selector"] != model["checkpoint"]
    ):
        _fail(
            "generated Hugging Face closure does not match the service model",
            code="mismatched_service_huggingface_closure",
        )
    return ServiceDeploymentRequest(
        service_id=service["service_id"],
        service_plan_sha256=definition.plan_sha256,
        huggingface_closure_sha256=closure.closure_sha256,
        remote_port=_required_port(remote_port),
    )


def _deployment_record(
    materialization: MaterializedService,
) -> dict[str, Any]:
    records = [
        record
        for record in materialization.install_document["files"]
        if record["role"] == "deployment-manifest"
    ]
    if len(records) != 1:
        _fail(
            "materialization has no unique deployment manifest",
            code="invalid_service_installation",
        )
    return records[0]


def _safe_deployment_bytes(
    materialization: MaterializedService,
    record: dict[str, Any],
) -> bytes:
    relative = pathlib.PurePosixPath(record["local_path"])
    path = materialization.root.joinpath(*relative.parts)
    try:
        before = path.lstat()
    except OSError as error:
        raise ModelLabError(
            f"cannot inspect materialized deployment manifest: {path}",
            code="unsafe_service_installation",
        ) from error
    if (
        path.is_symlink()
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size < 1
        or before.st_size > MAX_DEPLOYMENT_DOCUMENT_BYTES
        or stat.S_IMODE(before.st_mode) != 0o600
        or (hasattr(os, "getuid") and before.st_uid != os.getuid())
    ):
        _fail(
            "materialized deployment manifest has an unsafe identity",
            code="unsafe_service_installation",
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ModelLabError(
            "cannot safely open materialized deployment manifest",
            code="unsafe_service_installation",
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size != before.st_size
            or opened.st_uid != before.st_uid
            or opened.st_nlink != before.st_nlink
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            _fail(
                "materialized deployment manifest changed while opening",
                code="unsafe_service_installation",
            )
        chunks: list[bytes] = []
        remaining = MAX_DEPLOYMENT_DOCUMENT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        len(payload) != opened.st_size
        or len(payload) != record["bytes"]
        or hashlib.sha256(payload).hexdigest() != record["sha256"]
        or after.st_dev != opened.st_dev
        or after.st_ino != opened.st_ino
        or after.st_size != opened.st_size
        or after.st_mtime_ns != opened.st_mtime_ns
        or after.st_ctime_ns != opened.st_ctime_ns
        or after.st_uid != opened.st_uid
        or after.st_nlink != opened.st_nlink
        or stat.S_IMODE(after.st_mode) != 0o600
    ):
        _fail(
            "materialized deployment manifest changed while reading",
            code="unsafe_service_installation",
        )
    return payload


def _deployment_document(
    materialization: MaterializedService,
) -> tuple[dict[str, Any], dict[str, Any]]:
    record = _deployment_record(materialization)
    payload = _safe_deployment_bytes(materialization, record)

    def reject_duplicates(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                _fail(
                    f"deployment manifest repeats field {key!r}",
                    code="invalid_service_installation",
                )
            value[key] = item
        return value

    try:
        value = json.loads(payload, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ModelLabError(
            "materialized deployment manifest is not valid JSON",
            code="invalid_service_installation",
        ) from error
    if not isinstance(value, dict) or payload != _canonical_bytes(value):
        _fail(
            "materialized deployment manifest is not canonical",
            code="invalid_service_installation",
        )
    return value, record


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(
            f"deployment manifest {label} is malformed",
            code="invalid_service_installation",
        )
    return value


def materialized_service_identity(
    materialization: MaterializedService,
) -> tuple[ServiceDeploymentRequest, dict[str, str]]:
    """Extract and cross-check the exact installed paths and service request."""

    materialization = load_service_materialization(materialization.root)
    document, deployment_record = _deployment_document(materialization)
    definition = _mapping(document.get("definition"), label="definition")
    service = _mapping(definition.get("service"), label="service")
    closure = _mapping(
        document.get("huggingface_closure"),
        label="Hugging Face closure",
    )
    implementation = _mapping(
        document.get("implementation"),
        label="implementation",
    )
    deployment = _mapping(document.get("deployment"), label="deployment")
    launch = _mapping(deployment.get("launch"), label="launch")
    request = ServiceDeploymentRequest(
        service_id=_required_service_id(service.get("service_id")),
        service_plan_sha256=_required_sha256(
            definition.get("service_plan_sha256"),
            label="service plan",
        ),
        huggingface_closure_sha256=_required_sha256(
            closure.get("closure_sha256"),
            label="Hugging Face closure",
        ),
        remote_port=_required_port(launch.get("port")),
    )
    bundle_sha256 = _required_sha256(
        implementation.get("bundle_sha256"),
        label="implementation bundle",
    )
    entrypoint = implementation.get("entrypoint")
    manifest = deployment.get("manifest_path")
    deployment_id = _required_sha256(
        deployment.get("deployment_id"),
        label="deployment",
    )
    expected_root = REMOTE_IMPLEMENTATION_PARENT / bundle_sha256
    expected_entrypoint = expected_root / RELATIVE_ENTRYPOINT
    expected_manifest = (
        REMOTE_SERVICE_PARENT
        / request.service_id
        / "deployments"
        / deployment_id
        / "deployment.json"
    )
    files = materialization.install_document["files"]
    if (
        entrypoint != str(expected_entrypoint)
        or manifest != str(expected_manifest)
        or deployment_record["remote_path"] != manifest
        or len(
            [
                record
                for record in files
                if record["role"] == "implementation-member"
                and record["remote_path"] == entrypoint
                and record["mode"] == "0755"
            ]
        )
        != 1
    ):
        _fail(
            "materialization paths do not match the generated deployment",
            code="invalid_service_installation",
        )
    return request, {
        "deployment_manifest_sha256": deployment_record["sha256"],
        "implementation_bundle_sha256": bundle_sha256,
        "entrypoint": entrypoint,
        "manifest": manifest,
    }


def _record_name(instance_name: str, service_id: str) -> str:
    validate_record_name(instance_name)
    _required_service_id(service_id)
    digest = hashlib.sha256(
        f"{instance_name}\0{service_id}".encode("ascii")
    ).hexdigest()[:40]
    return f"installation-{digest}"


def _lock_name(instance_name: str, service_id: str) -> str:
    return _record_name(instance_name, service_id)


def _validate_endpoint_identity(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "name",
        "operation_id",
        "pod_id",
    }:
        _fail(
            "installation instance identity is malformed",
            code="invalid_service_installation",
        )
    name = value["name"]
    operation_id = value["operation_id"]
    pod_id = value["pod_id"]
    validate_record_name(name)
    if (
        not isinstance(operation_id, str)
        or OPERATION_ID_PATTERN.fullmatch(operation_id) is None
        or not isinstance(pod_id, str)
        or PROVIDER_ID_PATTERN.fullmatch(pod_id) is None
    ):
        _fail(
            "installation Pod identity is malformed",
            code="invalid_service_installation",
        )
    return value


def _validated_request(value: Any) -> ServiceDeploymentRequest:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "service_id",
        "service_plan_sha256",
        "huggingface_closure_sha256",
        "remote_port",
    }:
        _fail(
            "installation service request is malformed",
            code="invalid_service_installation",
        )
    if value["schema_version"] != SERVICE_CONTRACT_SCHEMA:
        _fail(
            "installation service request schema is unsupported",
            code="invalid_service_installation",
        )
    request = ServiceDeploymentRequest(
        service_id=value["service_id"],
        service_plan_sha256=value["service_plan_sha256"],
        huggingface_closure_sha256=value["huggingface_closure_sha256"],
        remote_port=value["remote_port"],
    )
    if request.as_dict() != value:
        _fail(
            "installation service request is not normalized",
            code="invalid_service_installation",
        )
    return request


@dataclass(frozen=True)
class InstalledService:
    """One verified local receipt and its exact generated materialization."""

    document: dict[str, Any]
    materialization: MaterializedService
    request: ServiceDeploymentRequest

    @property
    def instance(self) -> dict[str, str]:
        return self.document["instance"]

    @property
    def installation_sha256(self) -> str:
        return self.document["installation_sha256"]

    def safe_summary(self) -> dict[str, Any]:
        return {
            "schema_version": INSTALLATION_SCHEMA,
            "installation_sha256": self.installation_sha256,
            "instance": self.instance,
            "service": self.request.as_dict(),
            "materialization": self.document["materialization"],
        }


class ServiceInstallationStore:
    """Crash-safe model installation bindings beneath model-lab state."""

    def __init__(self, state: StateStore) -> None:
        self.state = state

    def receipt_path(
        self,
        *,
        instance_name: str,
        service_id: str,
    ) -> pathlib.Path:
        return self.state.record_path(
            INSTALLATION_NAMESPACE,
            _record_name(instance_name, service_id),
        )

    def _expected_materialization_root(
        self,
        materialization_sha256: str,
    ) -> pathlib.Path:
        return (
            (self.state.root / "service-materializations" / materialization_sha256)
            .expanduser()
            .absolute()
        )

    def _document(
        self,
        *,
        materialization: MaterializedService,
        endpoint: SshEndpoint,
    ) -> dict[str, Any]:
        request, generated = materialized_service_identity(materialization)
        expected_root = self._expected_materialization_root(
            materialization.materialization_sha256
        )
        if materialization.root != expected_root:
            _fail(
                "installed materialization is outside model-lab state",
                code="invalid_service_installation",
            )
        instance = _validate_endpoint_identity(
            {
                "name": endpoint.instance_name,
                "operation_id": endpoint.operation_id,
                "pod_id": endpoint.pod_id,
            }
        )
        materialization_identity = {
            "sha256": materialization.materialization_sha256,
            "root": str(materialization.root),
            **generated,
        }
        identity = {
            "schema_version": INSTALLATION_IDENTITY_SCHEMA,
            "instance": instance,
            "service": request.as_dict(),
            "materialization": materialization_identity,
        }
        return {
            "schema_version": INSTALLATION_SCHEMA,
            "installation_sha256": _sha256(identity),
            "instance": instance,
            "service": request.as_dict(),
            "materialization": materialization_identity,
        }

    def _validate(
        self,
        value: Any,
        *,
        expected_instance_name: str,
        expected_service_id: str,
    ) -> InstalledService:
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "installation_sha256",
            "instance",
            "service",
            "materialization",
        }:
            _fail(
                "service installation receipt is malformed",
                code="invalid_service_installation",
            )
        if value["schema_version"] != INSTALLATION_SCHEMA:
            _fail(
                "service installation receipt schema is unsupported",
                code="invalid_service_installation",
            )
        instance = _validate_endpoint_identity(value["instance"])
        request = _validated_request(value["service"])
        materialization_identity = value["materialization"]
        if (
            instance["name"] != expected_instance_name
            or request.service_id != expected_service_id
            or not isinstance(materialization_identity, dict)
            or set(materialization_identity)
            != {
                "sha256",
                "root",
                "deployment_manifest_sha256",
                "implementation_bundle_sha256",
                "entrypoint",
                "manifest",
            }
        ):
            _fail(
                "service installation receipt identity is inconsistent",
                code="invalid_service_installation",
            )
        materialization_sha256 = _required_sha256(
            materialization_identity["sha256"],
            label="materialization",
        )
        expected_root = self._expected_materialization_root(materialization_sha256)
        if materialization_identity["root"] != str(expected_root):
            _fail(
                "service installation materialization root is inconsistent",
                code="invalid_service_installation",
            )
        for field in (
            "deployment_manifest_sha256",
            "implementation_bundle_sha256",
        ):
            _required_sha256(
                materialization_identity[field],
                label=field,
            )
        identity = {
            "schema_version": INSTALLATION_IDENTITY_SCHEMA,
            "instance": instance,
            "service": request.as_dict(),
            "materialization": materialization_identity,
        }
        if not isinstance(value["installation_sha256"], str) or value[
            "installation_sha256"
        ] != _sha256(identity):
            _fail(
                "service installation receipt digest does not match",
                code="invalid_service_installation",
            )
        materialization = load_service_materialization(expected_root)
        observed_request, generated = materialized_service_identity(materialization)
        if observed_request != request or materialization_identity != {
            "sha256": materialization.materialization_sha256,
            "root": str(materialization.root),
            **generated,
        }:
            _fail(
                "service installation receipt differs from its materialization",
                code="invalid_service_installation",
            )
        return InstalledService(
            document=value,
            materialization=materialization,
            request=request,
        )

    def load(
        self,
        *,
        instance_name: str,
        service_id: str,
        required: bool = True,
    ) -> InstalledService | None:
        record = self.state.read(
            INSTALLATION_NAMESPACE,
            _record_name(instance_name, service_id),
        )
        if record is None:
            if required:
                raise ModelLabError(
                    "no installation receipt exists for this instance and "
                    "service; run install or use --installed-materialization "
                    "with an exact generated identity",
                    code="service_installation_not_found",
                )
            return None
        return self._validate(
            record,
            expected_instance_name=instance_name,
            expected_service_id=service_id,
        )

    def publish(
        self,
        *,
        materialization: MaterializedService,
        endpoint: SshEndpoint,
        instances: InstanceStore,
        clock: Callable[[], datetime.datetime] | None = None,
    ) -> tuple[InstalledService, bool]:
        """Commit only while the successful remote Pod still owns the lease."""

        if not isinstance(instances, InstanceStore):
            _fail(
                "installation publication requires a RunPod instance "
                "lease guard",
                code="invalid_service_installation",
            )
        desired = self._document(
            materialization=materialization,
            endpoint=endpoint,
        )
        service_id = desired["service"]["service_id"]
        record_name = _record_name(endpoint.instance_name, service_id)
        changed = False
        try:
            with instances.locked_active_lease(
                endpoint.instance_name,
                expected_operation_id=endpoint.operation_id,
                expected_pod_id=endpoint.pod_id,
                clock=clock,
            ):
                with self.state.locked(
                    _lock_name(endpoint.instance_name, service_id)
                ):
                    existing = self.state.read(
                        INSTALLATION_NAMESPACE,
                        record_name,
                    )
                    if existing != desired:
                        # Remote success authorizes replacement. Requiring the
                        # previous receipt's local materialization here would make
                        # a fresh install unable to recover from lost generated
                        # state.
                        self.state.write(
                            INSTALLATION_NAMESPACE,
                            record_name,
                            desired,
                        )
                        changed = True
                    installed = self.load(
                        instance_name=endpoint.instance_name,
                        service_id=service_id,
                    )
        except RunpodLocalError as error:
            raise ModelLabError(str(error), code=error.code) from error
        if installed is None:
            raise AssertionError("published installation unexpectedly absent")
        return installed, changed

    def inspect(
        self,
        *,
        materialization: MaterializedService,
        endpoint: SshEndpoint,
    ) -> InstalledService:
        """Build and verify a recovery binding without publishing state."""

        desired = self._document(
            materialization=materialization,
            endpoint=endpoint,
        )
        return self._validate(
            desired,
            expected_instance_name=endpoint.instance_name,
            expected_service_id=desired["service"]["service_id"],
        )

    def load_selector(
        self,
        value: str,
    ) -> MaterializedService:
        """Resolve one exact generated materialization beneath model-lab state."""

        if SHA256_PATTERN.fullmatch(value):
            identity = value
            root = self._expected_materialization_root(identity)
        else:
            candidate = pathlib.Path(value).expanduser().absolute()
            identity = candidate.name
            if SHA256_PATTERN.fullmatch(
                identity
            ) is None or candidate != self._expected_materialization_root(identity):
                _fail(
                    "installed materialization selector must be an exact "
                    "identity or its model-lab state path",
                    code="invalid_service_materialization_selector",
                )
            root = candidate
        return load_service_materialization(root)


def require_current_instance(
    installed: InstalledService,
    *,
    endpoint: SshEndpoint,
) -> None:
    expected = installed.instance
    if (
        expected["name"] != endpoint.instance_name
        or expected["operation_id"] != endpoint.operation_id
        or expected["pod_id"] != endpoint.pod_id
    ):
        raise ModelLabError(
            "service installation receipt belongs to another Pod operation; "
            "reinstall or use an exact materialization recovery selector",
            code="service_installation_instance_changed",
        )
