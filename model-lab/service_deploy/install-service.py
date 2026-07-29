#!/usr/bin/python3.12
"""Install one exact model-service materialization without replacing state."""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import pathlib
import re
import stat
import sys
from typing import Any

INSTALL_SCHEMA = "model-lab.service-install.v1"
INSTALL_IDENTITY_SCHEMA = "model-lab.service-install-identity.v1"
SESSION_ROOT = pathlib.Path("/root/runpod-session")
INCOMING_ROOT = SESSION_ROOT / "incoming" / "service-materializations"
IMPLEMENTATION_ROOT = SESSION_ROOT / "control" / "model-service-runtime"
RUNTIME_CONTROL_ROOT = SESSION_ROOT / "control" / "runtime-verifier"
SERVICES_ROOT = SESSION_ROOT / "services"
MAX_INSTALL_BYTES = 16 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TRANSFER_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SERVICE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
REMOTE_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9._+@%=-]+$")
AT_FDCWD = -100
AT_SYMLINK_FOLLOW = 0x400
ROLES = frozenset(
    {
        "implementation-member",
        "implementation-receipt",
        "runtime-manifest",
        "runtime-verifier",
        "deployment-manifest",
    }
)


class InstallError(Exception):
    """The incoming or installed deployment violates its exact contract."""


def fail(message: str) -> None:
    raise InstallError(message)


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("ascii")


def safe_relative_path(value: Any, *, label: str) -> pathlib.PurePosixPath:
    if not isinstance(value, str):
        fail(f"{label} is not a string")
    path = pathlib.PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in value
    ):
        fail(f"{label} is not a safe relative path")
    return path


def safe_absolute_path(value: Any, *, label: str) -> pathlib.Path:
    if not isinstance(value, str):
        fail(f"{label} is not a string")
    path = pathlib.Path(value)
    if (
        not path.is_absolute()
        or str(path) != value
        or value != os.path.normpath(value)
        or "\x00" in value
        or not (path == SESSION_ROOT or SESSION_ROOT in path.parents)
        or any(
            REMOTE_SEGMENT_PATTERN.fullmatch(part) is None
            for part in path.relative_to(SESSION_ROOT).parts
        )
    ):
        fail(f"{label} is not beneath the service session root")
    return path


def safe_file_bytes(
    path: pathlib.Path,
    *,
    mode: int,
    maximum_bytes: int,
) -> bytes:
    try:
        before = path.lstat()
    except OSError as error:
        raise InstallError(f"required file is absent: {path}") from error
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_uid != os.getuid()
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) != mode
        or not 1 <= before.st_size <= maximum_bytes
    ):
        fail(f"file has an unsafe identity: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise InstallError(f"cannot safely open file: {path}") from error
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != before.st_uid
            or opened.st_nlink != before.st_nlink
            or stat.S_IMODE(opened.st_mode) != mode
            or opened.st_size != before.st_size
        ):
            fail(f"file changed while opening: {path}")
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
        or after.st_size != opened.st_size
        or after.st_mtime_ns != opened.st_mtime_ns
        or after.st_ctime_ns != opened.st_ctime_ns
        or not stat.S_ISREG(after.st_mode)
        or after.st_uid != opened.st_uid
        or after.st_nlink != opened.st_nlink
        or stat.S_IMODE(after.st_mode) != mode
    ):
        fail(f"file changed while reading: {path}")
    return payload


