from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runpod"))

from runpod_local.errors import RunpodLocalError  # noqa: E402
from runpod_local.service_compile_cache import (  # noqa: E402
    build_compile_cache_contract,
)
from service_runtime.collaborators import (  # noqa: E402
    verify_compile_cache_stage,
)
from service_runtime.compile_cache_archive import (  # noqa: E402
    deterministic_bundle_size,
)
from service_runtime.compile_cache_files import (  # noqa: E402
    AUTHOR_PUBLICATION_RESERVE_BYTES,
    STAGED_CACHE_GROWTH_RESERVE_BYTES,
)
import service_runtime.compile_cache_stage as compile_cache_stage  # noqa: E402
from service_runtime.compile_cache_stage import (  # noqa: E402
    ACCEPTED_NAME,
    AUTHORED_NAME,
    BUNDLE_NAME,
    COMPILE_CACHE_ACCEPTANCE_SCHEMA,
    COMPILE_CACHE_AUTHOR_CANDIDATE_SCHEMA,
    COMPILE_CACHE_LAUNCH_MEASUREMENT_SCHEMA,
    COMPILE_CACHE_PREPARATION_SCHEMA,
    MANIFEST_NAME,
    VLLM_CACHE_EVIDENCE_SCHEMA,
    accept_compile_cache_candidate,
    inventory_compile_cache,
    load_persistent_compile_cache,
    prepare_compile_cache_author,
    prepare_compile_cache_ephemeral,
    seal_compile_cache_candidate,
    stage_compile_cache,
)
from service_runtime.layout import RuntimeLayout  # noqa: E402
from service_runtime.execution_environment import (  # noqa: E402
    runtime_execution_environment,
)


AUTHOR_BOOT_ID = "11111111-1111-1111-1111-111111111111"
REQUIRE_BOOT_ID = "22222222-2222-2222-2222-222222222222"
ACCEPTED_BOOT_ID = "33333333-3333-3333-3333-333333333333"
SERVICE_MANIFEST_SHA256 = "a" * 64
PRODUCED_ARTIFACT = "vllm/torch_compile_cache/aot/model"
SECONDARY_ARTIFACT = "cuda/" + "kernel-component-" * 8 + ".bin"


def canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("ascii")


def write_private_json(path: pathlib.Path, value: dict[str, object]) -> str:
    payload = canonical_bytes(value)
    path.write_bytes(payload)
    path.chmod(0o600)
    return hashlib.sha256(payload).hexdigest()


def contract_for(
    *,
    closure_digit: str = "1",
    launch_digit: str = "2",
) -> dict[str, object]:
    return build_compile_cache_contract(
        driver="vllm-openai.v1",
        runtime={
            "runtime_id": "fixture-vllm-cu129",
            "image": f"vllm/vllm-openai@sha256:{'3' * 64}",
            "manifest": {"sha256": "4" * 64},
        },
        runtime_execution_environment=runtime_execution_environment({}).normalized(),
        implementation_bundle_sha256="5" * 64,
        huggingface_closure_sha256=closure_digit * 64,
        compile_affecting_launch_sha256=launch_digit * 64,
        observed_gpu={
            "name": "NVIDIA Fixture GPU",
            "compute_capability": [12, 0],
            "memory_mib": 98304,
            "driver_version": "575.57.08",
        },
    )


def sidecar_path(
    layout: RuntimeLayout,
    contract: dict[str, object],
    suffix: str,
) -> pathlib.Path:
    return layout.localize(pathlib.PurePosixPath(f"{contract['local_root']}{suffix}"))


