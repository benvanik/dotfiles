"""Strict external profile contracts for isolated model sessions.

The package contains only infrastructure. A concrete profile and every file it
names must live outside the dotfiles repository.
"""

from __future__ import annotations

import os
import pathlib
import re
import stat
import tomllib
from dataclasses import dataclass, field
from typing import Any

from .checkpoint import CheckpointLimits, maximum_encoded_bytes
from .errors import ModelSessionError
from .ownership import owner_has_private_primary_group
from .storage_limits import STORAGE_PAGE_SIZE, StoragePoolLimits


PROFILE_SCHEMA_V1 = "model-session.profile.v1"
PROFILE_SCHEMA_V2 = "model-session.profile.v2"
PROFILE_SCHEMA = PROFILE_SCHEMA_V2
KNOWN_PROFILE_SCHEMAS = frozenset({PROFILE_SCHEMA_V1, PROFILE_SCHEMA_V2})
PROFILE_FILE_NAME = "profile.toml"
AGENTS_FILE_NAME = "AGENTS.md"
MAX_PROFILE_BYTES = 256 * 1024
MAX_RESOURCE_BYTES = 4 * 1024 * 1024
SANDBOX_STORAGE_HEADROOM_BYTES = 512 * 1024 * 1024
MAX_SESSIONS = 64
MAX_VOLUME_BYTES = 32 * 1024**3
MAX_STORAGE_INODES = 1_000_000
MAX_CHECKPOINT_BYTES = 72 * 1024**3
MAX_FILE_BYTES = 64 * 1024**3
MAX_LOGICAL_BYTES = 64 * 1024**3
MAX_SANDBOX_MEMORY_BYTES = 128 * 1024**3
MIN_SANDBOX_TASKS = 64
MAX_SANDBOX_TASKS = 1024
MAX_RUNTIME_SECONDS = 7 * 24 * 60 * 60
MAX_SHUTDOWN_GRACE_SECONDS = 60 * 60

IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
PROVIDER_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"
)
COMMIT_PATTERN = re.compile(r"^[0-9A-Fa-f]{40}$")
PI_VERSION_PATTERN = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?$"
)
PI_NODE_EXECUTABLE = pathlib.PurePosixPath("bin/node")
RELATIVE_COMPONENT_PATTERN = re.compile(r"^[A-Za-z0-9._+-]+$")
PI_TOOLS = frozenset({"read", "write", "edit", "bash"})
WEIGHT_FORMATS = frozenset({"native", "bf16", "fp8", "int8", "q8"})
KV_CACHE_DTYPES = frozenset({"bf16", "fp16", "fp8"})
INPUT_MODALITIES = frozenset({"text", "image"})
SENSITIVE_FIELD_PATTERN = re.compile(
    r"(?:^|_)(?:api_?)?(?:token|key|secret|password|credential)(?:_|$)",
    re.IGNORECASE,
)

_PROFILE_V1_KEYS = {
    "schema",
    "profile_id",
    "project_id",
    "state_root",
    "project_root",
    "model",
    "runtime",
    "pi",
}
_PROFILE_V2_KEYS = {
    *_PROFILE_V1_KEYS,
    "storage",
    "sandbox",
}
_MODEL_KEYS = {
    "repository",
    "revision",
    "context_tokens",
    "max_output_tokens",
    "kv_cache_dtype",
    "max_sequences",
    "weight_format",
}
_RUNTIME_KEYS = {
    "provider",
    "model_id",
    "reasoning",
    "input_modalities",
}
_PI_REQUIRED_KEYS = {
    "installation_root",
    "executable",
    "version",
    "tools",
}
_PI_OPTIONAL_KEYS = {"system_prompt_file", "append_system_prompt_file"}
_STORAGE_KEYS = {
    "max_sessions",
    "work_bytes",
    "work_inodes",
    "history_bytes",
    "history_inodes",
    "checkpoint_bytes",
    "max_file_bytes",
    "max_logical_bytes",
}
_SANDBOX_KEYS = {
    "memory_bytes",
    "max_tasks",
    "max_runtime_seconds",
    "idle_timeout_seconds",
    "shutdown_grace_seconds",
}

_SYSTEM_SENSITIVE_TREES = (
    pathlib.Path("/bin"),
    pathlib.Path("/boot"),
    pathlib.Path("/dev"),
    pathlib.Path("/etc"),
    pathlib.Path("/lib"),
    pathlib.Path("/lib64"),
    pathlib.Path("/proc"),
    pathlib.Path("/root"),
    pathlib.Path("/run"),
    pathlib.Path("/sbin"),
    pathlib.Path("/sys"),
    pathlib.Path("/usr"),
)
_BROAD_ROOTS = {
    pathlib.Path("/"),
    pathlib.Path("/home"),
    pathlib.Path("/media"),
    pathlib.Path("/mnt"),
    pathlib.Path("/opt"),
    pathlib.Path("/srv"),
    pathlib.Path("/tmp"),
    pathlib.Path("/var"),
}


@dataclass(frozen=True)
class ModelContract:
    repository: str
    revision: str
    context_tokens: int
    max_output_tokens: int
    kv_cache_dtype: str
    max_sequences: int
    weight_format: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "revision": self.revision,
            "context_tokens": self.context_tokens,
            "max_output_tokens": self.max_output_tokens,
            "kv_cache_dtype": self.kv_cache_dtype,
            "max_sequences": self.max_sequences,
            "weight_format": self.weight_format,
        }


@dataclass(frozen=True)
class RuntimeContract:
    provider: str
    model_id: str
    reasoning: bool
    input_modalities: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "reasoning": self.reasoning,
            "input_modalities": list(self.input_modalities),
        }


