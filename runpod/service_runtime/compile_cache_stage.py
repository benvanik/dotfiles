"""Public author, proof, acceptance, and staging lifecycle for vLLM caches."""

from __future__ import annotations

import os
import pathlib
import stat
from dataclasses import dataclass
from typing import Any, Literal

from runpod_local.errors import RunpodLocalError

from .collaborators import verify_compile_cache_stage
from .compile_cache_archive import (
    PersistentCompileCache,
    deterministic_bundle_size,
    extract_bundle,
    load_persistent_compile_cache,
    write_bundle,
)
from .compile_cache_document import (
    ACCEPTED_NAME,
    AUTHORED_NAME,
    BUNDLE_NAME,
    COMPILE_CACHE_ACCEPTANCE_SCHEMA,
    COMPILE_CACHE_AUTHOR_CANDIDATE_SCHEMA,
    COMPILE_CACHE_BUNDLE_MANIFEST_SCHEMA,
    COMPILE_CACHE_LAUNCH_MEASUREMENT_SCHEMA,
    COMPILE_CACHE_PREPARATION_SCHEMA,
    COMPILE_CACHE_PREREQUISITE_SUMMARY_SCHEMA,
    COMPILE_CACHE_STAGE_SOURCE_SCHEMA,
    COMPILE_CACHE_STAGE_SCHEMA,
    MANIFEST_NAME,
    VLLM_CACHE_EVIDENCE_SCHEMA,
    CompileCacheMode,
    artifact_records,
    candidate_startup_proof,
    load_preparation,
    load_measurement,
    localized_paths,
    validate_contract,
    validate_descriptor,
)
from .compile_cache_files import (
    AUTHOR_PUBLICATION_RESERVE_BYTES,
    COMPILE_CACHE_SUBDIRECTORIES,
    EMPTY_CACHE_GROWTH_RESERVE_BYTES,
    MAX_MEASUREMENT_BYTES,
    STAGED_CACHE_GROWTH_RESERVE_BYTES,
    canonical_bytes,
    directory_flags,
    directory_stat,
    ensure_private_parents,
    ensure_untrusted_parents,
    fail,
    fsync_directory,
    inventory_compile_cache,
    mkdir_exclusive,
    mkdir_untrusted_exclusive,
    open_owned_untrusted_directory,
    preflight_compile_cache_headroom,
    probe_directory_noreplace,
    publish_directory_noreplace,
    publish_file_noreplace,
    read_exact_json,
    require_directory,
    require_owned_untrusted_directory,
    sha256_bytes,
    write_json_exclusive,
    write_json_resumable,
)
from .layout import RuntimeLayout
from .state import open_advisory_lock, read_private_json


_VOLUME_PUBLICATION_LOCK_NAME = ".runpod-compile-cache-publication.lock"


def _volume_publication_lock(layout: RuntimeLayout) -> pathlib.Path:
    """Return the one Pod-writer lease covering persistent cache headroom."""

    return layout.workspace_root / _VOLUME_PUBLICATION_LOCK_NAME


def _candidate_staging_path(
    *,
    persistent_root: pathlib.Path,
    cache_id: str,
) -> pathlib.Path:
    return persistent_root.parent / f".{cache_id}.candidate-staging"


def _candidate_publication_lock(
    *,
    persistent_root: pathlib.Path,
    cache_id: str,
) -> pathlib.Path:
    return persistent_root.parent / f".{cache_id}.publication.lock"


def _acceptance_staging_path(
    *,
    persistent_root: pathlib.Path,
    cache_id: str,
) -> pathlib.Path:
    return persistent_root.parent / f".{cache_id}.acceptance-staging"


def _recover_acceptance_transition(
    *,
    persistent_root: pathlib.Path,
    staging_path: pathlib.Path,
) -> None:
    """Recognize an entry-complete acceptance without trusting volume modes."""

    require_owned_untrusted_directory(persistent_root)
    try:
        entries = {entry.name for entry in persistent_root.iterdir()}
    except OSError as error:
        raise RunpodLocalError(
            "cannot inspect interrupted cache acceptance",
            code="compile_cache_operation_failed",
        ) from error
    candidate_entries = {BUNDLE_NAME, MANIFEST_NAME, AUTHORED_NAME}
    accepted_entries = {*candidate_entries, ACCEPTED_NAME}
    if frozenset(entries) not in {
        frozenset(candidate_entries),
        frozenset(accepted_entries),
    }:
        fail("compiled-cache acceptance transition has unexpected entries")
    if entries == accepted_entries and os.path.lexists(staging_path):
        fail("accepted cache conflicts with an interrupted acceptance document")


