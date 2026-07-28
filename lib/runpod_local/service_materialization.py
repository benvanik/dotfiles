"""Content-addressed local materialization for generic model services."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import pathlib
import re
import stat
from dataclasses import dataclass
from typing import Any

from .errors import RunpodLocalError
from .paths import ensure_private_directory
from .runtime_catalog import RuntimeDefinition
from .service_bundle import build_service_bundle_plan
from .service_definition import InferenceServiceDefinition
from .service_huggingface import HuggingFaceClosure
from .service_vllm import DEFAULT_REMOTE_PORT

INSTALL_SCHEMA = "runpod.inference-service-install.v1"
INSTALL_IDENTITY_SCHEMA = "runpod.inference-service-install-identity.v1"
MATERIALIZATION_PLAN_SCHEMA = "runpod.inference-service-materialization-plan.v1"
MATERIALIZATION_RESULT_SCHEMA = "runpod.inference-service-materialization.v1"
INSTALLER_RELATIVE_PATH = pathlib.PurePosixPath(
    "runpod/service_deploy/install-service.py"
)
REMOTE_SESSION_ROOT = pathlib.PurePosixPath("/root/runpod-session")
REMOTE_RUNTIME_CONTROL_ROOT = REMOTE_SESSION_ROOT / "control" / "runtime-verifier"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SERVICE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
REMOTE_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9._+@%=-]+$")
MAX_LOCAL_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_LOCAL_MEMBER_BYTES = 16 * 1024 * 1024
ALLOWED_MODES = frozenset({"0600", "0644", "0755"})
ALLOWED_ROLES = frozenset(
    {
        "implementation-member",
        "implementation-receipt",
        "runtime-manifest",
        "runtime-verifier",
        "deployment-manifest",
    }
)
AT_FDCWD = -100
AT_SYMLINK_FOLLOW = 0x400


def _fail(message: str, *, code: str) -> None:
    raise RunpodLocalError(message, code=code)


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


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _relative_path(value: Any, *, label: str) -> pathlib.PurePosixPath:
    if not isinstance(value, str):
        _fail(f"{label} is not a string", code="invalid_service_materialization")
    path = pathlib.PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or str(path) != value
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        _fail(f"{label} is unsafe", code="invalid_service_materialization")
    return path


def _remote_path(value: Any, *, label: str) -> pathlib.PurePosixPath:
    if not isinstance(value, str):
        _fail(f"{label} is not a string", code="invalid_service_materialization")
    path = pathlib.PurePosixPath(value)
    if (
        not path.is_absolute()
        or str(path) != value
        or "\\" in value
        or not (path == REMOTE_SESSION_ROOT or REMOTE_SESSION_ROOT in path.parents)
        or any(
            REMOTE_SEGMENT_PATTERN.fullmatch(part) is None
            for part in path.relative_to(REMOTE_SESSION_ROOT).parts
        )
    ):
        _fail(
            f"{label} is outside the remote session root",
            code="invalid_service_materialization",
        )
    return path


def _source_file_bytes(
    path: pathlib.Path,
    *,
    maximum_bytes: int,
    label: str,
    required_mode: int | None = None,
) -> bytes:
    """Read one current-UID source through one stable no-follow descriptor."""

    try:
        before = path.lstat()
    except OSError as error:
        raise RunpodLocalError(
            f"cannot inspect {label}: {path}",
            code="unsafe_service_materialization_source",
        ) from error
    if (
        path.is_symlink()
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size < 1
        or before.st_size > maximum_bytes
        or before.st_mode & 0o002
        or (required_mode is not None and stat.S_IMODE(before.st_mode) != required_mode)
        or (hasattr(os, "getuid") and before.st_uid != os.getuid())
    ):
        _fail(
            f"{label} has an unsafe identity: {path}",
            code="unsafe_service_materialization_source",
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RunpodLocalError(
            f"cannot safely open {label}: {path}",
            code="unsafe_service_materialization_source",
        ) from error
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
            or (
                required_mode is not None
                and stat.S_IMODE(opened.st_mode) != required_mode
            )
        ):
            _fail(
                f"{label} changed while opening: {path}",
                code="service_materialization_source_drift",
            )
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
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
        or len(payload) > maximum_bytes
        or after.st_dev != opened.st_dev
        or after.st_ino != opened.st_ino
        or not stat.S_ISREG(after.st_mode)
        or after.st_uid != opened.st_uid
        or after.st_nlink != opened.st_nlink
        or stat.S_IMODE(after.st_mode) != stat.S_IMODE(opened.st_mode)
        or (required_mode is not None and stat.S_IMODE(after.st_mode) != required_mode)
        or after.st_size != opened.st_size
        or after.st_mtime_ns != opened.st_mtime_ns
        or after.st_ctime_ns != opened.st_ctime_ns
    ):
        _fail(
            f"{label} changed while reading: {path}",
            code="service_materialization_source_drift",
        )
    return payload


def _safe_local_file_bytes(
    path: pathlib.Path,
    *,
    mode: int,
    maximum_bytes: int,
) -> bytes:
    payload = _source_file_bytes(
        path,
        maximum_bytes=maximum_bytes,
        label="materialized file",
        required_mode=mode,
    )
    return payload


def _derived_remote_directories(
    files: list[dict[str, Any]],
) -> list[dict[str, str]]:
    directories = {REMOTE_SESSION_ROOT}
    for record in files:
        path = pathlib.PurePosixPath(record["remote_path"])
        directories.update(
            parent
            for parent in path.parents
            if parent == REMOTE_SESSION_ROOT or REMOTE_SESSION_ROOT in parent.parents
        )
    return [
        {"path": str(path), "mode": "0700"}
        for path in sorted(
            directories,
            key=lambda item: (len(item.parts), str(item)),
        )
    ]


def _validate_file_roles(files: list[dict[str, Any]]) -> None:
    implementation_parent = (
        REMOTE_SESSION_ROOT / "control" / "inference-service-runtime"
    )
    services_root = REMOTE_SESSION_ROOT / "services"
    implementation_roots: set[pathlib.PurePosixPath] = set()
    implementation_members: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    runtime_roles: set[str] = set()
    deployments: list[dict[str, Any]] = []
    for record in files:
        role = record["role"]
        local = pathlib.PurePosixPath(record["local_path"])
        remote = pathlib.PurePosixPath(record["remote_path"])
        if role.startswith("implementation-"):
            try:
                relative = remote.relative_to(implementation_parent)
            except ValueError:
                _fail(
                    "implementation file is outside its fixed root",
                    code="invalid_service_materialization",
                )
            if (
                len(relative.parts) < 2
                or SHA256_PATTERN.fullmatch(relative.parts[0]) is None
                or local
                != pathlib.PurePosixPath(
                    "payload",
                    "implementation",
                    *relative.parts[1:],
                )
            ):
                _fail(
                    "implementation file mapping is malformed",
                    code="invalid_service_materialization",
                )
            implementation_roots.add(implementation_parent / relative.parts[0])
            if role == "implementation-receipt":
                if relative.parts[1:] != ("bundle.json",) or record["mode"] != "0600":
                    _fail(
                        "implementation receipt mapping is malformed",
                        code="invalid_service_materialization",
                    )
                receipts.append(record)
            else:
                if record["mode"] not in {"0644", "0755"}:
                    _fail(
                        "implementation member mode is malformed",
                        code="invalid_service_materialization",
                    )
                implementation_members.append(record)
        elif role in {"runtime-manifest", "runtime-verifier"}:
            expected_name = (
                "runtime-manifest.json"
                if role == "runtime-manifest"
                else "verify-runtime.py"
            )
            if (
                remote != REMOTE_RUNTIME_CONTROL_ROOT / expected_name
                or local
                != pathlib.PurePosixPath(
                    "payload",
                    "runtime-control",
                    expected_name,
                )
                or record["mode"] != "0600"
                or role in runtime_roles
            ):
                _fail(
                    "runtime control mapping is malformed",
                    code="invalid_service_materialization",
                )
            runtime_roles.add(role)
        elif role == "deployment-manifest":
            try:
                relative = remote.relative_to(services_root)
            except ValueError:
                _fail(
                    "deployment manifest is outside its fixed root",
                    code="invalid_service_materialization",
                )
            if (
                len(relative.parts) != 4
                or SERVICE_ID_PATTERN.fullmatch(relative.parts[0]) is None
                or relative.parts[1] != "deployments"
                or SHA256_PATTERN.fullmatch(relative.parts[2]) is None
                or relative.parts[3] != "deployment.json"
                or local
                != pathlib.PurePosixPath(
                    "payload",
                    "service",
                    "deployment.json",
                )
                or record["mode"] != "0600"
            ):
                _fail(
                    "deployment manifest mapping is malformed",
                    code="invalid_service_materialization",
                )
            deployments.append(record)
    if (
        len(implementation_roots) != 1
        or not implementation_members
        or len(receipts) != 1
        or runtime_roles != {"runtime-manifest", "runtime-verifier"}
        or len(deployments) != 1
        or receipts[0]["publish_order"]
        <= max(record["publish_order"] for record in implementation_members)
        or deployments[0]["publish_order"]
        != max(record["publish_order"] for record in files)
    ):
        _fail(
            "materialization is not one complete deployment closure",
            code="invalid_service_materialization",
        )


@dataclass(frozen=True)
class MaterializationPayload:
    """One exact local payload, sourced either from the repo or generated."""

    record: dict[str, Any]
    source_path: pathlib.Path | None
    generated_bytes: bytes | None

    def bytes(self) -> bytes:
        if (self.source_path is None) == (self.generated_bytes is None):
            _fail(
                "materialization payload has ambiguous byte ownership",
                code="invalid_service_materialization",
            )
        if self.source_path is not None:
            payload = _source_file_bytes(
                self.source_path,
                maximum_bytes=MAX_LOCAL_MEMBER_BYTES,
                label="materialization source",
            )
        else:
            assert self.generated_bytes is not None
            payload = self.generated_bytes
        if (
            len(payload) != self.record["bytes"]
            or _sha256(payload) != self.record["sha256"]
        ):
            _fail(
                "materialization payload changed after planning",
                code="service_materialization_source_drift",
            )
        return payload


@dataclass(frozen=True)
class ServiceMaterializationPlan:
    """A non-mutating plan for one exact local transfer closure."""

    source_root: pathlib.Path
    local_root: pathlib.Path
    installer_path: pathlib.Path
    installer: dict[str, Any]
    install_document: dict[str, Any]
    payloads: tuple[MaterializationPayload, ...]
    bundle_plan: dict[str, Any]

    @property
    def materialization_sha256(self) -> str:
        return self.install_document["materialization_sha256"]

    def safe_summary(self) -> dict[str, Any]:
        return {
            "schema_version": MATERIALIZATION_PLAN_SCHEMA,
            "executed": False,
            "materialization_sha256": self.materialization_sha256,
            "local_root": str(self.local_root),
            "install_path": str(self.local_root / "install.json"),
            "payload_root": str(self.local_root / "payload"),
            "installer": self.installer,
            "directories": self.install_document["directories"],
            "files": self.install_document["files"],
            "implementation_bundle_sha256": self.bundle_plan["implementation_bundle"][
                "bundle_sha256"
            ],
            "deployment_manifest_sha256": self.bundle_plan["deployment_manifest"][
                "sha256"
            ],
        }


@dataclass(frozen=True)
class MaterializedService:
    """A verified, complete local transfer closure."""

    root: pathlib.Path
    install_document: dict[str, Any]

    @property
    def materialization_sha256(self) -> str:
        return self.install_document["materialization_sha256"]

    @property
    def install_path(self) -> pathlib.Path:
        return self.root / "install.json"

    @property
    def payload_root(self) -> pathlib.Path:
        return self.root / "payload"

    def safe_summary(self) -> dict[str, Any]:
        return {
            "schema_version": MATERIALIZATION_RESULT_SCHEMA,
            "materialization_sha256": self.materialization_sha256,
            "root": str(self.root),
            "install_path": str(self.install_path),
            "payload_root": str(self.payload_root),
            "file_count": len(self.install_document["files"]),
        }


def _payload_record(
    *,
    local_path: str,
    remote_path: str,
    mode: str,
    payload: bytes,
    role: str,
    publish_order: int,
) -> dict[str, Any]:
    _relative_path(local_path, label="materialization local path")
    _remote_path(remote_path, label="materialization remote path")
    if (
        mode not in ALLOWED_MODES
        or role not in ALLOWED_ROLES
        or not 1 <= len(payload) <= MAX_LOCAL_MEMBER_BYTES
    ):
        _fail(
            "materialization payload descriptor is unsupported",
            code="invalid_service_materialization",
        )
    return {
        "local_path": local_path,
        "remote_path": remote_path,
        "mode": mode,
        "bytes": len(payload),
        "sha256": _sha256(payload),
        "role": role,
        "publish_order": publish_order,
    }


def build_service_materialization_plan(
    definition: InferenceServiceDefinition,
    *,
    source_root: pathlib.Path,
    state_root: pathlib.Path,
    runtime: RuntimeDefinition,
    closure: HuggingFaceClosure,
    remote_port: int = DEFAULT_REMOTE_PORT,
) -> ServiceMaterializationPlan:
    """Plan the generic implementation plus sole generated deployment input."""

    source_root = source_root.absolute()
    state_root = state_root.expanduser().absolute()
    bundle_plan = build_service_bundle_plan(
        definition,
        source_root=source_root,
        runtime=runtime,
        closure=closure,
        remote_port=remote_port,
    )
    implementation = bundle_plan["implementation_bundle"]
    payloads: list[MaterializationPayload] = []
    order = 0
    for member in implementation["files"]:
        source_path = source_root.joinpath(
            *pathlib.PurePosixPath(member["source_path"]).parts
        )
        payload = _source_file_bytes(
            source_path,
            maximum_bytes=MAX_LOCAL_MEMBER_BYTES,
            label="implementation member",
        )
        if len(payload) != member["bytes"] or _sha256(payload) != member["sha256"]:
            _fail(
                "implementation member changed after bundle planning",
                code="service_materialization_source_drift",
            )
        record = _payload_record(
            local_path=f"payload/implementation/{member['bundle_path']}",
            remote_path=(f"{implementation['remote_root']}/{member['bundle_path']}"),
            mode=member["mode"],
            payload=payload,
            role="implementation-member",
            publish_order=order,
        )
        payloads.append(
            MaterializationPayload(
                record=record,
                source_path=source_path,
                generated_bytes=None,
            )
        )
        order += 1

    receipt = implementation["receipt"]
    receipt_payload = _canonical_bytes(receipt["document"])
    receipt_record = _payload_record(
        local_path="payload/implementation/bundle.json",
        remote_path=receipt["remote_path"],
        mode=receipt["mode"],
        payload=receipt_payload,
        role="implementation-receipt",
        publish_order=order,
    )
    if (
        receipt_record["bytes"] != receipt["bytes"]
        or receipt_record["sha256"] != receipt["sha256"]
    ):
        _fail(
            "implementation receipt descriptor is inconsistent",
            code="invalid_service_materialization",
        )
    payloads.append(
        MaterializationPayload(
            record=receipt_record,
            source_path=None,
            generated_bytes=receipt_payload,
        )
    )
    order += 1

    for role, name, payload, descriptor in (
        (
            "runtime-manifest",
            "runtime-manifest.json",
            runtime.manifest_bytes,
            runtime.safe_summary()["manifest"],
        ),
        (
            "runtime-verifier",
            "verify-runtime.py",
            runtime.verifier_bytes,
            runtime.safe_summary()["verifier"],
        ),
    ):
        record = _payload_record(
            local_path=f"payload/runtime-control/{name}",
            remote_path=str(REMOTE_RUNTIME_CONTROL_ROOT / name),
            mode="0600",
            payload=payload,
            role=role,
            publish_order=order,
        )
        if (
            record["remote_path"] != descriptor["remote_path"]
            or record["bytes"] != descriptor["bytes"]
            or record["sha256"] != descriptor["sha256"]
        ):
            _fail(
                "runtime control descriptor is inconsistent",
                code="invalid_service_materialization",
            )
        payloads.append(
            MaterializationPayload(
                record=record,
                source_path=None,
                generated_bytes=payload,
            )
        )
        order += 1

    deployment = bundle_plan["deployment_manifest"]
    deployment_payload = _canonical_bytes(deployment["document"])
    deployment_record = _payload_record(
        local_path="payload/service/deployment.json",
        remote_path=deployment["remote_path"],
        mode=deployment["mode"],
        payload=deployment_payload,
        role="deployment-manifest",
        publish_order=order,
    )
    if (
        deployment_record["bytes"] != deployment["bytes"]
        or deployment_record["sha256"] != deployment["sha256"]
    ):
        _fail(
            "deployment manifest descriptor is inconsistent",
            code="invalid_service_materialization",
        )
    payloads.append(
        MaterializationPayload(
            record=deployment_record,
            source_path=None,
            generated_bytes=deployment_payload,
        )
    )

    installer_path = source_root.joinpath(*INSTALLER_RELATIVE_PATH.parts)
    installer_payload = _source_file_bytes(
        installer_path,
        maximum_bytes=1024 * 1024,
        label="service installer",
    )
    installer = {
        "bytes": len(installer_payload),
        "sha256": _sha256(installer_payload),
    }
    files = [item.record for item in payloads]
    directories = _derived_remote_directories(files)
    identity = {
        "schema_version": INSTALL_IDENTITY_SCHEMA,
        "installer": installer,
        "directories": directories,
        "files": files,
    }
    materialization_sha256 = _sha256(_canonical_bytes(identity))
    install_document = {
        "schema_version": INSTALL_SCHEMA,
        "materialization_sha256": materialization_sha256,
        "installer": installer,
        "directories": directories,
        "files": files,
    }
    return ServiceMaterializationPlan(
        source_root=source_root,
        local_root=(state_root / "service-materializations" / materialization_sha256),
        installer_path=installer_path,
        installer=installer,
        install_document=install_document,
        payloads=tuple(payloads),
        bundle_plan=bundle_plan,
    )


def _expected_local_tree(
    root: pathlib.Path,
    document: dict[str, Any],
) -> tuple[set[pathlib.Path], set[pathlib.Path]]:
    files = {root / "install.json"}
    directories = {root}
    for record in document["files"]:
        relative = _relative_path(
            record["local_path"],
            label="materialization local path",
        )
        path = root.joinpath(*relative.parts)
        files.add(path)
        directories.update(
            parent
            for parent in path.parents
            if parent == root or root in parent.parents
        )
    return files, directories


def _validate_install_document(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "materialization_sha256",
        "installer",
        "directories",
        "files",
    }:
        _fail(
            "materialization install document has unsupported fields",
            code="invalid_service_materialization",
        )
    if value["schema_version"] != INSTALL_SCHEMA:
        _fail(
            "materialization install schema is unsupported",
            code="invalid_service_materialization",
        )
    identity = value["materialization_sha256"]
    installer = value["installer"]
    directories = value["directories"]
    files = value["files"]
    if (
        not isinstance(identity, str)
        or SHA256_PATTERN.fullmatch(identity) is None
        or not isinstance(installer, dict)
        or set(installer) != {"bytes", "sha256"}
        or not isinstance(installer["bytes"], int)
        or isinstance(installer["bytes"], bool)
        or not 1 <= installer["bytes"] <= 1024 * 1024
        or not isinstance(installer["sha256"], str)
        or SHA256_PATTERN.fullmatch(installer["sha256"]) is None
        or not isinstance(directories, list)
        or not directories
        or not isinstance(files, list)
        or not files
    ):
        _fail(
            "materialization install identity is malformed",
            code="invalid_service_materialization",
        )
    local_paths: set[pathlib.PurePosixPath] = set()
    remote_paths: set[pathlib.PurePosixPath] = set()
    orders: list[int] = []
    for record in files:
        if not isinstance(record, dict) or set(record) != {
            "local_path",
            "remote_path",
            "mode",
            "bytes",
            "sha256",
            "role",
            "publish_order",
        }:
            _fail(
                "materialization file record is malformed",
                code="invalid_service_materialization",
            )
        local = _relative_path(
            record["local_path"],
            label="materialization local path",
        )
        remote = _remote_path(
            record["remote_path"],
            label="materialization remote path",
        )
        if (
            not local.parts
            or local.parts[0] != "payload"
            or local in local_paths
            or remote in remote_paths
            or record["mode"] not in ALLOWED_MODES
            or record["role"] not in ALLOWED_ROLES
            or not isinstance(record["bytes"], int)
            or isinstance(record["bytes"], bool)
            or not 1 <= record["bytes"] <= MAX_LOCAL_MEMBER_BYTES
            or not isinstance(record["sha256"], str)
            or SHA256_PATTERN.fullmatch(record["sha256"]) is None
            or not isinstance(record["publish_order"], int)
            or isinstance(record["publish_order"], bool)
            or record["publish_order"] < 0
        ):
            _fail(
                "materialization file identity is malformed",
                code="invalid_service_materialization",
            )
        local_paths.add(local)
        remote_paths.add(remote)
        orders.append(record["publish_order"])
    if orders != sorted(orders) or len(orders) != len(set(orders)):
        _fail(
            "materialization publication order is malformed",
            code="invalid_service_materialization",
        )
    _validate_file_roles(files)
    if directories != _derived_remote_directories(files):
        _fail(
            "materialization remote directory closure is malformed",
            code="invalid_service_materialization",
        )
    identity_document = {
        "schema_version": INSTALL_IDENTITY_SCHEMA,
        "installer": installer,
        "directories": directories,
        "files": files,
    }
    if _sha256(_canonical_bytes(identity_document)) != identity:
        _fail(
            "materialization identity does not match its closure",
            code="invalid_service_materialization",
        )
    return value


def _json_document(payload: bytes) -> dict[str, Any]:
    def reject_duplicates(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _fail(
                    f"materialization document repeats field {key!r}",
                    code="invalid_service_materialization",
                )
            result[key] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunpodLocalError(
            "materialization install document is not valid JSON",
            code="invalid_service_materialization",
        ) from error
    return _validate_install_document(value)


def _verify_local_tree(
    root: pathlib.Path,
    document: dict[str, Any],
    *,
    complete: bool,
) -> None:
    expected_files, expected_directories = _expected_local_tree(root, document)
    observed_files: set[pathlib.Path] = set()
    observed_directories: set[pathlib.Path] = set()
    for directory, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        directory_path = pathlib.Path(directory)
        observed_directories.add(directory_path)
        try:
            directory_metadata = directory_path.lstat()
        except OSError as error:
            raise RunpodLocalError(
                f"cannot inspect materialization directory: {directory_path}",
                code="unsafe_service_materialization",
            ) from error
        if (
            directory_path.is_symlink()
            or not stat.S_ISDIR(directory_metadata.st_mode)
            or stat.S_IMODE(directory_metadata.st_mode) != 0o700
            or (hasattr(os, "getuid") and directory_metadata.st_uid != os.getuid())
        ):
            _fail(
                f"materialization directory is unsafe: {directory_path}",
                code="unsafe_service_materialization",
            )
        for name in directory_names:
            child = directory_path / name
            metadata = child.lstat()
            if child.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
                _fail(
                    f"materialization contains a non-directory: {child}",
                    code="unsafe_service_materialization",
                )
        for name in file_names:
            child = directory_path / name
            metadata = child.lstat()
            if child.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                _fail(
                    f"materialization contains a non-regular file: {child}",
                    code="unsafe_service_materialization",
                )
            observed_files.add(child)
    if (
        not observed_files.issubset(expected_files)
        or not observed_directories.issubset(expected_directories)
        or (
            complete
            and (
                observed_files != expected_files
                or observed_directories != expected_directories
            )
        )
    ):
        _fail(
            "local materialization tree differs from its exact closure",
            code="unsafe_service_materialization",
        )
    by_path = {
        root.joinpath(
            *_relative_path(
                record["local_path"],
                label="materialization local path",
            ).parts
        ): record
        for record in document["files"]
    }
    for path in observed_files:
        if path == root / "install.json":
            expected = _canonical_bytes(document)
            mode = 0o600
        else:
            record = by_path[path]
            expected = None
            mode = int(record["mode"], 8)
        payload = _safe_local_file_bytes(
            path,
            mode=mode,
            maximum_bytes=MAX_LOCAL_DOCUMENT_BYTES,
        )
        if path == root / "install.json":
            valid = payload == expected
        else:
            valid = (
                len(payload) == record["bytes"] and _sha256(payload) == record["sha256"]
            )
        if not valid:
            _fail(
                f"materialized file differs from its identity: {path}",
                code="unsafe_service_materialization",
            )


def _publish_no_replace(
    *,
    destination: pathlib.Path,
    payload: bytes,
    mode: int,
) -> None:
    if os.path.lexists(destination):
        existing = _safe_local_file_bytes(
            destination,
            mode=mode,
            maximum_bytes=MAX_LOCAL_DOCUMENT_BYTES,
        )
        if existing != payload:
            _fail(
                f"materialized path already has another identity: {destination}",
                code="service_materialization_conflict",
            )
        return
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        parent_descriptor = os.open(destination.parent, flags)
    except OSError as error:
        raise RunpodLocalError(
            f"cannot open materialization destination: {destination.parent}",
            code="unsafe_service_materialization",
        ) from error
    try:
        if not hasattr(os, "O_TMPFILE"):
            _fail(
                "local filesystem has no anonymous publication support",
                code="service_materialization_failed",
            )
        try:
            descriptor = os.open(
                ".",
                os.O_WRONLY
                | os.O_TMPFILE
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                mode,
                dir_fd=parent_descriptor,
            )
        except OSError as error:
            raise RunpodLocalError(
                "local filesystem cannot create an anonymous publication inode",
                code="service_materialization_failed",
            ) from error
        try:
            os.fchmod(descriptor, mode)
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    _fail(
                        "materialization write made no progress",
                        code="service_materialization_failed",
                    )
                offset += written
            os.fsync(descriptor)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 0
                or stat.S_IMODE(metadata.st_mode) != mode
                or metadata.st_size != len(payload)
            ):
                _fail(
                    "anonymous materialization inode changed while writing",
                    code="service_materialization_failed",
                )
            try:
                linkat = ctypes.CDLL(None, use_errno=True).linkat
            except AttributeError as error:
                raise RunpodLocalError(
                    "local runtime has no publication primitive",
                    code="service_materialization_failed",
                ) from error
            linkat.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
            )
            linkat.restype = ctypes.c_int
            result = linkat(
                AT_FDCWD,
                f"/proc/self/fd/{descriptor}".encode("ascii"),
                parent_descriptor,
                os.fsencode(destination.name),
                AT_SYMLINK_FOLLOW,
            )
            if result != 0 and ctypes.get_errno() != errno.EEXIST:
                _fail(
                    "anonymous materialization publication failed",
                    code="service_materialization_failed",
                )
        finally:
            os.close(descriptor)
        if os.path.lexists(destination):
            existing = _safe_local_file_bytes(
                destination,
                mode=mode,
                maximum_bytes=MAX_LOCAL_DOCUMENT_BYTES,
            )
            if existing != payload:
                _fail(
                    f"materialized path raced with another identity: {destination}",
                    code="service_materialization_conflict",
                )
        else:
            _fail(
                "anonymous materialization publication produced no file",
                code="service_materialization_failed",
            )
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def materialize_service(
    plan: ServiceMaterializationPlan,
) -> MaterializedService:
    """Publish an exact local closure, with install.json as the final marker."""

    installer_payload = _source_file_bytes(
        plan.installer_path,
        maximum_bytes=1024 * 1024,
        label="service installer",
    )
    if (
        len(installer_payload) != plan.installer["bytes"]
        or _sha256(installer_payload) != plan.installer["sha256"]
    ):
        _fail(
            "service installer changed after planning",
            code="service_materialization_source_drift",
        )
    _validate_install_document(plan.install_document)
    ensure_private_directory(plan.local_root.parent)
    ensure_private_directory(plan.local_root)
    _, expected_directories = _expected_local_tree(
        plan.local_root,
        plan.install_document,
    )
    for directory in sorted(
        expected_directories,
        key=lambda item: (len(item.parts), str(item)),
    ):
        ensure_private_directory(directory)
    marker = plan.local_root / "install.json"
    _verify_local_tree(
        plan.local_root,
        plan.install_document,
        complete=os.path.lexists(marker),
    )
    by_local = {item.record["local_path"]: item for item in plan.payloads}
    if set(by_local) != {
        record["local_path"] for record in plan.install_document["files"]
    }:
        _fail(
            "materialization plan payload closure is inconsistent",
            code="invalid_service_materialization",
        )
    for record in plan.install_document["files"]:
        source = by_local[record["local_path"]]
        relative = _relative_path(
            record["local_path"],
            label="materialization local path",
        )
        _publish_no_replace(
            destination=plan.local_root.joinpath(*relative.parts),
            payload=source.bytes(),
            mode=int(record["mode"], 8),
        )
    _publish_no_replace(
        destination=marker,
        payload=_canonical_bytes(plan.install_document),
        mode=0o600,
    )
    return load_service_materialization(plan.local_root)


def load_service_materialization(
    root: pathlib.Path,
) -> MaterializedService:
    """Load and fully verify one completed local transfer closure."""

    root = root.expanduser().absolute()
    marker = root / "install.json"
    payload = _safe_local_file_bytes(
        marker,
        mode=0o600,
        maximum_bytes=MAX_LOCAL_DOCUMENT_BYTES,
    )
    document = _json_document(payload)
    if payload != _canonical_bytes(document):
        _fail(
            "materialization install document is not canonical JSON",
            code="invalid_service_materialization",
        )
    if root.name != document["materialization_sha256"]:
        _fail(
            "materialization directory is not content-addressed",
            code="invalid_service_materialization",
        )
    _verify_local_tree(root, document, complete=True)
    return MaterializedService(root=root, install_document=document)
