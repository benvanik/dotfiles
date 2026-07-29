"""Model-owned deterministic archives for persistent vLLM compiled caches."""

from __future__ import annotations

import hashlib
import os
import pathlib
import stat
import tarfile
from dataclasses import dataclass
from typing import Any, BinaryIO, Literal

from model_lab.errors import ModelLabError

from .compile_cache_document import (
    ACCEPTED_ENTRIES,
    ACCEPTED_NAME,
    AUTHORED_NAME,
    BUNDLE_NAME,
    CANDIDATE_ENTRIES,
    COMPILE_CACHE_ACCEPTANCE_SCHEMA,
    COMPILE_CACHE_AUTHOR_CANDIDATE_SCHEMA,
    COMPILE_CACHE_BUNDLE_MANIFEST_SCHEMA,
    MANIFEST_NAME,
    artifact_records,
    candidate_startup_proof,
    localized_paths,
    validate_contract,
    validate_descriptor,
    validate_measurement_document,
)
from .compile_cache_files import (
    COPY_BUFFER_BYTES,
    MAX_MEASUREMENT_BYTES,
    fail,
    file_mode,
    hash_regular_file,
    is_sha256,
    mkdir_exclusive,
    read_exact_json,
    require_owned_untrusted_directory,
    safe_relative,
    sha256_bytes,
    sha256_document,
    validate_inventory,
    write_all,
)
from .layout import RuntimeLayout


@dataclass(frozen=True)
class PersistentCompileCache:
    """One completely verified candidate or accepted sequential bundle."""

    state: Literal["candidate", "accepted"]
    contract: dict[str, Any]
    root: pathlib.Path
    manifest: dict[str, Any]
    manifest_payload: bytes
    authored: dict[str, Any]
    authored_payload: bytes
    acceptance: dict[str, Any] | None
    acceptance_payload: bytes | None
    inventory: dict[str, Any]
    bundle: dict[str, Any]


def _directory_tar_member(record: dict[str, Any]) -> tarfile.TarInfo:
    member = tarfile.TarInfo(record["path"])
    member.type = tarfile.DIRTYPE
    member.mode = record["mode"]
    member.uid = 0
    member.gid = 0
    member.mtime = 0
    member.uname = ""
    member.gname = ""
    return member


def _file_tar_member(record: dict[str, Any]) -> tarfile.TarInfo:
    member = tarfile.TarInfo(record["path"])
    member.size = record["bytes"]
    member.mode = record["mode"]
    member.uid = 0
    member.gid = 0
    member.mtime = 0
    member.uname = ""
    member.gname = ""
    return member


def deterministic_bundle_size(inventory: dict[str, Any]) -> int:
    """Return the exact GNU streaming-tar bytes without reading file payloads."""

    validated = validate_inventory(inventory)
    offset = 0
    for record in validated["directories"]:
        offset += len(
            _directory_tar_member(record).tobuf(
                format=tarfile.GNU_FORMAT,
                encoding=tarfile.ENCODING,
                errors="surrogateescape",
            )
        )
    for record in validated["files"]:
        offset += len(
            _file_tar_member(record).tobuf(
                format=tarfile.GNU_FORMAT,
                encoding=tarfile.ENCODING,
                errors="surrogateescape",
            )
        )
        offset += (
            (record["bytes"] + tarfile.BLOCKSIZE - 1)
            // tarfile.BLOCKSIZE
            * tarfile.BLOCKSIZE
        )
    offset += 2 * tarfile.BLOCKSIZE
    return (offset + tarfile.RECORDSIZE - 1) // tarfile.RECORDSIZE * tarfile.RECORDSIZE