def json_object(payload: bytes, *, label: str) -> dict[str, Any]:
    def reject_duplicates(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                fail(f"{label} repeats field {key!r}")
            value[key] = item
        return value

    try:
        value = json.loads(
            payload,
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InstallError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        fail(f"{label} is not an object")
    return value


def require_private_directory(path: pathlib.Path, *, create: bool) -> None:
    if create and not os.path.lexists(path):
        try:
            path.mkdir(mode=0o700)
        except OSError as error:
            raise InstallError(f"cannot create private directory: {path}") from error
    try:
        metadata = path.lstat()
    except OSError as error:
        raise InstallError(f"private directory is absent: {path}") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        fail(f"private directory has an unsafe identity: {path}")


def ensure_foundation(incoming: pathlib.Path) -> None:
    require_private_directory(SESSION_ROOT, create=True)
    require_private_directory(SESSION_ROOT / "incoming", create=True)
    require_private_directory(INCOMING_ROOT, create=True)
    require_private_directory(incoming.parent, create=True)
    require_private_directory(incoming, create=True)


def incoming_path(identity: str, transfer_id: str) -> pathlib.Path:
    if SHA256_PATTERN.fullmatch(identity) is None:
        fail("materialization identity is not a lowercase SHA-256")
    if TRANSFER_ID_PATTERN.fullmatch(transfer_id) is None:
        fail("transfer identity is not a lowercase SHA-256-shaped nonce")
    return INCOMING_ROOT / identity / transfer_id


def expected_final_directories(
    files: list[dict[str, Any]],
) -> list[str]:
    directories = {SESSION_ROOT}
    for record in files:
        path = pathlib.Path(record["remote_path"])
        directories.update(
            parent
            for parent in path.parents
            if parent == SESSION_ROOT or SESSION_ROOT in parent.parents
        )
    return [
        str(path)
        for path in sorted(
            directories,
            key=lambda path: (len(path.parts), str(path)),
        )
    ]


def validate_file_roles(files: list[dict[str, Any]]) -> None:
    implementation_roots: set[pathlib.Path] = set()
    deployment_paths: list[pathlib.Path] = []
    runtime_roles: dict[str, pathlib.Path] = {}
    receipt_paths: list[pathlib.Path] = []
    member_paths: list[pathlib.Path] = []
    for record in files:
        role = record["role"]
        remote_path = pathlib.Path(record["remote_path"])
        local_path = pathlib.PurePosixPath(record["local_path"])
        if role.startswith("implementation-"):
            try:
                relative = remote_path.relative_to(IMPLEMENTATION_ROOT)
            except ValueError:
                fail("implementation file is outside its fixed root")
            if (
                len(relative.parts) < 2
                or SHA256_PATTERN.fullmatch(relative.parts[0]) is None
            ):
                fail("implementation file has no bundle-derived root")
            root = IMPLEMENTATION_ROOT / relative.parts[0]
            implementation_roots.add(root)
            if local_path != pathlib.PurePosixPath(
                "payload",
                "implementation",
                *relative.parts[1:],
            ):
                fail("implementation file has an invalid local path")
            if role == "implementation-receipt":
                if relative.parts[1:] != ("bundle.json",) or record["mode"] != "0600":
                    fail("implementation receipt path is unsupported")
                receipt_paths.append(remote_path)
            else:
                if record["mode"] not in {"0644", "0755"}:
                    fail("implementation member mode is unsupported")
                member_paths.append(remote_path)
        elif role in {"runtime-manifest", "runtime-verifier"}:
            expected_name = (
                "runtime-manifest.json"
                if role == "runtime-manifest"
                else "verify-runtime.py"
            )
            if (
                remote_path != RUNTIME_CONTROL_ROOT / expected_name
                or local_path
                != pathlib.PurePosixPath(f"payload/runtime-control/{expected_name}")
                or record["mode"] != "0600"
            ):
                fail("runtime control path is unsupported")
            runtime_roles[role] = remote_path
        elif role == "deployment-manifest":
            try:
                relative = remote_path.relative_to(SERVICES_ROOT)
            except ValueError:
                fail("deployment manifest is outside the services root")
            if (
                len(relative.parts) != 4
                or SERVICE_ID_PATTERN.fullmatch(relative.parts[0]) is None
                or relative.parts[1] != "deployments"
                or SHA256_PATTERN.fullmatch(relative.parts[2]) is None
                or relative.parts[3] != "deployment.json"
                or local_path
                != pathlib.PurePosixPath("payload/service/deployment.json")
                or record["mode"] != "0600"
            ):
                fail("deployment manifest path is unsupported")
            deployment_paths.append(remote_path)
    if (
        len(implementation_roots) != 1
        or len(receipt_paths) != 1
        or not member_paths
        or set(runtime_roles) != {"runtime-manifest", "runtime-verifier"}
        or len(deployment_paths) != 1
    ):
        fail("install document does not contain one complete deployment closure")
    receipt_order = next(
        record["publish_order"]
        for record in files
        if record["role"] == "implementation-receipt"
    )
    deployment_order = next(
        record["publish_order"]
        for record in files
        if record["role"] == "deployment-manifest"
    )
    if receipt_order <= max(
        record["publish_order"]
        for record in files
        if record["role"] == "implementation-member"
    ) or deployment_order != max(record["publish_order"] for record in files):
        fail("install publication order does not publish identities last")


def validate_install_document(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "materialization_sha256",
        "installer",
        "directories",
        "files",
    }:
        fail("install document has unsupported or missing fields")
    if value["schema_version"] != INSTALL_SCHEMA:
        fail("install document schema is unsupported")
    identity = value["materialization_sha256"]
    if not isinstance(identity, str) or SHA256_PATTERN.fullmatch(identity) is None:
        fail("install document identity is malformed")
    installer = value["installer"]
    if (
        not isinstance(installer, dict)
        or set(installer) != {"bytes", "sha256"}
        or not isinstance(installer["bytes"], int)
        or isinstance(installer["bytes"], bool)
        or not 1 <= installer["bytes"] <= 1024 * 1024
        or not isinstance(installer["sha256"], str)
        or SHA256_PATTERN.fullmatch(installer["sha256"]) is None
    ):
        fail("install bootstrap identity is malformed")
    directories = value["directories"]
    if (
        not isinstance(directories, list)
        or not directories
        or any(
            not isinstance(record, dict)
            or set(record) != {"path", "mode"}
            or record["mode"] != "0700"
            for record in directories
        )
    ):
        fail("install directory closure is malformed")
    directory_paths = [
        safe_absolute_path(record["path"], label="install directory")
        for record in directories
    ]
    if len(set(directory_paths)) != len(directory_paths):
        fail("install directory closure has duplicate paths")
    files = value["files"]
    if not isinstance(files, list) or not files:
        fail("install file closure is malformed")
    local_paths: set[pathlib.PurePosixPath] = set()
    remote_paths: set[pathlib.Path] = set()
    publish_orders: list[int] = []
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
            fail("install file record is malformed")
        local_path = safe_relative_path(
            record["local_path"],
            label="install local path",
        )
        if not local_path.parts or local_path.parts[0] != "payload":
            fail("install local path is outside payload")
        remote_path = safe_absolute_path(
            record["remote_path"],
            label="install remote path",
        )
        if (
            local_path in local_paths
            or remote_path in remote_paths
            or record["mode"] not in {"0600", "0644", "0755"}
            or not isinstance(record["bytes"], int)
            or isinstance(record["bytes"], bool)
            or not 1 <= record["bytes"] <= MAX_MEMBER_BYTES
            or not isinstance(record["sha256"], str)
            or SHA256_PATTERN.fullmatch(record["sha256"]) is None
            or record["role"] not in ROLES
            or not isinstance(record["publish_order"], int)
            or isinstance(record["publish_order"], bool)
            or record["publish_order"] < 0
        ):
            fail("install file record identity is malformed")
        local_paths.add(local_path)
        remote_paths.add(remote_path)
        publish_orders.append(record["publish_order"])
    if publish_orders != sorted(publish_orders) or len(set(publish_orders)) != len(
        publish_orders
    ):
        fail("install file publication order is not unique and sorted")
    validate_file_roles(files)
    if [record["path"] for record in directories] != expected_final_directories(files):
        fail("install directory closure is not derived from its files")
    identity_document = {
        "schema_version": INSTALL_IDENTITY_SCHEMA,
        "installer": installer,
        "directories": directories,
        "files": files,
    }
    if hashlib.sha256(canonical_bytes(identity_document)).hexdigest() != identity:
        fail("install document identity does not match its closure")
    return value


def load_install_document(path: pathlib.Path) -> dict[str, Any]:
    payload = safe_file_bytes(
        path,
        mode=0o600,
        maximum_bytes=MAX_INSTALL_BYTES,
    )
    value = validate_install_document(json_object(payload, label="install document"))
    if payload != canonical_bytes(value):
        fail("install document is not canonical JSON")
    return value


def expected_incoming_paths(
    incoming: pathlib.Path,
    document: dict[str, Any],
) -> tuple[set[pathlib.Path], set[pathlib.Path]]:
    files = {incoming / "install.json"}
    directories = {incoming}
    for record in document["files"]:
        local = pathlib.PurePosixPath(record["local_path"])
        path = incoming.joinpath(*local.parts)
        files.add(path)
        directories.update(
            parent
            for parent in path.parents
            if parent == incoming or incoming in parent.parents
        )
    return files, directories


def verify_incoming(
    incoming: pathlib.Path,
    document: dict[str, Any],
    *,
    allow_missing: bool,
) -> None:
    expected_files, expected_directories = expected_incoming_paths(
        incoming,
        document,
    )
    observed_files: set[pathlib.Path] = set()
    observed_directories: set[pathlib.Path] = set()
    for directory, directory_names, file_names in os.walk(
        incoming,
        topdown=True,
        followlinks=False,
    ):
        directory_path = pathlib.Path(directory)
        observed_directories.add(directory_path)
        require_private_directory(directory_path, create=False)
        for name in directory_names:
            child = directory_path / name
            metadata = child.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                fail(f"incoming tree contains a non-directory: {child}")
        for name in file_names:
            child = directory_path / name
            metadata = child.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                fail(f"incoming tree contains a non-regular file: {child}")
            observed_files.add(child)
    if not observed_files.issubset(expected_files) or not observed_directories.issubset(
        expected_directories
    ):
        fail("incoming tree contains files outside the install closure")
    if not allow_missing and (
        observed_files != expected_files or observed_directories != expected_directories
    ):
        fail("incoming tree is incomplete")
    install_path = incoming / "install.json"
    if install_path in observed_files:
        payload = safe_file_bytes(
            install_path,
            mode=0o600,
            maximum_bytes=MAX_INSTALL_BYTES,
        )
        if payload != canonical_bytes(document):
            fail("incoming install document changed")
    by_local = {
        incoming.joinpath(*pathlib.PurePosixPath(record["local_path"]).parts): record
        for record in document["files"]
    }
    for path in observed_files.difference({install_path}):
        record = by_local[path]
        payload = safe_file_bytes(
            path,
            mode=int(record["mode"], 8),
            maximum_bytes=MAX_MEMBER_BYTES,
        )
        if (
            len(payload) != record["bytes"]
            or hashlib.sha256(payload).hexdigest() != record["sha256"]
        ):
            fail(f"incoming file does not match its identity: {path}")


def verify_existing_final_subset(
    document: dict[str, Any],
    *,
    complete: bool,
) -> None:
    for root, roles in (
        (
            IMPLEMENTATION_ROOT
            / next(
                pathlib.Path(record["remote_path"])
                .relative_to(IMPLEMENTATION_ROOT)
                .parts[0]
                for record in document["files"]
                if record["role"] == "implementation-member"
            ),
            {"implementation-member", "implementation-receipt"},
        ),
        (
            RUNTIME_CONTROL_ROOT,
            {"runtime-manifest", "runtime-verifier"},
        ),
    ):
        if not os.path.lexists(root):
            continue
        expected = {
            pathlib.Path(record["remote_path"])
            for record in document["files"]
            if record["role"] in roles
        }
        expected_directories = {root}
        for path in expected:
            expected_directories.update(
                parent
                for parent in path.parents
                if parent == root or root in parent.parents
            )
        observed: set[pathlib.Path] = set()
        observed_directories: set[pathlib.Path] = set()
        for directory, directory_names, file_names in os.walk(
            root,
            topdown=True,
            followlinks=False,
        ):
            directory_path = pathlib.Path(directory)
            observed_directories.add(directory_path)
            require_private_directory(directory_path, create=False)
            for name in directory_names:
                child = directory_path / name
                metadata = child.lstat()
                if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                    fail(f"installed closure contains a non-directory: {child}")
            for name in file_names:
                child = directory_path / name
                metadata = child.lstat()
                if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                    fail(f"installed closure contains a non-regular file: {child}")
                observed.add(child)
        if (
            not observed.issubset(expected)
            or not observed_directories.issubset(expected_directories)
            or (
                complete
                and (
                    observed != expected or observed_directories != expected_directories
                )
            )
        ):
            fail(f"installed closure differs from its exact identity: {root}")


def deployment_installation_paths(
    document: dict[str, Any],
) -> tuple[pathlib.Path, pathlib.Path, str]:
    deployment = next(
        pathlib.Path(record["remote_path"])
        for record in document["files"]
        if record["role"] == "deployment-manifest"
    )
    relative = deployment.relative_to(SERVICES_ROOT)
    service_root = SERVICES_ROOT / relative.parts[0]
    return deployment, service_root, relative.parts[2]


def require_private_regular_file(path: pathlib.Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise InstallError(f"service state is absent: {path}") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        fail(f"service state has an unsafe identity: {path}")


def verify_service_directory(
    document: dict[str, Any],
    *,
    allow_incomplete_target: bool,
) -> None:
    _, root, target_deployment_id = deployment_installation_paths(document)
    if not os.path.lexists(root):
        return
    require_private_directory(root, create=False)
    allowed_fixed = {
        "process.json",
        "service.log",
        "lifecycle.lock",
        "serving.lock",
        "setup.json",
    }
    allowed_receipt = re.compile(r"^(?:ready|cache-measurement)-[0-9a-f]{64}\.json$")
    for entry in os.scandir(root):
        metadata = entry.stat(follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            if entry.name != "deployments":
                fail(
                    f"service directory contains an undeclared directory: {entry.path}"
                )
            deployments_root = pathlib.Path(entry.path)
            require_private_directory(deployments_root, create=False)
            for version_entry in os.scandir(deployments_root):
                version_metadata = version_entry.stat(follow_symlinks=False)
                if (
                    not stat.S_ISDIR(version_metadata.st_mode)
                    or stat.S_ISLNK(version_metadata.st_mode)
                    or SHA256_PATTERN.fullmatch(version_entry.name) is None
                ):
                    fail(
                        "service deployments contain an undeclared entry: "
                        f"{version_entry.path}"
                    )
                version_root = pathlib.Path(version_entry.path)
                require_private_directory(version_root, create=False)
                version_entries = list(os.scandir(version_root))
                if (
                    allow_incomplete_target
                    and version_entry.name == target_deployment_id
                    and not version_entries
                ):
                    continue
                if (
                    len(version_entries) != 1
                    or version_entries[0].name != "deployment.json"
                ):
                    fail(f"installed deployment version is incomplete: {version_root}")
                require_private_regular_file(version_root / "deployment.json")
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or entry.name not in allowed_fixed
            and allowed_receipt.fullmatch(entry.name) is None
        ):
            fail(f"service directory contains an undeclared entry: {entry.path}")
        require_private_regular_file(pathlib.Path(entry.path))


def open_private_directory_descriptor(path: pathlib.Path) -> int:
    """Traverse an existing destination parent without following path swaps."""

    safe_absolute_path(str(path), label="publication directory")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open("/", flags)
    cursor = pathlib.Path("/")
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            cursor /= part
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                fail(f"publication ancestor is not a directory: {cursor}")
            if (cursor == SESSION_ROOT or SESSION_ROOT in cursor.parents) and (
                metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                fail(f"publication directory has an unsafe identity: {cursor}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def safe_file_bytes_at(
    parent_descriptor: int,
    name: str,
    *,
    display_path: pathlib.Path,
    mode: int,
    maximum_bytes: int,
) -> bytes:
    """Read one fixed child of an already-open private directory."""

    try:
        before = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        raise InstallError(f"required file is absent: {display_path}") from error
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) != mode
        or not 1 <= before.st_size <= maximum_bytes
    ):
        fail(f"file has an unsafe identity: {display_path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != before.st_uid
            or opened.st_nlink != before.st_nlink
            or stat.S_IMODE(opened.st_mode) != mode
            or opened.st_size != before.st_size
        ):
            fail(f"file changed while opening: {display_path}")
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
        or stat.S_IMODE(after.st_mode) != mode
        or after.st_size != opened.st_size
        or after.st_mtime_ns != opened.st_mtime_ns
        or after.st_ctime_ns != opened.st_ctime_ns
    ):
        fail(f"file changed while reading: {display_path}")
    return payload


def link_anonymous_file(
    descriptor: int,
    *,
    parent_descriptor: int,
    name: str,
) -> None:
    """Atomically publish one anonymous inode; EEXIST never replaces bytes."""

    try:
        linkat = ctypes.CDLL(None, use_errno=True).linkat
    except AttributeError as error:
        raise InstallError(
            "this runtime has no descriptor-relative publication primitive"
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
        os.fsencode(name),
        AT_SYMLINK_FOLLOW,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise FileExistsError(error_number, os.strerror(error_number), name)
        raise InstallError(
            f"anonymous no-replace publication failed: {os.strerror(error_number)}"
        )


def install_file(
    *,
    source: pathlib.Path,
    destination: pathlib.Path,
    mode: int,
    byte_count: int,
    digest: str,
) -> None:
    source_payload = safe_file_bytes(
        source,
        mode=mode,
        maximum_bytes=MAX_MEMBER_BYTES,
    )
    if (
        len(source_payload) != byte_count
        or hashlib.sha256(source_payload).hexdigest() != digest
    ):
        fail(f"incoming source does not match its identity: {source}")
    parent_descriptor = open_private_directory_descriptor(destination.parent)
    try:
        try:
            existing = safe_file_bytes_at(
                parent_descriptor,
                destination.name,
                display_path=destination,
                mode=mode,
                maximum_bytes=MAX_MEMBER_BYTES,
            )
        except InstallError as error:
            try:
                os.stat(
                    destination.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                existing = None
            else:
                raise error
        if existing is not None:
            if existing != source_payload:
                fail(f"installed path has another identity: {destination}")
            return
        if not hasattr(os, "O_TMPFILE"):
            fail("runtime filesystem has no anonymous publication support")
        flags = (
            os.O_WRONLY
            | os.O_TMPFILE
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(".", flags, mode, dir_fd=parent_descriptor)
        except OSError as error:
            raise InstallError(
                "runtime filesystem cannot create an anonymous publication inode"
            ) from error
        try:
            os.fchmod(descriptor, mode)
            offset = 0
            while offset < len(source_payload):
                written = os.write(descriptor, source_payload[offset:])
                if written <= 0:
                    fail("installed file write made no progress")
                offset += written
            os.fsync(descriptor)
            written_metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(written_metadata.st_mode)
                or written_metadata.st_uid != os.getuid()
                or written_metadata.st_nlink != 0
                or stat.S_IMODE(written_metadata.st_mode) != mode
                or written_metadata.st_size != len(source_payload)
            ):
                fail("anonymous publication inode changed while writing")
            try:
                link_anonymous_file(
                    descriptor,
                    parent_descriptor=parent_descriptor,
                    name=destination.name,
                )
            except FileExistsError:
                pass
        finally:
            os.close(descriptor)
        installed = safe_file_bytes_at(
            parent_descriptor,
            destination.name,
            display_path=destination,
            mode=mode,
            maximum_bytes=MAX_MEMBER_BYTES,
        )
        if installed != source_payload:
            fail(f"installed path raced with another identity: {destination}")
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def installed_files_match(
    *,
    incoming: pathlib.Path,
    document: dict[str, Any],
) -> bool:
    """Verify every existing destination and report whether all are present."""

    complete = True
    for record in document["files"]:
        destination = pathlib.Path(record["remote_path"])
        if not os.path.lexists(destination):
            complete = False
            continue
        local = pathlib.PurePosixPath(record["local_path"])
        source_payload = safe_file_bytes(
            incoming.joinpath(*local.parts),
            mode=int(record["mode"], 8),
            maximum_bytes=MAX_MEMBER_BYTES,
        )
        installed_payload = safe_file_bytes(
            destination,
            mode=int(record["mode"], 8),
            maximum_bytes=MAX_MEMBER_BYTES,
        )
        if (
            len(source_payload) != record["bytes"]
            or hashlib.sha256(source_payload).hexdigest() != record["sha256"]
            or installed_payload != source_payload
        ):
            fail(f"installed path has another identity: {destination}")
    return complete


def open_service_lock(path: pathlib.Path) -> int:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise InstallError(f"cannot safely open service lock: {path}") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            fail(f"service lock has an unsafe identity: {path}")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def acquire_install_locks(service_root: pathlib.Path) -> tuple[int, int]:
    """Serialize installation against every lifecycle and serving owner."""

    lifecycle_descriptor = open_service_lock(service_root / "lifecycle.lock")
    try:
        fcntl.flock(lifecycle_descriptor, fcntl.LOCK_EX)
        serving_descriptor = open_service_lock(service_root / "serving.lock")
        try:
            try:
                fcntl.flock(
                    serving_descriptor,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError:
                fail("cannot install a deployment while its service is running")
        except BaseException:
            os.close(serving_descriptor)
            raise
    except BaseException:
        os.close(lifecycle_descriptor)
        raise
    return lifecycle_descriptor, serving_descriptor


def prepare(identity: str, transfer_id: str) -> dict[str, Any]:
    incoming = incoming_path(identity, transfer_id)
    ensure_foundation(incoming)
    install_path = incoming / "install.json"
    if os.path.lexists(install_path):
        document = load_install_document(install_path)
        if document["materialization_sha256"] != identity:
            fail("incoming install document has another identity")
        verify_incoming(incoming, document, allow_missing=True)
    elif any(incoming.iterdir()):
        fail("incoming tree has partial state without its install document")
    return {
        "schema_version": "model-lab.service-install-operation.v1",
        "action": "prepare",
        "materialization_sha256": identity,
        "transfer_id": transfer_id,
        "incoming_path": str(incoming),
        "status": "ready-for-copy",
    }


def install(identity: str, transfer_id: str) -> dict[str, Any]:
    incoming = incoming_path(identity, transfer_id)
    require_private_directory(incoming, create=False)
    document = load_install_document(incoming / "install.json")
    if document["materialization_sha256"] != identity:
        fail("incoming install document has another identity")
    verify_incoming(incoming, document, allow_missing=False)
    deployment, service_root, _ = deployment_installation_paths(document)
    if os.path.lexists(deployment):
        if not installed_files_match(incoming=incoming, document=document):
            fail(
                "installed deployment manifest exists without its complete "
                "immutable closure"
            )
        verify_existing_final_subset(document, complete=True)
        verify_service_directory(
            document,
            allow_incomplete_target=False,
        )
        return {
            "schema_version": "model-lab.service-install-operation.v1",
            "action": "install",
            "materialization_sha256": identity,
            "transfer_id": transfer_id,
            "incoming_path": str(incoming),
            "status": "installed",
            "installed_file_count": len(document["files"]),
        }
    verify_existing_final_subset(document, complete=False)
    verify_service_directory(
        document,
        allow_incomplete_target=True,
    )
    require_private_directory(SERVICES_ROOT, create=True)
    require_private_directory(service_root, create=True)
    lifecycle_descriptor, serving_descriptor = acquire_install_locks(service_root)
    try:
        verify_existing_final_subset(document, complete=False)
        verify_service_directory(
            document,
            allow_incomplete_target=True,
        )
        if os.path.lexists(deployment):
            if not installed_files_match(incoming=incoming, document=document):
                fail(
                    "installed deployment manifest exists without its complete "
                    "immutable closure"
                )
        else:
            process_state = service_root / "process.json"
            if os.path.lexists(process_state):
                fail(
                    "cannot install a changed deployment while service process "
                    "state is retained"
                )
            for record in document["directories"]:
                require_private_directory(
                    pathlib.Path(record["path"]),
                    create=True,
                )
            for record in document["files"]:
                local = pathlib.PurePosixPath(record["local_path"])
                install_file(
                    source=incoming.joinpath(*local.parts),
                    destination=pathlib.Path(record["remote_path"]),
                    mode=int(record["mode"], 8),
                    byte_count=record["bytes"],
                    digest=record["sha256"],
                )
        verify_existing_final_subset(document, complete=True)
        verify_service_directory(
            document,
            allow_incomplete_target=False,
        )
    finally:
        os.close(serving_descriptor)
        os.close(lifecycle_descriptor)
    return {
        "schema_version": "model-lab.service-install-operation.v1",
        "action": "install",
        "materialization_sha256": identity,
        "transfer_id": transfer_id,
        "incoming_path": str(incoming),
        "status": "installed",
        "installed_file_count": len(document["files"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install one content-bound inference service deployment."
    )
    parser.add_argument("action", choices=("prepare", "install"))
    parser.add_argument(
        "--identity",
        required=True,
        help="exact materialization SHA-256",
    )
    parser.add_argument(
        "--transfer-id",
        required=True,
        help="unique lowercase 256-bit transfer-attempt identity",
    )
    arguments = parser.parse_args()
    try:
        result = (
            prepare(arguments.identity, arguments.transfer_id)
            if arguments.action == "prepare"
            else install(arguments.identity, arguments.transfer_id)
        )
    except InstallError as error:
        print(
            json.dumps(
                {
                    "schema_version": ("model-lab.service-install-error.v1"),
                    "error": "service_install_failed",
                    "message": str(error),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
