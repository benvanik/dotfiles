"""Credential storage that never accepts API keys in process arguments."""

from __future__ import annotations

import os
import pathlib
import stat
import tempfile
from collections.abc import Mapping

from .errors import RunpodLocalError
from .paths import credentials_file, ensure_private_directory


MAX_API_KEY_BYTES = 8192


def validate_api_key(value: str) -> str:
    if (
        not value
        or len(value.encode("utf-8")) > MAX_API_KEY_BYTES
        or value != value.strip()
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise RunpodLocalError(
            "Runpod API key must be a non-empty single token with no whitespace",
            code="invalid_api_key",
        )
    return value


class ApiCredential:
    __slots__ = ("_token", "source", "path")

    def __init__(
        self, token: str, *, source: str, path: pathlib.Path | None = None
    ) -> None:
        self._token = validate_api_key(token)
        self.source = source
        self.path = path

    @property
    def token(self) -> str:
        return self._token

    def __repr__(self) -> str:
        return f"ApiCredential(source={self.source!r}, token=<redacted>)"


class CredentialStore:
    def __init__(
        self,
        path: pathlib.Path | None = None,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.path = path or credentials_file()
        self.environment = environment if environment is not None else os.environ

    def load(self, *, required: bool = True) -> ApiCredential | None:
        environment_value = self.environment.get("RUNPOD_API_KEY")
        if environment_value:
            return ApiCredential(environment_value, source="environment")
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            if required:
                raise RunpodLocalError(
                    "no Runpod credential configured; run `runpod-auth login` "
                    "from a trusted terminal",
                    code="credential_missing",
                )
            return None
        if self.path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise RunpodLocalError(
                f"Runpod credential path is not a regular file: {self.path}",
                code="unsafe_credential_file",
            )
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise RunpodLocalError(
                f"Runpod credential file is not owned by the current user: {self.path}",
                code="unsafe_credential_file",
            )
        if metadata.st_mode & 0o077:
            raise RunpodLocalError(
                f"Runpod credential file must have mode 0600: {self.path}",
                code="unsafe_credential_permissions",
            )
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags)
            with os.fdopen(descriptor, "rb") as credential_file:
                raw = credential_file.read(MAX_API_KEY_BYTES + 1)
        except OSError as error:
            raise RunpodLocalError(
                f"cannot read Runpod credential file {self.path}: {error}",
                code="credential_read_error",
            ) from error
        if len(raw) > MAX_API_KEY_BYTES:
            raise RunpodLocalError(
                f"Runpod credential file exceeds {MAX_API_KEY_BYTES} bytes",
                code="invalid_credential_file",
            )
        try:
            token = raw.decode("utf-8").rstrip("\n")
        except UnicodeDecodeError as error:
            raise RunpodLocalError(
                "Runpod credential file is not UTF-8",
                code="invalid_credential_file",
            ) from error
        return ApiCredential(token, source="file", path=self.path)

    def store(self, token: str) -> ApiCredential:
        token = validate_api_key(token)
        parent = ensure_private_directory(self.path.parent)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=parent, prefix=f".{self.path.name}.", suffix=".tmp"
        )
        temporary_path = pathlib.Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as credential_file:
                credential_file.write(token)
                credential_file.write("\n")
                credential_file.flush()
                os.fsync(credential_file.fileno())
            os.replace(temporary_path, self.path)
            self.path.chmod(0o600)
            directory_descriptor = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except Exception:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            raise
        return ApiCredential(token, source="file", path=self.path)

    def remove(self) -> bool:
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            return False
        if self.path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise RunpodLocalError(
                f"refusing to remove unsafe credential path: {self.path}",
                code="unsafe_credential_file",
            )
        self.path.unlink()
        return True

    def status(self) -> dict[str, object]:
        credential = self.load(required=False)
        if credential is None:
            return {
                "configured": False,
                "source": None,
                "path": str(self.path),
            }
        return {
            "configured": True,
            "source": credential.source,
            "path": str(credential.path) if credential.path is not None else None,
        }