def _acceptance_result(
    *,
    action: str,
    contract: dict[str, Any],
    generation: PersistentCompileCache,
    acceptance_sha256: str,
    require_boot_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": COMPILE_CACHE_ACCEPTANCE_SCHEMA,
        "action": action,
        "state": "accepted",
        "cache_id": contract["cache_id"],
        "persistent_root": contract["persistent_root"],
        "author_boot_id": generation.authored["author_boot_id"],
        "require_boot_id": require_boot_id,
        "acceptance_sha256": acceptance_sha256,
        "manifest_sha256": generation.authored["manifest"]["sha256"],
        "bundle_sha256": generation.authored["bundle"]["sha256"],
        "inventory_sha256": generation.inventory["sha256"],
        "file_count": generation.inventory["file_count"],
        "total_bytes": generation.inventory["total_bytes"],
        "startup_proof": (
            None
            if generation.acceptance is None
            else generation.acceptance["startup_proof"]
        ),
    }


def _recover_candidate_staging(
    path: pathlib.Path,
    *,
    expected_file_bytes: dict[str, int] | None = None,
) -> dict[str, int]:
    """Validate and reopen one exact resumable candidate publication."""

    if not os.path.lexists(path):
        return {}
    allowed_names = {BUNDLE_NAME, MANIFEST_NAME, AUTHORED_NAME}
    if expected_file_bytes is not None and (
        set(expected_file_bytes) != allowed_names
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in expected_file_bytes.values()
        )
    ):
        fail("candidate staging byte bounds are malformed")
    observed_file_bytes: dict[str, int] = {}
    root_stat = require_owned_untrusted_directory(path)
    parent_descriptor = open_owned_untrusted_directory(path.parent)
    try:
        root_descriptor = os.open(
            path.name,
            directory_flags(),
            dir_fd=parent_descriptor,
        )
        try:
            opened_root = os.fstat(root_descriptor)
            if (
                opened_root.st_dev != root_stat.st_dev
                or opened_root.st_ino != root_stat.st_ino
                or not stat.S_ISDIR(opened_root.st_mode)
                or opened_root.st_uid != os.getuid()
            ):
                fail("interrupted candidate staging root changed while opening")
            entries = list(os.scandir(root_descriptor))
            names = {entry.name for entry in entries}
            if len(entries) > 3 or not names.issubset(allowed_names):
                fail("interrupted candidate staging root has unexpected entries")
            for entry in sorted(entries, key=lambda item: item.name):
                descriptor = os.open(
                    entry.name,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=root_descriptor,
                )
                try:
                    opened = os.fstat(descriptor)
                    current = os.stat(
                        entry.name,
                        dir_fd=root_descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or opened.st_uid != os.getuid()
                        or opened.st_nlink != 1
                        or stat.S_IMODE(opened.st_mode) not in {0o444, 0o600}
                        or current.st_dev != opened.st_dev
                        or current.st_ino != opened.st_ino
                    ):
                        fail("interrupted candidate artifact has an unsafe identity")
                    if (
                        expected_file_bytes is not None
                        and opened.st_size > expected_file_bytes[entry.name]
                    ):
                        fail(
                            "interrupted candidate artifact exceeds its "
                            "predicted byte bound"
                        )
                    observed_file_bytes[entry.name] = opened.st_size
                finally:
                    os.close(descriptor)
        finally:
            os.close(root_descriptor)
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)
    return observed_file_bytes


def _quarantine_candidate_staging(
    *,
    path: pathlib.Path,
    canonical_persistent_root: pathlib.PurePosixPath,
) -> str | None:
    """Move an interrupted generation aside without deleting volume data."""

    if not os.path.lexists(path):
        return None
    _recover_candidate_staging(path)
    value = path.lstat()
    identity = (
        f"{value.st_dev}:{value.st_ino}:{value.st_ctime_ns}:{value.st_mtime_ns}"
    ).encode("ascii")
    token = sha256_bytes(identity)[:24]
    quarantine = path.parent / f"{path.name}.interrupted-{token}"
    fsync_directory(path)
    publish_directory_noreplace(
        source=path,
        destination=quarantine,
    )
    canonical = canonical_persistent_root.parent / quarantine.name
    return str(canonical)


def _empty_local_root(
    *,
    path: pathlib.Path,
    anchor: pathlib.Path,
) -> dict[str, Any]:
    ensure_private_parents(anchor=anchor, parent=path.parent)
    mkdir_exclusive(path)
    for name in COMPILE_CACHE_SUBDIRECTORIES:
        mkdir_exclusive(path / name)
    return inventory_compile_cache(path)