class CacheFixture:
    def __init__(self, testcase: unittest.TestCase) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        testcase.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(mode=0o700)
        self.contract = contract_for()
        self.layouts: list[RuntimeLayout] = []

    def layout(self, name: str) -> RuntimeLayout:
        session = self.root / name
        session.mkdir(mode=0o700)
        layout = RuntimeLayout(
            session_root=session,
            workspace_root=self.workspace,
        )
        self.layouts.append(layout)
        return layout

    def force_network_volume_directory_modes(self) -> None:
        """Model RunPod's network volume, which reports every directory 0777."""

        for current, _, _ in os.walk(self.workspace, followlinks=False):
            pathlib.Path(current).chmod(0o777)

    def author_candidate(
        self,
        *,
        force_network_volume_modes: bool = False,
    ) -> tuple[RuntimeLayout, dict[str, object], dict[str, object]]:
        layout = self.layout("author-session")
        if force_network_volume_modes:
            persistent_root = layout.localize(self.contract["persistent_root"])
            persistent_root.parent.mkdir(parents=True, mode=0o700)
            self.force_network_volume_directory_modes()
        prepared = prepare_compile_cache_author(
            contract=self.contract,
            layout=layout,
            boot_id=AUTHOR_BOOT_ID,
        )
        local_root = layout.localize(self.contract["local_root"])
        pre_inventory_sha256 = inventory_compile_cache(local_root)["sha256"]
        artifact = local_root.joinpath(*pathlib.PurePosixPath(PRODUCED_ARTIFACT).parts)
        artifact.parent.mkdir(parents=True, mode=0o700)
        for parent in artifact.parents:
            if parent == local_root:
                break
            parent.chmod(0o700)
        artifact.write_bytes(b"compiled-aot-fixture")
        artifact.chmod(0o600)
        cuda = local_root.joinpath(*pathlib.PurePosixPath(SECONDARY_ARTIFACT).parts)
        cuda.write_bytes(b"compiled-cuda-fixture")
        cuda.chmod(0o600)
        inventory = inventory_compile_cache(local_root)
        measurement = launch_measurement(
            contract=self.contract,
            mode="author",
            boot_id=AUTHOR_BOOT_ID,
            prerequisite_receipt_sha256=prepared["receipt_sha256"],
            inventory_sha256=inventory["sha256"],
            pre_inventory_sha256=pre_inventory_sha256,
            produced=[PRODUCED_ARTIFACT],
        )
        measurement_path = layout.session_root / "author-measurement.json"
        write_private_json(measurement_path, measurement)
        if force_network_volume_modes:
            self.force_network_volume_directory_modes()
            real_mkdir = compile_cache_stage.mkdir_untrusted_exclusive

            def force_created_directory_mode(path: pathlib.Path) -> None:
                real_mkdir(path)
                path.chmod(0o777)

            with mock.patch.object(
                compile_cache_stage,
                "mkdir_untrusted_exclusive",
                side_effect=force_created_directory_mode,
            ):
                sealed = seal_compile_cache_candidate(
                    contract=self.contract,
                    layout=layout,
                    measurement_path=measurement_path,
                )
            self.force_network_volume_directory_modes()
        else:
            sealed = seal_compile_cache_candidate(
                contract=self.contract,
                layout=layout,
                measurement_path=measurement_path,
            )
        return layout, sealed, inventory

    def proof_stage(
        self,
        *,
        boot_id: str = REQUIRE_BOOT_ID,
    ) -> tuple[RuntimeLayout, dict[str, object]]:
        layout = self.layout(f"proof-session-{len(self.layouts)}")
        staged = stage_compile_cache(
            contract=self.contract,
            layout=layout,
            boot_id=boot_id,
            source="candidate-proof",
        )
        return layout, staged

    def accept(
        self,
        layout: RuntimeLayout,
        inventory: dict[str, object],
        *,
        loaded: list[str] | None = None,
        service_manifest_sha256: str = SERVICE_MANIFEST_SHA256,
        started_monotonic_ns: int = 10,
        ready_monotonic_ns: int = 20,
    ) -> dict[str, object]:
        prerequisite_path = sidecar_path(
            layout,
            self.contract,
            ".prerequisite.json",
        )
        receipt_sha256 = hashlib.sha256(prerequisite_path.read_bytes()).hexdigest()
        observed = inventory_compile_cache(layout.localize(self.contract["local_root"]))
        measurement = launch_measurement(
            contract=self.contract,
            mode="candidate-proof",
            boot_id=REQUIRE_BOOT_ID,
            prerequisite_receipt_sha256=receipt_sha256,
            inventory_sha256=observed["sha256"],
            pre_inventory_sha256=inventory["sha256"],
            loaded=([PRODUCED_ARTIFACT] if loaded is None else loaded),
            service_manifest_sha256=service_manifest_sha256,
            started_monotonic_ns=started_monotonic_ns,
            ready_monotonic_ns=ready_monotonic_ns,
        )
        path = layout.session_root / "require-measurement.json"
        write_private_json(path, measurement)
        return accept_compile_cache_candidate(
            contract=self.contract,
            layout=layout,
            measurement_path=path,
        )


