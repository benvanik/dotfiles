from __future__ import annotations

import fcntl
import json
import os
import pathlib
import threading
import tempfile
import unittest
from unittest import mock

import model_session.history as history_module
import model_session.lease as lease_module
import model_session.runs as runs_module
from model_session.errors import ModelSessionError
from model_session.history import (
    acquire_history_run_from_state,
    enumerate_history,
    prompt_fingerprint,
)
from model_session.lease import (
    RunSource,
    acquire_run_from_state,
    open_pi_session_at,
)
from model_session.profile import load_profile
from model_session.materialization import materialize_new_run


REVISION = "a" * 40
PI_TIMESTAMP = "2026-07-26T00:00:00.123Z"


class HistoryFixture:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.profile_root = root / "profiles" / "fixture"
        self.state_root = root / "state"
        self.project_root = root / "project"
        self.pi_root = root / "pi"
        self.profile_root.mkdir(parents=True)
        self.profile_root.chmod(0o755)
        self.project_root.mkdir()
        target = self.pi_root / "lib" / "pi" / "cli.js"
        target.parent.mkdir(parents=True)
        target.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        target.chmod(0o755)
        (self.pi_root / "bin").mkdir()
        node = self.pi_root / "bin" / "node"
        node.write_text("#!/bin/sh\necho v24.11.1\n", encoding="utf-8")
        node.chmod(0o755)
        (self.pi_root / "bin" / "pi").symlink_to("../lib/pi/cli.js")
        for path in (
            self.pi_root,
            self.pi_root / "bin",
            self.pi_root / "lib",
            target.parent,
        ):
            path.chmod(0o755)
        (self.profile_root / "AGENTS.md").write_text(
            "profile instructions\n",
            encoding="utf-8",
        )
        (self.profile_root / "SYSTEM.md").write_text(
            "system instructions\n",
            encoding="utf-8",
        )
        for name in ("AGENTS.md", "SYSTEM.md"):
            (self.profile_root / name).chmod(0o644)
        (self.profile_root / "profile.toml").write_text(
            f"""schema = "model-session.profile.v1"
profile_id = "fixture"
project_id = "project"
state_root = "{self.state_root}"
project_root = "{self.project_root}"

[model]
repository = "namespace/model"
revision = "{REVISION}"
context_tokens = 65536
max_output_tokens = 8192
kv_cache_dtype = "bf16"
max_sequences = 1
weight_format = "bf16"

[runtime]
provider = "fixture-provider"
model_id = "served-model"
reasoning = false
input_modalities = ["text"]

[pi]
installation_root = "{self.pi_root}"
executable = "bin/pi"
version = "0.82.1"
tools = ["read", "write", "edit", "bash"]
system_prompt_file = "SYSTEM.md"
""",
            encoding="utf-8",
        )
        (self.profile_root / "profile.toml").chmod(0o644)

    def profile(self):
        return load_profile(self.profile_root)

    @staticmethod
    def header(session_id: str, **updates) -> dict:
        value = {
            "type": "session",
            "version": 3,
            "id": session_id,
            "timestamp": PI_TIMESTAMP,
            "cwd": "/workspace",
        }
        value.update(updates)
        return value

    @staticmethod
    def pi_name(session_id: str, timestamp: str = PI_TIMESTAMP) -> str:
        return (
            timestamp.replace(":", "-").replace(".", "-")
            + f"_{session_id}.jsonl"
        )

    def write_pi_session(
        self,
        run,
        entries: list[dict],
        *,
        name: str | None = None,
        final_newline: bool = True,
    ) -> pathlib.Path:
        path = run.pi_sessions / (name or self.pi_name(run.session_id))
        content = "\n".join(json.dumps(entry) for entry in entries)
        if final_newline:
            content += "\n"
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
        return path


class ModelSessionHistoryTest(unittest.TestCase):
    def test_missing_state_is_empty_but_still_validates_the_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = HistoryFixture(pathlib.Path(directory))
            with enumerate_history(fixture.state_root, "fixture") as catalog:
                self.assertEqual(catalog.entries, ())
            self.assertFalse(fixture.state_root.exists())

            with self.assertRaises(ModelSessionError):
                enumerate_history("relative-state", "fixture")
            with self.assertRaises(ModelSessionError):
                enumerate_history(fixture.state_root, "INVALID/PROFILE")

    def test_catalog_orders_runs_and_transfers_exact_launch_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = HistoryFixture(pathlib.Path(directory))
            first = materialize_new_run(fixture.profile())
            second = materialize_new_run(fixture.profile())

            with enumerate_history(fixture.state_root, "fixture") as catalog:
                self.assertEqual(
                    [entry.session_id for entry in catalog.entries],
                    [second.session_id, first.session_id],
                )
                first_entry = next(
                    entry
                    for entry in catalog.entries
                    if entry.session_id == first.session_id
                )
                self.assertEqual(first_entry.title, "(empty session)")
                self.assertFalse(first_entry.active)
                self.assertIsNone(first_entry.pi_session_name)
                self.assertEqual(len(prompt_fingerprint(first)), 12)

                original_workspace = first.workspace.stat()
                moved = first.root.parent / f".moved-{first.session_id}"
                first.root.rename(moved)
                first.root.mkdir(mode=0o700)
                with catalog.acquire(first.session_id) as lease:
                    descriptor = lease.duplicate_source(RunSource.WORKSPACE)
                    try:
                        retained = os.fstat(descriptor)
                        self.assertEqual(
                            (retained.st_dev, retained.st_ino),
                            (
                                original_workspace.st_dev,
                                original_workspace.st_ino,
                            ),
                        )
                    finally:
                        os.close(descriptor)

    def test_catalog_orders_inactive_runs_by_last_pi_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = HistoryFixture(pathlib.Path(directory))
            older_run = materialize_new_run(fixture.profile())
            newer_run = materialize_new_run(fixture.profile())
            older_path = fixture.write_pi_session(
                older_run,
                [fixture.header(older_run.session_id)],
            )
            newer_path = fixture.write_pi_session(
                newer_run,
                [fixture.header(newer_run.session_id)],
            )
            same_second = 2_000_000_000
            os.utime(
                older_path,
                ns=(
                    same_second * 1_000_000_000 + 900_000_000,
                    same_second * 1_000_000_000 + 900_000_000,
                ),
            )
            os.utime(
                newer_path,
                ns=(
                    same_second * 1_000_000_000 + 100_000_000,
                    same_second * 1_000_000_000 + 100_000_000,
                ),
            )

            with enumerate_history(fixture.state_root, "fixture") as catalog:
                self.assertEqual(
                    [entry.session_id for entry in catalog.entries],
                    [older_run.session_id, newer_run.session_id],
                )
                self.assertTrue(
                    catalog.entries[0].updated_at.endswith(".900000Z")
                )
                self.assertTrue(
                    catalog.entries[1].updated_at.endswith(".100000Z")
                )

    def test_title_uses_latest_name_and_explicit_clear_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = HistoryFixture(pathlib.Path(directory))
            run = materialize_new_run(fixture.profile())
            fixture.write_pi_session(
                run,
                [
                    fixture.header(run.session_id),
                    {
                        "type": "message",
                        "message": {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "first\nrequest"}
                            ],
                        },
                    },
                    {"type": "session_info", "name": " named experiment "},
                ],
            )
            with enumerate_history(fixture.state_root, "fixture") as catalog:
                self.assertEqual(catalog.entries[0].title, "named experiment")

            path = run.pi_sessions / fixture.pi_name(run.session_id)
            with path.open("a", encoding="utf-8") as output:
                output.write(
                    json.dumps({"type": "session_info", "name": None}) + "\n"
                )
            with enumerate_history(fixture.state_root, "fixture") as catalog:
                self.assertEqual(catalog.entries[0].title, "first request")

    def test_title_sanitizes_terminal_and_unicode_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = HistoryFixture(pathlib.Path(directory))
            run = materialize_new_run(fixture.profile())
            fixture.write_pi_session(
                run,
                [
                    fixture.header(run.session_id),
                    {
                        "type": "session_info",
                        "name": (
                            "\x1b[2Jalpha\u202ebeta\u2066gamma"
                            "\u200bdelta\ue000omega"
                        ),
                    },
                ],
            )
            with enumerate_history(fixture.state_root, "fixture") as catalog:
                title = catalog.entries[0].title
            self.assertEqual(title, "[2Jalpha beta gamma delta omega")
            self.assertNotIn("\x1b", title)
            self.assertNotIn("\u202e", title)

    def test_header_and_filename_are_exactly_bound(self) -> None:
        invalid_cases = (
            (
                {"timestamp": "2026-07-26T00:00:00Z"},
                None,
            ),
            (
                {"timestamp": "2026-02-30T00:00:00.123Z"},
                None,
            ),
            (
                {"cwd": "/tmp"},
                None,
            ),
            (
                {"version": 2},
                None,
            ),
            (
                {"parentSession": "/host/session.jsonl"},
                None,
            ),
            (
                {},
                "garbage.jsonl",
            ),
        )
        for updates, name in invalid_cases:
            with self.subTest(updates=updates, name=name):
                with tempfile.TemporaryDirectory() as directory:
                    fixture = HistoryFixture(pathlib.Path(directory))
                    run = materialize_new_run(fixture.profile())
                    header = fixture.header(run.session_id, **updates)
                    selected_name = name
                    if selected_name is None:
                        timestamp = header.get("timestamp", PI_TIMESTAMP)
                        selected_name = fixture.pi_name(
                            run.session_id,
                            timestamp=timestamp,
                        )
                    fixture.write_pi_session(
                        run,
                        [header],
                        name=selected_name,
                    )
                    with self.assertRaises(ModelSessionError) as caught:
                        acquire_history_run_from_state(
                            fixture.state_root,
                            "fixture",
                            run.session_id,
                        )
                    self.assertEqual(
                        caught.exception.code,
                        "foreign_pi_session",
                    )

    def test_rejects_duplicate_header_and_strict_json_failures(self) -> None:
        payloads = []
        with tempfile.TemporaryDirectory() as directory:
            fixture = HistoryFixture(pathlib.Path(directory))
            run = materialize_new_run(fixture.profile())
            header = json.dumps(fixture.header(run.session_id))
            payloads.extend(
                (
                    header + "\n" + header + "\n",
                    header + '\n{"type":"session_info","name":NaN}\n',
                    header
                    + '\n{"type":"session_info","name":"a","name":"b"}\n',
                    header + "\n[]\n",
                    "\n" + header + "\n",
                )
            )

        for payload in payloads:
            with self.subTest(payload=payload[-80:]):
                with tempfile.TemporaryDirectory() as directory:
                    fixture = HistoryFixture(pathlib.Path(directory))
                    run = materialize_new_run(fixture.profile())
                    path = run.pi_sessions / fixture.pi_name(run.session_id)
                    header = json.dumps(fixture.header(run.session_id))
                    selected = payload.replace(
                        payload.split("\n", 1)[0],
                        header,
                        1,
                    )
                    if payload.startswith("\n"):
                        selected = "\n" + header + "\n"
                    path.write_bytes(selected.encode("utf-8"))
                    path.chmod(0o600)
                    with self.assertRaises(ModelSessionError):
                        acquire_history_run_from_state(
                            fixture.state_root,
                            "fixture",
                            run.session_id,
                        )

    def test_rejects_non_utf8_and_deeply_nested_json(self) -> None:
        for payload in (
            '{"type":"session"}\n'.encode("utf-16"),
            (
                json.dumps(
                    HistoryFixture.header("ignored"),
                )
                + "\n"
                + '{"type":"other","value":'
                + "[" * 20000
                + "0"
                + "]" * 20000
                + "}\n"
            ).encode("utf-8"),
        ):
            with self.subTest(encoding_prefix=payload[:8]):
                with tempfile.TemporaryDirectory() as directory:
                    fixture = HistoryFixture(pathlib.Path(directory))
                    run = materialize_new_run(fixture.profile())
                    path = run.pi_sessions / fixture.pi_name(run.session_id)
                    if payload.startswith(b"{"):
                        header = (
                            json.dumps(fixture.header(run.session_id)) + "\n"
                        ).encode("utf-8")
                        selected = header + payload.split(b"\n", 1)[1]
                    else:
                        selected = payload
                    path.write_bytes(selected)
                    path.chmod(0o600)
                    with self.assertRaises(ModelSessionError) as caught:
                        acquire_history_run_from_state(
                            fixture.state_root,
                            "fixture",
                            run.session_id,
                        )
                    self.assertEqual(
                        caught.exception.code,
                        "invalid_pi_session",
                    )

    def test_control_characters_in_a_hostile_filename_never_reach_errors(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = HistoryFixture(pathlib.Path(directory))
            run = materialize_new_run(fixture.profile())
            hostile_name = "\x1b[2J\u202e-session.jsonl"
            path = run.pi_sessions / hostile_name
            path.write_text(
                json.dumps(fixture.header(run.session_id)) + "\n",
                encoding="utf-8",
            )
            path.chmod(0o600)
            with self.assertRaises(ModelSessionError) as caught:
                acquire_history_run_from_state(
                    fixture.state_root,
                    "fixture",
                    run.session_id,
                )
            message = str(caught.exception)
            self.assertNotIn("\x1b", message)
            self.assertNotIn("\u202e", message)
            self.assertIn("\\x1b", message)
            self.assertIn("\\u202e", message)

    def test_nonterminated_tail_has_no_history_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = HistoryFixture(pathlib.Path(directory))
            run = materialize_new_run(fixture.profile())
            header = fixture.header(run.session_id)
            fixture.write_pi_session(
                run,
                [
                    header,
                    {"type": "session_info", "name": "ignored tail"},
                ],
                final_newline=False,
            )
            with enumerate_history(fixture.state_root, "fixture") as catalog:
                self.assertEqual(catalog.entries[0].title, "(empty session)")

        with tempfile.TemporaryDirectory() as directory:
            fixture = HistoryFixture(pathlib.Path(directory))
            run = materialize_new_run(fixture.profile())
            fixture.write_pi_session(
                run,
                [fixture.header(run.session_id)],
                final_newline=False,
            )
            with self.assertRaises(ModelSessionError) as caught:
                acquire_history_run_from_state(
                    fixture.state_root,
                    "fixture",
                    run.session_id,
                )
            self.assertEqual(caught.exception.code, "invalid_pi_session")

    def test_parser_does_not_chase_an_append_after_initial_fstat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = HistoryFixture(pathlib.Path(directory))
            run = materialize_new_run(fixture.profile())
            path = fixture.write_pi_session(
                run,
                [fixture.header(run.session_id)],
            )
            descriptor = os.open(path, os.O_RDONLY)
            real_read = history_module.os.read
            appended = False

            def read_with_append(file_descriptor: int, count: int) -> bytes:
                nonlocal appended
                chunk = real_read(file_descriptor, count)
                if not appended:
                    appended = True
                    with path.open("a", encoding="utf-8") as output:
                        output.write(
                            json.dumps(
                                {
                                    "type": "session_info",
                                    "name": "late authority",
                                }
                            )
                            + "\n"
                        )
                return chunk

            try:
                with mock.patch.object(
                    history_module.os,
                    "read",
                    side_effect=read_with_append,
                ):
                    title, _ = history_module._pi_session_metadata(
                        descriptor,
                        name=path.name,
                        session_id=run.session_id,
                    )
            finally:
                os.close(descriptor)
            self.assertEqual(title, "(empty session)")

    def test_parser_detects_a_shrink_during_streaming_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = HistoryFixture(pathlib.Path(directory))
            run = materialize_new_run(fixture.profile())
            path = fixture.write_pi_session(
                run,
                [
                    fixture.header(run.session_id),
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "content": "x" * (128 * 1024),
                        },
                    },
                ],
            )
            descriptor = os.open(path, os.O_RDONLY)
            real_read = history_module.os.read
            read_count = 0

            def read_with_shrink(
                file_descriptor: int,
                count: int,
            ) -> bytes:
                nonlocal read_count
                chunk = real_read(file_descriptor, count)
                read_count += 1
                if read_count == 1:
                    path.write_bytes(b"")
                return chunk

            try:
                with mock.patch.object(
                    history_module.os,
                    "read",
                    side_effect=read_with_shrink,
                ):
                    with self.assertRaises(ModelSessionError) as caught:
                        history_module._pi_session_metadata(
                            descriptor,
                            name=path.name,
                            session_id=run.session_id,
                        )
            finally:
                os.close(descriptor)
            self.assertEqual(caught.exception.code, "invalid_pi_session")

    def test_active_run_is_reported_without_parsing_mutable_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = HistoryFixture(pathlib.Path(directory))
            run = materialize_new_run(fixture.profile())
            malformed = run.pi_sessions / "malformed"
            malformed.write_text("not json\n", encoding="utf-8")
            malformed.chmod(0o600)
            run_descriptor = os.open(
                run.root,
                os.O_RDONLY | os.O_DIRECTORY,
            )
            try:
                fcntl.flock(
                    run_descriptor,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
                with enumerate_history(
                    fixture.state_root,
                    "fixture",
                ) as catalog:
                    entry = catalog.entries[0]
                    self.assertTrue(entry.active)
                    self.assertEqual(entry.title, "(active session)")
                    self.assertIsNone(entry.pi_session_name)
                    with self.assertRaises(ModelSessionError) as caught:
                        catalog.acquire(run.session_id)
                    self.assertEqual(caught.exception.code, "session_in_use")
            finally:
                fcntl.flock(run_descriptor, fcntl.LOCK_UN)
                os.close(run_descriptor)

    def test_rejects_ambiguous_special_and_hardlinked_pi_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = HistoryFixture(pathlib.Path(directory))
            run = materialize_new_run(fixture.profile())
            fixture.write_pi_session(run, [fixture.header(run.session_id)])
            extra = run.pi_sessions / "extra"
            extra.write_text("{}\n", encoding="utf-8")
            extra.chmod(0o600)
            with self.assertRaises(ModelSessionError) as caught:
                acquire_history_run_from_state(
                    fixture.state_root,
                    "fixture",
                    run.session_id,
                )
            self.assertEqual(caught.exception.code, "ambiguous_pi_session")

        for kind in ("fifo", "symlink", "hardlink"):
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as directory:
                    fixture = HistoryFixture(pathlib.Path(directory))
                    run = materialize_new_run(fixture.profile())
                    path = run.pi_sessions / fixture.pi_name(run.session_id)
                    if kind == "fifo":
                        os.mkfifo(path, mode=0o600)
                    else:
                        target = fixture.root / "target.jsonl"
                        target.write_text(
                            json.dumps(fixture.header(run.session_id)) + "\n",
                            encoding="utf-8",
                        )
                        target.chmod(0o600)
                        if kind == "symlink":
                            path.symlink_to(target)
                        else:
                            os.link(target, path)
                    with self.assertRaises(ModelSessionError) as caught:
                        acquire_history_run_from_state(
                            fixture.state_root,
                            "fixture",
                            run.session_id,
                        )
                    self.assertEqual(
                        caught.exception.code,
                        "unsafe_session_state",
                    )

    def test_catalog_receipt_and_source_descriptors_survive_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = HistoryFixture(pathlib.Path(directory))
            run = materialize_new_run(fixture.profile())
            with enumerate_history(fixture.state_root, "fixture") as catalog:
                receipt = run.root / "run.json"
                original_receipt = run.root / "run.original.json"
                receipt.rename(original_receipt)
                receipt.write_bytes(original_receipt.read_bytes())
                receipt.chmod(0o600)

                with catalog.acquire(run.session_id) as lease:
                    with self.assertRaises(ModelSessionError) as caught:
                        acquire_run_from_state(
                            fixture.state_root,
                            "fixture",
                            run.session_id,
                        )
                    self.assertEqual(caught.exception.code, "session_in_use")
                    workspace = run.workspace
                    original_workspace = run.root / "workspace.original"
                    old_metadata = workspace.stat()
                    workspace.rename(original_workspace)
                    workspace.mkdir(mode=0o700)
                    descriptor = lease.duplicate_source(RunSource.WORKSPACE)
                    try:
                        metadata = os.fstat(descriptor)
                        self.assertEqual(
                            (metadata.st_dev, metadata.st_ino),
                            (old_metadata.st_dev, old_metadata.st_ino),
                        )
                    finally:
                        os.close(descriptor)

    def test_catalog_revalidates_mutable_pi_state_when_acquiring(self) -> None:
        for mutation in ("ambiguous", "foreign"):
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as directory:
                    fixture = HistoryFixture(pathlib.Path(directory))
                    run = materialize_new_run(fixture.profile())
                    original = fixture.write_pi_session(
                        run,
                        [fixture.header(run.session_id)],
                    )
                    with enumerate_history(
                        fixture.state_root,
                        "fixture",
                    ) as catalog:
                        if mutation == "ambiguous":
                            extra = run.pi_sessions / "extra.jsonl"
                            extra.write_text("{}\n", encoding="utf-8")
                            extra.chmod(0o600)
                        else:
                            original.unlink()
                            foreign = run.pi_sessions / "foreign.jsonl"
                            foreign.write_text(
                                json.dumps(
                                    fixture.header(run.session_id)
                                )
                                + "\n",
                                encoding="utf-8",
                            )
                            foreign.chmod(0o600)
                        with self.assertRaises(ModelSessionError) as caught:
                            catalog.acquire(run.session_id)
                        self.assertIn(
                            caught.exception.code,
                            {
                                "ambiguous_pi_session",
                                "foreign_pi_session",
                            },
                        )

                    root_descriptor = os.open(
                        run.root,
                        os.O_RDONLY | os.O_DIRECTORY,
                    )
                    try:
                        fcntl.flock(
                            root_descriptor,
                            fcntl.LOCK_EX | fcntl.LOCK_NB,
                        )
                    finally:
                        fcntl.flock(root_descriptor, fcntl.LOCK_UN)
                        os.close(root_descriptor)

    def test_invalid_sibling_does_not_block_healthy_status_or_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = HistoryFixture(pathlib.Path(directory))
            poisoned = materialize_new_run(fixture.profile())
            healthy = materialize_new_run(fixture.profile())
            poisoned_path = (
                poisoned.pi_sessions / fixture.pi_name(poisoned.session_id)
            )
            poisoned_path.write_text("not json\n", encoding="utf-8")
            poisoned_path.chmod(0o600)
            fixture.write_pi_session(
                healthy,
                [fixture.header(healthy.session_id)],
            )

            with enumerate_history(
                fixture.state_root,
                "fixture",
            ) as catalog:
                by_id = {
                    entry.session_id: entry for entry in catalog.entries
                }
                self.assertEqual(
                    by_id[poisoned.session_id].history_error,
                    "invalid_pi_session",
                )
                self.assertEqual(
                    by_id[poisoned.session_id].title,
                    "(invalid session)",
                )
                self.assertIsNone(by_id[healthy.session_id].history_error)
                with catalog.acquire(healthy.session_id) as lease:
                    self.assertEqual(lease.run.session_id, healthy.session_id)

            with acquire_history_run_from_state(
                fixture.state_root,
                "fixture",
                healthy.session_id,
            ) as lease:
                self.assertEqual(lease.run.session_id, healthy.session_id)

    def test_structurally_damaged_sibling_is_an_isolated_catalog_entry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = HistoryFixture(pathlib.Path(directory))
            damaged = materialize_new_run(fixture.profile())
            healthy = materialize_new_run(fixture.profile())
            damaged.workspace.chmod(0o777)

            with enumerate_history(
                fixture.state_root,
                "fixture",
            ) as catalog:
                by_id = {
                    entry.session_id: entry for entry in catalog.entries
                }
                damaged_entry = by_id[damaged.session_id]
                self.assertEqual(
                    damaged_entry.title,
                    "(invalid session state)",
                )
                self.assertEqual(
                    damaged_entry.history_error,
                    "unsafe_session_permissions",
                )
                self.assertIsNone(damaged_entry.project_id)
                self.assertIsNone(damaged_entry.prompt_fingerprint)
                with self.assertRaises(ModelSessionError) as caught:
                    catalog.acquire(damaged.session_id)
                self.assertEqual(
                    caught.exception.code,
                    "unsafe_session_permissions",
                )
                with catalog.acquire(healthy.session_id) as lease:
                    self.assertEqual(lease.run.session_id, healthy.session_id)

    def test_lease_excludes_concurrent_resume_and_close_releases_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = HistoryFixture(pathlib.Path(directory))
            run = materialize_new_run(fixture.profile())
            lease = acquire_run_from_state(
                fixture.state_root,
                "fixture",
                run.session_id,
            )
            try:
                with self.assertRaises(ModelSessionError) as caught:
                    acquire_run_from_state(
                        fixture.state_root,
                        "fixture",
                        run.session_id,
                    )
                self.assertEqual(caught.exception.code, "session_in_use")
            finally:
                lease.close()
            with acquire_run_from_state(
                fixture.state_root,
                "fixture",
                run.session_id,
            ) as replacement:
                self.assertFalse(replacement.closed)
            self.assertTrue(replacement.closed)
            replacement.close()
            with self.assertRaises(ModelSessionError) as caught:
                replacement.duplicate_source(RunSource.WORKSPACE)
            self.assertEqual(caught.exception.code, "session_lease_closed")

    def test_lease_construction_failure_rolls_back_inspection_transfer(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = HistoryFixture(pathlib.Path(directory))
            run = materialize_new_run(fixture.profile())
            inspection = lease_module.inspect_run_from_state(
                fixture.state_root,
                "fixture",
                run.session_id,
            )
            try:
                descriptors_before = set(os.listdir("/proc/self/fd"))
                with mock.patch.object(
                    lease_module,
                    "RunLease",
                    side_effect=MemoryError(
                        "injected lease construction failure"
                    ),
                ):
                    with self.assertRaises(MemoryError):
                        inspection.acquire()
                self.assertEqual(
                    set(os.listdir("/proc/self/fd")),
                    descriptors_before,
                )
                self.assertFalse(inspection.closed)
                self.assertFalse(inspection.locked)

                with inspection.acquire() as lease:
                    self.assertEqual(lease.run.session_id, run.session_id)
            finally:
                inspection.close()

    def test_discovery_waits_for_materialization_lock_before_staging_audit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = HistoryFixture(pathlib.Path(directory))
            run = materialize_new_run(fixture.profile())
            lock_descriptor = os.open(
                fixture.state_root,
                os.O_RDONLY | os.O_DIRECTORY,
            )
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            staging = run.root.parent / f".creating-{run.session_id}"
            staging.mkdir(mode=0o700)
            shared_attempted = threading.Event()
            finished = threading.Event()
            failures: list[BaseException] = []
            real_flock = runs_module.fcntl.flock

            def instrumented_flock(descriptor: int, operation: int) -> None:
                if operation == fcntl.LOCK_SH:
                    shared_attempted.set()
                real_flock(descriptor, operation)

            def enumerate_worker() -> None:
                try:
                    with enumerate_history(
                        fixture.state_root,
                        "fixture",
                    ) as catalog:
                        self.assertEqual(len(catalog.entries), 1)
                except BaseException as error:
                    failures.append(error)
                finally:
                    finished.set()

            try:
                with mock.patch.object(
                    runs_module.fcntl,
                    "flock",
                    side_effect=instrumented_flock,
                ):
                    worker = threading.Thread(target=enumerate_worker)
                    worker.start()
                    self.assertTrue(shared_attempted.wait(timeout=2))
                    staging.rmdir()
                    real_flock(lock_descriptor, fcntl.LOCK_UN)
                    self.assertTrue(finished.wait(timeout=2))
                    worker.join()
            finally:
                try:
                    real_flock(lock_descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(lock_descriptor)
            if failures:
                raise failures[0]

    def test_open_pi_session_rejects_invalid_names_before_openat(self) -> None:
        descriptor = os.open("/tmp", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for name in ("../escape", "/absolute", b"bytes"):
                with self.subTest(name=name):
                    with self.assertRaises(ModelSessionError):
                        open_pi_session_at(descriptor, name)
        finally:
            os.close(descriptor)


if __name__ == "__main__":
    unittest.main()