def prepare_compile_cache(
    *,
    contract: dict[str, Any],
    layout: RuntimeLayout,
    boot_id: str,
    mode: Literal["ephemeral", "author"],
) -> dict[str, Any]:
    """Prepare a new empty mutable cache tree without persistent publication."""

    validated = validate_contract(contract)
    if not isinstance(boot_id, str) or not boot_id:
        fail("cache preparation requires an exact boot identity")
    if mode not in {"ephemeral", "author"}:
        fail("empty cache preparation mode is unsupported")
    paths = localized_paths(contract=validated, layout=layout)
    if any(
        os.path.lexists(paths[name])
        for name in ("local_root", "stage_receipt", "prerequisite")
    ):
        fail("cache preparation refuses existing cache state")
    headroom = preflight_compile_cache_headroom(
        filesystem_root=layout.session_root,
        purpose=f"empty-{mode}-compile-cache",
        archive_bytes=0,
        inventory_bytes=0,
        reserve_name="bounded-compile-cache-growth",
        reserve_bytes=EMPTY_CACHE_GROWTH_RESERVE_BYTES,
    )
    quarantined_candidate: str | None = None
    if mode == "author":
        ensure_untrusted_parents(
            anchor=layout.workspace_root,
            parent=paths["persistent_root"].parent,
        )
        staging_root = _candidate_staging_path(
            persistent_root=paths["persistent_root"],
            cache_id=validated["cache_id"],
        )
        acceptance_staging = _acceptance_staging_path(
            persistent_root=paths["persistent_root"],
            cache_id=validated["cache_id"],
        )
        publication_lock = _candidate_publication_lock(
            persistent_root=paths["persistent_root"],
            cache_id=validated["cache_id"],
        )
        with open_advisory_lock(publication_lock, create=True) as lock:
            lock.exclusive()
            if os.path.lexists(paths["persistent_root"]):
                fail("author preparation refuses an existing persistent generation")
            if os.path.lexists(acceptance_staging):
                fail("author preparation found an interrupted acceptance proof")
            quarantined_candidate = _quarantine_candidate_staging(
                path=staging_root,
                canonical_persistent_root=pathlib.PurePosixPath(
                    validated["persistent_root"]
                ),
            )
            probe_directory_noreplace(
                parent=paths["persistent_root"].parent,
                cache_id=validated["cache_id"],
            )
    inventory = _empty_local_root(
        path=paths["local_root"],
        anchor=layout.session_root,
    )
    if inventory["file_count"] != 0 or inventory["total_bytes"] != 0:
        fail("new author cache root is not empty")
    root_stat = require_directory(paths["local_root"], exact_mode=0o700)
    receipt = {
        "schema_version": COMPILE_CACHE_PREPARATION_SCHEMA,
        "mode": mode,
        "cache_id": validated["cache_id"],
        "contract": validated,
        "boot_id": boot_id,
        "local_root": validated["local_root"],
        "directory_stat": directory_stat(root_stat),
        "inventory_sha256": inventory["sha256"],
        "file_count": 0,
        "total_bytes": 0,
    }
    payload, receipt_sha256 = write_json_exclusive(
        paths["prerequisite"],
        receipt,
        mode=0o600,
        durable=False,
    )
    return {
        "schema_version": COMPILE_CACHE_PREPARATION_SCHEMA,
        "action": f"prepare-{mode}",
        "cache_id": validated["cache_id"],
        "local_root": validated["local_root"],
        "receipt_sha256": receipt_sha256,
        "receipt_bytes": len(payload),
        "state": f"mutable-empty-{mode}-root",
        "quarantined_candidate_staging": quarantined_candidate,
        "headroom": headroom,
    }


def prepare_compile_cache_author(
    *,
    contract: dict[str, Any],
    layout: RuntimeLayout,
    boot_id: str,
) -> dict[str, Any]:
    return prepare_compile_cache(
        contract=contract,
        layout=layout,
        boot_id=boot_id,
        mode="author",
    )


def prepare_compile_cache_ephemeral(
    *,
    contract: dict[str, Any],
    layout: RuntimeLayout,
    boot_id: str,
) -> dict[str, Any]:
    return prepare_compile_cache(
        contract=contract,
        layout=layout,
        boot_id=boot_id,
        mode="ephemeral",
    )


def _candidate_result(
    *,
    action: str,
    contract: dict[str, Any],
    author_boot_id: str,
    manifest_sha256: str,
    authored_sha256: str,
    bundle: dict[str, Any],
    inventory: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": COMPILE_CACHE_AUTHOR_CANDIDATE_SCHEMA,
        "action": action,
        "state": "candidate-requires-distinct-boot-proof",
        "cache_id": contract["cache_id"],
        "persistent_root": contract["persistent_root"],
        "author_boot_id": author_boot_id,
        "manifest_sha256": manifest_sha256,
        "authored_sha256": authored_sha256,
        "bundle_sha256": bundle["sha256"],
        "bundle_bytes": bundle["bytes"],
        "inventory_sha256": inventory["sha256"],
        "file_count": inventory["file_count"],
        "total_bytes": inventory["total_bytes"],
    }


def _candidate_manifest(
    *,
    contract: dict[str, Any],
    inventory: dict[str, Any],
    measurement: dict[str, Any],
    measurement_payload: bytes,
) -> dict[str, Any]:
    return {
        "schema_version": COMPILE_CACHE_BUNDLE_MANIFEST_SCHEMA,
        "contract": contract,
        "archive": {
            "name": BUNDLE_NAME,
            "format": "gnu-tar-uncompressed",
            "member_root": contract["local_root"],
        },
        "inventory": inventory,
        "author_measurement": {
            "sha256": sha256_bytes(measurement_payload),
            "document": measurement,
        },
    }