def launch_measurement(
    *,
    contract: dict[str, object],
    mode: str,
    boot_id: str,
    prerequisite_receipt_sha256: str,
    inventory_sha256: str,
    pre_inventory_sha256: str | None = None,
    produced: list[str] | None = None,
    loaded: list[str] | None = None,
    service_manifest_sha256: str = SERVICE_MANIFEST_SHA256,
    started_monotonic_ns: int = 10,
    ready_monotonic_ns: int | None = None,
) -> dict[str, object]:
    produced_artifacts = [] if produced is None else produced
    loaded_artifacts = [] if loaded is None else loaded
    actual_ready_monotonic_ns = (
        (50 if mode == "author" else 20)
        if ready_monotonic_ns is None
        else ready_monotonic_ns
    )
    return {
        "schema_version": COMPILE_CACHE_LAUNCH_MEASUREMENT_SCHEMA,
        "mode": mode,
        "cache_id": contract["cache_id"],
        "contract": contract,
        "boot_id": boot_id,
        "service_manifest_sha256": service_manifest_sha256,
        "prerequisite_receipt_sha256": prerequisite_receipt_sha256,
        "runtime_execution_environment": contract["identity"][
            "runtime_execution_environment"
        ],
        "runtime_execution_environment_sha256": contract["identity"][
            "runtime_execution_environment"
        ]["sha256"],
        "started_monotonic_ns": started_monotonic_ns,
        "ready_monotonic_ns": actual_ready_monotonic_ns,
        "stopped_monotonic_ns": actual_ready_monotonic_ns + 10,
        "ready": True,
        "process_stopped": True,
        "pre_inventory_sha256": (
            inventory_sha256 if pre_inventory_sha256 is None else pre_inventory_sha256
        ),
        "post_inventory_sha256": inventory_sha256,
        "cache_evidence": {
            "schema_version": VLLM_CACHE_EVIDENCE_SCHEMA,
            "driver": "vllm-openai.v1",
            "mode": mode,
            "cache_root": contract["local_root"],
            "produced_artifacts": produced_artifacts,
            "loaded_artifacts": loaded_artifacts,
            "cold_compile_observed": mode == "author",
            "unexpected_cache_paths": [],
        },
    }


