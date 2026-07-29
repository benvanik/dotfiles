"""Private crash-safe local state and same-host locking."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pathlib
import re
import stat
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from .errors import RunpodLocalError
from .paths import ensure_private_directory


RECORD_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
NAMESPACE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


@dataclass(frozen=True)
class StateRecordScan:
    """One independently readable state record or its exact local failure."""

    name: str
    value: dict[str, Any] | None
    error: RunpodLocalError | None


def validate_record_name(name: str) -> str:
    if not RECORD_NAME_PATTERN.fullmatch(name):
        raise RunpodLocalError(
            "local names must start with a lowercase letter and contain only "
            "lowercase letters, digits, and hyphens (maximum 63 characters)",
            code="invalid_local_name",
        )
    return name


def _write_json_atomic(path: pathlib.Path, value: Any) -> None:
    ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = pathlib.Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            json.dump(value, temporary_file, indent=2, sort_keys=True)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
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


class StateStore:
    """Local receipts are advisory; provider state remains authoritative."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root

    def _namespace_directory(self, namespace: str) -> pathlib.Path:
        if not NAMESPACE_PATTERN.fullmatch(namespace):
            raise RunpodLocalError(
                f"invalid state namespace: {namespace!r}",
                code="invalid_state_namespace",
            )
        return self.root / namespace

    def record_path(self, namespace: str, name: str) -> pathlib.Path:
        validate_record_name(name)
        return self._namespace_directory(namespace) / f"{name}.json"

    def read(self, namespace: str, name: str) -> dict[str, Any] | None:
        path = self.record_path(namespace, name)
        try:
            metadata = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                raise RunpodLocalError(
                    f"state record is not a regular file: {path}",
                    code="unsafe_state_record",
                )
            if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
                raise RunpodLocalError(
                    f"state record is not owned by the current user: {path}",
                    code="unsafe_state_record",
                )
            if metadata.st_mode & 0o077:
                raise RunpodLocalError(
                    f"state record permissions are broader than 0600: {path}",
                    code="unsafe_state_permissions",
                )
            with path.open("r", encoding="utf-8") as state_file:
                value = json.load(state_file)
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as error:
            raise RunpodLocalError(
                f"state record is not valid JSON: {path}",
                code="invalid_state_record",
            ) from error
        if not isinstance(value, dict):
            raise RunpodLocalError(
                f"state record is not a JSON object: {path}",
                code="invalid_state_record",
            )
        return value

    def write(self, namespace: str, name: str, value: dict[str, Any]) -> None:
        path = self.record_path(namespace, name)
        _write_json_atomic(path, value)

    def list(self, namespace: str) -> list[dict[str, Any]]:
        records = []
        for scanned in self.scan(namespace):
            if scanned.error is not None:
                raise scanned.error
            if scanned.value is not None:
                records.append(scanned.value)
        return records

    def scan(self, namespace: str) -> list[StateRecordScan]:
        """Read records independently so one corrupt file cannot hide peers."""

        directory = self._namespace_directory(namespace)
        try:
            paths = sorted(directory.glob("*.json"))
        except OSError as error:
            raise RunpodLocalError(
                f"cannot list state namespace {namespace}: {error}",
                code="state_list_error",
            ) from error
        records: list[StateRecordScan] = []
        for path in paths:
            name = path.stem
            try:
                validate_record_name(name)
                record = self.read(namespace, name)
            except RunpodLocalError as error:
                records.append(
                    StateRecordScan(name=name, value=None, error=error)
                )
                continue
            except OSError as error:
                records.append(
                    StateRecordScan(
                        name=name,
                        value=None,
                        error=RunpodLocalError(
                            f"cannot read state record {path}: {error}",
                            code="state_read_error",
                        ),
                    )
                )
                continue
            if record is not None:
                records.append(
                    StateRecordScan(name=name, value=record, error=None)
                )
        return records

    @contextlib.contextmanager
    def locked(self, scope: str) -> Iterator[None]:
        validate_record_name(scope)
        lock_directory = ensure_private_directory(self.root / "locks")
        lock_path = lock_directory / f"{scope}.lock"
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as error:
            raise RunpodLocalError(
                f"cannot open local state lock {lock_path}: {error}",
                code="state_lock_error",
            ) from error
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