def _authored_candidate(
    *,
    contract: dict[str, Any],
    measurement: dict[str, Any],
    measurement_payload: bytes,
    manifest_bytes: int,
    manifest_sha256: str,
    bundle: dict[str, Any],
    inventory: dict[str, Any],
    produced: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": COMPILE_CACHE_AUTHOR_CANDIDATE_SCHEMA,
        "state": "candidate",
        "cache_id": contract["cache_id"],
        "contract": contract,
        "author_boot_id": measurement["boot_id"],
        "service_manifest_sha256": measurement["service_manifest_sha256"],
        "author_measurement_sha256": sha256_bytes(measurement_payload),
        "manifest": {
            "name": MANIFEST_NAME,
            "bytes": manifest_bytes,
            "sha256": manifest_sha256,
        },
        "bundle": bundle,
        "inventory_sha256": inventory["sha256"],
        "produced_artifacts": produced,
    }


def seal_compile_cache_candidate(
    *,
    contract: dict[str, Any],
    layout: RuntimeLayout,
    measurement_path: pathlib.Path,
) -> dict[str, Any]:
    """Seal a measured cold-launch tree as an immutable unaccepted candidate."""

    validated = validate_contract(contract)
    paths = localized_paths(contract=validated, layout=layout)
    measurement, measurement_payload = load_measurement(
        path=measurement_path,
        contract=validated,
        mode="author",
    )
    preparation, preparation_payload = load_preparation(
        contract=validated,
        path=paths["prerequisite"],
        boot_id=measurement["boot_id"],
        expected_mode="author",
    )
    if (
        measurement["prerequisite_receipt_sha256"] != sha256_bytes(preparation_payload)
        or measurement["pre_inventory_sha256"] != preparation["inventory_sha256"]
    ):
        fail("author measurement does not bind its preparation receipt")
    inventory = inventory_compile_cache(paths["local_root"])
    if inventory["sha256"] != measurement["post_inventory_sha256"]:
        fail("author cache changed after the measured cold launch")
    produced = artifact_records(
        inventory,
        measurement["cache_evidence"]["produced_artifacts"],
    )
    expected_bundle_bytes = deterministic_bundle_size(inventory)
    preview_bundle = {
        "name": BUNDLE_NAME,
        "bytes": expected_bundle_bytes,
        "sha256": "0" * 64,
    }
    preview_manifest = _candidate_manifest(
        contract=validated,
        inventory=inventory,
        measurement=measurement,
        measurement_payload=measurement_payload,
    )
    preview_manifest_payload = canonical_bytes(preview_manifest)
    preview_authored = _authored_candidate(
        contract=validated,
        measurement=measurement,
        measurement_payload=measurement_payload,
        manifest_bytes=len(preview_manifest_payload),
        manifest_sha256="0" * 64,
        bundle=preview_bundle,
        inventory=inventory,
        produced=produced,
    )
    preview_authored_payload = canonical_bytes(preview_authored)
    document_bytes = len(preview_manifest_payload) + len(preview_authored_payload)
    expected_file_bytes = {
        BUNDLE_NAME: expected_bundle_bytes,
        MANIFEST_NAME: len(preview_manifest_payload),
        AUTHORED_NAME: len(preview_authored_payload),
    }
    ensure_untrusted_parents(
        anchor=layout.workspace_root,
        parent=paths["persistent_root"].parent,
    )
    staging_root = _candidate_staging_path(
        persistent_root=paths["persistent_root"],
        cache_id=validated["cache_id"],
    )
    publication_lock = _candidate_publication_lock(
        persistent_root=paths["persistent_root"],
        cache_id=validated["cache_id"],
    )
    volume_publication_lock = _volume_publication_lock(layout)
    with open_advisory_lock(
        volume_publication_lock,
        create=True,
    ) as volume_lock:
        volume_lock.exclusive()
        with open_advisory_lock(publication_lock, create=True) as lock:
            lock.exclusive()
            if os.path.lexists(paths["persistent_root"]):
                generation = load_persistent_compile_cache(
                    contract=validated,
                    layout=layout,
                    require_accepted=False,
                )
                if generation.state != "candidate" or generation.manifest[
                    "author_measurement"
                ]["sha256"] != sha256_bytes(measurement_payload):
                    fail("a different compiled-cache generation already exists")
                return _candidate_result(
                    action="reuse-sealed-author-candidate",
                    contract=validated,
                    author_boot_id=generation.authored["author_boot_id"],
                    manifest_sha256=sha256_bytes(generation.manifest_payload),
                    authored_sha256=sha256_bytes(generation.authored_payload),
                    bundle=generation.bundle,
                    inventory=generation.inventory,
                )
            observed_file_bytes = _recover_candidate_staging(
                staging_root,
                expected_file_bytes=expected_file_bytes,
            )
            remaining_archive_bytes = expected_bundle_bytes - observed_file_bytes.get(
                BUNDLE_NAME, 0
            )
            remaining_document_bytes = sum(
                expected_file_bytes[name] - observed_file_bytes.get(name, 0)
                for name in (MANIFEST_NAME, AUTHORED_NAME)
            )
            preflight_compile_cache_headroom(
                filesystem_root=layout.workspace_root,
                purpose="author-candidate-publication",
                archive_bytes=remaining_archive_bytes,
                inventory_bytes=0,
                document_bytes=remaining_document_bytes,
                reserve_name="bounded-author-publication-overhead",
                reserve_bytes=AUTHOR_PUBLICATION_RESERVE_BYTES,
            )
            if not os.path.lexists(staging_root):
                mkdir_untrusted_exclusive(staging_root)
            bundle = write_bundle(
                path=staging_root / BUNDLE_NAME,
                root=paths["local_root"],
                inventory=inventory,
            )
            if bundle["bytes"] != expected_bundle_bytes:
                fail("compiled-cache archive size prediction changed")
            if inventory_compile_cache(paths["local_root"]) != inventory:
                fail("author cache changed while writing its persistent archive")
            manifest = _candidate_manifest(
                contract=validated,
                inventory=inventory,
                measurement=measurement,
                measurement_payload=measurement_payload,
            )
            manifest_payload, manifest_sha256 = write_json_resumable(
                staging_root / MANIFEST_NAME,
                manifest,
                mode=0o444,
                durable=True,
            )
            authored = _authored_candidate(
                contract=validated,
                measurement=measurement,
                measurement_payload=measurement_payload,
                manifest_bytes=len(manifest_payload),
                manifest_sha256=manifest_sha256,
                bundle=bundle,
                inventory=inventory,
                produced=produced,
            )
            authored_payload, authored_sha256 = write_json_resumable(
                staging_root / AUTHORED_NAME,
                authored,
                mode=0o444,
                durable=True,
            )
            if len(manifest_payload) + len(authored_payload) != document_bytes:
                fail("compiled-cache publication document size prediction changed")
            fsync_directory(staging_root)
            publish_directory_noreplace(
                source=staging_root,
                destination=paths["persistent_root"],
            )
    return _candidate_result(
        action="seal-author-candidate",
        contract=validated,
        author_boot_id=measurement["boot_id"],
        manifest_sha256=manifest_sha256,
        authored_sha256=authored_sha256,
        bundle=bundle,
        inventory=inventory,
    )