class CompileCacheAuthorTest(unittest.TestCase):
    def test_author_publication_headroom_failure_writes_no_staging_bytes(self):
        fixture = CacheFixture(self)
        real_statvfs = os.statvfs

        def constrained_workspace(path: os.PathLike[str]) -> object:
            if pathlib.Path(path) == fixture.workspace:
                return mock.Mock(f_bavail=1, f_frsize=4096)
            return real_statvfs(path)

        with (
            mock.patch(
                "service_runtime.compile_cache_files.os.statvfs",
                side_effect=constrained_workspace,
            ),
            self.assertRaises(RunpodLocalError),
        ):
            fixture.author_candidate()

        persistent_root = fixture.layouts[0].localize(
            fixture.contract["persistent_root"]
        )
        staging_root = persistent_root.parent / (
            f".{fixture.contract['cache_id']}.candidate-staging"
        )
        self.assertFalse(os.path.lexists(staging_root))
        self.assertFalse(os.path.lexists(persistent_root))

    def test_empty_cache_headroom_failure_creates_no_cache_content(self):
        fixture = CacheFixture(self)
        layout = fixture.layout("headroom-refusal")
        statvfs = mock.Mock(f_bavail=1, f_frsize=4096)

        with (
            mock.patch(
                "service_runtime.compile_cache_files.os.statvfs",
                return_value=statvfs,
            ),
            self.assertRaises(RunpodLocalError),
        ):
            prepare_compile_cache_author(
                contract=fixture.contract,
                layout=layout,
                boot_id=AUTHOR_BOOT_ID,
            )

        self.assertFalse(
            os.path.lexists(layout.localize(fixture.contract["local_root"]))
        )

    def test_author_seals_candidate_but_cannot_call_it_accepted(self):
        fixture = CacheFixture(self)
        _, sealed, inventory = fixture.author_candidate()
        persistent_root = fixture.workspace.joinpath(
            *pathlib.PurePosixPath(fixture.contract["persistent_root"])
            .relative_to("/workspace")
            .parts
        )

        self.assertEqual(
            sealed["schema_version"],
            COMPILE_CACHE_AUTHOR_CANDIDATE_SCHEMA,
        )
        self.assertEqual(sealed["state"], "candidate-requires-distinct-boot-proof")
        self.assertEqual(sealed["inventory_sha256"], inventory["sha256"])
        self.assertEqual(
            sealed["bundle_bytes"],
            deterministic_bundle_size(inventory),
        )
        self.assertEqual(
            {entry.name for entry in persistent_root.iterdir()},
            {BUNDLE_NAME, MANIFEST_NAME, AUTHORED_NAME},
        )
        for entry in persistent_root.iterdir():
            self.assertEqual(stat.S_IMODE(entry.stat().st_mode), 0o444)
        candidate = load_persistent_compile_cache(
            contract=fixture.contract,
            layout=fixture.layouts[0],
            require_accepted=False,
        )
        self.assertEqual(candidate.state, "candidate")
        with self.assertRaises(RunpodLocalError):
            load_persistent_compile_cache(
                contract=fixture.contract,
                layout=fixture.layouts[0],
                require_accepted=True,
            )

    def test_interrupted_candidate_publication_charges_only_remaining_bytes(self):
        fixture = CacheFixture(self)
        with (
            mock.patch(
                "service_runtime.compile_cache_stage.publish_directory_noreplace",
                side_effect=RunpodLocalError(
                    "injected publication interruption",
                    code="compile_cache_operation_failed",
                ),
            ),
            self.assertRaises(RunpodLocalError),
        ):
            fixture.author_candidate()
        author_layout = fixture.layouts[0]
        persistent_root = author_layout.localize(fixture.contract["persistent_root"])
        staging_root = persistent_root.parent / (
            f".{fixture.contract['cache_id']}.candidate-staging"
        )
        bundle = staging_root / BUNDLE_NAME
        self.assertTrue(bundle.exists())
        original_size = bundle.stat().st_size
        bundle.chmod(0o600)
        with bundle.open("r+b") as output:
            output.truncate(original_size // 2)
        remaining_archive_bytes = original_size - bundle.stat().st_size
        remaining_document_bytes = 0
        for name in (MANIFEST_NAME, AUTHORED_NAME):
            document = staging_root / name
            original_document_bytes = document.stat().st_size
            document.chmod(0o600)
            with document.open("r+b") as output:
                output.truncate(original_document_bytes // 2)
            remaining_document_bytes += (
                original_document_bytes - document.stat().st_size
            )
        available_bytes = (
            remaining_archive_bytes
            + remaining_document_bytes
            + AUTHOR_PUBLICATION_RESERVE_BYTES
        )

        with mock.patch(
            "service_runtime.compile_cache_files.os.statvfs",
            return_value=mock.Mock(f_bavail=available_bytes, f_frsize=1),
        ):
            sealed = seal_compile_cache_candidate(
                contract=fixture.contract,
                layout=author_layout,
                measurement_path=author_layout.session_root / "author-measurement.json",
            )

        self.assertEqual(
            sealed["state"],
            "candidate-requires-distinct-boot-proof",
        )
        self.assertEqual(sealed["bundle_bytes"], original_size)
        self.assertFalse(staging_root.exists())

    def test_resumption_rejects_staging_larger_than_prediction(self):
        fixture = CacheFixture(self)
        with (
            mock.patch(
                "service_runtime.compile_cache_stage.publish_directory_noreplace",
                side_effect=RunpodLocalError(
                    "injected publication interruption",
                    code="compile_cache_operation_failed",
                ),
            ),
            self.assertRaises(RunpodLocalError),
        ):
            fixture.author_candidate()
        author_layout = fixture.layouts[0]
        persistent_root = author_layout.localize(fixture.contract["persistent_root"])
        staging_root = persistent_root.parent / (
            f".{fixture.contract['cache_id']}.candidate-staging"
        )
        bundle = staging_root / BUNDLE_NAME
        bundle.chmod(0o600)
        with bundle.open("ab") as output:
            output.write(b"unexpected-tail")

        with (
            mock.patch.object(
                compile_cache_stage,
                "preflight_compile_cache_headroom",
            ) as preflight,
            self.assertRaises(RunpodLocalError),
        ):
            seal_compile_cache_candidate(
                contract=fixture.contract,
                layout=author_layout,
                measurement_path=author_layout.session_root / "author-measurement.json",
            )

        preflight.assert_not_called()

    def test_volume_lease_serializes_distinct_cache_publications(self):
        first = CacheFixture(self)
        second = CacheFixture(self)
        second.workspace = first.workspace
        second.contract = contract_for(closure_digit="6")
        release_first = threading.Event()
        first_headroom = threading.Event()
        second_headroom = threading.Event()
        second_volume_attempt = threading.Event()
        outcomes: dict[str, object] = {}
        volume_lock = first.workspace / (
            compile_cache_stage._VOLUME_PUBLICATION_LOCK_NAME
        )
        real_open_advisory_lock = compile_cache_stage.open_advisory_lock
        real_preflight = compile_cache_stage.preflight_compile_cache_headroom

        class ObservedLock:
            def __init__(self, path: pathlib.Path, inner: object) -> None:
                self.path = path
                self.inner = inner

            def exclusive(self, *, nonblocking: bool = False) -> bool:
                if (
                    self.path == volume_lock
                    and threading.current_thread().name == "second-author"
                ):
                    second_volume_attempt.set()
                return self.inner.exclusive(nonblocking=nonblocking)

            def __enter__(self) -> ObservedLock:
                return self

            def __exit__(self, *_: object) -> None:
                self.inner.close()

        def observed_open_advisory_lock(
            path: pathlib.Path,
            *,
            create: bool,
        ) -> ObservedLock:
            return ObservedLock(
                path,
                real_open_advisory_lock(path, create=create),
            )

        def observed_preflight(**arguments: object) -> dict[str, object]:
            if arguments["purpose"] == "author-candidate-publication":
                if threading.current_thread().name == "first-author":
                    first_headroom.set()
                    release_first.wait()
                else:
                    second_headroom.set()
            return real_preflight(**arguments)

        def author(label: str, fixture: CacheFixture) -> None:
            try:
                outcomes[label] = fixture.author_candidate()
            except BaseException as error:
                outcomes[label] = error

        with (
            mock.patch.object(
                compile_cache_stage,
                "open_advisory_lock",
                side_effect=observed_open_advisory_lock,
            ),
            mock.patch.object(
                compile_cache_stage,
                "preflight_compile_cache_headroom",
                side_effect=observed_preflight,
            ),
        ):
            first_thread = threading.Thread(
                target=author,
                args=("first", first),
                name="first-author",
            )
            first_thread.start()
            first_headroom.wait()
            second_thread = threading.Thread(
                target=author,
                args=("second", second),
                name="second-author",
            )
            second_thread.start()
            second_volume_attempt.wait()
            try:
                self.assertFalse(second_headroom.is_set())
            finally:
                release_first.set()
            first_thread.join()
            second_thread.join()

        self.assertTrue(second_headroom.is_set())
        for label in ("first", "second"):
            if isinstance(outcomes[label], BaseException):
                raise outcomes[label]

    def test_new_author_quarantines_interrupted_candidate_without_unlink(self):
        fixture = CacheFixture(self)
        with (
            mock.patch(
                "service_runtime.compile_cache_stage.publish_directory_noreplace",
                side_effect=RunpodLocalError(
                    "injected publication interruption",
                    code="compile_cache_operation_failed",
                ),
            ),
            self.assertRaises(RunpodLocalError),
        ):
            fixture.author_candidate()
        new_layout = fixture.layout("new-author-after-interruption")

        prepared = prepare_compile_cache_author(
            contract=fixture.contract,
            layout=new_layout,
            boot_id=REQUIRE_BOOT_ID,
        )

        quarantined = prepared["quarantined_candidate_staging"]
        self.assertIsInstance(quarantined, str)
        self.assertTrue(new_layout.localize(quarantined).exists())

    def test_author_bundle_is_deterministic_for_the_same_cache_tree(self):
        first = CacheFixture(self)
        _, first_sealed, _ = first.author_candidate()
        second = CacheFixture(self)
        _, second_sealed, _ = second.author_candidate()

        self.assertEqual(
            first_sealed["bundle_sha256"],
            second_sealed["bundle_sha256"],
        )
        self.assertEqual(first_sealed["bundle_bytes"], second_sealed["bundle_bytes"])

    def test_author_requires_exact_empty_no_clobber_state(self):
        fixture = CacheFixture(self)
        layout = fixture.layout("author-session")
        first = prepare_compile_cache_author(
            contract=fixture.contract,
            layout=layout,
            boot_id=AUTHOR_BOOT_ID,
        )
        self.assertEqual(first["schema_version"], COMPILE_CACHE_PREPARATION_SCHEMA)
        with self.assertRaises(RunpodLocalError):
            prepare_compile_cache_author(
                contract=fixture.contract,
                layout=layout,
                boot_id=AUTHOR_BOOT_ID,
            )

    def test_inventory_rejects_symlinks_and_hardlinks(self):
        fixture = CacheFixture(self)
        layout = fixture.layout("author-session")
        prepare_compile_cache_author(
            contract=fixture.contract,
            layout=layout,
            boot_id=AUTHOR_BOOT_ID,
        )
        local_root = layout.localize(fixture.contract["local_root"])
        target = local_root / "cuda" / "target"
        target.write_bytes(b"target")
        target.chmod(0o600)
        (local_root / "cuda" / "link").symlink_to(target)
        with self.assertRaises(RunpodLocalError):
            inventory_compile_cache(local_root)

        # A separate fixture avoids cleanup as part of the production contract.
        second = CacheFixture(self)
        second_layout = second.layout("author-session")
        prepare_compile_cache_author(
            contract=second.contract,
            layout=second_layout,
            boot_id=AUTHOR_BOOT_ID,
        )
        second_root = second_layout.localize(second.contract["local_root"])
        source = second_root / "cuda" / "source"
        source.write_bytes(b"source")
        source.chmod(0o600)
        os.link(source, second_root / "cuda" / "hardlink")
        with self.assertRaises(RunpodLocalError):
            inventory_compile_cache(second_root)


class CompileCacheProofTest(unittest.TestCase):
    def test_stage_headroom_counts_only_bytes_created_on_ephemeral_disk(self):
        fixture = CacheFixture(self)
        _, _, inventory = fixture.author_candidate()
        proof_layout = fixture.layout("proof-exact-headroom")
        available_bytes = STAGED_CACHE_GROWTH_RESERVE_BYTES + inventory["total_bytes"]
        statvfs = mock.Mock(f_bavail=available_bytes, f_frsize=1)

        with mock.patch(
            "service_runtime.compile_cache_files.os.statvfs",
            return_value=statvfs,
        ):
            staged = stage_compile_cache(
                contract=fixture.contract,
                layout=proof_layout,
                boot_id=REQUIRE_BOOT_ID,
                source="candidate-proof",
            )

        self.assertEqual(staged["headroom"]["archive_bytes"], 0)
        self.assertEqual(
            staged["headroom"]["required_bytes"],
            available_bytes,
        )

    def test_stage_headroom_failure_precedes_local_cache_creation(self):
        fixture = CacheFixture(self)
        fixture.author_candidate()
        proof_layout = fixture.layout("proof-headroom-refusal")
        statvfs = mock.Mock(f_bavail=1, f_frsize=4096)

        with (
            mock.patch(
                "service_runtime.compile_cache_files.os.statvfs",
                return_value=statvfs,
            ),
            self.assertRaises(RunpodLocalError),
        ):
            stage_compile_cache(
                contract=fixture.contract,
                layout=proof_layout,
                boot_id=REQUIRE_BOOT_ID,
                source="candidate-proof",
            )

        self.assertFalse(
            os.path.lexists(proof_layout.localize(fixture.contract["local_root"]))
        )

    def test_distinct_boot_proof_accepts_then_stages_exact_archive(self):
        fixture = CacheFixture(self)
        _, _, inventory = fixture.author_candidate()
        proof_layout, proof_stage = fixture.proof_stage()

        self.assertEqual(proof_stage["source"], "candidate-proof")
        self.assertEqual(
            proof_stage["state"],
            "verified-at-publication-mutable-runtime-cache",
        )
        verified = verify_compile_cache_stage(
            contract=fixture.contract,
            layout=proof_layout,
            boot_id=REQUIRE_BOOT_ID,
        )
        self.assertEqual(
            verified.receipt["files_sha256"],
            inventory["sha256"],
        )

        accepted = fixture.accept(proof_layout, inventory)
        self.assertEqual(
            accepted["schema_version"],
            COMPILE_CACHE_ACCEPTANCE_SCHEMA,
        )
        self.assertEqual(accepted["state"], "accepted")
        persistent = load_persistent_compile_cache(
            contract=fixture.contract,
            layout=proof_layout,
            require_accepted=True,
        )
        self.assertEqual(persistent.state, "accepted")
        self.assertEqual(
            {entry.name for entry in persistent.root.iterdir()},
            {BUNDLE_NAME, MANIFEST_NAME, AUTHORED_NAME, ACCEPTED_NAME},
        )
        retried = fixture.accept(proof_layout, inventory)
        self.assertEqual(retried["action"], "reuse-accepted-proof")

        accepted_layout = fixture.layout("accepted-session")
        staged = stage_compile_cache(
            contract=fixture.contract,
            layout=accepted_layout,
            boot_id=ACCEPTED_BOOT_ID,
        )
        self.assertEqual(staged["source"], "accepted")
        self.assertEqual(staged["inventory_sha256"], inventory["sha256"])
        self.assertEqual(
            inventory_compile_cache(
                accepted_layout.localize(fixture.contract["local_root"])
            ),
            inventory,
        )

    def test_forced_0777_volume_supports_full_cache_publication_lifecycle(self):
        fixture = CacheFixture(self)
        author_layout, sealed, inventory = fixture.author_candidate(
            force_network_volume_modes=True,
        )

        self.assertEqual(
            sealed["state"],
            "candidate-requires-distinct-boot-proof",
        )
        self.assertEqual(stat.S_IMODE(fixture.workspace.stat().st_mode), 0o777)
        candidate = load_persistent_compile_cache(
            contract=fixture.contract,
            layout=author_layout,
            require_accepted=False,
        )
        self.assertEqual(candidate.state, "candidate")
        self.assertEqual(stat.S_IMODE(candidate.root.stat().st_mode), 0o777)

        proof_layout, proof_stage = fixture.proof_stage()
        self.assertEqual(proof_stage["source"], "candidate-proof")
        fixture.force_network_volume_directory_modes()
        accepted = fixture.accept(proof_layout, inventory)
        self.assertEqual(accepted["state"], "accepted")

        fixture.force_network_volume_directory_modes()
        accepted_layout = fixture.layout("forced-mode-accepted-session")
        staged = stage_compile_cache(
            contract=fixture.contract,
            layout=accepted_layout,
            boot_id=ACCEPTED_BOOT_ID,
        )
        self.assertEqual(staged["source"], "accepted")
        self.assertEqual(staged["inventory_sha256"], inventory["sha256"])

    def test_candidate_cannot_be_normal_stage_or_same_boot_proof(self):
        fixture = CacheFixture(self)
        fixture.author_candidate()
        accepted_layout = fixture.layout("normal-stage")
        with self.assertRaises(RunpodLocalError):
            stage_compile_cache(
                contract=fixture.contract,
                layout=accepted_layout,
                boot_id=REQUIRE_BOOT_ID,
            )

        same_boot_layout = fixture.layout("same-boot")
        with self.assertRaises(RunpodLocalError):
            stage_compile_cache(
                contract=fixture.contract,
                layout=same_boot_layout,
                boot_id=AUTHOR_BOOT_ID,
                source="candidate-proof",
            )

    def test_acceptance_rejects_cache_mutation_and_wrong_load_evidence(self):
        fixture = CacheFixture(self)
        _, _, inventory = fixture.author_candidate()
        proof_layout, _ = fixture.proof_stage()
        artifact = proof_layout.localize(fixture.contract["local_root"]).joinpath(
            *pathlib.PurePosixPath(PRODUCED_ARTIFACT).parts
        )
        artifact.write_bytes(b"mutated")
        artifact.chmod(0o600)
        with self.assertRaises(RunpodLocalError):
            fixture.accept(proof_layout, inventory)

        second = CacheFixture(self)
        _, _, second_inventory = second.author_candidate()
        second_layout, _ = second.proof_stage()
        with self.assertRaises(RunpodLocalError):
            second.accept(
                second_layout,
                second_inventory,
                loaded=[SECONDARY_ARTIFACT],
            )

    def test_acceptance_allows_equivalent_service_deployment(self):
        fixture = CacheFixture(self)
        _, _, inventory = fixture.author_candidate()
        proof_layout, _ = fixture.proof_stage()
        accepted = fixture.accept(
            proof_layout,
            inventory,
            service_manifest_sha256="b" * 64,
        )
        self.assertEqual(accepted["state"], "accepted")

    def test_acceptance_allows_mutable_runtime_scratch(self):
        fixture = CacheFixture(self)
        _, _, inventory = fixture.author_candidate()
        proof_layout, _ = fixture.proof_stage()
        scratch = (
            proof_layout.localize(fixture.contract["local_root"])
            / "vllm"
            / "runtime-autotune.bin"
        )
        scratch.write_bytes(b"mutable post-load scratch")
        scratch.chmod(0o600)

        accepted = fixture.accept(proof_layout, inventory)

        self.assertEqual(accepted["state"], "accepted")

    def test_acceptance_rejects_slow_or_non_improving_startup(self):
        slow = CacheFixture(self)
        _, _, slow_inventory = slow.author_candidate()
        slow_layout, _ = slow.proof_stage()
        with self.assertRaises(RunpodLocalError):
            slow.accept(
                slow_layout,
                slow_inventory,
                ready_monotonic_ns=5 * 60 * 1_000_000_000 + 11,
            )

        unimproved = CacheFixture(self)
        _, _, unimproved_inventory = unimproved.author_candidate()
        unimproved_layout, _ = unimproved.proof_stage()
        with self.assertRaises(RunpodLocalError):
            unimproved.accept(
                unimproved_layout,
                unimproved_inventory,
                ready_monotonic_ns=50,
            )

    def test_persistent_archive_tampering_is_detected_before_staging(self):
        fixture = CacheFixture(self)
        author_layout, _, _ = fixture.author_candidate()
        generation = load_persistent_compile_cache(
            contract=fixture.contract,
            layout=author_layout,
            require_accepted=False,
        )
        generation.root.chmod(0o700)
        bundle = generation.root / BUNDLE_NAME
        bundle.chmod(0o600)
        with bundle.open("ab") as output:
            output.write(b"tamper")
        bundle.chmod(0o444)
        generation.root.chmod(0o500)

        with self.assertRaises(RunpodLocalError):
            load_persistent_compile_cache(
                contract=fixture.contract,
                layout=author_layout,
                require_accepted=False,
            )


class CompileCacheGenericityTest(unittest.TestCase):
    def test_contract_rejects_physical_gpu_identity(self):
        fixture = CacheFixture(self)
        changed = copy.deepcopy(fixture.contract)
        changed["identity"]["gpu"]["uuid"] = "GPU-secret-host-identity"
        layout = fixture.layout("author-session")
        with self.assertRaises(RunpodLocalError):
            prepare_compile_cache_author(
                contract=changed,
                layout=layout,
                boot_id=AUTHOR_BOOT_ID,
            )

    def test_two_model_closures_use_the_same_implementation(self):
        fixture = CacheFixture(self)
        first_layout = fixture.layout("first-service")
        first_contract = contract_for(closure_digit="5", launch_digit="6")
        second_contract = contract_for(closure_digit="7", launch_digit="8")
        second_layout = fixture.layout("second-service")

        first = prepare_compile_cache_author(
            contract=first_contract,
            layout=first_layout,
            boot_id=AUTHOR_BOOT_ID,
        )
        second = prepare_compile_cache_author(
            contract=second_contract,
            layout=second_layout,
            boot_id=AUTHOR_BOOT_ID,
        )

        self.assertNotEqual(first["cache_id"], second["cache_id"])
        self.assertEqual(first["action"], second["action"])
        self.assertEqual(
            first["state"],
            second["state"],
        )

    def test_ephemeral_mode_never_requires_or_publishes_a_volume_bundle(self):
        fixture = CacheFixture(self)
        layout = fixture.layout("ephemeral-service")
        prepared = prepare_compile_cache_ephemeral(
            contract=fixture.contract,
            layout=layout,
            boot_id=AUTHOR_BOOT_ID,
        )

        self.assertEqual(prepared["action"], "prepare-ephemeral")
        self.assertEqual(prepared["state"], "mutable-empty-ephemeral-root")
        self.assertFalse(
            os.path.lexists(layout.localize(fixture.contract["persistent_root"]))
        )


if __name__ == "__main__":
    unittest.main()
