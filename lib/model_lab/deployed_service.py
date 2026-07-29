"""Immutable service-definition snapshots retained for lifecycle recovery."""

from __future__ import annotations

import hashlib
import os
import pathlib
import secrets
import stat

from .errors import ModelLabError
from .paths import ensure_private_directory
from .service_definition import ServiceDefinition, parse_service_toml


class DeployedServiceStore:
    """Retain the exact authored bytes needed to stop after config drift."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root

    def _path(self, service_id: str, service_sha256: str) -> pathlib.Path:
        return (
            self.root
            / "deployed-services"
            / service_id
            / f"{service_sha256}.toml"
        )

    def publish(self, service: ServiceDefinition) -> pathlib.Path:
        directory = ensure_private_directory(
            self.root / "deployed-services" / service.service_id
        )
        path = self._path(service.service_id, service.service_sha256)
        try:
            existing = self.load(service.service_id, service.service_sha256)
        except ModelLabError as error:
            if error.code != "deployed_service_not_found":
                raise
        else:
            if existing.source_bytes != service.source_bytes:
                raise ModelLabError(
                    "deployed service snapshot hash collision",
                    code="unsafe_deployed_service",
                )
            return path
        temporary = directory / f".snapshot-{secrets.token_hex(12)}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            position = 0
            while position < len(service.source_bytes):
                written = os.write(descriptor, service.source_bytes[position:])
                if written <= 0:
                    raise ModelLabError(
                        "short write while publishing deployed service",
                        code="unsafe_deployed_service",
                    )
                position += written
            os.fsync(descriptor)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        finally:
            os.close(descriptor)
        try:
            os.link(temporary, path)
        except FileExistsError:
            existing = self.load(service.service_id, service.service_sha256)
            if existing.source_bytes != service.source_bytes:
                raise ModelLabError(
                    "deployed service snapshot changed concurrently",
                    code="unsafe_deployed_service",
                )
        finally:
            temporary.unlink(missing_ok=True)
        directory_descriptor = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return path

    def load(self, service_id: str, service_sha256: str) -> ServiceDefinition:
        path = self._path(service_id, service_sha256)
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError as error:
            raise ModelLabError(
                "deployed service snapshot is unavailable",
                code="deployed_service_not_found",
            ) from error
        except OSError as error:
            raise ModelLabError(
                f"cannot open deployed service snapshot: {error}",
                code="unsafe_deployed_service",
            ) from error
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_mode & 0o077
                or metadata.st_size > 1024 * 1024
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            ):
                raise ModelLabError(
                    "deployed service snapshot has an unsafe identity",
                    code="unsafe_deployed_service",
                )
            chunks: list[bytes] = []
            remaining = metadata.st_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    raise ModelLabError(
                        "deployed service snapshot was truncated",
                        code="unsafe_deployed_service",
                    )
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
        finally:
            os.close(descriptor)
        service = parse_service_toml(payload, source=str(path))
        if (
            service.service_id != service_id
            or service.service_sha256 != service_sha256
            or hashlib.sha256(payload).hexdigest() != service.source_sha256
        ):
            raise ModelLabError(
                "deployed service snapshot identity is inconsistent",
                code="unsafe_deployed_service",
            )
        return service