def write_bundle(
    *,
    path: pathlib.Path,
    root: pathlib.Path,
    inventory: dict[str, Any],
) -> dict[str, Any]:
    """Write or resume one deterministic uncompressed sequential archive."""

    existed = os.path.lexists(path)
    if existed:
        path_stat = path.lstat()
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or path_stat.st_uid != os.getuid()
            or path_stat.st_nlink != 1
            or file_mode(path_stat) not in {0o444, 0o600}
        ):
            fail("resumable compiled-cache archive has an unsafe identity")
        access = os.O_RDONLY if file_mode(path_stat) == 0o444 else os.O_RDWR
        descriptor_value = os.open(
            path,
            access | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    else:
        descriptor_value = os.open(
            path,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        path_stat = os.fstat(descriptor_value)
    position = 0
    digest = hashlib.sha256()

    class ResumableWriter:
        def write(self, payload: bytes) -> int:
            nonlocal position
            view = memoryview(payload)
            try:
                overlap = min(len(view), max(0, path_stat.st_size - position))
                if overlap:
                    existing = os.pread(descriptor_value, overlap, position)
                    if len(existing) != overlap or existing != view[:overlap]:
                        fail("interrupted compiled-cache archive prefix changed")
                tail = view[overlap:]
                if tail and file_mode(path_stat) == 0o444:
                    fail("completed compiled-cache archive is truncated")
                tail_position = 0
                while tail_position < len(tail):
                    written = os.pwrite(
                        descriptor_value,
                        tail[tail_position:],
                        position + overlap + tail_position,
                    )
                    if written <= 0:
                        fail("resumed compiled-cache archive write made no progress")
                    tail_position += written
                digest.update(view)
                position += len(view)
                return len(view)
            finally:
                view.release()

        def tell(self) -> int:
            return position

        def flush(self) -> None:
            return None

    writer = ResumableWriter()
    try:
        with tarfile.open(
            fileobj=writer,
            mode="w|",
            format=tarfile.GNU_FORMAT,
        ) as archive:
            for record in inventory["directories"]:
                archive.addfile(_directory_tar_member(record))
            for record in inventory["files"]:
                relative = safe_relative(
                    record["path"],
                    label="compiled-cache archive source",
                )
                source_path = root.joinpath(*relative.parts)
                source_digest, source_stat = hash_regular_file(source_path)
                if (
                    source_digest != record["sha256"]
                    or source_stat.st_size != record["bytes"]
                    or file_mode(source_stat) != record["mode"]
                ):
                    fail(
                        f"compiled-cache tree changed before archive: {record['path']}"
                    )
                member = _file_tar_member(record)
                source_descriptor = os.open(
                    source_path,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    with os.fdopen(
                        source_descriptor,
                        "rb",
                        closefd=False,
                    ) as source:
                        archive.addfile(member, source)
                finally:
                    os.close(source_descriptor)
        final = os.fstat(descriptor_value)
        if final.st_size != position:
            fail("interrupted compiled-cache archive has an unexpected tail")
        if file_mode(final) != 0o444:
            os.fchmod(descriptor_value, 0o444)
            os.fsync(descriptor_value)
    finally:
        os.close(descriptor_value)
    return {
        "name": path.name,
        "bytes": position,
        "sha256": digest.hexdigest(),
    }


def _validate_manifest(
    *,
    manifest: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    if (
        set(manifest)
        != {
            "schema_version",
            "contract",
            "archive",
            "inventory",
            "author_measurement",
        }
        or manifest["schema_version"] != COMPILE_CACHE_BUNDLE_MANIFEST_SCHEMA
        or manifest["contract"] != contract
        or manifest["archive"]
        != {
            "name": BUNDLE_NAME,
            "format": "gnu-tar-uncompressed",
            "member_root": contract["local_root"],
        }
    ):
        fail("persistent compiled-cache manifest is malformed or mismatched")
    inventory = validate_inventory(manifest["inventory"])
    measurement = manifest["author_measurement"]
    if (
        not isinstance(measurement, dict)
        or set(measurement) != {"sha256", "document"}
        or not is_sha256(measurement["sha256"])
        or sha256_document(measurement["document"]) != measurement["sha256"]
    ):
        fail("persistent compiled-cache author measurement is malformed")
    validate_measurement_document(
        measurement["document"],
        contract=contract,
        mode="author",
    )
    return inventory


def _validate_authored(
    *,
    authored: dict[str, Any],
    authored_payload: bytes,
    manifest: dict[str, Any],
    manifest_payload: bytes,
    contract: dict[str, Any],
    inventory: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    measurement = manifest["author_measurement"]
    manifest_descriptor = validate_descriptor(
        authored.get("manifest"),
        expected_name=MANIFEST_NAME,
    )
    bundle_descriptor = validate_descriptor(
        authored.get("bundle"),
        expected_name=BUNDLE_NAME,
    )
    if (
        set(authored)
        != {
            "schema_version",
            "state",
            "cache_id",
            "contract",
            "author_boot_id",
            "service_manifest_sha256",
            "author_measurement_sha256",
            "manifest",
            "bundle",
            "inventory_sha256",
            "produced_artifacts",
        }
        or authored["schema_version"] != COMPILE_CACHE_AUTHOR_CANDIDATE_SCHEMA
        or authored["state"] != "candidate"
        or authored["cache_id"] != contract["cache_id"]
        or authored["contract"] != contract
        or authored["author_boot_id"] != measurement["document"]["boot_id"]
        or authored["service_manifest_sha256"]
        != measurement["document"]["service_manifest_sha256"]
        or authored["author_measurement_sha256"] != measurement["sha256"]
        or authored["inventory_sha256"] != inventory["sha256"]
        or manifest_descriptor["bytes"] != len(manifest_payload)
        or manifest_descriptor["sha256"] != sha256_bytes(manifest_payload)
    ):
        fail("persistent compiled-cache author receipt is malformed or mismatched")
    expected_produced = artifact_records(
        inventory,
        measurement["document"]["cache_evidence"]["produced_artifacts"],
    )
    if authored["produced_artifacts"] != expected_produced:
        fail("persistent compiled-cache produced-artifact evidence changed")
    if sha256_bytes(authored_payload) == "0" * 64:
        fail("persistent compiled-cache author receipt has an impossible digest")
    return manifest_descriptor, bundle_descriptor


def _validate_acceptance(
    *,
    acceptance: dict[str, Any],
    contract: dict[str, Any],
    authored: dict[str, Any],
    authored_payload: bytes,
    author_measurement: dict[str, Any],
    manifest_descriptor: dict[str, Any],
    bundle_descriptor: dict[str, Any],
    inventory: dict[str, Any],
) -> None:
    if (
        set(acceptance)
        != {
            "schema_version",
            "state",
            "cache_id",
            "contract",
            "author_boot_id",
            "require_boot_id",
            "manifest",
            "bundle",
            "authored_sha256",
            "inventory_sha256",
            "require_measurement",
            "startup_proof",
            "loaded_artifacts",
        }
        or acceptance["schema_version"] != COMPILE_CACHE_ACCEPTANCE_SCHEMA
        or acceptance["state"] != "accepted"
        or acceptance["cache_id"] != contract["cache_id"]
        or acceptance["contract"] != contract
        or acceptance["author_boot_id"] != authored["author_boot_id"]
        or acceptance["require_boot_id"] == authored["author_boot_id"]
        or acceptance["manifest"] != manifest_descriptor
        or acceptance["bundle"] != bundle_descriptor
        or acceptance["authored_sha256"] != sha256_bytes(authored_payload)
        or acceptance["inventory_sha256"] != inventory["sha256"]
        or acceptance["loaded_artifacts"] != authored["produced_artifacts"]
    ):
        fail("persistent compiled-cache acceptance is malformed or mismatched")
    measurement = acceptance["require_measurement"]
    if (
        not isinstance(measurement, dict)
        or set(measurement) != {"sha256", "document"}
        or not is_sha256(measurement["sha256"])
        or sha256_document(measurement["document"]) != measurement["sha256"]
    ):
        fail("persistent compiled-cache require measurement is malformed")
    validate_measurement_document(
        measurement["document"],
        contract=contract,
        mode="candidate-proof",
    )
    expected_startup_proof = candidate_startup_proof(
        author_measurement=author_measurement,
        candidate_measurement=measurement["document"],
    )
    if (
        measurement["document"]["boot_id"] != acceptance["require_boot_id"]
        or acceptance["startup_proof"] != expected_startup_proof
        or artifact_records(
            inventory,
            measurement["document"]["cache_evidence"]["loaded_artifacts"],
        )
        != authored["produced_artifacts"]
    ):
        fail("persistent compiled-cache require proof does not match")


def load_persistent_compile_cache(
    *,
    contract: dict[str, Any],
    layout: RuntimeLayout,
    require_accepted: bool,
    verify_bundle_content: bool = True,
) -> PersistentCompileCache:
    """Verify metadata and optionally stream-hash the sequential archive."""

    validated = validate_contract(contract)
    root = localized_paths(contract=validated, layout=layout)["persistent_root"]
    root_stat = require_owned_untrusted_directory(root)
    try:
        entries = {entry.name for entry in root.iterdir()}
    except OSError as error:
        raise ModelLabError(
            "cannot enumerate persistent compiled-cache generation",
            code="compile_cache_operation_failed",
        ) from error
    if entries == CANDIDATE_ENTRIES:
        state: Literal["candidate", "accepted"] = "candidate"
    elif entries == ACCEPTED_ENTRIES:
        state = "accepted"
    else:
        fail("persistent compiled-cache generation has unexpected entries")
    if require_accepted and state != "accepted":
        fail(
            "compiled-cache candidate has not passed a distinct-boot require-mode proof"
        )
    manifest, manifest_payload = read_exact_json(
        root / MANIFEST_NAME,
        mode=0o444,
    )
    inventory = _validate_manifest(
        manifest=manifest,
        contract=validated,
    )
    authored, authored_payload = read_exact_json(
        root / AUTHORED_NAME,
        mode=0o444,
        maximum_bytes=MAX_MEASUREMENT_BYTES,
    )
    manifest_descriptor, bundle_descriptor = _validate_authored(
        authored=authored,
        authored_payload=authored_payload,
        manifest=manifest,
        manifest_payload=manifest_payload,
        contract=validated,
        inventory=inventory,
    )
    bundle_path = root / BUNDLE_NAME
    if verify_bundle_content:
        bundle_digest, bundle_stat = hash_regular_file(bundle_path)
    else:
        try:
            bundle_stat = bundle_path.lstat()
        except OSError as error:
            raise ModelLabError(
                "persistent compiled-cache archive is absent",
                code="compile_cache_operation_failed",
            ) from error
        if (
            not stat.S_ISREG(bundle_stat.st_mode)
            or stat.S_ISLNK(bundle_stat.st_mode)
            or bundle_stat.st_uid != os.getuid()
            or bundle_stat.st_nlink != 1
        ):
            fail("persistent compiled-cache archive has an unsafe identity")
        bundle_digest = bundle_descriptor["sha256"]
    if (
        file_mode(bundle_stat) != 0o444
        or bundle_descriptor["bytes"] != bundle_stat.st_size
        or bundle_descriptor["sha256"] != bundle_digest
    ):
        fail("persistent compiled-cache archive changed")
    acceptance: dict[str, Any] | None = None
    acceptance_payload: bytes | None = None
    if state == "accepted":
        acceptance, acceptance_payload = read_exact_json(
            root / ACCEPTED_NAME,
            mode=0o444,
            maximum_bytes=MAX_MEASUREMENT_BYTES,
        )
        _validate_acceptance(
            acceptance=acceptance,
            contract=validated,
            authored=authored,
            authored_payload=authored_payload,
            author_measurement=manifest["author_measurement"]["document"],
            manifest_descriptor=manifest_descriptor,
            bundle_descriptor=bundle_descriptor,
            inventory=inventory,
        )
    final_root_stat = require_owned_untrusted_directory(root)
    if (
        final_root_stat.st_dev != root_stat.st_dev
        or final_root_stat.st_ino != root_stat.st_ino
    ):
        fail("persistent compiled-cache root changed while loading")
    return PersistentCompileCache(
        state=state,
        contract=validated,
        root=root,
        manifest=manifest,
        manifest_payload=manifest_payload,
        authored=authored,
        authored_payload=authored_payload,
        acceptance=acceptance,
        acceptance_payload=acceptance_payload,
        inventory=inventory,
        bundle=bundle_descriptor,
    )


class _HashingReader:
    def __init__(self, source: BinaryIO) -> None:
        self.source = source
        self.hasher = hashlib.sha256()
        self.bytes = 0

    def read(self, size: int = -1) -> bytes:
        value = self.source.read(size)
        self.hasher.update(value)
        self.bytes += len(value)
        return value


def extract_bundle(
    *,
    generation: PersistentCompileCache,
    destination: pathlib.Path,
) -> None:
    """Stream, constrain, and hash one persistent archive into local storage."""

    expected_directories = {
        record["path"]: record for record in generation.inventory["directories"]
    }
    expected_files = {
        record["path"]: record for record in generation.inventory["files"]
    }
    descriptor_value = os.open(
        generation.root / BUNDLE_NAME,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    seen_directories: set[str] = set()
    seen_files: set[str] = set()
    try:
        with os.fdopen(descriptor_value, "rb", closefd=False) as raw_source:
            source = _HashingReader(raw_source)
            with tarfile.open(fileobj=source, mode="r|") as archive:
                for member in archive:
                    relative = safe_relative(
                        member.name,
                        label="compiled-cache archive member",
                    ).as_posix()
                    if member.isdir():
                        record = expected_directories.get(relative)
                        if (
                            record is None
                            or relative in seen_directories
                            or member.mode != record["mode"]
                        ):
                            fail(
                                "compiled-cache archive has an unexpected "
                                f"directory: {relative}"
                            )
                        path = destination.joinpath(
                            *pathlib.PurePosixPath(relative).parts
                        )
                        mkdir_exclusive(path, mode=record["mode"])
                        seen_directories.add(relative)
                        continue
                    if not member.isfile():
                        fail(
                            f"compiled-cache archive has a forbidden member: {relative}"
                        )
                    record = expected_files.get(relative)
                    if (
                        record is None
                        or relative in seen_files
                        or member.size != record["bytes"]
                        or member.mode != record["mode"]
                    ):
                        fail(
                            f"compiled-cache archive has an unexpected file: {relative}"
                        )
                    source_file = archive.extractfile(member)
                    if source_file is None:
                        fail(f"compiled-cache archive file is unreadable: {relative}")
                    output_path = destination.joinpath(
                        *pathlib.PurePosixPath(relative).parts
                    )
                    output_descriptor = os.open(
                        output_path,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                    )
                    file_hasher = hashlib.sha256()
                    observed_bytes = 0
                    try:
                        while True:
                            chunk = source_file.read(COPY_BUFFER_BYTES)
                            if not chunk:
                                break
                            file_hasher.update(chunk)
                            write_all(output_descriptor, chunk)
                            observed_bytes += len(chunk)
                        os.fchmod(output_descriptor, record["mode"])
                    finally:
                        os.close(output_descriptor)
                    if (
                        observed_bytes != record["bytes"]
                        or file_hasher.hexdigest() != record["sha256"]
                    ):
                        fail(f"compiled-cache archive file content changed: {relative}")
                    seen_files.add(relative)
            while source.read(COPY_BUFFER_BYTES):
                pass
            if (
                source.bytes != generation.bundle["bytes"]
                or source.hasher.hexdigest() != generation.bundle["sha256"]
            ):
                fail("persistent compiled-cache archive changed while staging")
    except tarfile.TarError as error:
        raise ModelLabError(
            "persistent compiled-cache archive is malformed",
            code="compile_cache_operation_failed",
        ) from error
    finally:
        os.close(descriptor_value)
    if seen_directories != set(expected_directories) or seen_files != set(
        expected_files
    ):
        fail("compiled-cache archive closure is incomplete")
