"""Generated Hugging Face snapshot closures for model services.

Resolution is metadata-only.  The authored service selects one immutable
revision and optional checkpoint; this module resolves the runnable file set,
retains exact Hub blob identities and byte counts, and produces the canonical
closure identity consumed by later staging and deployment layers.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import stat
import tempfile
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import ModelLabError
from .huggingface_model import (
    INDEX_CANDIDATES,
    SINGLE_FILE_CANDIDATES,
    validate_repository_path,
)
from .paths import ensure_private_directory
from .service_definition import ServiceDefinition
from .service_huggingface_policy import (
    HuggingFaceSnapshotPolicyError,
    is_huggingface_loader_asset,
    validate_huggingface_nonweight_assets,
)

HUGGINGFACE_CLOSURE_SCHEMA = "model-lab.huggingface-closure.v1"
HUGGINGFACE_CLOSURE_IDENTITY_SCHEMA = "model-lab.huggingface-closure-identity.v1"
MAX_HUGGINGFACE_CLOSURE_BYTES = 16 * 1024 * 1024

_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "source",
        "checkpoint",
        "files",
        "file_count",
        "total_bytes",
        "closure_sha256",
    }
)
_SOURCE_FIELDS = frozenset({"kind", "repository", "revision"})
_CHECKPOINT_FIELDS = frozenset({"requested_selector", "resolved_index", "weight_files"})
_FILE_FIELDS = frozenset({"path", "bytes", "role", "identity"})
_IDENTITY_FIELDS = frozenset({"algorithm", "digest"})
_FILE_ROLES = frozenset({"checkpoint-index", "checkpoint-weight", "snapshot"})
_IDENTITY_LENGTHS = {
    "git-blob-sha1": 40,
    "sha256": 64,
}
_HEX_PATTERN = re.compile(r"^[0-9a-f]+$")
_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"
)
_WEIGHT_SUFFIXES = (
    ".safetensors",
    ".bin",
    ".pt",
    ".pth",
    ".ckpt",
    ".gguf",
    ".h5",
    ".msgpack",
)
_WEIGHT_INDEX_SUFFIXES = (
    ".safetensors.index.json",
    ".bin.index.json",
)
_CHECKPOINT_WEIGHT_SUFFIXES = (".safetensors", ".bin")


class HuggingFaceMetadataClient(Protocol):
    """Metadata-only Hugging Face client surface used by the resolver."""

    def model_info(
        self,
        repository: str,
        revision: str,
    ) -> dict[str, Any]: ...

    def json_file(
        self,
        repository: str,
        resolved_revision: str,
        path: str,
        *,
        optional: bool = False,
    ) -> dict[str, Any] | None: ...

    def file_size(
        self,
        repository: str,
        resolved_revision: str,
        path: str,
    ) -> int: ...


def _fail(message: str) -> None:
    raise ModelLabError(
        message,
        code="invalid_huggingface_closure",
    )


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("ascii")


def _closure_sha256(
    *,
    source: dict[str, Any],
    checkpoint: dict[str, Any],
    files: list[dict[str, Any]],
) -> str:
    identity = {
        "schema_version": HUGGINGFACE_CLOSURE_IDENTITY_SCHEMA,
        "source": source,
        "checkpoint": checkpoint,
        "files": files,
    }
    return hashlib.sha256(_canonical_json_bytes(identity)).hexdigest()


@dataclass(frozen=True)
class HuggingFaceClosureFile:
    """One exact snapshot member admitted into the generated closure."""

    path: str
    bytes: int
    role: str
    identity_algorithm: str
    identity_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "bytes": self.bytes,
            "role": self.role,
            "identity": {
                "algorithm": self.identity_algorithm,
                "digest": self.identity_digest,
            },
        }


@dataclass(frozen=True)
class HuggingFaceClosure:
    """Canonical generated closure for one exact Hugging Face snapshot."""

    repository: str
    revision: str
    requested_selector: str | None
    resolved_index: str | None
    weight_files: tuple[str, ...]
    files: tuple[HuggingFaceClosureFile, ...]

    def _source(self) -> dict[str, Any]:
        return {
            "kind": "huggingface",
            "repository": self.repository,
            "revision": self.revision,
        }

    def _checkpoint(self) -> dict[str, Any]:
        return {
            "requested_selector": self.requested_selector,
            "resolved_index": self.resolved_index,
            "weight_files": list(self.weight_files),
        }

    def _file_documents(self) -> list[dict[str, Any]]:
        return [member.as_dict() for member in self.files]

    @property
    def closure_sha256(self) -> str:
        return _closure_sha256(
            source=self._source(),
            checkpoint=self._checkpoint(),
            files=self._file_documents(),
        )

    @property
    def total_bytes(self) -> int:
        return sum(member.bytes for member in self.files)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": HUGGINGFACE_CLOSURE_SCHEMA,
            "source": self._source(),
            "checkpoint": self._checkpoint(),
            "files": self._file_documents(),
            "file_count": len(self.files),
            "total_bytes": self.total_bytes,
            "closure_sha256": self.closure_sha256,
        }


def _require_exact_fields(
    value: dict[str, Any],
    *,
    label: str,
    fields: frozenset[str],
) -> None:
    unknown = sorted(set(value).difference(fields))
    if unknown:
        _fail(f"{label} has unknown fields: {', '.join(unknown)}")
    missing = sorted(fields.difference(value))
    if missing:
        _fail(f"{label} is missing fields: {', '.join(missing)}")


def _require_table(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    return value


def _require_string(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        _fail(f"{label} must be a non-empty printable string")
    return value


def _require_optional_path(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    path = _require_string(value, label=label)
    try:
        return validate_repository_path(path, label=label)
    except ModelLabError as error:
        raise ModelLabError(
            str(error),
            code="invalid_huggingface_closure",
        ) from error


def _require_nonnegative_integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(f"{label} must be a nonnegative integer")
    return value


def _require_digest(
    value: Any,
    *,
    algorithm: str,
    label: str,
) -> str:
    digest = _require_string(value, label=label)
    expected_length = _IDENTITY_LENGTHS.get(algorithm)
    if (
        expected_length is None
        or len(digest) != expected_length
        or _HEX_PATTERN.fullmatch(digest) is None
    ):
        _fail(f"{label} is not a valid {algorithm} digest")
    return digest


def _is_checkpoint_weight(path: str) -> bool:
    return path.endswith(_CHECKPOINT_WEIGHT_SUFFIXES)


def _is_checkpoint_index(path: str) -> bool:
    return path.endswith(_WEIGHT_INDEX_SUFFIXES)


def _is_root_checkpoint_path(path: str) -> bool:
    return "/" not in path


def parse_huggingface_closure(value: Any) -> HuggingFaceClosure:
    """Validate a generated closure document and verify its canonical digest."""

    manifest = _require_table(value, label="Hugging Face closure")
    _require_exact_fields(
        manifest,
        label="Hugging Face closure",
        fields=_MANIFEST_FIELDS,
    )
    if manifest["schema_version"] != HUGGINGFACE_CLOSURE_SCHEMA:
        _fail("Hugging Face closure has an unsupported schema")

    source = _require_table(
        manifest["source"],
        label="Hugging Face closure source",
    )
    _require_exact_fields(
        source,
        label="Hugging Face closure source",
        fields=_SOURCE_FIELDS,
    )
    if source["kind"] != "huggingface":
        _fail("Hugging Face closure source kind must be huggingface")
    repository = _require_string(
        source["repository"],
        label="Hugging Face closure repository",
    )
    if _REPOSITORY_PATTERN.fullmatch(repository) is None:
        _fail("Hugging Face closure repository is invalid")
    revision = _require_string(
        source["revision"],
        label="Hugging Face closure revision",
    )
    if _REVISION_PATTERN.fullmatch(revision) is None:
        _fail("Hugging Face closure revision is not an exact commit")

    checkpoint = _require_table(
        manifest["checkpoint"],
        label="Hugging Face closure checkpoint",
    )
    _require_exact_fields(
        checkpoint,
        label="Hugging Face closure checkpoint",
        fields=_CHECKPOINT_FIELDS,
    )
    requested_selector = _require_optional_path(
        checkpoint["requested_selector"],
        label="Hugging Face closure requested selector",
    )
    resolved_index = _require_optional_path(
        checkpoint["resolved_index"],
        label="Hugging Face closure resolved index",
    )
    raw_weight_files = checkpoint["weight_files"]
    if not isinstance(raw_weight_files, list) or not raw_weight_files:
        _fail("Hugging Face closure weight_files must be a non-empty array")
    weight_files = tuple(
        _require_optional_path(
            path,
            label="Hugging Face closure weight file",
        )
        for path in raw_weight_files
    )
    if any(path is None for path in weight_files):
        _fail("Hugging Face closure weight file cannot be null")
    normalized_weight_files = tuple(path for path in weight_files if path is not None)
    if normalized_weight_files != tuple(sorted(normalized_weight_files)) or len(
        set(normalized_weight_files)
    ) != len(normalized_weight_files):
        _fail("Hugging Face closure weight_files must be unique and sorted")
    if any(not _is_checkpoint_weight(path) for path in normalized_weight_files):
        _fail("Hugging Face closure has an unsupported checkpoint weight")
    if any(not _is_root_checkpoint_path(path) for path in normalized_weight_files):
        _fail("Hugging Face closure has an unsupported non-root checkpoint weight")
    if resolved_index is not None:
        if not _is_checkpoint_index(resolved_index):
            _fail("Hugging Face closure has an unsupported checkpoint index")
        if not _is_root_checkpoint_path(resolved_index):
            _fail("Hugging Face closure has an unsupported non-root checkpoint index")
    if resolved_index is not None:
        expected_weight_suffix = (
            ".safetensors"
            if resolved_index.endswith(".safetensors.index.json")
            else ".bin"
        )
        if any(
            not path.endswith(expected_weight_suffix)
            for path in normalized_weight_files
        ):
            _fail(
                "Hugging Face closure checkpoint index and weights use "
                "different formats"
            )
    if resolved_index is None and len(normalized_weight_files) != 1:
        _fail("an unindexed Hugging Face closure must have exactly one weight")
    if requested_selector is not None:
        selected_checkpoint = (
            resolved_index if resolved_index is not None else normalized_weight_files[0]
        )
        if requested_selector != selected_checkpoint:
            _fail(
                "Hugging Face closure requested selector does not match "
                "the resolved checkpoint"
            )

    raw_files = manifest["files"]
    if not isinstance(raw_files, list) or not raw_files:
        _fail("Hugging Face closure files must be a non-empty array")
    members: list[HuggingFaceClosureFile] = []
    for position, raw_member in enumerate(raw_files):
        member = _require_table(
            raw_member,
            label=f"Hugging Face closure file {position}",
        )
        _require_exact_fields(
            member,
            label=f"Hugging Face closure file {position}",
            fields=_FILE_FIELDS,
        )
        path = _require_optional_path(
            member["path"],
            label=f"Hugging Face closure file {position} path",
        )
        if path is None:
            _fail("Hugging Face closure file path cannot be null")
        role = _require_string(
            member["role"],
            label=f"Hugging Face closure file {position} role",
        )
        if role not in _FILE_ROLES:
            _fail(f"Hugging Face closure file {position} role is invalid")
        if role == "checkpoint-index" and not _is_checkpoint_index(path):
            _fail(f"Hugging Face closure file {position} index role is invalid")
        if role == "checkpoint-weight" and not _is_checkpoint_weight(path):
            _fail(f"Hugging Face closure file {position} weight role is invalid")
        if role == "snapshot" and _is_weight_artifact(path):
            _fail(f"Hugging Face closure file {position} snapshot role is invalid")
        identity = _require_table(
            member["identity"],
            label=f"Hugging Face closure file {position} identity",
        )
        _require_exact_fields(
            identity,
            label=f"Hugging Face closure file {position} identity",
            fields=_IDENTITY_FIELDS,
        )
        algorithm = _require_string(
            identity["algorithm"],
            label=f"Hugging Face closure file {position} identity algorithm",
        )
        digest = _require_digest(
            identity["digest"],
            algorithm=algorithm,
            label=f"Hugging Face closure file {position} identity digest",
        )
        members.append(
            HuggingFaceClosureFile(
                path=path,
                bytes=_require_nonnegative_integer(
                    member["bytes"],
                    label=f"Hugging Face closure file {position} bytes",
                ),
                role=role,
                identity_algorithm=algorithm,
                identity_digest=digest,
            )
        )
    member_paths = tuple(member.path for member in members)
    if member_paths != tuple(sorted(member_paths)) or len(set(member_paths)) != len(
        member_paths
    ):
        _fail("Hugging Face closure files must have unique sorted paths")
    by_path = {member.path: member for member in members}
    for path in normalized_weight_files:
        member = by_path.get(path)
        if member is None or member.role != "checkpoint-weight":
            _fail("Hugging Face closure checkpoint weight role does not match")
    if {member.path for member in members if member.role == "checkpoint-weight"} != set(
        normalized_weight_files
    ):
        _fail("Hugging Face closure has an undeclared checkpoint weight")
    index_members = [
        member.path for member in members if member.role == "checkpoint-index"
    ]
    if index_members != ([] if resolved_index is None else [resolved_index]):
        _fail("Hugging Face closure checkpoint index role does not match")
    try:
        validate_huggingface_nonweight_assets(
            (member.path, member.bytes)
            for member in members
            if member.role != "checkpoint-weight"
        )
    except HuggingFaceSnapshotPolicyError as error:
        _fail(str(error))

    closure = HuggingFaceClosure(
        repository=repository,
        revision=revision,
        requested_selector=requested_selector,
        resolved_index=resolved_index,
        weight_files=normalized_weight_files,
        files=tuple(members),
    )
    if manifest["file_count"] != len(closure.files):
        _fail("Hugging Face closure file_count does not match")
    if manifest["total_bytes"] != closure.total_bytes:
        _fail("Hugging Face closure total_bytes does not match")
    supplied_digest = _require_digest(
        manifest["closure_sha256"],
        algorithm="sha256",
        label="Hugging Face closure digest",
    )
    if supplied_digest != closure.closure_sha256:
        _fail("Hugging Face closure digest does not match its contents")
    return closure


def _reject_duplicate_json_fields(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"Hugging Face closure repeats JSON field {key!r}")
        result[key] = value
    return result


def load_huggingface_closure(
    path: os.PathLike[str] | str,
) -> HuggingFaceClosure:
    """Read one bounded owned closure without following or reopening a path."""

    try:
        source_path = pathlib.Path(path)
    except TypeError as error:
        raise ModelLabError(
            "Hugging Face closure path is invalid",
            code="unsafe_huggingface_closure",
        ) from error
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source_path, flags)
    except OSError as error:
        raise ModelLabError(
            f"cannot safely open Hugging Face closure {source_path}: {error}",
            code="unsafe_huggingface_closure",
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size < 1
            or opened.st_size > MAX_HUGGINGFACE_CLOSURE_BYTES
            or opened.st_mode & 0o022
            or (hasattr(os, "getuid") and opened.st_uid != os.getuid())
        ):
            raise ModelLabError(
                f"Hugging Face closure has an unsafe identity: {source_path}",
                code="unsafe_huggingface_closure",
            )
        chunks: list[bytes] = []
        observed_bytes = 0
        while observed_bytes <= MAX_HUGGINGFACE_CLOSURE_BYTES:
            chunk = os.read(
                descriptor,
                min(
                    64 * 1024,
                    MAX_HUGGINGFACE_CLOSURE_BYTES + 1 - observed_bytes,
                ),
            )
            if not chunk:
                break
            chunks.append(chunk)
            observed_bytes += len(chunk)
        final = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        observed_bytes != opened.st_size
        or final.st_size != opened.st_size
        or final.st_mtime_ns != opened.st_mtime_ns
        or final.st_ctime_ns != opened.st_ctime_ns
    ):
        raise ModelLabError(
            f"Hugging Face closure changed while reading: {source_path}",
            code="unsafe_huggingface_closure",
        )
    try:
        value = json.loads(
            b"".join(chunks).decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_fields,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ModelLabError(
            f"Hugging Face closure is not valid JSON: {source_path}",
            code="invalid_huggingface_closure",
        ) from error
    return parse_huggingface_closure(value)


def default_huggingface_closure_path(
    root: pathlib.Path,
    closure: HuggingFaceClosure,
) -> pathlib.Path:
    """Return the model-independent content-addressed generated-state path."""

    return root / "closures" / "huggingface" / closure.closure_sha256 / "closure.json"


def write_huggingface_closure(
    path: os.PathLike[str] | str,
    closure: HuggingFaceClosure,
) -> pathlib.Path:
    """Atomically install one canonical closure without replacing an identity."""

    validated = parse_huggingface_closure(closure.as_dict())
    try:
        destination = pathlib.Path(path)
    except TypeError as error:
        raise ModelLabError(
            "Hugging Face closure output path is invalid",
            code="unsafe_huggingface_closure_output",
        ) from error
    ensure_private_directory(destination.parent)
    try:
        destination.lstat()
    except FileNotFoundError:
        pass
    else:
        existing = load_huggingface_closure(destination)
        if existing.as_dict() != validated.as_dict():
            raise ModelLabError(
                f"Hugging Face closure output already has another identity: "
                f"{destination}",
                code="huggingface_closure_output_collision",
            )
        return destination

    payload = _canonical_json_bytes(validated.as_dict())
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary_path = pathlib.Path(temporary_name)
    temporary_exists = True
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(
                temporary_path,
                destination,
                follow_symlinks=False,
            )
        except FileExistsError:
            existing = load_huggingface_closure(destination)
            if existing.as_dict() != validated.as_dict():
                raise ModelLabError(
                    "Hugging Face closure output changed during installation",
                    code="huggingface_closure_output_collision",
                )
            return destination
        temporary_path.unlink()
        temporary_exists = False
        directory_descriptor = os.open(
            destination.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_exists:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    return destination


def _sibling_map(model_info: dict[str, Any]) -> dict[str, dict[str, Any]]:
    siblings = model_info.get("siblings")
    if not isinstance(siblings, list):
        _fail("Hugging Face metadata has no sibling file list")
    result: dict[str, dict[str, Any]] = {}
    for raw_sibling in siblings:
        sibling = _require_table(raw_sibling, label="Hugging Face sibling")
        path = _require_string(
            sibling.get("rfilename"),
            label="Hugging Face sibling path",
        )
        try:
            validate_repository_path(path, label="Hugging Face sibling path")
        except ModelLabError as error:
            raise ModelLabError(
                str(error),
                code="invalid_huggingface_closure",
            ) from error
        if path in result:
            _fail(f"Hugging Face metadata duplicates sibling {path}")
        result[path] = sibling
    return result


def _checkpoint_shards(
    index: dict[str, Any],
    *,
    index_path: str,
    siblings: dict[str, dict[str, Any]],
) -> tuple[str, ...]:
    weight_map = index.get("weight_map")
    if not isinstance(weight_map, dict) or not weight_map:
        _fail(f"checkpoint index {index_path} has no non-empty weight_map")
    index_directory = index_path.rpartition("/")[0]
    expected_weight_suffix = (
        ".safetensors" if index_path.endswith(".safetensors.index.json") else ".bin"
    )
    selected: set[str] = set()
    for tensor_name, shard_name in weight_map.items():
        if (
            not isinstance(tensor_name, str)
            or not tensor_name
            or not isinstance(shard_name, str)
        ):
            _fail(f"checkpoint index {index_path} has a malformed weight_map")
        try:
            validate_repository_path(
                shard_name,
                label="checkpoint shard path",
            )
        except ModelLabError as error:
            raise ModelLabError(
                str(error),
                code="invalid_huggingface_closure",
            ) from error
        candidates = {shard_name}
        if index_directory:
            candidates.add(f"{index_directory}/{shard_name}")
        available = sorted(candidates.intersection(siblings))
        if len(available) != 1:
            _fail(
                f"checkpoint index {index_path} does not resolve {shard_name} uniquely"
            )
        resolved_path = available[0]
        if not _is_root_checkpoint_path(resolved_path):
            _fail(
                f"checkpoint index {index_path} references a non-root shard "
                "that the vLLM root loader cannot load"
            )
        if not resolved_path.endswith(expected_weight_suffix):
            _fail(
                f"checkpoint index {index_path} references a shard from "
                "another checkpoint format"
            )
        selected.add(resolved_path)
    return tuple(sorted(selected))


def _checkpoint_path_admitted(
    definition: ServiceDefinition,
    path: str,
) -> bool:
    if definition.vllm.load_format == "safetensors":
        return path.endswith((".safetensors", ".safetensors.index.json"))
    return path.endswith(
        (
            ".safetensors",
            ".safetensors.index.json",
            ".bin",
            ".bin.index.json",
        )
    )


def _resolve_checkpoint(
    definition: ServiceDefinition,
    *,
    client: HuggingFaceMetadataClient,
    siblings: dict[str, dict[str, Any]],
) -> tuple[str | None, tuple[str, ...]]:
    repository = definition.model.repository
    revision = definition.model.revision
    requested = definition.model.checkpoint
    if requested is not None:
        if not _is_root_checkpoint_path(requested):
            _fail("requested checkpoint must name a root-level file")
        if requested not in siblings:
            _fail(f"requested checkpoint does not exist: {requested}")
        if not _checkpoint_path_admitted(definition, requested):
            _fail("requested checkpoint is incompatible with vLLM load_format")
        if _is_checkpoint_index(requested):
            index = client.json_file(repository, revision, requested)
            if index is None:
                _fail(f"checkpoint index disappeared: {requested}")
            return requested, _checkpoint_shards(
                index,
                index_path=requested,
                siblings=siblings,
            )
        return None, (requested,)

    root_indices = [
        path
        for path in INDEX_CANDIDATES
        if path in siblings and _checkpoint_path_admitted(definition, path)
    ]
    if len(root_indices) > 1:
        _fail("repository has more than one recognized root checkpoint index")
    if len(root_indices) == 1:
        index_path = root_indices[0]
        index = client.json_file(repository, revision, index_path)
        if index is None:
            _fail(f"checkpoint index disappeared: {index_path}")
        return index_path, _checkpoint_shards(
            index,
            index_path=index_path,
            siblings=siblings,
        )

    root_single_files = [
        path
        for path in SINGLE_FILE_CANDIDATES
        if path in siblings and _checkpoint_path_admitted(definition, path)
    ]
    if len(root_single_files) > 1:
        _fail("repository has more than one recognized root checkpoint")
    if len(root_single_files) == 1:
        return None, (root_single_files[0],)

    root_weight_files = sorted(
        path
        for path in siblings
        if "/" not in path
        and _is_checkpoint_weight(path)
        and _checkpoint_path_admitted(definition, path)
    )
    if len(root_weight_files) != 1:
        _fail("repository has no unambiguous root checkpoint")
    return None, (root_weight_files[0],)


def _is_weight_artifact(path: str) -> bool:
    return path.endswith(_WEIGHT_SUFFIXES) or path.endswith(_WEIGHT_INDEX_SUFFIXES)


def _sibling_size(
    sibling: dict[str, Any],
    *,
    client: HuggingFaceMetadataClient,
    repository: str,
    revision: str,
    path: str,
) -> int:
    raw_size = sibling.get("size")
    direct_size = (
        raw_size
        if isinstance(raw_size, int)
        and not isinstance(raw_size, bool)
        and raw_size >= 0
        else None
    )
    lfs = sibling.get("lfs")
    raw_lfs_size = lfs.get("size") if isinstance(lfs, dict) else None
    lfs_size = (
        raw_lfs_size
        if isinstance(raw_lfs_size, int)
        and not isinstance(raw_lfs_size, bool)
        and raw_lfs_size >= 0
        else None
    )
    if direct_size is not None and lfs_size is not None and direct_size != lfs_size:
        _fail(f"Hugging Face reports conflicting sizes for {path}")
    if direct_size is not None:
        return direct_size
    if lfs_size is not None:
        return lfs_size
    size = client.file_size(repository, revision, path)
    return _require_nonnegative_integer(
        size,
        label=f"Hugging Face file size for {path}",
    )


def _sibling_identity(
    sibling: dict[str, Any],
    *,
    path: str,
) -> tuple[str, str]:
    lfs = sibling.get("lfs")
    if isinstance(lfs, dict):
        return (
            "sha256",
            _require_digest(
                lfs.get("sha256"),
                algorithm="sha256",
                label=f"Hugging Face LFS identity for {path}",
            ),
        )
    if lfs is not None:
        _fail(f"Hugging Face LFS metadata for {path} is malformed")
    return (
        "git-blob-sha1",
        _require_digest(
            sibling.get("blobId"),
            algorithm="git-blob-sha1",
            label=f"Hugging Face blob identity for {path}",
        ),
    )


def resolve_huggingface_closure(
    definition: ServiceDefinition,
    *,
    client: HuggingFaceMetadataClient,
) -> HuggingFaceClosure:
    """Resolve one immutable service source into a generated file closure."""

    repository = definition.model.repository
    authored_revision = definition.model.revision
    model_info = _require_table(
        client.model_info(repository, authored_revision),
        label="Hugging Face model metadata",
    )
    resolved_revision = model_info.get("sha")
    if resolved_revision != authored_revision:
        raise ModelLabError(
            "Hugging Face did not resolve the authored exact revision identically",
            code="huggingface_revision_mismatch",
        )
    siblings = _sibling_map(model_info)
    resolved_index, weight_files = _resolve_checkpoint(
        definition,
        client=client,
        siblings=siblings,
    )
    selected_weights = set(weight_files)
    members: list[HuggingFaceClosureFile] = []
    for path in sorted(siblings):
        if path == resolved_index:
            role = "checkpoint-index"
        elif path in selected_weights:
            role = "checkpoint-weight"
        elif _is_weight_artifact(path):
            continue
        elif not is_huggingface_loader_asset(path):
            continue
        else:
            role = "snapshot"
        sibling = siblings[path]
        algorithm, digest = _sibling_identity(sibling, path=path)
        members.append(
            HuggingFaceClosureFile(
                path=path,
                bytes=_sibling_size(
                    sibling,
                    client=client,
                    repository=repository,
                    revision=authored_revision,
                    path=path,
                ),
                role=role,
                identity_algorithm=algorithm,
                identity_digest=digest,
            )
        )
    closure = HuggingFaceClosure(
        repository=repository,
        revision=authored_revision,
        requested_selector=definition.model.checkpoint,
        resolved_index=resolved_index,
        weight_files=weight_files,
        files=tuple(members),
    )
    return parse_huggingface_closure(closure.as_dict())
