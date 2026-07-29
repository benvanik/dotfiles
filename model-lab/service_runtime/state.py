"""Private filesystem state and advisory leases for model lifecycle."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pathlib
import secrets
import stat
from dataclasses import dataclass
from typing import Any, Iterator

from model_lab.errors import ModelLabError

from .execution_environment import validate_runtime_execution_environment


PROCESS_STATE_SCHEMA = "model-lab.service-process.v1"
SETUP_RECEIPT_SCHEMA = "model-lab.service-setup.v1"
MAX_STATE_BYTES = 16 * 1024 * 1024


def _fail(message: str, *, code: str = "unsafe_service_runtime_state") -> None:
    raise ModelLabError(message, code=code)


def _mode(value: os.stat_result) -> int:
    return stat.S_IMODE(value.st_mode)


def ensure_private_directory(path: pathlib.Path, *, create: bool) -> None:
    """Require one current-UID directory with exact private permissions."""

    if create and not os.path.lexists(path):
        try:
            path.mkdir(mode=0o700)
        except OSError as error:
            raise ModelLabError(
                f"cannot create private runtime directory: {path}",
                code="unsafe_service_runtime_state",
            ) from error
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise ModelLabError(
            f"private runtime directory is absent: {path}",
            code="unsafe_service_runtime_state",
        ) from error
    if (
        not stat.S_ISDIR(path_stat.st_mode)
        or stat.S_ISLNK(path_stat.st_mode)
        or path_stat.st_uid != os.getuid()
        or _mode(path_stat) != 0o700
    ):
        _fail(f"private runtime directory has an unsafe identity: {path}")


def require_owned_untrusted_directory(path: pathlib.Path) -> None:
    """Open and bind one owned directory without treating its mode as authority.

    RunPod network volumes force directory permissions to 0777.  The volume is
    consequently an untrusted reconstructible content store, not private
    runtime state.  This check proves only that the named root is a current-UID
    non-symlink directory and that the descriptor names the object observed by
    ``lstat``.  Consumers must still traverse beneath that descriptor without
    following links and validate exact content identities.
    """

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    try:
        path_stat = path.lstat()
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ModelLabError(
            f"required untrusted runtime directory is absent or unsafe: {path}",
            code="unsafe_service_runtime_state",
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(path_stat.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or path_stat.st_uid != os.getuid()
            or not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_dev != path_stat.st_dev
            or opened.st_ino != path_stat.st_ino
        ):
            _fail(f"required untrusted runtime directory is unsafe: {path}")
    finally:
        os.close(descriptor)


def _open_private_file(
    path: pathlib.Path,
    *,
    create: bool,
    writable: bool,
) -> int:
    flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise ModelLabError(
            f"cannot safely open private runtime file: {path}",
            code="unsafe_service_runtime_state",
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or _mode(opened) != 0o600
        ):
            _fail(f"private runtime file has an unsafe identity: {path}")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


@dataclass
class AdvisoryLock:
    """One open private lock file and its current lease."""

    path: pathlib.Path
    descriptor: int

    def exclusive(self, *, nonblocking: bool = False) -> bool:
        operation = fcntl.LOCK_EX
        if nonblocking:
            operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(self.descriptor, operation)
        except BlockingIOError:
            return False
        return True

    def shared(self, *, nonblocking: bool = False) -> bool:
        operation = fcntl.LOCK_SH
        if nonblocking:
            operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(self.descriptor, operation)
        except BlockingIOError:
            return False
        return True

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1

    def __enter__(self) -> AdvisoryLock:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def open_advisory_lock(path: pathlib.Path, *, create: bool) -> AdvisoryLock:
    return AdvisoryLock(
        path=path,
        descriptor=_open_private_file(path, create=create, writable=True),
    )


@contextlib.contextmanager
def lifecycle_lock(
    path: pathlib.Path,
    *,
    create: bool,
    exclusive: bool,
) -> Iterator[AdvisoryLock]:
    lock = open_advisory_lock(path, create=create)
    try:
        acquired = lock.exclusive() if exclusive else lock.shared()
        if not acquired:
            _fail(f"cannot acquire service lifecycle lock: {path}")
        yield lock
    finally:
        lock.close()


def _read_descriptor(descriptor: int, *, maximum_bytes: int) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum_bytes + 1
    while remaining:
        chunk = os.read(descriptor, min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > maximum_bytes:
        _fail("private runtime document exceeds its size bound")
    return payload


def read_private_json(
    path: pathlib.Path,
    *,
    maximum_bytes: int = MAX_STATE_BYTES,
) -> tuple[dict[str, Any], bytes]:
    descriptor = _open_private_file(path, create=False, writable=False)
    try:
        opened = os.fstat(descriptor)
        if not 1 <= opened.st_size <= maximum_bytes:
            _fail(f"private runtime document has an invalid size: {path}")
        payload = _read_descriptor(descriptor, maximum_bytes=maximum_bytes)
        final = os.fstat(descriptor)
        if (
            final.st_size != opened.st_size
            or final.st_mtime_ns != opened.st_mtime_ns
            or final.st_ctime_ns != opened.st_ctime_ns
        ):
            _fail(f"private runtime document changed while reading: {path}")
    finally:
        os.close(descriptor)

    def reject_duplicate_fields(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                _fail(f"private runtime document repeats field {key!r}")
            value[key] = item
        return value

    try:
        value = json.loads(
            payload,
            object_pairs_hook=reject_duplicate_fields,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ModelLabError(
            f"private runtime document is not valid JSON: {path}",
            code="unsafe_service_runtime_state",
        ) from error
    if not isinstance(value, dict):
        _fail(f"private runtime document is not an object: {path}")
    return value, payload


def atomic_write_private_json(path: pathlib.Path, value: dict[str, Any]) -> bytes:
    """Atomically publish one bounded mode-0600 JSON document."""

    payload = (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("ascii")
    if not 1 <= len(payload) <= MAX_STATE_BYTES:
        _fail("private runtime document exceeds its size bound")
    temporary = path.parent / f".{path.name}.{secrets.token_hex(16)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(temporary, flags, 0o600)
        position = 0
        while position < len(payload):
            written = os.write(descriptor, payload[position:])
            if written <= 0:
                _fail("private runtime document write made no progress")
            position += written
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        parent_descriptor = os.open(path.parent, directory_flags)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return payload


def write_private_json_once(path: pathlib.Path, value: dict[str, Any]) -> bytes:
    """Durably consume one private one-attempt receipt path without replacement."""

    payload = (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("ascii")
    if not 1 <= len(payload) <= MAX_STATE_BYTES:
        _fail("private runtime document exceeds its size bound")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise ModelLabError(
            f"private one-attempt receipt already exists or is unsafe: {path}",
            code="unsafe_service_runtime_state",
        ) from error
    try:
        position = 0
        while position < len(payload):
            written = os.write(descriptor, payload[position:])
            if written <= 0:
                _fail("private one-attempt receipt write made no progress")
            position += written
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    parent_descriptor = os.open(
        path.parent,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)
    return payload


def remove_private_file(path: pathlib.Path) -> None:
    """Unlink one exact validated state file."""

    descriptor = _open_private_file(path, create=False, writable=False)
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if current.st_dev != opened.st_dev or current.st_ino != opened.st_ino:
            _fail(f"private runtime file changed before unlink: {path}")
        path.unlink()
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class ProcessState:
    service_id: str
    service_plan_sha256: str
    manifest_sha256: str
    boot_id: str
    pid: int
    process_nonce: str
    process_start_ticks: int
    compile_cache_id: str
    compile_cache_mode: str
    compile_cache_prerequisite_sha256: str
    started_monotonic_ns: int
    runtime_execution_environment: dict[str, Any]

    def normalized(self) -> dict[str, Any]:
        return {
            "schema_version": PROCESS_STATE_SCHEMA,
            "service_id": self.service_id,
            "service_plan_sha256": self.service_plan_sha256,
            "manifest_sha256": self.manifest_sha256,
            "boot_id": self.boot_id,
            "pid": self.pid,
            "process_nonce": self.process_nonce,
            "process_start_ticks": self.process_start_ticks,
            "compile_cache_id": self.compile_cache_id,
            "compile_cache_mode": self.compile_cache_mode,
            "compile_cache_prerequisite_sha256": (
                self.compile_cache_prerequisite_sha256
            ),
            "started_monotonic_ns": self.started_monotonic_ns,
            "runtime_execution_environment": self.runtime_execution_environment,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> ProcessState:
        fields = frozenset(
            {
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
            }
        )
        if not isinstance(value, dict) or set(value) != fields:
            _fail("service process state fields are malformed")
        if value["schema_version"] != PROCESS_STATE_SCHEMA:
            _fail("service process state schema is unsupported")
        string_fields = (
            "service_id",
            "service_plan_sha256",
            "manifest_sha256",
            "boot_id",
            "process_nonce",
            "compile_cache_id",
            "compile_cache_prerequisite_sha256",
        )
        if any(
            not isinstance(value[name], str) or not value[name]
            for name in string_fields
        ):
            _fail("service process state string identity is malformed")
        if (
            isinstance(value["pid"], bool)
            or not isinstance(value["pid"], int)
            or value["pid"] <= 0
            or isinstance(value["process_start_ticks"], bool)
            or not isinstance(value["process_start_ticks"], int)
            or value["process_start_ticks"] <= 0
            or isinstance(value["started_monotonic_ns"], bool)
            or not isinstance(value["started_monotonic_ns"], int)
            or value["started_monotonic_ns"] <= 0
        ):
            _fail("service process state process identity is malformed")
        if value["compile_cache_mode"] not in {
            "ephemeral",
            "author",
            "candidate-proof",
            "accepted",
        }:
            _fail("service process state cache mode is malformed")
        cache_receipt_sha256 = value["compile_cache_prerequisite_sha256"]
        if (
            len(cache_receipt_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in cache_receipt_sha256
            )
        ):
            _fail("service process state cache receipt identity is malformed")
        execution_environment = validate_runtime_execution_environment(
            value["runtime_execution_environment"]
        )
        return cls(
            service_id=value["service_id"],
            service_plan_sha256=value["service_plan_sha256"],
            manifest_sha256=value["manifest_sha256"],
            boot_id=value["boot_id"],
            pid=value["pid"],
            process_nonce=value["process_nonce"],
            process_start_ticks=value["process_start_ticks"],
            compile_cache_id=value["compile_cache_id"],
            compile_cache_mode=value["compile_cache_mode"],
            compile_cache_prerequisite_sha256=cache_receipt_sha256,
            started_monotonic_ns=value["started_monotonic_ns"],
            runtime_execution_environment=execution_environment.normalized(),
        )


def read_process_state(path: pathlib.Path) -> ProcessState:
    value, _ = read_private_json(path, maximum_bytes=64 * 1024)
    return ProcessState.from_mapping(value)