def _stage_receipt(
    *,
    contract: dict[str, Any],
    boot_id: str,
    root: pathlib.Path,
    inventory: dict[str, Any],
) -> dict[str, Any]:
    root_stat = require_directory(root, exact_mode=0o700)
    return {
        "schema_version": COMPILE_CACHE_STAGE_SCHEMA,
        "cache_id": contract["cache_id"],
        "contract": contract,
        "boot_id": boot_id,
        "persistent_root": contract["persistent_root"],
        "local_root": contract["local_root"],
        "directory_stat": directory_stat(root_stat),
        "file_count": inventory["file_count"],
        "total_bytes": inventory["total_bytes"],
        "files_sha256": inventory["sha256"],
    }


def stage_compile_cache(
    *,
    contract: dict[str, Any],
    layout: RuntimeLayout,
    boot_id: str,
    source: Literal["accepted", "candidate-proof"] = "accepted",
) -> dict[str, Any]:
    """Stream one persistent archive into a new mutable ephemeral cache tree."""

    validated = validate_contract(contract)
    if not isinstance(boot_id, str) or not boot_id:
        fail("compiled-cache stage requires an exact boot identity")
    if source not in {"accepted", "candidate-proof"}:
        fail("compiled-cache stage source mode is unsupported")
    generation = load_persistent_compile_cache(
        contract=validated,
        layout=layout,
        require_accepted=source == "accepted",
        verify_bundle_content=False,
    )
    if source == "candidate-proof":
        if generation.state != "candidate":
            fail("candidate proof mode refuses an already accepted generation")
        if boot_id == generation.authored["author_boot_id"]:
            fail("candidate proof must run on a different boot than authoring")
    paths = localized_paths(contract=validated, layout=layout)
    if any(
        os.path.lexists(paths[name])
        for name in ("local_root", "stage_receipt", "prerequisite")
    ):
        fail("compiled-cache stage refuses existing or partial local state")
    headroom = preflight_compile_cache_headroom(
        filesystem_root=layout.session_root,
        purpose=f"{source}-compile-cache-extraction",
        archive_bytes=0,
        inventory_bytes=generation.inventory["total_bytes"],
        reserve_name="bounded-runtime-cache-growth",
        reserve_bytes=STAGED_CACHE_GROWTH_RESERVE_BYTES,
    )
    ensure_private_parents(
        anchor=layout.session_root,
        parent=paths["local_root"].parent,
    )
    mkdir_exclusive(paths["local_root"])
    extract_bundle(
        generation=generation,
        destination=paths["local_root"],
    )
    observed = inventory_compile_cache(paths["local_root"])
    if observed != generation.inventory:
        fail("ephemeral compiled-cache stage does not match its exact inventory")
    receipt = _stage_receipt(
        contract=validated,
        boot_id=boot_id,
        root=paths["local_root"],
        inventory=observed,
    )
    receipt_payload = canonical_bytes(receipt)
    source_receipt = {
        "schema_version": COMPILE_CACHE_STAGE_SOURCE_SCHEMA,
        "source": source,
        "cache_id": validated["cache_id"],
        "contract": validated,
        "boot_id": boot_id,
        "persistent_state": generation.state,
        "persistent_root": validated["persistent_root"],
        "manifest_sha256": sha256_bytes(generation.manifest_payload),
        "bundle_sha256": generation.bundle["sha256"],
        "authored_sha256": sha256_bytes(generation.authored_payload),
        "acceptance_sha256": (
            None
            if generation.acceptance_payload is None
            else sha256_bytes(generation.acceptance_payload)
        ),
        "inventory_sha256": observed["sha256"],
        "stage_receipt_sha256": sha256_bytes(receipt_payload),
    }
    write_json_exclusive(
        paths["prerequisite"],
        source_receipt,
        mode=0o600,
        durable=False,
    )
    _, receipt_sha256 = write_json_exclusive(
        paths["stage_receipt"],
        receipt,
        mode=0o600,
        durable=False,
    )
    verify_compile_cache_stage(
        contract=validated,
        layout=layout,
        boot_id=boot_id,
    )
    return {
        "schema_version": COMPILE_CACHE_STAGE_SCHEMA,
        "action": "stage",
        "source": source,
        "source_state": generation.state,
        "cache_id": validated["cache_id"],
        "persistent_root": validated["persistent_root"],
        "local_root": validated["local_root"],
        "receipt_sha256": receipt_sha256,
        "inventory_sha256": observed["sha256"],
        "file_count": observed["file_count"],
        "total_bytes": observed["total_bytes"],
        "headroom": headroom,
        "state": "verified-at-publication-mutable-runtime-cache",
    }