@dataclass(frozen=True)
class PiContract:
    installation_root: pathlib.Path
    executable: pathlib.PurePosixPath
    version: str
    tools: tuple[str, ...]
    system_prompt_file: pathlib.PurePosixPath | None
    append_system_prompt_file: pathlib.PurePosixPath | None

    @property
    def executable_path(self) -> pathlib.Path:
        return self.installation_root.joinpath(*self.executable.parts)

    @property
    def node_executable_path(self) -> pathlib.Path:
        return self.installation_root.joinpath(*PI_NODE_EXECUTABLE.parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "installation_root": str(self.installation_root),
            "executable": self.executable.as_posix(),
            "version": self.version,
            "tools": list(self.tools),
            "system_prompt_file": (
                self.system_prompt_file.as_posix()
                if self.system_prompt_file is not None
                else None
            ),
            "append_system_prompt_file": (
                self.append_system_prompt_file.as_posix()
                if self.append_system_prompt_file is not None
                else None
            ),
        }


@dataclass(frozen=True)
class StorageContract:
    """Runtime tmpfs capacities and checkpoint-eligibility policy."""

    # Retained-session ceiling for materialization admission policy.
    max_sessions: int
    # Hard byte capacity of the mutable work tmpfs.
    work_bytes: int
    # Hard inode/dentry capacity of the mutable work tmpfs.
    work_inodes: int
    # Hard byte capacity of the mutable history tmpfs.
    history_bytes: int
    # Hard inode/dentry capacity of the mutable history tmpfs.
    history_inodes: int
    # Maximum encoded bytes accepted for one checkpoint pack.
    checkpoint_bytes: int
    # Derived aggregate sparse-extent eligibility ceiling.
    max_sparse_extents: int
    # Maximum logical size of one checkpoint-eligible regular file.
    max_file_bytes: int
    # Maximum aggregate logical size of checkpoint-eligible state.
    max_logical_bytes: int

    def checkpoint_limits(self) -> CheckpointLimits:
        """Return the codec envelope for checkpoint-eligible mutable state."""

        return CheckpointLimits(
            max_entries=self.work_inodes + self.history_inodes,
            max_file_logical_bytes=self.max_file_bytes,
            max_logical_bytes=self.max_logical_bytes,
            # Inline symlink targets consume tmpfs inodes but no data blocks.
            # Logical bytes are therefore the only safe aggregate payload cap.
            max_payload_bytes=self.max_logical_bytes,
            max_pack_bytes=self.checkpoint_bytes,
            max_sparse_extents_per_file=(
                max(self.work_bytes, self.history_bytes)
                // STORAGE_PAGE_SIZE
            ),
            max_sparse_extents=self.max_sparse_extents,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "max_sessions": self.max_sessions,
            "work_bytes": self.work_bytes,
            "work_inodes": self.work_inodes,
            "history_bytes": self.history_bytes,
            "history_inodes": self.history_inodes,
            "checkpoint_bytes": self.checkpoint_bytes,
            "max_sparse_extents": self.max_sparse_extents,
            "max_file_bytes": self.max_file_bytes,
            "max_logical_bytes": self.max_logical_bytes,
        }


@dataclass(frozen=True)
class SandboxContract:
    # Page-exact hard memory ceiling for the untrusted workload cgroup.
    memory_bytes: int
    # Hard task/thread ceiling for the untrusted workload cgroup.
    max_tasks: int
    # Maximum elapsed duration of one workload launch.
    max_runtime_seconds: int
    # Maximum duration without accepted workload activity.
    idle_timeout_seconds: int
    # Grace between cooperative shutdown and forced workload termination.
    shutdown_grace_seconds: int

    def as_dict(self) -> dict[str, int]:
        return {
            "memory_bytes": self.memory_bytes,
            "max_tasks": self.max_tasks,
            "max_runtime_seconds": self.max_runtime_seconds,
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "shutdown_grace_seconds": self.shutdown_grace_seconds,
        }


@dataclass(frozen=True)
class ProfileContract:
    schema: str
    profile_id: str
    project_id: str
    profile_root: pathlib.Path
    state_root: pathlib.Path
    project_root: pathlib.Path
    model: ModelContract
    runtime: RuntimeContract
    pi: PiContract
    storage: StorageContract | None = None
    sandbox: SandboxContract | None = None

    def __post_init__(self) -> None:
        if self.schema == PROFILE_SCHEMA_V1:
            if self.storage is not None or self.sandbox is not None:
                raise ValueError(
                    "profile v1 contracts cannot contain storage or sandbox policy"
                )
            return
        if self.schema == PROFILE_SCHEMA_V2:
            if self.storage is None or self.sandbox is None:
                raise ValueError(
                    "profile v2 contracts require storage and sandbox policy"
                )
            return
        raise ValueError(f"unsupported profile schema {self.schema!r}")

    def as_dict(self) -> dict[str, Any]:
        value = {
            "schema": self.schema,
            "profile_id": self.profile_id,
            "project_id": self.project_id,
            "profile_root": str(self.profile_root),
            "state_root": str(self.state_root),
            "project_root": str(self.project_root),
            "model": self.model.as_dict(),
            "runtime": self.runtime.as_dict(),
            "pi": self.pi.as_dict(),
        }
        if self.schema == PROFILE_SCHEMA_V2:
            if self.storage is None or self.sandbox is None:
                raise AssertionError("profile v2 policy is absent")
            value["storage"] = self.storage.as_dict()
            value["sandbox"] = self.sandbox.as_dict()
        return value


