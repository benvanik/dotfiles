"""Portable, permanent profile-to-workload bindings."""

from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import pathlib
import re
import secrets
import stat
from typing import Any, Protocol

from .documents import canonical_json_bytes, read_owned_regular_file
from .errors import ModelLabError
from .paths import profiles_root
from .service_definition import ServiceDefinition

PROFILE_BINDING_SCHEMA = "model-lab.profile-binding.v1"
PROFILE_BINDING_FILE_NAME = "service-binding.json"
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ProfileRoute(Protocol):
    profile_id: str
    service_id: str


@dataclasses.dataclass(frozen=True)
class ProfileBinding:
    profile_id: str
    service_id: str
    workload_sha256: str

    def normalized(self) -> dict[str, str]:
        return {
            "schema": PROFILE_BINDING_SCHEMA,
            "profile_id": self.profile_id,
            "service_id": self.service_id,
            "workload_sha256": self.workload_sha256,
        }


def parse_profile_binding(value: Any) -> ProfileBinding:
    fields = {"schema", "profile_id", "service_id", "workload_sha256"}
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value.get("schema") != PROFILE_BINDING_SCHEMA
    ):
        raise ModelLabError(
            "profile binding has unsupported fields or schema",
            code="invalid_profile_binding",
        )
    profile_id = value["profile_id"]
    service_id = value["service_id"]
    workload_sha256 = value["workload_sha256"]
    if (
        not isinstance(profile_id, str)
        or not _IDENTIFIER.fullmatch(profile_id)
        or not isinstance(service_id, str)
        or not _IDENTIFIER.fullmatch(service_id)
        or not isinstance(workload_sha256, str)
        or not _SHA256.fullmatch(workload_sha256)
    ):
        raise ModelLabError(
            "profile binding contains an invalid identity",
            code="invalid_profile_binding",
        )
    return ProfileBinding(
        profile_id=profile_id,
        service_id=service_id,
        workload_sha256=workload_sha256,
    )


class ProfileBindingStore:
    """Creates or attests one immutable semantic binding per profile."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root

    def path(self, profile_id: str) -> pathlib.Path:
        if not isinstance(profile_id, str) or not _IDENTIFIER.fullmatch(profile_id):
            raise ModelLabError(
                "profile ID is invalid",
                code="invalid_profile_binding",
            )
        return profiles_root(self.root) / profile_id / PROFILE_BINDING_FILE_NAME

    def load(self, profile_id: str) -> ProfileBinding | None:
        path = self.path(profile_id)
        try:
            payload = read_owned_regular_file(path, label="profile binding")
        except ModelLabError as error:
            if error.code == "unsafe_authored_document" and not path.exists():
                return None
            raise
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ModelLabError(
                f"profile binding is not valid JSON: {path}",
                code="invalid_profile_binding",
            ) from error
        if canonical_json_bytes(value) != payload:
            raise ModelLabError(
                f"profile binding is not canonical JSON: {path}",
                code="invalid_profile_binding",
            )
        return parse_profile_binding(value)

    def attest(
        self,
        profile: ProfileRoute,
        service: ServiceDefinition,
    ) -> ProfileBinding:
        if profile.service_id != service.service_id:
            raise ModelLabError(
                "profile and service identifiers do not agree",
                code="profile_service_mismatch",
            )
        expected = ProfileBinding(
            profile_id=profile.profile_id,
            service_id=service.service_id,
            workload_sha256=service.workload_sha256,
        )
        directory = self.path(profile.profile_id).parent
        descriptor = self._open_profile_directory(directory)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            current = self.load(profile.profile_id)
            if current is None:
                self._publish(descriptor, directory, expected)
                current = self.load(profile.profile_id)
            if current != expected:
                raise ModelLabError(
                    "profile is permanently bound to "
                    f"{current.service_id}/{current.workload_sha256}; changing "
                    "service or model workload requires a new profile ID",
                    code="profile_binding_drift",
                )
            return current
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _open_profile_directory(path: pathlib.Path) -> int:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise ModelLabError(
                f"cannot open profile directory {path}: {error}",
                code="unsafe_profile_binding_state",
            ) from error
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            or metadata.st_mode & 0o022
        ):
            os.close(descriptor)
            raise ModelLabError(
                f"profile directory has an unsafe identity: {path}",
                code="unsafe_profile_binding_state",
            )
        return descriptor

    @staticmethod
    def _publish(
        directory_descriptor: int,
        directory: pathlib.Path,
        binding: ProfileBinding,
    ) -> None:
        destination_name = PROFILE_BINDING_FILE_NAME
        temporary_name = (
            f".{PROFILE_BINDING_FILE_NAME}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        )
        payload = canonical_json_bytes(binding.normalized())
        file_descriptor: int | None = None
        replaced = False
        try:
            file_descriptor = os.open(
                temporary_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_descriptor,
            )
            position = 0
            while position < len(payload):
                written = os.write(file_descriptor, payload[position:])
                if written <= 0:
                    raise ModelLabError(
                        "short write while publishing profile binding",
                        code="profile_binding_publish_failed",
                    )
                position += written
            os.fsync(file_descriptor)
            os.close(file_descriptor)
            file_descriptor = None
            os.replace(
                temporary_name,
                destination_name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
            replaced = True
            os.fsync(directory_descriptor)
        except ModelLabError:
            raise
        except OSError as error:
            code = (
                "profile_binding_publish_durability_unknown"
                if replaced
                else "profile_binding_publish_failed"
            )
            raise ModelLabError(
                f"cannot publish profile binding in {directory}: {error}",
                code=code,
            ) from error
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass
