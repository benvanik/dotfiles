from __future__ import annotations

import hashlib
import json
import os
import pathlib
import stat
import subprocess
import tempfile
import unittest
from typing import Any
from unittest import mock

from runpod_local.errors import RunpodLocalError

import runpod.service_runtime.snapshot_stager as snapshot_stager
from runpod.service_runtime.layout import RuntimeLayout
from runpod.service_runtime.snapshot_stage import verify_snapshot_stage
from runpod.service_runtime.snapshot_stager import (
    HUGGINGFACE_CACHE_FREE_SPACE_RESERVE_BYTES,
    HUGGINGFACE_CACHE_WRITER_LOCK,
    REMOTE_HUGGINGFACE_TOKEN,
    SNAPSHOT_STAGE_FREE_SPACE_RESERVE_BYTES,
    stage_huggingface_snapshot,
)
from runpod.service_runtime.state import open_advisory_lock


BOOT_ID = "11111111-2222-4333-8444-555555555555"


class FilesystemStatus:
    def __init__(self, available_bytes: int) -> None:
        self.f_frsize = 1
        self.f_bavail = available_bytes


def identity_for(payload: bytes, algorithm: str) -> str:
    if algorithm == "sha256":
        return hashlib.sha256(payload).hexdigest()
    if algorithm == "git-blob-sha1":
        hasher = hashlib.sha1()
        hasher.update(f"blob {len(payload)}\0".encode("ascii"))
        hasher.update(payload)
        return hasher.hexdigest()
    raise ValueError(algorithm)


def closure_for(
    *,
    repository: str,
    revision_digit: str,
    checkpoint: str,
    members: list[tuple[str, bytes, str, str]],
) -> tuple[dict[str, Any], dict[str, bytes]]:
    revision = revision_digit * 40
    payloads = {path: payload for path, payload, _, _ in members}
    files = [
        {
            "path": path,
            "bytes": len(payload),
            "role": role,
            "identity": {
                "algorithm": algorithm,
                "digest": identity_for(payload, algorithm),
            },
        }
        for path, payload, algorithm, role in sorted(members)
    ]
    source = {
        "kind": "huggingface",
        "repository": repository,
        "revision": revision,
    }
    checkpoint_record = {
        "requested_selector": checkpoint,
        "resolved_index": None,
        "weight_files": [checkpoint],
    }
    identity = {
        "schema_version": "runpod.huggingface-closure-identity.v1",
        "source": source,
        "checkpoint": checkpoint_record,
        "files": files,
    }
    closure_sha256 = hashlib.sha256(
        (
            json.dumps(
                identity,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            + "\n"
        ).encode("ascii")
    ).hexdigest()
    return (
        {
            "schema_version": "runpod.huggingface-closure.v1",
            "source": source,
            "checkpoint": checkpoint_record,
            "files": files,
            "file_count": len(files),
            "total_bytes": sum(len(payload) for payload in payloads.values()),
            "closure_sha256": closure_sha256,
        },
        payloads,
    )


class SnapshotFixture:
    def __init__(self, testcase: unittest.TestCase) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        testcase.addCleanup(self.temporary.cleanup)
        root = pathlib.Path(self.temporary.name)
        self.session = root / "session"
        self.workspace = root / "workspace"
        self.session.mkdir(mode=0o700)
        self.workspace.mkdir(mode=0o700)
        self.snapshots = self.session / "model-snapshots"
        self.snapshots.mkdir(mode=0o700)
        self.layout = RuntimeLayout(
            session_root=self.session,
            workspace_root=self.workspace,
        )

    def stage_paths(
        self,
        closure: dict[str, Any],
    ) -> tuple[pathlib.PurePosixPath, pathlib.Path, pathlib.Path]:
        digest = closure["closure_sha256"]
        canonical = pathlib.PurePosixPath(
            f"/root/runpod-session/model-snapshots/{digest}"
        )
        return (
            canonical,
            self.snapshots / digest,
            self.snapshots / f"{digest}.stage.json",
        )

    def populate_cache(
        self,
        closure: dict[str, Any],
        payloads: dict[str, bytes],
    ) -> pathlib.Path:
        repository = closure["source"]["repository"]
        revision = closure["source"]["revision"]
        model_root = (
            self.workspace
            / ".cache"
            / "huggingface"
            / "hub"
            / f"models--{repository.replace('/', '--')}"
        )
        blobs = model_root / "blobs"
        snapshot = model_root / "snapshots" / revision
        blobs.mkdir(parents=True, mode=0o700, exist_ok=True)
        snapshot.mkdir(parents=True, mode=0o700, exist_ok=True)
        for parent in (
            self.workspace,
            *model_root.parents,
            model_root,
            blobs,
            snapshot,
        ):
            if self.workspace == parent or self.workspace in parent.parents:
                parent.chmod(0o700)
        for record in closure["files"]:
            digest = record["identity"]["digest"]
            blob = blobs / digest
            if not blob.exists():
                blob.write_bytes(payloads[record["path"]])
                blob.chmod(0o600)
            link = snapshot.joinpath(*pathlib.PurePosixPath(record["path"]).parts)
            link.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            link.parent.chmod(0o700)
            target = os.path.relpath(blob, link.parent)
            link.symlink_to(target)
        return snapshot

    def force_network_volume_directory_modes(self) -> None:
        """Model RunPod's network volume, which reports every directory 0777."""

        for current, _, _ in os.walk(self.workspace, followlinks=False):
            pathlib.Path(current).chmod(0o777)

    def stage(
        self,
        closure: dict[str, Any],
        **kwargs: Any,
    ):
        canonical, root, receipt = self.stage_paths(closure)
        boot_id = kwargs.pop("boot_id", BOOT_ID)
        return stage_huggingface_snapshot(
            closure=closure,
            canonical_snapshot_root=canonical,
            local_snapshot_root=root,
            receipt_path=receipt,
            layout=self.layout,
            boot_id=boot_id,
            **kwargs,
        )


class GenericSnapshotStagerTest(unittest.TestCase):
    def test_forced_0777_network_volume_still_stages_hash_verified_content(self):
        fixture = SnapshotFixture(self)
        closure, payloads = closure_for(
            repository="fixture-lab/forced-mode-model",
            revision_digit="0",
            checkpoint="model.safetensors",
            members=[
                (
                    "config.json",
                    b'{"model_type":"forced-mode"}',
                    "git-blob-sha1",
                    "snapshot",
                ),
                (
                    "model.safetensors",
                    b"forced-mode-weights",
                    "sha256",
                    "checkpoint-weight",
                ),
            ],
        )
        fixture.populate_cache(closure, payloads)
        fixture.force_network_volume_directory_modes()

        result = fixture.stage(closure, allow_download=False)

        self.assertEqual(result.disposition, "created")
        self.assertEqual(result.cache_source, "network-volume")
        _, root, _ = fixture.stage_paths(closure)
        self.assertEqual(
            (root / "model.safetensors").read_bytes(),
            payloads["model.safetensors"],
        )
        self.assertEqual(
            stat.S_IMODE((fixture.workspace / ".cache" / "huggingface").stat().st_mode),
            0o777,
        )

    def test_forced_0777_network_volume_does_not_weaken_content_identity(self):
        fixture = SnapshotFixture(self)
        closure, payloads = closure_for(
            repository="fixture-lab/untrusted-content-model",
            revision_digit="a",
            checkpoint="model.safetensors",
            members=[
                (
                    "model.safetensors",
                    b"expected-weight-content",
                    "sha256",
                    "checkpoint-weight",
                )
            ],
        )
        snapshot = fixture.populate_cache(closure, payloads)
        blob = (snapshot / "model.safetensors").resolve()
        blob.write_bytes(b"tampered-weight-content")
        blob.chmod(0o666)
        fixture.force_network_volume_directory_modes()

        with self.assertRaises(RunpodLocalError) as caught:
            fixture.stage(closure, allow_download=False)

        self.assertEqual(
            caught.exception.code,
            "huggingface_snapshot_content_mismatch",
        )

    def test_two_different_closures_use_the_same_generic_stager(self):
        fixture = SnapshotFixture(self)
        first, first_payloads = closure_for(
            repository="fixture-lab/alpha-model",
            revision_digit="1",
            checkpoint="weights/model.safetensors",
            members=[
                (
                    "config.json",
                    b'{"model_type":"alpha"}',
                    "git-blob-sha1",
                    "snapshot",
                ),
                (
                    "weights/model.safetensors",
                    b"alpha-weights",
                    "sha256",
                    "checkpoint-weight",
                ),
            ],
        )
        second, second_payloads = closure_for(
            repository="another-lab/beta-model",
            revision_digit="2",
            checkpoint="model.safetensors",
            members=[
                (
                    "generation_config.json",
                    b'{"temperature":0.7}',
                    "git-blob-sha1",
                    "snapshot",
                ),
                (
                    "model.safetensors",
                    b"beta-weights",
                    "sha256",
                    "checkpoint-weight",
                ),
                (
                    "tokenizer.json",
                    b'{"version":"1.0"}',
                    "sha256",
                    "snapshot",
                ),
            ],
        )
        fixture.populate_cache(first, first_payloads)
        fixture.populate_cache(second, second_payloads)

        first_result = fixture.stage(first, allow_download=False)
        second_result = fixture.stage(second, allow_download=False)

        self.assertEqual(first_result.disposition, "created")
        self.assertEqual(second_result.disposition, "created")
        self.assertEqual(first_result.cache_source, "network-volume")
        self.assertEqual(second_result.cache_source, "network-volume")
        for closure, payloads in (
            (first, first_payloads),
            (second, second_payloads),
        ):
            canonical, root, receipt = fixture.stage_paths(closure)
            verified = verify_snapshot_stage(
                closure=closure,
                canonical_snapshot_root=canonical,
                local_snapshot_root=root,
                receipt_path=receipt,
                boot_id=BOOT_ID,
            )
            self.assertEqual(
                [record["path"] for record in verified.receipt["files"]],
                [record["path"] for record in closure["files"]],
            )
            for path, payload in payloads.items():
                member = root.joinpath(*pathlib.PurePosixPath(path).parts)
                self.assertEqual(member.read_bytes(), payload)
                self.assertEqual(stat.S_IMODE(member.stat().st_mode), 0o400)

    def test_resume_reuses_complete_members_and_rewrites_owned_partial(self):
        fixture = SnapshotFixture(self)
        closure, payloads = closure_for(
            repository="fixture-lab/resume-model",
            revision_digit="3",
            checkpoint="weights/model.safetensors",
            members=[
                (
                    "config.json",
                    b'{"model_type":"resume"}',
                    "git-blob-sha1",
                    "snapshot",
                ),
                (
                    "weights/model.safetensors",
                    b"complete-weight-payload",
                    "sha256",
                    "checkpoint-weight",
                ),
            ],
        )
        fixture.populate_cache(closure, payloads)
        _, final_root, _ = fixture.stage_paths(closure)
        staging_root = final_root.parent / f".{closure['closure_sha256']}.staging"
        staging_root.mkdir(mode=0o700)
        complete = staging_root / "config.json"
        complete.write_bytes(payloads["config.json"])
        complete.chmod(0o400)
        complete_inode = complete.stat().st_ino
        partial = staging_root / "weights" / "model.safetensors"
        partial.parent.mkdir(mode=0o700)
        partial.write_bytes(b"interrupted")
        partial.chmod(0o600)

        result = fixture.stage(closure, allow_download=False)

        self.assertEqual(result.disposition, "created")
        self.assertEqual(
            (final_root / "config.json").stat().st_ino,
            complete_inode,
        )
        self.assertEqual(
            (final_root / "weights" / "model.safetensors").read_bytes(),
            payloads["weights/model.safetensors"],
        )

    def test_resume_capacity_counts_only_uncopied_partial_bytes(self):
        fixture = SnapshotFixture(self)
        closure, payloads = closure_for(
            repository="fixture-lab/capacity-resume-model",
            revision_digit="b",
            checkpoint="model.safetensors",
            members=[
                (
                    "model.safetensors",
                    b"complete-weight-payload",
                    "sha256",
                    "checkpoint-weight",
                )
            ],
        )
        fixture.populate_cache(closure, payloads)
        _, final_root, _ = fixture.stage_paths(closure)
        staging_root = final_root.parent / f".{closure['closure_sha256']}.staging"
        staging_root.mkdir(mode=0o700)
        partial_payload = b"interrupted"
        partial = staging_root / "model.safetensors"
        partial.write_bytes(partial_payload)
        partial.chmod(0o600)
        remaining_bytes = len(payloads["model.safetensors"]) - len(partial_payload)

        result = fixture.stage(
            closure,
            allow_download=False,
            filesystem_status_reader=lambda _: FilesystemStatus(
                SNAPSHOT_STAGE_FREE_SPACE_RESERVE_BYTES + remaining_bytes
            ),
        )

        self.assertEqual(result.disposition, "created")
        self.assertEqual(
            (final_root / "model.safetensors").read_bytes(),
            payloads["model.safetensors"],
        )

    def test_ephemeral_capacity_failure_precedes_member_copy(self):
        fixture = SnapshotFixture(self)
        closure, payloads = closure_for(
            repository="fixture-lab/ephemeral-capacity-model",
            revision_digit="c",
            checkpoint="model.safetensors",
            members=[
                (
                    "model.safetensors",
                    b"ephemeral-capacity",
                    "sha256",
                    "checkpoint-weight",
                )
            ],
        )
        fixture.populate_cache(closure, payloads)
        _, final_root, _ = fixture.stage_paths(closure)

        with (
            mock.patch.object(
                snapshot_stager,
                "_copy_member",
                side_effect=AssertionError("copy must not begin"),
            ),
            self.assertRaises(RunpodLocalError) as caught,
        ):
            fixture.stage(
                closure,
                allow_download=False,
                filesystem_status_reader=lambda _: FilesystemStatus(
                    SNAPSHOT_STAGE_FREE_SPACE_RESERVE_BYTES + closure["total_bytes"] - 1
                ),
            )

        self.assertEqual(
            caught.exception.code,
            "insufficient_huggingface_snapshot_stage_space",
        )
        staging_root = final_root.parent / f".{closure['closure_sha256']}.staging"
        self.assertEqual(list(staging_root.iterdir()), [])

    def test_persistent_capacity_failure_precedes_download_and_copy(self):
        fixture = SnapshotFixture(self)
        closure, _ = closure_for(
            repository="fixture-lab/persistent-capacity-model",
            revision_digit="d",
            checkpoint="model.safetensors",
            members=[
                (
                    "model.safetensors",
                    b"persistent-capacity",
                    "sha256",
                    "checkpoint-weight",
                )
            ],
        )
        capacities = iter(
            (
                FilesystemStatus(
                    SNAPSHOT_STAGE_FREE_SPACE_RESERVE_BYTES + closure["total_bytes"]
                ),
                FilesystemStatus(
                    HUGGINGFACE_CACHE_FREE_SPACE_RESERVE_BYTES
                    + closure["total_bytes"]
                    - 1
                ),
            )
        )
        runner = mock.Mock(side_effect=AssertionError("download must not begin"))

        with (
            mock.patch.object(
                snapshot_stager,
                "_copy_member",
                side_effect=AssertionError("copy must not begin"),
            ),
            self.assertRaises(RunpodLocalError) as caught,
        ):
            fixture.stage(
                closure,
                command_runner=runner,
                filesystem_status_reader=lambda _: next(capacities),
            )

        self.assertEqual(
            caught.exception.code,
            "insufficient_huggingface_cache_space",
        )
        runner.assert_not_called()

    def test_capacity_probe_failure_and_path_replacement_fail_closed(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                fixture = SnapshotFixture(self)
                closure, payloads = closure_for(
                    repository="fixture-lab/capacity-probe-model",
                    revision_digit="e",
                    checkpoint="model.safetensors",
                    members=[
                        (
                            "model.safetensors",
                            b"capacity-probe",
                            "sha256",
                            "checkpoint-weight",
                        )
                    ],
                )
                fixture.populate_cache(closure, payloads)
                _, final_root, _ = fixture.stage_paths(closure)
                staging_root = (
                    final_root.parent / f".{closure['closure_sha256']}.staging"
                )

                def filesystem_status(_: int) -> FilesystemStatus:
                    if not replacement:
                        raise OSError("statvfs unavailable")
                    moved = staging_root.with_name(f"{staging_root.name}.moved")
                    staging_root.rename(moved)
                    staging_root.mkdir(mode=0o700)
                    return FilesystemStatus(2**63)

                with self.assertRaises(RunpodLocalError) as caught:
                    fixture.stage(
                        closure,
                        allow_download=False,
                        filesystem_status_reader=filesystem_status,
                    )

                self.assertEqual(
                    caught.exception.code,
                    "huggingface_snapshot_capacity_unavailable",
                )

    def test_complete_root_recovers_only_its_missing_receipt(self):
        fixture = SnapshotFixture(self)
        closure, payloads = closure_for(
            repository="fixture-lab/recovery-model",
            revision_digit="4",
            checkpoint="model.safetensors",
            members=[
                (
                    "model.safetensors",
                    b"recoverable",
                    "sha256",
                    "checkpoint-weight",
                )
            ],
        )
        _, root, receipt = fixture.stage_paths(closure)
        root.mkdir(mode=0o700)
        member = root / "model.safetensors"
        member.write_bytes(payloads["model.safetensors"])
        member.chmod(0o400)

        result = fixture.stage(closure, allow_download=False)

        self.assertEqual(result.disposition, "receipt-recovered")
        self.assertTrue(receipt.is_file())
        self.assertEqual(member.read_bytes(), b"recoverable")

    def test_invalid_published_tree_is_never_replaced(self):
        fixture = SnapshotFixture(self)
        closure, _ = closure_for(
            repository="fixture-lab/collision-model",
            revision_digit="5",
            checkpoint="model.safetensors",
            members=[
                (
                    "model.safetensors",
                    b"expected",
                    "sha256",
                    "checkpoint-weight",
                )
            ],
        )
        _, root, _ = fixture.stage_paths(closure)
        root.mkdir(mode=0o700)
        sentinel = root / "model.safetensors"
        sentinel.write_bytes(b"must-not-change")
        sentinel.chmod(0o400)

        with self.assertRaises(RunpodLocalError):
            fixture.stage(closure, allow_download=False)

        self.assertEqual(sentinel.read_bytes(), b"must-not-change")

    def test_competing_empty_root_wins_without_being_replaced(self):
        fixture = SnapshotFixture(self)
        closure, payloads = closure_for(
            repository="fixture-lab/root-race-model",
            revision_digit="8",
            checkpoint="model.safetensors",
            members=[
                (
                    "model.safetensors",
                    b"root-race",
                    "sha256",
                    "checkpoint-weight",
                )
            ],
        )
        fixture.populate_cache(closure, payloads)
        _, final_root, _ = fixture.stage_paths(closure)
        real_rename = snapshot_stager._rename_no_replace

        def competing_rename(
            source: pathlib.Path,
            destination: pathlib.Path,
        ) -> None:
            if destination == final_root:
                destination.mkdir(mode=0o700)
            real_rename(source, destination)

        with (
            mock.patch.object(
                snapshot_stager,
                "_rename_no_replace",
                side_effect=competing_rename,
            ),
            self.assertRaises(RunpodLocalError) as caught,
        ):
            fixture.stage(closure, allow_download=False)

        self.assertEqual(
            caught.exception.code,
            "huggingface_snapshot_stage_collision",
        )
        self.assertEqual(list(final_root.iterdir()), [])
        staging_root = final_root.parent / f".{closure['closure_sha256']}.staging"
        self.assertEqual(
            (staging_root / "model.safetensors").read_bytes(),
            b"root-race",
        )

    def test_competing_receipt_wins_without_being_replaced(self):
        fixture = SnapshotFixture(self)
        closure, payloads = closure_for(
            repository="fixture-lab/receipt-race-model",
            revision_digit="9",
            checkpoint="model.safetensors",
            members=[
                (
                    "model.safetensors",
                    b"receipt-race",
                    "sha256",
                    "checkpoint-weight",
                )
            ],
        )
        fixture.populate_cache(closure, payloads)
        _, final_root, receipt = fixture.stage_paths(closure)
        real_rename = snapshot_stager._rename_no_replace
        competing_payload = b"competing receipt"

        def competing_rename(
            source: pathlib.Path,
            destination: pathlib.Path,
        ) -> None:
            if destination == receipt:
                destination.write_bytes(competing_payload)
                destination.chmod(0o600)
            real_rename(source, destination)

        with (
            mock.patch.object(
                snapshot_stager,
                "_rename_no_replace",
                side_effect=competing_rename,
            ),
            self.assertRaises(RunpodLocalError) as caught,
        ):
            fixture.stage(closure, allow_download=False)

        self.assertEqual(
            caught.exception.code,
            "huggingface_snapshot_stage_collision",
        )
        self.assertEqual(receipt.read_bytes(), competing_payload)
        self.assertEqual(
            (final_root / "model.safetensors").read_bytes(),
            b"receipt-race",
        )
        self.assertEqual(
            [
                path
                for path in receipt.parent.iterdir()
                if path.name.startswith(f".{receipt.name}.")
            ],
            [],
        )
        self.assertEqual(
            list(receipt.parent.glob(f".{receipt.name}.*")),
            [],
        )

    def test_old_boot_receipt_is_not_silently_reused(self):
        fixture = SnapshotFixture(self)
        closure, payloads = closure_for(
            repository="fixture-lab/boot-model",
            revision_digit="a",
            checkpoint="model.safetensors",
            members=[
                (
                    "model.safetensors",
                    b"boot-bound",
                    "sha256",
                    "checkpoint-weight",
                )
            ],
        )
        fixture.populate_cache(closure, payloads)
        fixture.stage(closure, allow_download=False)
        _, _, receipt = fixture.stage_paths(closure)
        original_receipt = receipt.read_bytes()

        with self.assertRaises(RunpodLocalError) as caught:
            fixture.stage(
                closure,
                allow_download=False,
                boot_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            )

        self.assertEqual(
            caught.exception.code,
            "invalid_huggingface_snapshot_stage",
        )
        self.assertEqual(receipt.read_bytes(), original_receipt)

    def test_source_link_must_name_the_exact_per_file_blob(self):
        fixture = SnapshotFixture(self)
        closure, payloads = closure_for(
            repository="fixture-lab/link-model",
            revision_digit="6",
            checkpoint="model.safetensors",
            members=[
                (
                    "model.safetensors",
                    b"linked",
                    "sha256",
                    "checkpoint-weight",
                )
            ],
        )
        snapshot = fixture.populate_cache(closure, payloads)
        link = snapshot / "model.safetensors"
        link.unlink()
        link.symlink_to("../../blobs/" + "0" * 64)

        with self.assertRaises(RunpodLocalError) as caught:
            fixture.stage(closure, allow_download=False)

        self.assertEqual(
            caught.exception.code,
            "unsafe_huggingface_snapshot_source",
        )

    def test_missing_cache_uses_scrubbed_token_path_download(self):
        fixture = SnapshotFixture(self)
        closure, payloads = closure_for(
            repository="fixture-lab/download-model",
            revision_digit="7",
            checkpoint="model.safetensors",
            members=[
                (
                    "config.json",
                    b'{"downloaded":true}',
                    "git-blob-sha1",
                    "snapshot",
                ),
                (
                    "model.safetensors",
                    b"downloaded-weights",
                    "sha256",
                    "checkpoint-weight",
                ),
            ],
        )
        token_path = fixture.layout.localize(REMOTE_HUGGINGFACE_TOKEN)
        token_path.parent.parent.mkdir(mode=0o700)
        token_path.parent.mkdir(mode=0o700)
        token_value = "hf_secret_that_must_not_escape"
        token_path.write_text(token_value, encoding="ascii")
        token_path.chmod(0o600)
        huggingface_cli = pathlib.Path(fixture.temporary.name) / "hf"
        huggingface_cli.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        huggingface_cli.chmod(0o755)
        calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

        def fake_runner(command: tuple[str, ...], **kwargs: Any) -> None:
            calls.append((command, kwargs))
            writer_lock_path = fixture.snapshots / HUGGINGFACE_CACHE_WRITER_LOCK
            with open_advisory_lock(
                writer_lock_path,
                create=False,
            ) as competing_writer:
                self.assertFalse(competing_writer.exclusive(nonblocking=True))
            if len(calls) == 1:
                fixture.populate_cache(closure, payloads)

        result = fixture.stage(
            closure,
            command_runner=fake_runner,
            huggingface_cli=huggingface_cli,
        )

        self.assertEqual(result.cache_source, "huggingface-download")
        self.assertEqual(result.authentication, "leased-token")
        self.assertEqual(len(calls), 1)
        command, options = calls[0]
        self.assertEqual(command[0], str(huggingface_cli))
        self.assertEqual(
            command[1:6],
            (
                "download",
                "--revision",
                closure["source"]["revision"],
                "--",
                "fixture-lab/download-model",
            ),
        )
        self.assertEqual(options["env"]["HF_TOKEN_PATH"], str(token_path))
        self.assertNotIn(token_value, "\0".join(command))
        self.assertNotIn(token_value, "\0".join(options["env"].values()))
        self.assertNotIn("HF_TOKEN", options["env"])
        self.assertIs(options["stdout"], subprocess.DEVNULL)

    def test_download_delimiter_prevents_option_like_member_reinterpretation(self):
        fixture = SnapshotFixture(self)
        closure, payloads = closure_for(
            repository="fixture-lab/option-member-model",
            revision_digit="f",
            checkpoint="-weights.safetensors",
            members=[
                (
                    "--local-dir",
                    b"option-shaped-loader-data",
                    "git-blob-sha1",
                    "snapshot",
                ),
                (
                    "-weights.safetensors",
                    b"option-shaped-weights",
                    "sha256",
                    "checkpoint-weight",
                ),
            ],
        )
        huggingface_cli = pathlib.Path(fixture.temporary.name) / "hf"
        huggingface_cli.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        huggingface_cli.chmod(0o755)
        commands: list[tuple[str, ...]] = []

        def fake_runner(command: tuple[str, ...], **_: Any) -> None:
            commands.append(command)
            fixture.populate_cache(closure, payloads)

        result = fixture.stage(
            closure,
            command_runner=fake_runner,
            huggingface_cli=huggingface_cli,
            filesystem_status_reader=lambda _: FilesystemStatus(2**63),
        )

        self.assertEqual(result.disposition, "created")
        self.assertEqual(
            commands,
            [
                (
                    str(huggingface_cli),
                    "download",
                    "--revision",
                    closure["source"]["revision"],
                    "--",
                    closure["source"]["repository"],
                    "--local-dir",
                    "-weights.safetensors",
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