@dataclass(frozen=True)
class ProfileResource:
    relative_path: pathlib.PurePosixPath
    roles: tuple[str, ...]
    content: bytes = field(repr=False)


@dataclass(frozen=True)
class Profile:
    contract: ProfileContract
    document: bytes = field(repr=False)
    resources: tuple[ProfileResource, ...] = field(repr=False)

    def resource_for_role(self, role: str) -> ProfileResource | None:
        for resource in self.resources:
            if role in resource.roles:
                return resource
        return None


@dataclass(frozen=True)
class ProfileRoute:
    """Minimum current-profile authority needed to find historical runs."""

    profile_root: pathlib.Path
    profile_id: str
    state_root: pathlib.Path


def infrastructure_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def _fail(message: str, *, code: str = "invalid_profile") -> None:
    raise ModelSessionError(message, code=code)


def _reject_sensitive_fields(value: Any, *, location: str = "profile") -> None:
    if not isinstance(value, dict):
        return
    for key, child in value.items():
        if isinstance(key, str) and SENSITIVE_FIELD_PATTERN.search(key):
            _fail(
                f"{location} contains forbidden secret-bearing field {key!r}",
                code="secret_field_rejected",
            )
        _reject_sensitive_fields(child, location=f"{location}.{key}")


def _require_exact_keys(
    value: Any,
    *,
    required: set[str],
    optional: set[str] | None = None,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be a TOML table")
    optional = optional or set()
    actual = set(value)
    missing = required - actual
    unexpected = actual - required - optional
    if missing:
        _fail(f"{label} is missing required fields: {', '.join(sorted(missing))}")
    if unexpected:
        _fail(f"{label} has unsupported fields: {', '.join(sorted(unexpected))}")
    return value


def _identifier(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_PATTERN.fullmatch(value):
        _fail(
            f"{label} must start with a lowercase letter and contain only "
            "lowercase letters, digits, and hyphens (maximum 63 characters)"
        )
    return value


def _string(
    value: Any,
    *,
    label: str,
    maximum_bytes: int = 512,
) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail(f"{label} must be a nonempty string without surrounding whitespace")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ModelSessionError(
            f"{label} is not valid UTF-8",
            code="invalid_profile",
        ) from error
    if len(encoded) > maximum_bytes or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        _fail(f"{label} contains control characters or is too long")
    return value


def _integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _fail(f"{label} must be a positive integer")
    return value


def _bounded_integer(
    value: Any,
    *,
    label: str,
    maximum: int,
) -> int:
    result = _integer(value, label=label)
    if result > maximum:
        _fail(f"{label} must not exceed {maximum}")
    return result


def _parse_storage_contract(value: dict[str, Any]) -> StorageContract:
    max_sessions = _bounded_integer(
        value["max_sessions"],
        label="profile.storage.max_sessions",
        maximum=MAX_SESSIONS,
    )
    work_bytes = _bounded_integer(
        value["work_bytes"],
        label="profile.storage.work_bytes",
        maximum=MAX_VOLUME_BYTES,
    )
    work_inodes = _bounded_integer(
        value["work_inodes"],
        label="profile.storage.work_inodes",
        maximum=MAX_STORAGE_INODES,
    )
    history_bytes = _bounded_integer(
        value["history_bytes"],
        label="profile.storage.history_bytes",
        maximum=MAX_VOLUME_BYTES,
    )
    history_inodes = _bounded_integer(
        value["history_inodes"],
        label="profile.storage.history_inodes",
        maximum=MAX_STORAGE_INODES,
    )
    checkpoint_bytes = _bounded_integer(
        value["checkpoint_bytes"],
        label="profile.storage.checkpoint_bytes",
        maximum=MAX_CHECKPOINT_BYTES,
    )
    max_file_bytes = _bounded_integer(
        value["max_file_bytes"],
        label="profile.storage.max_file_bytes",
        maximum=MAX_FILE_BYTES,
    )
    max_logical_bytes = _bounded_integer(
        value["max_logical_bytes"],
        label="profile.storage.max_logical_bytes",
        maximum=MAX_LOGICAL_BYTES,
    )

    total_inodes = work_inodes + history_inodes
    if total_inodes > MAX_STORAGE_INODES:
        _fail(
            "profile.storage work_inodes plus history_inodes must not exceed "
            f"{MAX_STORAGE_INODES}"
        )
    try:
        StoragePoolLimits(work_bytes, work_inodes)
        StoragePoolLimits(history_bytes, history_inodes)
    except ModelSessionError as error:
        _fail(f"profile.storage is not representable as bounded tmpfs: {error}")
    volume_bytes = work_bytes + history_bytes
    if max_file_bytes > max_logical_bytes:
        _fail(
            "profile.storage.max_file_bytes checkpoint eligibility must not "
            "exceed max_logical_bytes"
        )
    max_sparse_extents = volume_bytes // STORAGE_PAGE_SIZE
    storage = StorageContract(
        max_sessions=max_sessions,
        work_bytes=work_bytes,
        work_inodes=work_inodes,
        history_bytes=history_bytes,
        history_inodes=history_inodes,
        checkpoint_bytes=checkpoint_bytes,
        max_sparse_extents=max_sparse_extents,
        max_file_bytes=max_file_bytes,
        max_logical_bytes=max_logical_bytes,
    )
    minimum_checkpoint_bytes = maximum_encoded_bytes(
        storage.checkpoint_limits()
    )
    if checkpoint_bytes < minimum_checkpoint_bytes:
        _fail(
            "profile.storage.checkpoint_bytes must cover the complete "
            "checkpoint-eligible representation bound of "
            f"{minimum_checkpoint_bytes} bytes"
        )
    return storage


def _parse_sandbox_contract(
    value: dict[str, Any],
    *,
    storage: StorageContract,
) -> SandboxContract:
    memory_bytes = _bounded_integer(
        value["memory_bytes"],
        label="profile.sandbox.memory_bytes",
        maximum=MAX_SANDBOX_MEMORY_BYTES,
    )
    max_tasks = _bounded_integer(
        value["max_tasks"],
        label="profile.sandbox.max_tasks",
        maximum=MAX_SANDBOX_TASKS,
    )
    max_runtime_seconds = _bounded_integer(
        value["max_runtime_seconds"],
        label="profile.sandbox.max_runtime_seconds",
        maximum=MAX_RUNTIME_SECONDS,
    )
    idle_timeout_seconds = _bounded_integer(
        value["idle_timeout_seconds"],
        label="profile.sandbox.idle_timeout_seconds",
        maximum=MAX_RUNTIME_SECONDS,
    )
    shutdown_grace_seconds = _bounded_integer(
        value["shutdown_grace_seconds"],
        label="profile.sandbox.shutdown_grace_seconds",
        maximum=MAX_SHUTDOWN_GRACE_SECONDS,
    )

    if memory_bytes % STORAGE_PAGE_SIZE != 0:
        _fail(
            "profile.sandbox.memory_bytes must be a multiple of "
            f"{STORAGE_PAGE_SIZE}"
        )
    minimum_memory_bytes = (
        storage.work_bytes
        + storage.history_bytes
        + SANDBOX_STORAGE_HEADROOM_BYTES
    )
    if memory_bytes < minimum_memory_bytes:
        _fail(
            "profile.sandbox.memory_bytes must cover work_bytes plus "
            "history_bytes plus "
            f"{SANDBOX_STORAGE_HEADROOM_BYTES} bytes of process headroom"
        )
    if max_tasks < MIN_SANDBOX_TASKS:
        _fail(
            "profile.sandbox.max_tasks must be at least "
            f"{MIN_SANDBOX_TASKS} for launcher, helper, relay, Pi, and "
            "runtime threads"
        )
    if idle_timeout_seconds > max_runtime_seconds:
        _fail(
            "profile.sandbox.idle_timeout_seconds must not exceed "
            "max_runtime_seconds"
        )
    if shutdown_grace_seconds > idle_timeout_seconds:
        _fail(
            "profile.sandbox.shutdown_grace_seconds must not exceed "
            "idle_timeout_seconds"
        )
    return SandboxContract(
        memory_bytes=memory_bytes,
        max_tasks=max_tasks,
        max_runtime_seconds=max_runtime_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
        shutdown_grace_seconds=shutdown_grace_seconds,
    )


def _path_is_within(path: pathlib.Path, root: pathlib.Path) -> bool:
    return path == root or path.is_relative_to(root)


def _paths_overlap(first: pathlib.Path, second: pathlib.Path) -> bool:
    return _path_is_within(first, second) or _path_is_within(second, first)


def _sensitive_roots() -> tuple[pathlib.Path, ...]:
    home = pathlib.Path.home().resolve()
    return (
        *_SYSTEM_SENSITIVE_TREES,
        home / ".aws",
        home / ".config",
        home / ".gnupg",
        home / ".ssh",
    )


def _absolute_normalized_path(
    value: Any,
    *,
    label: str,
    must_exist: bool,
) -> pathlib.Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        _fail(f"{label} must be an absolute normalized path")
    path = pathlib.Path(value)
    if not path.is_absolute() or value != os.path.normpath(value):
        _fail(f"{label} must be an absolute normalized path")
    try:
        resolved = path.resolve(strict=must_exist)
    except (OSError, RuntimeError) as error:
        raise ModelSessionError(
            f"cannot resolve {label} {path}: {error}",
            code="unsafe_profile_path",
        ) from error
    if resolved != path:
        _fail(
            f"{label} must not contain symlink components",
            code="unsafe_profile_path",
        )
    if path in _BROAD_ROOTS:
        _fail(
            f"{label} is a dangerously broad root: {path}",
            code="unsafe_profile_path",
        )
    for root in _sensitive_roots():
        if _path_is_within(path, root):
            _fail(
                f"{label} is inside a sensitive system or credential root: {root}",
                code="unsafe_profile_path",
            )
    return path


def _absolute_lexical_path(value: Any, *, label: str) -> pathlib.Path:
    """Validate inert historical path text without consulting current state."""

    if not isinstance(value, str) or not value or "\x00" in value:
        _fail(f"{label} must be an absolute normalized path")
    path = pathlib.Path(value)
    if not path.is_absolute() or value != os.path.normpath(value):
        _fail(f"{label} must be an absolute normalized path")
    return path


def _validate_directory(
    path: pathlib.Path,
    *,
    label: str,
    require_current_owner: bool,
    reject_group_write: bool = False,
    error_code: str = "unsafe_profile_path",
) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ModelSessionError(
            f"cannot inspect {label} {path}: {error}",
            code=error_code,
        ) from error
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        _fail(f"{label} is not a real directory: {path}", code=error_code)
    if (
        require_current_owner
        and hasattr(os, "getuid")
        and metadata.st_uid != os.getuid()
    ):
        _fail(
            f"{label} is not owned by the current user: {path}",
            code=error_code,
        )
    if metadata.st_mode & stat.S_IWOTH:
        _fail(
            f"{label} is writable by another principal: {path}",
            code=error_code,
        )
    if metadata.st_mode & stat.S_IWGRP and (
        reject_group_write
        or not owner_has_private_primary_group(metadata)
    ):
        _fail(
            f"{label} is writable by another principal: {path}",
            code=error_code,
        )


def _relative_path(value: Any, *, label: str) -> pathlib.PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        _fail(f"{label} must be a normalized relative path")
    path = pathlib.PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(
            part in {"", ".", ".."} or not RELATIVE_COMPONENT_PATTERN.fullmatch(part)
            for part in path.parts
        )
    ):
        _fail(f"{label} must be a normalized relative path")
    return path


def _read_owned_regular_file(
    path: pathlib.Path,
    *,
    label: str,
    maximum_bytes: int,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ModelSessionError(
            f"cannot open {label} {path}: {error}",
            code="unsafe_profile_resource",
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _fail(
                f"{label} is not a regular non-symlink file: {path}",
                code="unsafe_profile_resource",
            )
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            _fail(
                f"{label} is not owned by the current user: {path}",
                code="unsafe_profile_resource",
            )
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            _fail(
                f"{label} is writable by another principal: {path}",
                code="unsafe_profile_resource",
            )
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > maximum_bytes:
            _fail(
                f"{label} exceeds the {maximum_bytes}-byte limit",
                code="profile_resource_too_large",
            )
        return content
    finally:
        os.close(descriptor)


def _resource_path(
    profile_root: pathlib.Path,
    relative_path: pathlib.PurePosixPath,
    *,
    label: str,
) -> pathlib.Path:
    path = profile_root.joinpath(*relative_path.parts)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ModelSessionError(
            f"cannot resolve {label} {path}: {error}",
            code="unsafe_profile_resource",
        ) from error
    if resolved != path or not _path_is_within(path, profile_root):
        _fail(
            f"{label} must be a non-symlink file inside the profile directory",
            code="unsafe_profile_resource",
        )
    cursor = path.parent
    while _path_is_within(cursor, profile_root):
        _validate_directory(
            cursor,
            label=f"{label} parent directory",
            require_current_owner=True,
            reject_group_write=True,
            error_code="unsafe_profile_resource",
        )
        if cursor == profile_root:
            break
        cursor = cursor.parent
    return path


def _resolve_pi_executable_path(
    contract: PiContract,
    executable: pathlib.PurePosixPath,
    *,
    label: str,
) -> pathlib.Path:
    """Resolve a relocatable Pi runtime executable inside its locked tree."""

    current = contract.installation_root
    pending = list(executable.parts)
    executable_path = contract.installation_root.joinpath(*executable.parts)
    followed_links = 0
    while pending:
        component = pending.pop(0)
        if component == ".":
            continue
        if component == "..":
            current = current.parent
            if not _path_is_within(current, contract.installation_root):
                _fail(
                    f"{label} symlink escapes installation_root",
                    code="unsafe_pi_installation",
                )
            continue
        candidate = current / component
        try:
            metadata = candidate.lstat()
        except OSError as error:
            raise ModelSessionError(
                f"cannot resolve {label} {executable_path}: {error}",
                code="unsafe_pi_installation",
            ) from error
        if stat.S_ISLNK(metadata.st_mode):
            followed_links += 1
            if followed_links > 40:
                _fail(
                    f"{label} contains too many symbolic links",
                    code="unsafe_pi_installation",
                )
            try:
                target = os.readlink(candidate)
            except OSError as error:
                raise ModelSessionError(
                    f"cannot inspect {label} symlink {candidate}: {error}",
                    code="unsafe_pi_installation",
                ) from error
            if os.path.isabs(target):
                _fail(
                    f"{label} uses an absolute symlink and cannot be "
                    "relocated into the sandbox",
                    code="unsafe_pi_installation",
                )
            pending = list(pathlib.PurePosixPath(target).parts) + pending
            continue
        current = candidate
    if not _path_is_within(current, contract.installation_root):
        _fail(
            f"{label} symlink escapes installation_root",
            code="unsafe_pi_installation",
        )
    return current


def _validate_pi_executable(
    contract: PiContract,
    executable: pathlib.PurePosixPath,
    *,
    label: str,
) -> None:
    candidate = contract.installation_root.joinpath(*executable.parts)
    resolved = _resolve_pi_executable_path(
        contract,
        executable,
        label=label,
    )
    if not _path_is_within(resolved, contract.installation_root):
        _fail(
            f"{label} symlink escapes installation_root",
            code="unsafe_pi_installation",
        )
    try:
        metadata = resolved.stat()
    except OSError as error:
        raise ModelSessionError(
            f"cannot inspect {label} {resolved}: {error}",
            code="unsafe_pi_installation",
        ) from error
    if not stat.S_ISREG(metadata.st_mode):
        _fail(
            f"{label} does not resolve to a regular file",
            code="unsafe_pi_installation",
        )
    if hasattr(os, "getuid") and metadata.st_uid not in {0, os.getuid()}:
        _fail(
            f"{label} is owned by an unexpected user",
            code="unsafe_pi_installation",
        )
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        _fail(
            f"{label} target is group- or world-writable",
            code="unsafe_pi_installation",
        )
    if not os.access(resolved, os.X_OK):
        _fail(f"{label} target is not executable", code="unsafe_pi_installation")


def _validate_pi_installation_tree(contract: PiContract) -> None:
    root = contract.installation_root
    paths = {root}
    for executable, label in (
        (contract.executable, "Pi executable"),
        (PI_NODE_EXECUTABLE, "bundled Node executable"),
    ):
        candidate = root.joinpath(*executable.parts)
        resolved = _resolve_pi_executable_path(
            contract,
            executable,
            label=label,
        )
        paths.add(resolved)
        for endpoint in (candidate.parent, resolved.parent):
            cursor = endpoint
            while _path_is_within(cursor, root):
                paths.add(cursor)
                if cursor == root:
                    break
                cursor = cursor.parent
    for path in paths:
        try:
            metadata = path.stat()
        except OSError as error:
            raise ModelSessionError(
                f"cannot inspect Pi installation path {path}: {error}",
                code="unsafe_pi_installation",
            ) from error
        if hasattr(os, "getuid") and metadata.st_uid not in {0, os.getuid()}:
            _fail(
                f"Pi installation path has an unexpected owner: {path}",
                code="unsafe_pi_installation",
            )
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            _fail(
                f"Pi installation path is group- or world-writable: {path}",
                code="unsafe_pi_installation",
            )


def _parse_document(
    document: bytes,
    profile_root: pathlib.Path,
    *,
    require_live_profile_root: bool,
    accepted_schemas: frozenset[str],
) -> ProfileContract:
    try:
        text = document.decode("utf-8")
        value = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ModelSessionError(
            f"{PROFILE_FILE_NAME} is not valid UTF-8 TOML: {error}",
            code="invalid_profile",
        ) from error
    if not isinstance(value, dict):
        _fail("profile must be a TOML table")
    _reject_sensitive_fields(value)
    schema = value.get("schema")
    if not isinstance(schema, str) or schema not in KNOWN_PROFILE_SCHEMAS:
        _fail(
            "profile.schema must be one of "
            f"{PROFILE_SCHEMA_V1!r} or {PROFILE_SCHEMA_V2!r}"
        )
    if schema not in accepted_schemas:
        if accepted_schemas == frozenset({PROFILE_SCHEMA_V2}):
            _fail(f"profile.schema must be exactly {PROFILE_SCHEMA_V2!r}")
        _fail(f"profile.schema {schema!r} is not accepted here")
    top_level_keys = (
        _PROFILE_V1_KEYS
        if schema == PROFILE_SCHEMA_V1
        else _PROFILE_V2_KEYS
    )
    top = _require_exact_keys(
        value,
        required=top_level_keys,
        label="profile",
    )
    model_value = _require_exact_keys(
        top["model"],
        required=_MODEL_KEYS,
        label="profile.model",
    )
    runtime_value = _require_exact_keys(
        top["runtime"],
        required=_RUNTIME_KEYS,
        label="profile.runtime",
    )
    pi_value = _require_exact_keys(
        top["pi"],
        required=_PI_REQUIRED_KEYS,
        optional=_PI_OPTIONAL_KEYS,
        label="profile.pi",
    )
    storage_value: dict[str, Any] | None = None
    sandbox_value: dict[str, Any] | None = None
    if schema == PROFILE_SCHEMA_V2:
        storage_value = _require_exact_keys(
            top["storage"],
            required=_STORAGE_KEYS,
            label="profile.storage",
        )
        sandbox_value = _require_exact_keys(
            top["sandbox"],
            required=_SANDBOX_KEYS,
            label="profile.sandbox",
        )

    profile_id = _identifier(top["profile_id"], label="profile.profile_id")
    project_id = _identifier(top["project_id"], label="profile.project_id")
    state_root = _absolute_normalized_path(
        top["state_root"],
        label="profile.state_root",
        must_exist=False,
    )
    project_root = _absolute_normalized_path(
        top["project_root"],
        label="profile.project_root",
        must_exist=True,
    )
    installation_root = _absolute_normalized_path(
        pi_value["installation_root"],
        label="profile.pi.installation_root",
        must_exist=True,
    )

    repository = _string(
        model_value["repository"],
        label="profile.model.repository",
        maximum_bytes=193,
    )
    if not REPOSITORY_PATTERN.fullmatch(repository):
        _fail("profile.model.repository must be an exact Hugging Face owner/name")
    revision = _string(
        model_value["revision"],
        label="profile.model.revision",
        maximum_bytes=40,
    )
    if not COMMIT_PATTERN.fullmatch(revision):
        _fail("profile.model.revision must be an immutable 40-hex commit")
    context_tokens = _integer(
        model_value["context_tokens"],
        label="profile.model.context_tokens",
    )
    max_output_tokens = _integer(
        model_value["max_output_tokens"],
        label="profile.model.max_output_tokens",
    )
    if max_output_tokens > context_tokens:
        _fail("profile.model.max_output_tokens must not exceed context_tokens")
    kv_cache_dtype = _string(
        model_value["kv_cache_dtype"],
        label="profile.model.kv_cache_dtype",
        maximum_bytes=4,
    )
    if kv_cache_dtype not in KV_CACHE_DTYPES:
        _fail("profile.model.kv_cache_dtype must be bf16, fp16, or fp8")
    max_sequences = _integer(
        model_value["max_sequences"],
        label="profile.model.max_sequences",
    )
    weight_format = _string(
        model_value["weight_format"],
        label="profile.model.weight_format",
        maximum_bytes=63,
    )
    if weight_format not in WEIGHT_FORMATS:
        _fail(
            "profile.model.weight_format must be one of "
            "native, bf16, fp8, int8, or q8"
        )

    provider = _string(
        runtime_value["provider"],
        label="profile.runtime.provider",
        maximum_bytes=63,
    )
    if not PROVIDER_PATTERN.fullmatch(provider):
        _fail("profile.runtime.provider must be a lowercase provider identifier")
    model_id = _string(
        runtime_value["model_id"],
        label="profile.runtime.model_id",
        maximum_bytes=256,
    )
    reasoning = runtime_value["reasoning"]
    if not isinstance(reasoning, bool):
        _fail("profile.runtime.reasoning must be a boolean")
    modalities_value = runtime_value["input_modalities"]
    if not isinstance(modalities_value, list) or not modalities_value:
        _fail("profile.runtime.input_modalities must be a nonempty array")
    input_modalities: list[str] = []
    for modality in modalities_value:
        if not isinstance(modality, str) or modality not in INPUT_MODALITIES:
            _fail(
                "profile.runtime.input_modalities entries must be text or image"
            )
        if modality in input_modalities:
            _fail(
                "profile.runtime.input_modalities contains duplicate entry "
                f"{modality!r}"
            )
        input_modalities.append(modality)
    if "text" not in input_modalities:
        _fail("profile.runtime.input_modalities must include text")

    executable = _relative_path(
        pi_value["executable"],
        label="profile.pi.executable",
    )
    version = _string(
        pi_value["version"],
        label="profile.pi.version",
        maximum_bytes=96,
    )
    if not PI_VERSION_PATTERN.fullmatch(version):
        _fail("profile.pi.version must be an exact semantic version")
    tools_value = pi_value["tools"]
    if not isinstance(tools_value, list) or not tools_value:
        _fail("profile.pi.tools must be a nonempty array")
    tools: list[str] = []
    for tool in tools_value:
        if not isinstance(tool, str) or tool not in PI_TOOLS:
            _fail(
                "profile.pi.tools entries must be selected from "
                "read, write, edit, and bash"
            )
        if tool in tools:
            _fail(f"profile.pi.tools contains duplicate entry {tool!r}")
        tools.append(tool)

    system_prompt_file = (
        _relative_path(
            pi_value["system_prompt_file"],
            label="profile.pi.system_prompt_file",
        )
        if "system_prompt_file" in pi_value
        else None
    )
    append_system_prompt_file = (
        _relative_path(
            pi_value["append_system_prompt_file"],
            label="profile.pi.append_system_prompt_file",
        )
        if "append_system_prompt_file" in pi_value
        else None
    )
    prompt_paths = [
        path
        for path in (system_prompt_file, append_system_prompt_file)
        if path is not None
    ]
    if any(path.as_posix() == PROFILE_FILE_NAME for path in prompt_paths):
        _fail("profile.toml cannot be used as a Pi prompt resource")
    if len(set(prompt_paths)) != len(prompt_paths):
        _fail("Pi system and append-system prompt files must be distinct")

    storage: StorageContract | None = None
    sandbox: SandboxContract | None = None
    if schema == PROFILE_SCHEMA_V2:
        if storage_value is None or sandbox_value is None:
            raise AssertionError("profile v2 tables are absent")
        storage = _parse_storage_contract(storage_value)
        sandbox = _parse_sandbox_contract(
            sandbox_value,
            storage=storage,
        )
    contract = ProfileContract(
        schema=schema,
        profile_id=profile_id,
        project_id=project_id,
        profile_root=profile_root,
        state_root=state_root,
        project_root=project_root,
        model=ModelContract(
            repository=repository,
            revision=revision.lower(),
            context_tokens=context_tokens,
            max_output_tokens=max_output_tokens,
            kv_cache_dtype=kv_cache_dtype,
            max_sequences=max_sequences,
            weight_format=weight_format,
        ),
        runtime=RuntimeContract(
            provider=provider,
            model_id=model_id,
            reasoning=reasoning,
            input_modalities=tuple(input_modalities),
        ),
        pi=PiContract(
            installation_root=installation_root,
            executable=executable,
            version=version,
            tools=tuple(tools),
            system_prompt_file=system_prompt_file,
            append_system_prompt_file=append_system_prompt_file,
        ),
        storage=storage,
        sandbox=sandbox,
    )
    _validate_contract_paths(
        contract,
        require_live_profile_root=require_live_profile_root,
    )
    return contract


def _validate_contract_paths(
    contract: ProfileContract,
    *,
    require_live_profile_root: bool,
) -> None:
    roots = {
        "state_root": contract.state_root,
        "project_root": contract.project_root,
        "Pi installation_root": contract.pi.installation_root,
    }
    if require_live_profile_root:
        roots = {"profile directory": contract.profile_root, **roots}
    dotfiles = infrastructure_root()
    for label, path in roots.items():
        if _paths_overlap(path, dotfiles):
            _fail(
                f"{label} overlaps the dotfiles infrastructure repository",
                code="profile_inside_infrastructure",
            )
    entries = list(roots.items())
    for index, (first_label, first_path) in enumerate(entries):
        for second_label, second_path in entries[index + 1 :]:
            if _paths_overlap(first_path, second_path):
                _fail(
                    f"{first_label} overlaps {second_label}",
                    code="overlapping_profile_paths",
                )

    if require_live_profile_root:
        _validate_directory(
            contract.profile_root,
            label="profile directory",
            require_current_owner=True,
            reject_group_write=True,
        )
    if contract.state_root.exists():
        _validate_directory(
            contract.state_root,
            label="state_root",
            require_current_owner=True,
            reject_group_write=True,
        )
    _validate_directory(
        contract.project_root,
        label="project_root",
        require_current_owner=True,
    )
    _validate_directory(
        contract.pi.installation_root,
        label="Pi installation_root",
        require_current_owner=False,
        reject_group_write=True,
        error_code="unsafe_pi_installation",
    )
    _validate_pi_executable(
        contract.pi,
        contract.pi.executable,
        label="Pi executable",
    )
    _validate_pi_executable(
        contract.pi,
        PI_NODE_EXECUTABLE,
        label="bundled Node executable",
    )
    _validate_pi_installation_tree(contract.pi)


def load_profile(profile_root: str | pathlib.Path) -> Profile:
    """Load and validate one concrete profile without mutating external state."""

    raw_root = str(profile_root)
    root = _absolute_normalized_path(
        raw_root,
        label="profile directory",
        must_exist=True,
    )
    _validate_directory(
        root,
        label="profile directory",
        require_current_owner=True,
        reject_group_write=True,
    )
    profile_path = _resource_path(
        root,
        pathlib.PurePosixPath(PROFILE_FILE_NAME),
        label=PROFILE_FILE_NAME,
    )
    document = _read_owned_regular_file(
        profile_path,
        label=PROFILE_FILE_NAME,
        maximum_bytes=MAX_PROFILE_BYTES,
    )
    contract = _parse_document(
        document,
        root,
        require_live_profile_root=True,
        accepted_schemas=frozenset({PROFILE_SCHEMA_V2}),
    )

    roles_by_path: dict[pathlib.PurePosixPath, list[str]] = {
        pathlib.PurePosixPath(AGENTS_FILE_NAME): ["agents"]
    }
    if contract.pi.system_prompt_file is not None:
        roles_by_path.setdefault(contract.pi.system_prompt_file, []).append(
            "system_prompt"
        )
    if contract.pi.append_system_prompt_file is not None:
        roles_by_path.setdefault(contract.pi.append_system_prompt_file, []).append(
            "append_system_prompt"
        )

    resources = []
    for relative_path in sorted(roles_by_path, key=lambda path: path.as_posix()):
        path = _resource_path(
            root,
            relative_path,
            label=relative_path.as_posix(),
        )
        content = _read_owned_regular_file(
            path,
            label=relative_path.as_posix(),
            maximum_bytes=MAX_RESOURCE_BYTES,
        )
        resources.append(
            ProfileResource(
                relative_path=relative_path,
                roles=tuple(roles_by_path[relative_path]),
                content=content,
            )
        )
    return Profile(
        contract=contract,
        document=document,
        resources=tuple(resources),
    )


def load_profile_route(profile_root: str | pathlib.Path) -> ProfileRoute:
    """Read only the stable route fields needed by status and resume."""

    root = _absolute_normalized_path(
        str(profile_root),
        label="profile directory",
        must_exist=True,
    )
    _validate_directory(
        root,
        label="profile directory",
        require_current_owner=True,
        reject_group_write=True,
    )
    profile_path = _resource_path(
        root,
        pathlib.PurePosixPath(PROFILE_FILE_NAME),
        label=PROFILE_FILE_NAME,
    )
    document = _read_owned_regular_file(
        profile_path,
        label=PROFILE_FILE_NAME,
        maximum_bytes=MAX_PROFILE_BYTES,
    )
    try:
        value = tomllib.loads(document.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ModelSessionError(
            f"{PROFILE_FILE_NAME} is not valid UTF-8 TOML: {error}",
            code="invalid_profile",
        ) from error
    _reject_sensitive_fields(value)
    if not isinstance(value, dict):
        _fail("profile must be a TOML table")
    schema = value.get("schema")
    if not isinstance(schema, str) or schema not in KNOWN_PROFILE_SCHEMAS:
        _fail(
            "profile.schema must be one of "
            f"{PROFILE_SCHEMA_V1!r} or {PROFILE_SCHEMA_V2!r}"
        )
    profile_id = _identifier(
        value.get("profile_id"),
        label="profile.profile_id",
    )
    state_root = _absolute_normalized_path(
        value.get("state_root"),
        label="profile.state_root",
        must_exist=False,
    )
    dotfiles = infrastructure_root()
    if _paths_overlap(root, dotfiles) or _paths_overlap(state_root, dotfiles):
        _fail(
            "profile route overlaps the dotfiles infrastructure repository",
            code="profile_inside_infrastructure",
        )
    if _paths_overlap(root, state_root):
        _fail(
            "profile directory overlaps state_root",
            code="overlapping_profile_paths",
        )
    if state_root.exists():
        _validate_directory(
            state_root,
            label="state_root",
            require_current_owner=True,
            reject_group_write=True,
        )
    return ProfileRoute(
        profile_root=root,
        profile_id=profile_id,
        state_root=state_root,
    )


def parse_locked_profile(
    document: bytes,
    *,
    source_profile_root: str,
) -> ProfileContract:
    """Re-parse a locked profile document without reading canonical resources."""

    root = _absolute_lexical_path(
        source_profile_root,
        label="locked source profile directory",
    )
    return _parse_document(
        document,
        root,
        require_live_profile_root=False,
        accepted_schemas=KNOWN_PROFILE_SCHEMAS,
    )


def validate_state_route(
    state_root: str | pathlib.Path,
    profile_id: str,
    *,
    require_existing: bool = True,
) -> tuple[pathlib.Path, str]:
    """Validate the minimum stable route needed to find locked sessions."""

    root = _absolute_normalized_path(
        str(state_root),
        label="model-session state_root",
        must_exist=require_existing,
    )
    identifier = _identifier(profile_id, label="profile_id")
    if _paths_overlap(root, infrastructure_root()):
        _fail(
            "model-session state_root overlaps the dotfiles infrastructure "
            "repository",
            code="profile_inside_infrastructure",
        )
    if require_existing:
        _validate_directory(
            root,
            label="model-session state_root",
            require_current_owner=True,
            reject_group_write=True,
        )
    return root, identifier