def _load_stage_source(
    *,
    contract: dict[str, Any],
    path: pathlib.Path,
    generation: PersistentCompileCache,
    boot_id: str,
    stage_receipt_sha256: str,
    expected_source: Literal["candidate-proof", "accepted"],
) -> dict[str, Any]:
    receipt, _ = read_exact_json(
        path,
        mode=0o600,
        maximum_bytes=MAX_MEASUREMENT_BYTES,
    )
    if (
        set(receipt)
        != {
            "schema_version",
            "source",
            "cache_id",
            "contract",
            "boot_id",
            "persistent_state",
            "persistent_root",
            "manifest_sha256",
            "bundle_sha256",
            "authored_sha256",
            "acceptance_sha256",
            "inventory_sha256",
            "stage_receipt_sha256",
        }
        or receipt["schema_version"] != COMPILE_CACHE_STAGE_SOURCE_SCHEMA
        or receipt["source"] != expected_source
        or receipt["cache_id"] != contract["cache_id"]
        or receipt["contract"] != contract
        or receipt["boot_id"] != boot_id
        or receipt["persistent_state"] != generation.state
        or receipt["persistent_root"] != contract["persistent_root"]
        or receipt["manifest_sha256"] != sha256_bytes(generation.manifest_payload)
        or receipt["bundle_sha256"] != generation.bundle["sha256"]
        or receipt["authored_sha256"] != sha256_bytes(generation.authored_payload)
        or receipt["acceptance_sha256"]
        != (
            None
            if generation.acceptance_payload is None
            else sha256_bytes(generation.acceptance_payload)
        )
        or receipt["inventory_sha256"] != generation.inventory["sha256"]
        or receipt["stage_receipt_sha256"] != stage_receipt_sha256
    ):
        fail("candidate proof stage source receipt is malformed or mismatched")
    return receipt


@dataclass(frozen=True)
class CompileCachePrerequisite:
    """One exact typed cache mode bound before service setup."""

    mode: Literal["ephemeral", "author", "candidate-proof", "accepted"]
    contract: dict[str, Any]
    receipt: dict[str, Any]
    receipt_payload: bytes
    local_root: pathlib.Path
    pre_inventory_sha256: str
    file_count: int
    total_bytes: int
    persistent_state: Literal["candidate", "accepted"] | None

    @property
    def receipt_sha256(self) -> str:
        return sha256_bytes(self.receipt_payload)

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": COMPILE_CACHE_PREREQUISITE_SUMMARY_SCHEMA,
            "mode": self.mode,
            "cache_id": self.contract["cache_id"],
            "receipt_sha256": self.receipt_sha256,
            "local_root": self.contract["local_root"],
            "pre_inventory_sha256": self.pre_inventory_sha256,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "persistent_state": self.persistent_state,
            "mutable_during_service": True,
        }


