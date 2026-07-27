"""Short-lived, provider-neutral inference socket admission receipts.

An attachment is an administrator assertion that one already-running local
Unix socket serves the requested profile workload.  It is not measured proof
of the engine, hardware, or launch configuration.  The receipt admits a new
session until ``admission_expires_at``; provider lifecycle and idle-TTL policy
own shutdown of services and sessions that are already running.

Receipts are machine-and-boot-local runtime state.  This module never starts,
stops, or provisions a model service and never stores provider credentials.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import re
import secrets
import socket
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from .errors import ModelSessionError
from .profile import Profile, ProfileContract


ATTACHMENT_SCHEMA = "model-session.inference-attachment.v1"
WORKLOAD_SCHEMA = "model-session.workload.v1"
MAX_ATTACHMENT_BYTES = 64 * 1024
MAX_ATTACHMENT_TTL_SECONDS = 24 * 60 * 60
SOCKET_CONNECT_TIMEOUT_SECONDS = 1.0
DEFAULT_RUNTIME_DIRECTORY_NAME = "model-session"
BOOT_ID_PATH = pathlib.Path("/proc/sys/kernel/random/boot_id")

_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PUBLICATION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_BOOT_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)
_RECEIPT_KEYS = {
    "schema",
    "publication_id",
    "profile_id",
    "project_id",
    "boot_id",
    "workload",
    "workload_sha256",
    "socket_path",
    "socket_device",
    "socket_inode",
    "published_at",
    "admission_expires_at",
    "payload_sha256",
}

Clock = Callable[[], datetime.datetime]


@dataclass(frozen=True)
class InferenceAttachment:
    """A validated admission assertion for a live local inference socket."""

    publication_id: str
    profile_id: str
    project_id: str
    workload_sha256: str
    socket_path: pathlib.Path
    socket_device: int
    socket_inode: int
    published_at: datetime.datetime
    admission_expires_at: datetime.datetime
    receipt_path: pathlib.Path


@dataclass(frozen=True)
class _SocketIdentity:
    path: pathlib.Path
    device: int
    inode: int


@dataclass
class _AttachmentDirectories:
    attachments_path: pathlib.Path
    attachments_descriptor: int
    locks_path: pathlib.Path
    locks_descriptor: int | None

    def close(self) -> None:
        if self.locks_descriptor is not None:
            os.close(self.locks_descriptor)
            self.locks_descriptor = None
        os.close(self.attachments_descriptor)


def _fail(message: str, *, code: str) -> None:
    raise ModelSessionError(message, code=code)


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _contract(profile: Profile | ProfileContract) -> ProfileContract:
    if isinstance(profile, Profile):
        return profile.contract
    if isinstance(profile, ProfileContract):
        return profile
    _fail(
        "inference attachment requires a validated model profile contract",
        code="invalid_inference_attachment_binding",
    )


def _validate_identifier(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        _fail(
            f"{label} is not a valid model-session identifier",
            code="invalid_inference_attachment_binding",
        )
    return value


def _normalized_absolute_path(
    value: os.PathLike[str] | str,
    *,
    label: str,
    code: str,
) -> pathlib.Path:
    try:
        text = os.fspath(value)
    except TypeError as error:
        raise ModelSessionError(
            f"{label} must be an absolute filesystem path",
            code=code,
        ) from error
    if (
        not isinstance(text, str)
        or not text
        or "\x00" in text
        or text != os.path.normpath(text)
    ):
        _fail(f"{label} must be an absolute normalized path", code=code)
    path = pathlib.Path(text)
    if not path.is_absolute():
        _fail(f"{label} must be an absolute normalized path", code=code)
    return path


def _runtime_root_path(
    runtime_root: os.PathLike[str] | str | None,
) -> pathlib.Path:
    if runtime_root is None:
        runtime_directory = os.environ.get("XDG_RUNTIME_DIR")
        if runtime_directory is None:
            _fail(
                "XDG_RUNTIME_DIR is required for machine-local inference "
                "attachments; pass runtime_root explicitly only for a controlled "
                "runtime location",
                code="inference_attachment_runtime_unavailable",
            )
        base = _normalized_absolute_path(
            runtime_directory,
            label="XDG_RUNTIME_DIR",
            code="unsafe_inference_attachment_state",
        )
        path = base / DEFAULT_RUNTIME_DIRECTORY_NAME
    else:
        path = _normalized_absolute_path(
            runtime_root,
            label="inference attachment runtime_root",
            code="unsafe_inference_attachment_state",
        )
    if path in {
        pathlib.Path("/"),
        pathlib.Path("/home"),
        pathlib.Path("/mnt"),
        pathlib.Path("/run"),
        pathlib.Path("/tmp"),
        pathlib.Path("/var"),
    }:
        _fail(
            "inference attachment runtime_root is dangerously broad",
            code="unsafe_inference_attachment_state",
        )
    return path


def _paths_overlap(first: pathlib.Path, second: pathlib.Path) -> bool:
    return (
        first == second
        or first.is_relative_to(second)
        or second.is_relative_to(first)
    )


def _validate_runtime_root_separation(
    contract: ProfileContract,
    runtime_root: pathlib.Path,
) -> pathlib.Path:
    protected_roots = {
        "profile directory": contract.profile_root,
        "durable session state_root": contract.state_root,
        "shared project_root": contract.project_root,
        "Pi installation_root": contract.pi.installation_root,
        "dotfiles infrastructure": pathlib.Path(__file__).resolve().parents[2],
    }
    for label, value in protected_roots.items():
        path = _normalized_absolute_path(
            value,
            label=label,
            code="unsafe_inference_attachment_state",
        )
        if _paths_overlap(runtime_root, path):
            _fail(
                f"inference attachment runtime_root overlaps {label}: {path}",
                code="unsafe_inference_attachment_state",
            )
    return runtime_root


def _workload_value(contract: ProfileContract) -> dict[str, Any]:
    return {
        "schema": WORKLOAD_SCHEMA,
        "model": contract.model.as_dict(),
        "runtime": contract.runtime.as_dict(),
    }


def inference_workload_identity(
    profile: Profile | ProfileContract,
) -> str:
    """Return the exact canonical model/runtime workload identity."""

    contract = _contract(profile)
    return _sha256(_canonical_json_bytes(_workload_value(contract)))


def inference_attachment_receipt_path(
    profile: Profile | ProfileContract,
    *,
    runtime_root: os.PathLike[str] | str | None = None,
) -> pathlib.Path:
    """Return this boot-local receipt path without creating any state."""

    contract = _contract(profile)
    profile_id = _validate_identifier(
        contract.profile_id,
        label="profile_id",
    )
    _validate_identifier(contract.project_id, label="project_id")
    resolved_runtime_root = _validate_runtime_root_separation(
        contract,
        _runtime_root_path(runtime_root),
    )
    return (
        resolved_runtime_root
        / "attachments"
        / f"{profile_id}.json"
    )


def _validate_private_directory_metadata(
    metadata: os.stat_result,
    *,
    label: str,
) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        _fail(
            f"{label} is not a real directory",
            code="unsafe_inference_attachment_state",
        )
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        _fail(
            f"{label} is not owned by the current user",
            code="unsafe_inference_attachment_state",
        )
    mode = stat.S_IMODE(metadata.st_mode)
    if mode != 0o700:
        _fail(
            f"{label} permissions must be exactly 0700",
            code="unsafe_inference_attachment_permissions",
        )


def _directory_open_flags() -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _open_directory_path(path: pathlib.Path, *, label: str) -> int:
    """Open every path component relative to a pinned parent descriptor."""

    if not hasattr(os, "O_NOFOLLOW"):
        _fail(
            "inference attachments require O_NOFOLLOW",
            code="inference_attachment_platform_unsupported",
        )
    descriptor = os.open("/", _directory_open_flags())
    try:
        for component in path.parts[1:]:
            try:
                child = os.open(
                    component,
                    _directory_open_flags(),
                    dir_fd=descriptor,
                )
            except OSError as error:
                raise ModelSessionError(
                    f"cannot open {label} {path} without following links: {error}",
                    code="unsafe_inference_attachment_state",
                ) from error
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_private_child_directory(
    parent_descriptor: int,
    *,
    name: str,
    path: pathlib.Path,
    label: str,
    create: bool,
) -> int | None:
    try:
        descriptor = os.open(
            name,
            _directory_open_flags(),
            dir_fd=parent_descriptor,
        )
    except FileNotFoundError:
        if not create:
            return None
        try:
            os.mkdir(name, 0o700, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        except FileExistsError:
            pass
        except OSError as error:
            raise ModelSessionError(
                f"cannot create {label} {path}: {error}",
                code="unsafe_inference_attachment_state",
            ) from error
        try:
            descriptor = os.open(
                name,
                _directory_open_flags(),
                dir_fd=parent_descriptor,
            )
        except OSError as error:
            raise ModelSessionError(
                f"cannot open newly created {label} {path}: {error}",
                code="unsafe_inference_attachment_state",
            ) from error
    except OSError as error:
        raise ModelSessionError(
            f"cannot open {label} {path}: {error}",
            code="unsafe_inference_attachment_state",
        ) from error
    try:
        _validate_private_directory_metadata(
            os.fstat(descriptor),
            label=label,
        )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_runtime_root(
    runtime_root: pathlib.Path,
    *,
    create: bool,
) -> int | None:
    if runtime_root.parent == runtime_root:
        _fail(
            "inference attachment runtime_root must be below an existing "
            "private parent directory",
            code="unsafe_inference_attachment_state",
        )
    try:
        parent_descriptor = _open_directory_path(
            runtime_root.parent,
            label="inference attachment runtime parent",
        )
    except ModelSessionError as error:
        if (
            not create
            and isinstance(error.__cause__, FileNotFoundError)
        ):
            return None
        raise
    try:
        _validate_private_directory_metadata(
            os.fstat(parent_descriptor),
            label="inference attachment runtime parent",
        )
        return _open_private_child_directory(
            parent_descriptor,
            name=runtime_root.name,
            path=runtime_root,
            label="inference attachment runtime_root",
            create=create,
        )
    finally:
        os.close(parent_descriptor)


def _attachment_directories(
    runtime_root: pathlib.Path,
    *,
    create: bool,
) -> _AttachmentDirectories | None:
    runtime_descriptor = _open_runtime_root(runtime_root, create=create)
    if runtime_descriptor is None:
        return None
    attachments = runtime_root / "attachments"
    locks = attachments / ".locks"
    try:
        attachments_descriptor = _open_private_child_directory(
            runtime_descriptor,
            name="attachments",
            path=attachments,
            label="inference attachment directory",
            create=create,
        )
    finally:
        os.close(runtime_descriptor)
    if attachments_descriptor is None:
        return None
    try:
        locks_descriptor = _open_private_child_directory(
            attachments_descriptor,
            name=".locks",
            path=locks,
            label="inference attachment lock directory",
            create=create,
        )
        return _AttachmentDirectories(
            attachments_path=attachments,
            attachments_descriptor=attachments_descriptor,
            locks_path=locks,
            locks_descriptor=locks_descriptor,
        )
    except BaseException:
        os.close(attachments_descriptor)
        raise


def _validate_private_regular_metadata(
    metadata: os.stat_result,
    *,
    label: str,
) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        _fail(
            f"{label} is not a regular file",
            code="unsafe_inference_attachment_state",
        )
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        _fail(
            f"{label} is not owned by the current user",
            code="unsafe_inference_attachment_state",
        )
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        _fail(
            f"{label} permissions must be exactly 0600",
            code="unsafe_inference_attachment_permissions",
        )


def _open_lock(
    lock_directory_descriptor: int,
    name: str,
    path: pathlib.Path,
    *,
    create: bool,
) -> tuple[int, bool]:
    flags = (
        os.O_RDWR
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    created = False
    if create:
        try:
            descriptor = os.open(
                name,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=lock_directory_descriptor,
            )
            created = True
        except FileExistsError:
            try:
                descriptor = os.open(
                    name,
                    flags,
                    dir_fd=lock_directory_descriptor,
                )
            except OSError as error:
                raise ModelSessionError(
                    f"cannot open inference attachment lock {path}: {error}",
                    code="unsafe_inference_attachment_state",
                ) from error
        except OSError as error:
            raise ModelSessionError(
                f"cannot create inference attachment lock {path}: {error}",
                code="unsafe_inference_attachment_state",
            ) from error
    else:
        try:
            descriptor = os.open(
                name,
                flags,
                dir_fd=lock_directory_descriptor,
            )
        except FileNotFoundError:
            _fail(
                "inference attachment lock is missing",
                code="unsafe_inference_attachment_state",
            )
        except OSError as error:
            raise ModelSessionError(
                f"cannot open inference attachment lock {path}: {error}",
                code="unsafe_inference_attachment_state",
            ) from error

    try:
        if created:
            os.fchmod(descriptor, 0o600)
        _validate_private_regular_metadata(
            os.fstat(descriptor),
            label="inference attachment lock",
        )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, created


@contextlib.contextmanager
def _attachment_lock(
    directories: _AttachmentDirectories,
    profile_id: str,
    *,
    exclusive: bool,
    create: bool,
) -> Iterator[None]:
    if directories.locks_descriptor is None:
        _fail(
            "inference attachment lock directory is missing",
            code="unsafe_inference_attachment_state",
        )
    lock_name = f"{profile_id}.lock"
    lock_path = directories.locks_path / lock_name
    descriptor, created = _open_lock(
        directories.locks_descriptor,
        lock_name,
        lock_path,
        create=create,
    )
    locked = False
    try:
        if created:
            os.fsync(descriptor)
            os.fsync(directories.locks_descriptor)
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(descriptor, operation)
        locked = True
        yield
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _open_absolute_no_symlinks(path: pathlib.Path, *, label: str) -> int:
    if not hasattr(os, "O_PATH") or not hasattr(os, "O_NOFOLLOW"):
        _fail(
            "inference attachments require Linux O_PATH and O_NOFOLLOW",
            code="inference_attachment_platform_unsupported",
        )
    common_flags = os.O_PATH | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(
        "/",
        common_flags | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        for index, component in enumerate(path.parts[1:]):
            final = index == len(path.parts) - 2
            flags = common_flags
            if not final:
                flags |= getattr(os, "O_DIRECTORY", 0)
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError as error:
                raise ModelSessionError(
                    f"{label} is unavailable: {path}",
                    code="inference_attachment_unavailable",
                ) from error
            except OSError as error:
                raise ModelSessionError(
                    f"cannot open {label} {path} without following links: {error}",
                    code="unsafe_inference_socket",
                ) from error
            os.close(descriptor)
            descriptor = child
            metadata = os.fstat(descriptor)
            if stat.S_ISLNK(metadata.st_mode):
                _fail(
                    f"{label} contains a symbolic-link component: {path}",
                    code="unsafe_inference_socket",
                )
            if not final and not stat.S_ISDIR(metadata.st_mode):
                _fail(
                    f"{label} contains a non-directory component: {path}",
                    code="unsafe_inference_socket",
                )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _validate_socket_metadata(
    metadata: os.stat_result,
    *,
    path: pathlib.Path,
) -> None:
    if not stat.S_ISSOCK(metadata.st_mode):
        _fail(
            f"inference endpoint is not an actual Unix socket: {path}",
            code="unsafe_inference_socket",
        )
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        _fail(
            f"inference socket is not owned by the current user: {path}",
            code="unsafe_inference_socket",
        )
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        _fail(
            f"inference socket permissions must be exactly 0600: {path}",
            code="unsafe_inference_socket",
        )


def _probe_stream_socket(path: pathlib.Path) -> None:
    endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        endpoint.settimeout(SOCKET_CONNECT_TIMEOUT_SECONDS)
        endpoint.connect(os.fspath(path))
    except (OSError, TimeoutError) as error:
        raise ModelSessionError(
            "inference endpoint does not accept bounded AF_UNIX/SOCK_STREAM "
            f"connections: {path}: {error}",
            code="inference_attachment_unavailable",
        ) from error
    finally:
        endpoint.close()


def _validate_socket(
    value: os.PathLike[str] | str,
) -> _SocketIdentity:
    path = _normalized_absolute_path(
        value,
        label="inference socket",
        code="unsafe_inference_socket",
    )
    first_descriptor = _open_absolute_no_symlinks(
        path,
        label="inference socket",
    )
    try:
        first_metadata = os.fstat(first_descriptor)
        _validate_socket_metadata(first_metadata, path=path)
        _probe_stream_socket(path)
        second_descriptor = _open_absolute_no_symlinks(
            path,
            label="inference socket",
        )
        try:
            second_metadata = os.fstat(second_descriptor)
            _validate_socket_metadata(second_metadata, path=path)
            first_identity = (
                first_metadata.st_dev,
                first_metadata.st_ino,
            )
            second_identity = (
                second_metadata.st_dev,
                second_metadata.st_ino,
            )
            if first_identity != second_identity:
                _fail(
                    "inference socket was replaced while it was being "
                    f"validated: {path}",
                    code="inference_attachment_unavailable",
                )
        finally:
            os.close(second_descriptor)
        return _SocketIdentity(
            path=path,
            device=first_metadata.st_dev,
            inode=first_metadata.st_ino,
        )
    finally:
        os.close(first_descriptor)


def _clock_time(clock: Clock | None) -> datetime.datetime:
    value = (clock or _utc_now)()
    if not isinstance(value, datetime.datetime) or value.tzinfo is None:
        _fail(
            "inference attachment clock must return a timezone-aware datetime",
            code="invalid_inference_attachment_clock",
        )
    return value.astimezone(datetime.timezone.utc)


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _read_boot_id() -> str:
    flags = (
        os.O_RDONLY
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(BOOT_ID_PATH, flags)
    except OSError as error:
        raise ModelSessionError(
            f"cannot read Linux boot identity {BOOT_ID_PATH}: {error}",
            code="inference_attachment_runtime_unavailable",
        ) from error
    try:
        content = os.read(descriptor, 128)
        if os.read(descriptor, 1):
            _fail(
                "Linux boot identity is unexpectedly large",
                code="inference_attachment_runtime_unavailable",
            )
    finally:
        os.close(descriptor)
    try:
        value = content.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise ModelSessionError(
            "Linux boot identity is not ASCII",
            code="inference_attachment_runtime_unavailable",
        ) from error
    if not _BOOT_ID_PATTERN.fullmatch(value):
        _fail(
            "Linux boot identity is not a canonical UUID",
            code="inference_attachment_runtime_unavailable",
        )
    return value


def _format_timestamp(value: datetime.datetime) -> str:
    return (
        value.astimezone(datetime.timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_timestamp(value: Any, *, label: str) -> datetime.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        _fail(
            f"inference attachment {label} is not a canonical UTC timestamp",
            code="inference_attachment_tampered",
        )
    try:
        parsed = datetime.datetime.fromisoformat(
            value.removesuffix("Z") + "+00:00"
        )
    except ValueError as error:
        raise ModelSessionError(
            f"inference attachment {label} is not a valid timestamp",
            code="inference_attachment_tampered",
        ) from error
    if _format_timestamp(parsed) != value:
        _fail(
            f"inference attachment {label} is not canonical",
            code="inference_attachment_tampered",
        )
    return parsed


def _validate_ttl(ttl_seconds: int) -> int:
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or ttl_seconds <= 0
        or ttl_seconds > MAX_ATTACHMENT_TTL_SECONDS
    ):
        _fail(
            "inference attachment ttl_seconds must be an integer from 1 "
            f"through {MAX_ATTACHMENT_TTL_SECONDS}",
            code="invalid_inference_attachment_ttl",
        )
    return ttl_seconds


def _entry_metadata(
    directory_descriptor: int,
    name: str,
    *,
    path: pathlib.Path,
) -> os.stat_result | None:
    try:
        return os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ModelSessionError(
            f"cannot inspect inference attachment entry {path}: {error}",
            code="unsafe_inference_attachment_state",
        ) from error


def _read_receipt(
    directory_descriptor: int,
    name: str,
    path: pathlib.Path,
) -> tuple[dict[str, Any], bytes]:
    flags = (
        os.O_RDONLY
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(
            name,
            flags,
            dir_fd=directory_descriptor,
        )
    except FileNotFoundError:
        _fail(
            "inference attachment receipt is missing",
            code="inference_attachment_missing",
        )
    except OSError as error:
        raise ModelSessionError(
            f"cannot open inference attachment receipt {path}: {error}",
            code="unsafe_inference_attachment_state",
        ) from error
    try:
        _validate_private_regular_metadata(
            os.fstat(descriptor),
            label="inference attachment receipt",
        )
        chunks: list[bytes] = []
        remaining = MAX_ATTACHMENT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(content) > MAX_ATTACHMENT_BYTES:
        _fail(
            "inference attachment receipt is unexpectedly large",
            code="inference_attachment_tampered",
        )
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ModelSessionError(
            "inference attachment receipt is not valid UTF-8 JSON",
            code="inference_attachment_tampered",
        ) from error
    if not isinstance(value, dict) or _canonical_json_bytes(value) != content:
        _fail(
            "inference attachment receipt is not canonical JSON",
            code="inference_attachment_tampered",
        )
    return value, content


def _existing_receipt_is_safe(
    directory_descriptor: int,
    name: str,
    path: pathlib.Path,
) -> None:
    if _entry_metadata(
        directory_descriptor,
        name,
        path=path,
    ) is None:
        return
    _read_receipt(directory_descriptor, name, path)


def _confirm_published_directory_durability(
    directory_descriptor: int,
    *,
    path: pathlib.Path,
) -> None:
    try:
        os.fsync(directory_descriptor)
    except OSError as error:
        raise ModelSessionError(
            "inference attachment receipt was replaced, but directory "
            f"durability is unknown for {path}: {error}",
            code="inference_attachment_publish_durability_unknown",
        ) from error


def _write_atomic_receipt(
    directory_descriptor: int,
    name: str,
    path: pathlib.Path,
    content: bytes,
) -> None:
    _existing_receipt_is_safe(directory_descriptor, name, path)
    temporary_name = (
        f".{path.stem}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        os.fchmod(descriptor, 0o600)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                _fail(
                    "short write while publishing inference attachment",
                    code="inference_attachment_publish_failed",
                )
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        _confirm_published_directory_durability(
            directory_descriptor,
            path=path,
        )
    except ModelSessionError:
        raise
    except OSError as error:
        raise ModelSessionError(
            f"cannot atomically publish inference attachment {path}: {error}",
            code="inference_attachment_publish_failed",
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass


def publish_inference_attachment(
    profile: Profile | ProfileContract,
    socket_path: os.PathLike[str] | str,
    *,
    ttl_seconds: int,
    clock: Clock | None = None,
    runtime_root: os.PathLike[str] | str | None = None,
) -> InferenceAttachment:
    """Publish an admission assertion for an already-running local service.

    The caller is the authority for the requested workload identity.  This
    function proves only that the exact private socket inode accepts a bounded
    AF_UNIX/SOCK_STREAM connection; engine and hardware attestation belongs to
    the service supervisor's launch manifest.
    """

    contract = _contract(profile)
    profile_id = _validate_identifier(contract.profile_id, label="profile_id")
    project_id = _validate_identifier(contract.project_id, label="project_id")
    ttl_seconds = _validate_ttl(ttl_seconds)
    workload = _workload_value(contract)
    workload_sha256 = _sha256(_canonical_json_bytes(workload))
    socket = _validate_socket(socket_path)
    boot_id = _read_boot_id()
    resolved_runtime_root = _validate_runtime_root_separation(
        contract,
        _runtime_root_path(runtime_root),
    )
    directories = _attachment_directories(
        resolved_runtime_root,
        create=True,
    )
    if directories is None:
        raise AssertionError("created attachment directories are absent")
    receipt_name = f"{profile_id}.json"
    receipt_path = directories.attachments_path / receipt_name

    try:
        with _attachment_lock(
            directories,
            profile_id,
            exclusive=True,
            create=True,
        ):
            socket = _validate_socket(socket.path)
            published_at = _clock_time(clock)
            try:
                admission_expires_at = published_at + datetime.timedelta(
                    seconds=ttl_seconds
                )
            except OverflowError as error:
                raise ModelSessionError(
                    "inference attachment clock cannot represent the admission "
                    "expiry",
                    code="invalid_inference_attachment_clock",
                ) from error
            payload: dict[str, Any] = {
                "schema": ATTACHMENT_SCHEMA,
                "publication_id": secrets.token_hex(16),
                "profile_id": profile_id,
                "project_id": project_id,
                "boot_id": boot_id,
                "workload": workload,
                "workload_sha256": workload_sha256,
                "socket_path": str(socket.path),
                "socket_device": socket.device,
                "socket_inode": socket.inode,
                "published_at": _format_timestamp(published_at),
                "admission_expires_at": _format_timestamp(
                    admission_expires_at
                ),
            }
            document = {
                **payload,
                "payload_sha256": _sha256(_canonical_json_bytes(payload)),
            }
            _write_atomic_receipt(
                directories.attachments_descriptor,
                receipt_name,
                receipt_path,
                _canonical_json_bytes(document),
            )
    finally:
        directories.close()

    return InferenceAttachment(
        publication_id=document["publication_id"],
        profile_id=profile_id,
        project_id=project_id,
        workload_sha256=workload_sha256,
        socket_path=socket.path,
        socket_device=socket.device,
        socket_inode=socket.inode,
        published_at=published_at,
        admission_expires_at=admission_expires_at,
        receipt_path=receipt_path,
    )


def _require_exact_receipt_keys(value: dict[str, Any]) -> None:
    actual = set(value)
    if actual != _RECEIPT_KEYS:
        missing = _RECEIPT_KEYS - actual
        unexpected = actual - _RECEIPT_KEYS
        details = []
        if missing:
            details.append(f"missing {', '.join(sorted(missing))}")
        if unexpected:
            details.append(f"unexpected {', '.join(sorted(unexpected))}")
        _fail(
            "inference attachment receipt has invalid fields "
            f"({'; '.join(details)})",
            code="inference_attachment_tampered",
        )


def _receipt_nonnegative_integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(
            f"inference attachment {label} is invalid",
            code="inference_attachment_tampered",
        )
    return value


def _validate_receipt(
    value: dict[str, Any],
    *,
    contract: ProfileContract,
    receipt_path: pathlib.Path,
    current_time: datetime.datetime,
    boot_id: str,
) -> InferenceAttachment:
    _require_exact_receipt_keys(value)
    if value["schema"] != ATTACHMENT_SCHEMA:
        _fail(
            f"inference attachment schema must be {ATTACHMENT_SCHEMA!r}",
            code="inference_attachment_tampered",
        )
    publication_id = value["publication_id"]
    if (
        not isinstance(publication_id, str)
        or not _PUBLICATION_ID_PATTERN.fullmatch(publication_id)
    ):
        _fail(
            "inference attachment publication_id is invalid",
            code="inference_attachment_tampered",
        )
    payload_sha256 = value["payload_sha256"]
    if (
        not isinstance(payload_sha256, str)
        or not _HASH_PATTERN.fullmatch(payload_sha256)
    ):
        _fail(
            "inference attachment payload hash is invalid",
            code="inference_attachment_tampered",
        )
    payload = {
        key: child
        for key, child in value.items()
        if key != "payload_sha256"
    }
    if _sha256(_canonical_json_bytes(payload)) != payload_sha256:
        _fail(
            "inference attachment payload hash does not match its receipt",
            code="inference_attachment_tampered",
        )
    receipt_boot_id = value["boot_id"]
    if (
        not isinstance(receipt_boot_id, str)
        or not _BOOT_ID_PATTERN.fullmatch(receipt_boot_id)
    ):
        _fail(
            "inference attachment boot_id is invalid",
            code="inference_attachment_tampered",
        )
    if receipt_boot_id != boot_id:
        _fail(
            "inference attachment belongs to a different machine boot",
            code="inference_attachment_wrong_boot",
        )

    expected_profile_id = _validate_identifier(
        contract.profile_id,
        label="profile_id",
    )
    expected_project_id = _validate_identifier(
        contract.project_id,
        label="project_id",
    )
    expected_workload = _workload_value(contract)
    expected_workload_sha256 = _sha256(
        _canonical_json_bytes(expected_workload)
    )
    workload_sha256 = value["workload_sha256"]
    if (
        not isinstance(workload_sha256, str)
        or not _HASH_PATTERN.fullmatch(workload_sha256)
        or _sha256(_canonical_json_bytes(value["workload"]))
        != workload_sha256
    ):
        _fail(
            "inference attachment workload identity is invalid",
            code="inference_attachment_tampered",
        )
    if (
        value["profile_id"] != expected_profile_id
        or value["project_id"] != expected_project_id
        or value["workload"] != expected_workload
        or workload_sha256 != expected_workload_sha256
    ):
        _fail(
            "inference attachment belongs to a different profile or workload",
            code="inference_attachment_mismatch",
        )

    published_at = _parse_timestamp(
        value["published_at"],
        label="published_at",
    )
    admission_expires_at = _parse_timestamp(
        value["admission_expires_at"],
        label="admission_expires_at",
    )
    lifetime = (admission_expires_at - published_at).total_seconds()
    if (
        lifetime <= 0
        or lifetime > MAX_ATTACHMENT_TTL_SECONDS
        or not lifetime.is_integer()
    ):
        _fail(
            "inference attachment admission lifetime is invalid",
            code="inference_attachment_tampered",
        )
    if current_time < published_at:
        _fail(
            "inference attachment is not yet valid",
            code="inference_attachment_not_yet_valid",
        )
    if current_time >= admission_expires_at:
        _fail(
            "inference attachment admission lease has expired; an administrator "
            "must attach a currently running model service",
            code="inference_attachment_expired",
        )
    socket_path = value["socket_path"]
    if not isinstance(socket_path, str):
        _fail(
            "inference attachment socket_path is invalid",
            code="inference_attachment_tampered",
        )
    try:
        socket = _validate_socket(socket_path)
    except ModelSessionError as error:
        unavailable_codes = {
            "unsafe_inference_socket",
            "inference_attachment_unavailable",
        }
        if error.code in unavailable_codes:
            raise ModelSessionError(
                "attached inference service is unavailable or its socket "
                "failed validation",
                code="inference_attachment_unavailable",
            ) from error
        raise
    socket_device = _receipt_nonnegative_integer(
        value["socket_device"],
        label="socket_device",
    )
    socket_inode = _receipt_nonnegative_integer(
        value["socket_inode"],
        label="socket_inode",
    )
    if (
        socket.device != socket_device
        or socket.inode != socket_inode
    ):
        _fail(
            "attached inference socket inode was replaced after publication",
            code="inference_attachment_unavailable",
        )

    return InferenceAttachment(
        publication_id=publication_id,
        profile_id=expected_profile_id,
        project_id=expected_project_id,
        workload_sha256=expected_workload_sha256,
        socket_path=socket.path,
        socket_device=socket.device,
        socket_inode=socket.inode,
        published_at=published_at,
        admission_expires_at=admission_expires_at,
        receipt_path=receipt_path,
    )


def _missing_attachment(contract: ProfileContract) -> None:
    _fail(
        "no active inference attachment for "
        f"profile {contract.profile_id!r}; an administrator must start and "
        "attach the required model service",
        code="inference_attachment_missing",
    )


def load_inference_attachment(
    profile: Profile | ProfileContract,
    *,
    clock: Clock | None = None,
    runtime_root: os.PathLike[str] | str | None = None,
) -> InferenceAttachment:
    """Load a live admission lease immediately before starting a session.

    Loading is read-only and never starts, provisions, or repairs a service.
    The returned deadline admits this launch; it is not the provider's service
    shutdown or idle deadline.
    """

    contract = _contract(profile)
    profile_id = _validate_identifier(contract.profile_id, label="profile_id")
    _validate_identifier(contract.project_id, label="project_id")
    resolved_runtime_root = _validate_runtime_root_separation(
        contract,
        _runtime_root_path(runtime_root),
    )
    directories = _attachment_directories(
        resolved_runtime_root,
        create=False,
    )
    if directories is None:
        _missing_attachment(contract)
    receipt_name = f"{profile_id}.json"
    receipt_path = directories.attachments_path / receipt_name
    lock_name = f"{profile_id}.lock"
    lock_path = directories.locks_path / lock_name
    try:
        receipt_metadata = _entry_metadata(
            directories.attachments_descriptor,
            receipt_name,
            path=receipt_path,
        )
        lock_metadata = (
            _entry_metadata(
                directories.locks_descriptor,
                lock_name,
                path=lock_path,
            )
            if directories.locks_descriptor is not None
            else None
        )
        if receipt_metadata is None:
            if lock_metadata is not None:
                _validate_private_regular_metadata(
                    lock_metadata,
                    label="persistent inference attachment lock",
                )
            _missing_attachment(contract)
        if lock_metadata is None:
            _fail(
                "inference attachment receipt exists without its persistent "
                "profile lock",
                code="unsafe_inference_attachment_state",
            )
        with _attachment_lock(
            directories,
            profile_id,
            exclusive=False,
            create=False,
        ):
            value, _ = _read_receipt(
                directories.attachments_descriptor,
                receipt_name,
                receipt_path,
            )
            current_time = _clock_time(clock)
            return _validate_receipt(
                value,
                contract=contract,
                receipt_path=receipt_path,
                current_time=current_time,
                boot_id=_read_boot_id(),
            )
    finally:
        directories.close()