def load_compile_cache_prerequisite(
    *,
    contract: dict[str, Any],
    layout: RuntimeLayout,
    boot_id: str,
    expected_mode: CompileCacheMode,
    verify_inventory: bool,
) -> CompileCachePrerequisite:
    """Load the explicitly selected cache prerequisite and reject mixed state."""

    validated = validate_contract(contract)
    if expected_mode not in {
        "ephemeral",
        "author",
        "candidate-proof",
        "accepted",
    }:
        fail("cache prerequisite mode is unsupported")
    paths = localized_paths(contract=validated, layout=layout)
    receipt, payload = read_exact_json(
        paths["prerequisite"],
        mode=0o600,
        maximum_bytes=MAX_MEASUREMENT_BYTES,
    )
    schema = receipt.get("schema_version")
    has_stage_receipt = os.path.lexists(paths["stage_receipt"])
    if schema == COMPILE_CACHE_PREPARATION_SCHEMA:
        mode = receipt.get("mode")
        if (
            expected_mode not in {"ephemeral", "author"}
            or mode != expected_mode
            or has_stage_receipt
        ):
            fail("empty cache preparation conflicts with staged cache state")
        preparation, _ = load_preparation(
            contract=validated,
            path=paths["prerequisite"],
            boot_id=boot_id,
            expected_mode=mode,
        )
        root_stat = require_directory(paths["local_root"], exact_mode=0o700)
        if verify_inventory:
            if directory_stat(root_stat) != preparation["directory_stat"]:
                fail("prepared empty cache root changed before service start")
            observed = inventory_compile_cache(paths["local_root"])
            if (
                observed["sha256"] != preparation["inventory_sha256"]
                or observed["file_count"] != 0
                or observed["total_bytes"] != 0
            ):
                fail("prepared empty cache is no longer empty")
        return CompileCachePrerequisite(
            mode=mode,
            contract=validated,
            receipt=preparation,
            receipt_payload=payload,
            local_root=paths["local_root"],
            pre_inventory_sha256=preparation["inventory_sha256"],
            file_count=0,
            total_bytes=0,
            persistent_state=None,
        )
    if schema != COMPILE_CACHE_STAGE_SOURCE_SCHEMA or not has_stage_receipt:
        fail("cache prerequisite is absent, partial, or has an unsupported mode")
    source = receipt.get("source")
    if expected_mode not in {"candidate-proof", "accepted"} or source != expected_mode:
        fail("staged cache prerequisite mode is unsupported")
    generation = load_persistent_compile_cache(
        contract=validated,
        layout=layout,
        require_accepted=source == "accepted",
        verify_bundle_content=False,
    )
    stage_receipt, stage_payload = read_private_json(
        paths["stage_receipt"],
        maximum_bytes=MAX_MEASUREMENT_BYTES,
    )
    _load_stage_source(
        contract=validated,
        path=paths["prerequisite"],
        generation=generation,
        boot_id=boot_id,
        stage_receipt_sha256=sha256_bytes(stage_payload),
        expected_source=source,
    )
    if verify_inventory:
        verified = verify_compile_cache_stage(
            contract=validated,
            layout=layout,
            boot_id=boot_id,
        )
        observed = inventory_compile_cache(paths["local_root"])
        if verified.receipt != stage_receipt or observed != generation.inventory:
            fail("staged cache changed before service start")
    return CompileCachePrerequisite(
        mode=source,
        contract=validated,
        receipt=receipt,
        receipt_payload=payload,
        local_root=paths["local_root"],
        pre_inventory_sha256=generation.inventory["sha256"],
        file_count=generation.inventory["file_count"],
        total_bytes=generation.inventory["total_bytes"],
        persistent_state=generation.state,
    )


def accept_compile_cache_candidate(
    *,
    contract: dict[str, Any],
    layout: RuntimeLayout,
    measurement_path: pathlib.Path,
) -> dict[str, Any]:
    """Accept a candidate only after a distinct-boot exact cache-hit launch."""

    validated = validate_contract(contract)
    measurement, measurement_payload = load_measurement(
        path=measurement_path,
        contract=validated,
        mode="candidate-proof",
    )
    paths = localized_paths(contract=validated, layout=layout)
    publication_lock = _candidate_publication_lock(
        persistent_root=paths["persistent_root"],
        cache_id=validated["cache_id"],
    )
    acceptance_staging = _acceptance_staging_path(
        persistent_root=paths["persistent_root"],
        cache_id=validated["cache_id"],
    )
    with open_advisory_lock(publication_lock, create=True) as lock:
        lock.exclusive()
        _recover_acceptance_transition(
            persistent_root=paths["persistent_root"],
            staging_path=acceptance_staging,
        )
        generation = load_persistent_compile_cache(
            contract=validated,
            layout=layout,
            require_accepted=False,
            verify_bundle_content=False,
        )
        measurement_sha256 = sha256_bytes(measurement_payload)
        if generation.state == "accepted":
            assert generation.acceptance is not None
            assert generation.acceptance_payload is not None
            if (
                generation.acceptance["require_measurement"]["sha256"]
                != measurement_sha256
            ):
                fail("a different candidate proof already accepted this cache")
            return _acceptance_result(
                action="reuse-accepted-proof",
                contract=validated,
                generation=generation,
                acceptance_sha256=sha256_bytes(generation.acceptance_payload),
                require_boot_id=measurement["boot_id"],
            )
        if measurement["boot_id"] == generation.authored["author_boot_id"]:
            fail("require proof must run on a different boot than authoring")
        stage_receipt, stage_payload = read_private_json(
            paths["stage_receipt"],
            maximum_bytes=MAX_MEASUREMENT_BYTES,
        )
        _, prerequisite_payload = read_exact_json(
            paths["prerequisite"],
            mode=0o600,
            maximum_bytes=MAX_MEASUREMENT_BYTES,
        )
        stage_sha256 = sha256_bytes(stage_payload)
        if (
            measurement["prerequisite_receipt_sha256"]
            != sha256_bytes(prerequisite_payload)
            or measurement["pre_inventory_sha256"] != generation.inventory["sha256"]
        ):
            fail("require measurement does not bind the exact candidate stage")
        _load_stage_source(
            contract=validated,
            path=paths["prerequisite"],
            generation=generation,
            boot_id=measurement["boot_id"],
            stage_receipt_sha256=stage_sha256,
            expected_source="candidate-proof",
        )
        if (
            stage_receipt["files_sha256"] != generation.inventory["sha256"]
            or stage_receipt["boot_id"] != measurement["boot_id"]
        ):
            fail("candidate proof stage is no longer receipt-bound")
        observed = inventory_compile_cache(paths["local_root"])
        if observed["sha256"] != measurement["post_inventory_sha256"]:
            fail("candidate cache changed after its measured proof")
        loaded = artifact_records(
            observed,
            measurement["cache_evidence"]["loaded_artifacts"],
        )
        if loaded != generation.authored["produced_artifacts"]:
            fail("require launch did not load the exact authored cache artifacts")
        manifest_descriptor = validate_descriptor(
            generation.authored["manifest"],
            expected_name=MANIFEST_NAME,
        )
        bundle_descriptor = validate_descriptor(
            generation.authored["bundle"],
            expected_name=BUNDLE_NAME,
        )
        startup_proof = candidate_startup_proof(
            author_measurement=(generation.manifest["author_measurement"]["document"]),
            candidate_measurement=measurement,
        )
        acceptance = {
            "schema_version": COMPILE_CACHE_ACCEPTANCE_SCHEMA,
            "state": "accepted",
            "cache_id": validated["cache_id"],
            "contract": validated,
            "author_boot_id": generation.authored["author_boot_id"],
            "require_boot_id": measurement["boot_id"],
            "manifest": manifest_descriptor,
            "bundle": bundle_descriptor,
            "authored_sha256": sha256_bytes(generation.authored_payload),
            "inventory_sha256": generation.inventory["sha256"],
            "require_measurement": {
                "sha256": measurement_sha256,
                "document": measurement,
            },
            "startup_proof": startup_proof,
            "loaded_artifacts": loaded,
        }
        _, acceptance_sha256 = write_json_resumable(
            acceptance_staging,
            acceptance,
            mode=0o444,
            durable=True,
        )
        publish_file_noreplace(
            source=acceptance_staging,
            destination=generation.root / ACCEPTED_NAME,
            mode=0o444,
        )
        fsync_directory(generation.root)
        fsync_directory(generation.root.parent)
        accepted = load_persistent_compile_cache(
            contract=validated,
            layout=layout,
            require_accepted=True,
            verify_bundle_content=False,
        )
        if accepted.acceptance != acceptance:
            fail("compiled-cache acceptance changed during publication")
        return _acceptance_result(
            action="accept",
            contract=validated,
            generation=accepted,
            acceptance_sha256=acceptance_sha256,
            require_boot_id=measurement["boot_id"],
        )


__all__ = [
    "ACCEPTED_NAME",
    "AUTHORED_NAME",
    "BUNDLE_NAME",
    "COMPILE_CACHE_ACCEPTANCE_SCHEMA",
    "COMPILE_CACHE_AUTHOR_CANDIDATE_SCHEMA",
    "COMPILE_CACHE_LAUNCH_MEASUREMENT_SCHEMA",
    "COMPILE_CACHE_PREPARATION_SCHEMA",
    "MANIFEST_NAME",
    "VLLM_CACHE_EVIDENCE_SCHEMA",
    "accept_compile_cache_candidate",
    "inventory_compile_cache",
    "load_persistent_compile_cache",
    "load_compile_cache_prerequisite",
    "prepare_compile_cache",
    "prepare_compile_cache_author",
    "prepare_compile_cache_ephemeral",
    "seal_compile_cache_candidate",
    "stage_compile_cache",
]
