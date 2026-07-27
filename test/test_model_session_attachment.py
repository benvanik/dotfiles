from __future__ import annotations

import contextlib
import dataclasses
import datetime
import hashlib
import json
import os
import pathlib
import socket
import tempfile
import unittest
from collections.abc import Callable, Iterator
from unittest import mock

from model_session.attachment import (
    ATTACHMENT_SCHEMA,
    MAX_ATTACHMENT_TTL_SECONDS,
    InferenceAttachment,
    inference_attachment_receipt_path as _inference_attachment_receipt_path,
    inference_workload_identity,
    load_inference_attachment as _load_inference_attachment,
    publish_inference_attachment as _publish_inference_attachment,
)
from model_session.errors import ModelSessionError
from model_session.profile import (
    PROFILE_SCHEMA,
    ModelContract,
    PiContract,
    ProfileContract,
    RuntimeContract,
    SandboxContract,
    StorageContract,
)
from model_session.storage_limits import STORAGE_PAGE_SIZE


NOW = datetime.datetime(
    2026,
    7,
    26,
    18,
    30,
    15,
    123456,
    tzinfo=datetime.timezone.utc,
)
REVISION = "a" * 40


def _runtime_root(profile: ProfileContract) -> pathlib.Path:
    return profile.profile_root.parent / "runtime"


def inference_attachment_receipt_path(
    profile: ProfileContract,
    *,
    runtime_root: pathlib.Path | None = None,
) -> pathlib.Path:
    return _inference_attachment_receipt_path(
        profile,
        runtime_root=runtime_root or _runtime_root(profile),
    )


def publish_inference_attachment(
    profile: ProfileContract,
    socket_path: pathlib.Path | str,
    *,
    ttl_seconds: int,
    clock: Callable[[], datetime.datetime],
    runtime_root: pathlib.Path | None = None,
) -> InferenceAttachment:
    return _publish_inference_attachment(
        profile,
        socket_path,
        ttl_seconds=ttl_seconds,
        clock=clock,
        runtime_root=runtime_root or _runtime_root(profile),
    )


def load_inference_attachment(
    profile: ProfileContract,
    *,
    clock: Callable[[], datetime.datetime],
    runtime_root: pathlib.Path | None = None,
) -> InferenceAttachment:
    return _load_inference_attachment(
        profile,
        clock=clock,
        runtime_root=runtime_root or _runtime_root(profile),
    )


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _rewrite_receipt(
    path: pathlib.Path,
    mutate: Callable[[dict[str, object]], None],
    *,
    recompute_payload_hash: bool,
) -> None:
    value = json.loads(path.read_bytes())
    mutate(value)
    if recompute_payload_hash:
        payload = {
            key: child
            for key, child in value.items()
            if key != "payload_sha256"
        }
        value["payload_sha256"] = hashlib.sha256(
            _canonical_json_bytes(payload)
        ).hexdigest()
    path.write_bytes(_canonical_json_bytes(value))
    path.chmod(0o600)


class AttachmentFixture:
    def __init__(
        self,
        root: pathlib.Path,
        *,
        state_root: pathlib.Path | None = None,
    ) -> None:
        self.root = root
        self.state_root = state_root or root / "state"
        self.contract = ProfileContract(
            schema=PROFILE_SCHEMA,
            profile_id="fixture-profile",
            project_id="fixture-project",
            profile_root=root / "profile",
            state_root=self.state_root,
            project_root=root / "project",
            model=ModelContract(
                repository="example-org/example-model",
                revision=REVISION,
                context_tokens=65536,
                max_output_tokens=8192,
                weight_format="bf16",
                kv_cache_dtype="bf16",
                max_sequences=1,
            ),
            runtime=RuntimeContract(
                provider="fixture-provider",
                model_id="fixture-model",
                reasoning=False,
                input_modalities=("text",),
            ),
            pi=PiContract(
                installation_root=root / "pi-0.82.1",
                executable=pathlib.PurePosixPath("bin/pi"),
                version="0.82.1",
                tools=("read", "write", "edit", "bash"),
                system_prompt_file=None,
                append_system_prompt_file=None,
            ),
            storage=StorageContract(
                max_sessions=7,
                work_bytes=8 * 1024**3,
                work_inodes=65_536,
                history_bytes=2 * 1024**3,
                history_inodes=16_384,
                checkpoint_bytes=17 * 1024**3,
                max_sparse_extents=(
                    (10 * 1024**3) // STORAGE_PAGE_SIZE
                ),
                max_file_bytes=4 * 1024**3,
                max_logical_bytes=16 * 1024**3,
            ),
            sandbox=SandboxContract(
                memory_bytes=16 * 1024**3,
                max_tasks=256,
                max_runtime_seconds=86_400,
                idle_timeout_seconds=3_600,
                shutdown_grace_seconds=30,
            ),
        )

    @contextlib.contextmanager
    def unix_socket(
        self,
        name: str = "inference.sock",
    ) -> Iterator[pathlib.Path]:
        path = self.root / name
        endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        endpoint.bind(str(path))
        endpoint.listen(128)
        path.chmod(0o600)
        try:
            yield path
        finally:
            endpoint.close()


class ModelSessionAttachmentTest(unittest.TestCase):
    def assert_error_code(
        self,
        code: str,
        operation: Callable[[], object],
    ) -> None:
        with self.assertRaises(ModelSessionError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_publish_and_load_exact_private_short_lived_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            with fixture.unix_socket() as socket_path:
                published = publish_inference_attachment(
                    fixture.contract,
                    socket_path,
                    ttl_seconds=7200,
                    clock=lambda: NOW,
                )
                loaded = load_inference_attachment(
                    fixture.contract,
                    clock=lambda: NOW + datetime.timedelta(hours=1),
                )

            self.assertIsInstance(published, InferenceAttachment)
            self.assertEqual(loaded.publication_id, published.publication_id)
            self.assertEqual(loaded.profile_id, "fixture-profile")
            self.assertEqual(loaded.project_id, "fixture-project")
            self.assertEqual(loaded.socket_path, socket_path)
            self.assertEqual(loaded.published_at, NOW)
            self.assertEqual(
                loaded.admission_expires_at,
                NOW + datetime.timedelta(hours=2),
            )
            self.assertEqual(
                loaded.workload_sha256,
                inference_workload_identity(fixture.contract),
            )
            self.assertEqual(
                published.receipt_path,
                inference_attachment_receipt_path(fixture.contract),
            )

            runtime_root = _runtime_root(fixture.contract)
            attachments = runtime_root / "attachments"
            locks = attachments / ".locks"
            for path in (runtime_root, attachments, locks):
                self.assertEqual(stat_mode(path), 0o700)
            self.assertEqual(stat_mode(published.receipt_path), 0o600)
            self.assertEqual(
                stat_mode(locks / "fixture-profile.lock"),
                0o600,
            )

            document = published.receipt_path.read_bytes()
            receipt = json.loads(document)
            self.assertEqual(document, _canonical_json_bytes(receipt))
            self.assertEqual(receipt["schema"], ATTACHMENT_SCHEMA)
            self.assertEqual(receipt["socket_path"], str(socket_path))
            self.assertEqual(receipt["socket_device"], socket_path.stat().st_dev)
            self.assertEqual(receipt["socket_inode"], socket_path.stat().st_ino)
            self.assertEqual(
                receipt["admission_expires_at"],
                "2026-07-26T20:30:15.123456Z",
            )
            self.assertEqual(
                receipt["workload"]["model"],
                fixture.contract.model.as_dict(),
            )
            self.assertEqual(
                receipt["workload"]["runtime"],
                fixture.contract.runtime.as_dict(),
            )
            for forbidden_field in (
                "api_key",
                "credential",
                "password",
                "secret",
                "token",
            ):
                self.assertNotIn(forbidden_field, receipt)
                self.assertNotIn(
                    forbidden_field,
                    receipt["workload"]["runtime"],
                )
            self.assertFalse(fixture.state_root.exists())

    def test_explicit_home_shaped_runtime_root_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            runtime_root = (
                root
                / "home"
                / "operator"
                / ".local"
                / "model-sessions"
            )
            runtime_root.parent.mkdir(parents=True, mode=0o700)
            runtime_root.parent.chmod(0o700)
            fixture = AttachmentFixture(root)
            with fixture.unix_socket() as socket_path:
                attachment = publish_inference_attachment(
                    fixture.contract,
                    socket_path,
                    ttl_seconds=60,
                    clock=lambda: NOW,
                    runtime_root=runtime_root,
                )
                loaded = load_inference_attachment(
                    fixture.contract,
                    clock=lambda: NOW,
                    runtime_root=runtime_root,
                )
            self.assertEqual(loaded.receipt_path, attachment.receipt_path)
            self.assertTrue(loaded.receipt_path.is_relative_to(runtime_root))

    def test_runtime_receipts_cannot_enter_durable_profile_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            with fixture.unix_socket() as socket_path:
                self.assert_error_code(
                    "unsafe_inference_attachment_state",
                    lambda: publish_inference_attachment(
                        fixture.contract,
                        socket_path,
                        ttl_seconds=60,
                        clock=lambda: NOW,
                        runtime_root=fixture.state_root,
                    ),
                )
            self.assertFalse(fixture.state_root.exists())

    def test_load_missing_fails_without_creating_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            self.assert_error_code(
                "inference_attachment_missing",
                lambda: load_inference_attachment(
                    fixture.contract,
                    clock=lambda: NOW,
                ),
            )
            self.assertFalse(_runtime_root(fixture.contract).exists())

    def test_missing_profile_and_persistent_lock_only_state_are_clean_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            with fixture.unix_socket() as socket_path:
                attachment = publish_inference_attachment(
                    fixture.contract,
                    socket_path,
                    ttl_seconds=60,
                    clock=lambda: NOW,
                )
                other_profile = dataclasses.replace(
                    fixture.contract,
                    profile_id="other-profile",
                )
                self.assert_error_code(
                    "inference_attachment_missing",
                    lambda: load_inference_attachment(
                        other_profile,
                        clock=lambda: NOW,
                    ),
                )
                self.assertFalse(
                    (
                        _runtime_root(fixture.contract)
                        / "attachments"
                        / ".locks"
                        / "other-profile.lock"
                    ).exists()
                )

                attachment.receipt_path.unlink()
                self.assert_error_code(
                    "inference_attachment_missing",
                    lambda: load_inference_attachment(
                        fixture.contract,
                        clock=lambda: NOW,
                    ),
                )

    def test_receipt_without_persistent_lock_is_unsafe_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            with fixture.unix_socket() as socket_path:
                attachment = publish_inference_attachment(
                    fixture.contract,
                    socket_path,
                    ttl_seconds=60,
                    clock=lambda: NOW,
                )
                lock_path = (
                    _runtime_root(fixture.contract)
                    / "attachments"
                    / ".locks"
                    / "fixture-profile.lock"
                )
                lock_path.unlink()
                self.assert_error_code(
                    "unsafe_inference_attachment_state",
                    lambda: load_inference_attachment(
                        fixture.contract,
                        clock=lambda: NOW,
                    ),
                )
                self.assertTrue(attachment.receipt_path.exists())

    def test_default_xdg_runtime_is_boot_local_and_state_root_stays_unused(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            xdg_runtime = fixture.root / "xdg-runtime"
            xdg_runtime.mkdir(mode=0o700)
            with (
                fixture.unix_socket() as socket_path,
                mock.patch.dict(
                    os.environ,
                    {"XDG_RUNTIME_DIR": str(xdg_runtime)},
                ),
            ):
                attachment = _publish_inference_attachment(
                    fixture.contract,
                    socket_path,
                    ttl_seconds=60,
                    clock=lambda: NOW,
                )
                loaded = _load_inference_attachment(
                    fixture.contract,
                    clock=lambda: NOW,
                )
                self.assertEqual(
                    attachment.receipt_path,
                    xdg_runtime
                    / "model-session"
                    / "attachments"
                    / "fixture-profile.json",
                )
                self.assertEqual(loaded.publication_id, attachment.publication_id)
            self.assertFalse(fixture.state_root.exists())

    def test_expiry_and_future_publication_fail_at_exact_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            with fixture.unix_socket() as socket_path:
                publish_inference_attachment(
                    fixture.contract,
                    socket_path,
                    ttl_seconds=60,
                    clock=lambda: NOW,
                )
                self.assert_error_code(
                    "inference_attachment_not_yet_valid",
                    lambda: load_inference_attachment(
                        fixture.contract,
                        clock=lambda: NOW - datetime.timedelta(microseconds=1),
                    ),
                )
                self.assert_error_code(
                    "inference_attachment_expired",
                    lambda: load_inference_attachment(
                        fixture.contract,
                        clock=lambda: NOW + datetime.timedelta(seconds=60),
                    ),
                )

    def test_ttl_and_clock_are_strict_and_injectable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            with fixture.unix_socket() as socket_path:
                for ttl in (
                    True,
                    0,
                    -1,
                    MAX_ATTACHMENT_TTL_SECONDS + 1,
                ):
                    with self.subTest(ttl=ttl):
                        self.assert_error_code(
                            "invalid_inference_attachment_ttl",
                            lambda ttl=ttl: publish_inference_attachment(
                                fixture.contract,
                                socket_path,
                                ttl_seconds=ttl,
                                clock=lambda: NOW,
                            ),
                        )
                self.assert_error_code(
                    "invalid_inference_attachment_clock",
                    lambda: publish_inference_attachment(
                        fixture.contract,
                        socket_path,
                        ttl_seconds=60,
                        clock=lambda: NOW.replace(tzinfo=None),
                    ),
                )

    def test_workload_and_profile_binding_mismatches_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            with fixture.unix_socket() as socket_path:
                attachment = publish_inference_attachment(
                    fixture.contract,
                    socket_path,
                    ttl_seconds=600,
                    clock=lambda: NOW,
                )
                different_model = dataclasses.replace(
                    fixture.contract.model,
                    revision="1" * 40,
                )
                different_workload = dataclasses.replace(
                    fixture.contract,
                    model=different_model,
                )
                self.assert_error_code(
                    "inference_attachment_mismatch",
                    lambda: load_inference_attachment(
                        different_workload,
                        clock=lambda: NOW,
                    ),
                )

                def mutate_project(value: dict[str, object]) -> None:
                    value["project_id"] = "another-project"

                _rewrite_receipt(
                    attachment.receipt_path,
                    mutate_project,
                    recompute_payload_hash=True,
                )
                self.assert_error_code(
                    "inference_attachment_mismatch",
                    lambda: load_inference_attachment(
                        fixture.contract,
                        clock=lambda: NOW,
                    ),
                )

    def test_receipt_payload_and_canonical_bytes_are_tamper_evident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            with fixture.unix_socket() as socket_path:
                attachment = publish_inference_attachment(
                    fixture.contract,
                    socket_path,
                    ttl_seconds=60,
                    clock=lambda: NOW,
                )

                def extend_expiry(value: dict[str, object]) -> None:
                    value["admission_expires_at"] = (
                        "2026-07-27T18:30:15.123456Z"
                    )

                _rewrite_receipt(
                    attachment.receipt_path,
                    extend_expiry,
                    recompute_payload_hash=False,
                )
                self.assert_error_code(
                    "inference_attachment_tampered",
                    lambda: load_inference_attachment(
                        fixture.contract,
                        clock=lambda: NOW,
                    ),
                )

                publish_inference_attachment(
                    fixture.contract,
                    socket_path,
                    ttl_seconds=60,
                    clock=lambda: NOW,
                )
                with attachment.receipt_path.open("ab") as receipt_file:
                    receipt_file.write(b"\n")
                self.assert_error_code(
                    "inference_attachment_tampered",
                    lambda: load_inference_attachment(
                        fixture.contract,
                        clock=lambda: NOW,
                    ),
                )

    def test_atomic_replace_preserves_old_receipt_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            with (
                fixture.unix_socket("first.sock") as first_socket,
                fixture.unix_socket("second.sock") as second_socket,
            ):
                first = publish_inference_attachment(
                    fixture.contract,
                    first_socket,
                    ttl_seconds=600,
                    clock=lambda: NOW,
                )
                with mock.patch(
                    "model_session.attachment.os.replace",
                    side_effect=OSError("injected rename failure"),
                ):
                    self.assert_error_code(
                        "inference_attachment_publish_failed",
                        lambda: publish_inference_attachment(
                            fixture.contract,
                            second_socket,
                            ttl_seconds=600,
                            clock=lambda: NOW,
                        ),
                    )
                after_failure = load_inference_attachment(
                    fixture.contract,
                    clock=lambda: NOW,
                )
                self.assertEqual(
                    after_failure.publication_id,
                    first.publication_id,
                )
                self.assertEqual(after_failure.socket_path, first_socket)
                self.assertEqual(
                    list(
                        (
                            _runtime_root(fixture.contract) / "attachments"
                        ).glob("*.tmp")
                    ),
                    [],
                )

                second = publish_inference_attachment(
                    fixture.contract,
                    second_socket,
                    ttl_seconds=600,
                    clock=lambda: NOW,
                )
                self.assertNotEqual(second.publication_id, first.publication_id)
                self.assertEqual(
                    load_inference_attachment(
                        fixture.contract,
                        clock=lambda: NOW,
                    ).socket_path,
                    second_socket,
                )

    def test_post_replace_fsync_failure_reports_durability_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            with (
                fixture.unix_socket("first.sock") as first_socket,
                fixture.unix_socket("second.sock") as second_socket,
            ):
                first = publish_inference_attachment(
                    fixture.contract,
                    first_socket,
                    ttl_seconds=600,
                    clock=lambda: NOW,
                )
                fsync_call_count = 0

                def fail_directory_fsync(descriptor: int) -> None:
                    nonlocal fsync_call_count
                    fsync_call_count += 1
                    if fsync_call_count == 2:
                        raise OSError("injected directory fsync failure")

                with mock.patch(
                    "model_session.attachment.os.fsync",
                    side_effect=fail_directory_fsync,
                ):
                    self.assert_error_code(
                        "inference_attachment_publish_durability_unknown",
                        lambda: publish_inference_attachment(
                            fixture.contract,
                            second_socket,
                            ttl_seconds=600,
                            clock=lambda: NOW,
                        ),
                    )

                active = load_inference_attachment(
                    fixture.contract,
                    clock=lambda: NOW,
                )
                self.assertNotEqual(active.publication_id, first.publication_id)
                self.assertEqual(active.socket_path, second_socket)

    def test_receipt_and_lock_symlinks_or_unsafe_modes_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            with fixture.unix_socket() as socket_path:
                attachment = publish_inference_attachment(
                    fixture.contract,
                    socket_path,
                    ttl_seconds=600,
                    clock=lambda: NOW,
                )
                attachment.receipt_path.chmod(0o644)
                self.assert_error_code(
                    "unsafe_inference_attachment_permissions",
                    lambda: load_inference_attachment(
                        fixture.contract,
                        clock=lambda: NOW,
                    ),
                )

        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            with fixture.unix_socket() as socket_path:
                attachment = publish_inference_attachment(
                    fixture.contract,
                    socket_path,
                    ttl_seconds=600,
                    clock=lambda: NOW,
                )
                target = fixture.root / "receipt-target.json"
                target.write_bytes(attachment.receipt_path.read_bytes())
                target.chmod(0o600)
                attachment.receipt_path.unlink()
                attachment.receipt_path.symlink_to(target)
                self.assert_error_code(
                    "unsafe_inference_attachment_state",
                    lambda: load_inference_attachment(
                        fixture.contract,
                        clock=lambda: NOW,
                    ),
                )

        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            with fixture.unix_socket() as socket_path:
                publish_inference_attachment(
                    fixture.contract,
                    socket_path,
                    ttl_seconds=600,
                    clock=lambda: NOW,
                )
                lock_path = (
                    _runtime_root(fixture.contract)
                    / "attachments"
                    / ".locks"
                    / "fixture-profile.lock"
                )
                target = fixture.root / "lock-target"
                target.write_bytes(b"")
                target.chmod(0o600)
                lock_path.unlink()
                lock_path.symlink_to(target)
                self.assert_error_code(
                    "unsafe_inference_attachment_state",
                    lambda: load_inference_attachment(
                        fixture.contract,
                        clock=lambda: NOW,
                    ),
                )

    def test_private_directory_modes_and_runtime_symlinks_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            with fixture.unix_socket() as socket_path:
                publish_inference_attachment(
                    fixture.contract,
                    socket_path,
                    ttl_seconds=600,
                    clock=lambda: NOW,
                )
                _runtime_root(fixture.contract).chmod(0o755)
                self.assert_error_code(
                    "unsafe_inference_attachment_permissions",
                    lambda: load_inference_attachment(
                        fixture.contract,
                        clock=lambda: NOW,
                    ),
                )

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            real_runtime = root / "real-runtime"
            real_runtime.mkdir(mode=0o700)
            linked_runtime = root / "linked-runtime"
            linked_runtime.symlink_to(real_runtime, target_is_directory=True)
            fixture = AttachmentFixture(root)
            with fixture.unix_socket() as socket_path:
                self.assert_error_code(
                    "unsafe_inference_attachment_state",
                    lambda: publish_inference_attachment(
                        fixture.contract,
                        socket_path,
                        ttl_seconds=600,
                        clock=lambda: NOW,
                        runtime_root=linked_runtime,
                    ),
                )

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            real_parent = root / "real-parent"
            real_parent.mkdir(mode=0o700)
            linked_parent = root / "linked-parent"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            fixture = AttachmentFixture(root)
            with fixture.unix_socket() as socket_path:
                self.assert_error_code(
                    "unsafe_inference_attachment_state",
                    lambda: publish_inference_attachment(
                        fixture.contract,
                        socket_path,
                        ttl_seconds=600,
                        clock=lambda: NOW,
                        runtime_root=linked_parent / "runtime",
                    ),
                )

    def test_socket_must_be_exact_owned_private_af_unix_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            regular_file = fixture.root / "regular"
            regular_file.write_bytes(b"not a socket")
            regular_file.chmod(0o600)
            self.assert_error_code(
                "unsafe_inference_socket",
                lambda: publish_inference_attachment(
                    fixture.contract,
                    regular_file,
                    ttl_seconds=60,
                    clock=lambda: NOW,
                ),
            )

        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            with fixture.unix_socket() as socket_path:
                for mode in (0o400, 0o660):
                    with self.subTest(mode=oct(mode)):
                        socket_path.chmod(mode)
                        self.assert_error_code(
                            "unsafe_inference_socket",
                            lambda: publish_inference_attachment(
                                fixture.contract,
                                socket_path,
                                ttl_seconds=60,
                                clock=lambda: NOW,
                            ),
                        )
                socket_path.chmod(0o600)

        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            with fixture.unix_socket("target.sock") as socket_path:
                symlink_path = fixture.root / "linked.sock"
                symlink_path.symlink_to(socket_path)
                self.assert_error_code(
                    "unsafe_inference_socket",
                    lambda: publish_inference_attachment(
                        fixture.contract,
                        symlink_path,
                        ttl_seconds=60,
                        clock=lambda: NOW,
                    ),
                )

        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            real_parent = fixture.root / "socket-parent"
            real_parent.mkdir(mode=0o700)
            linked_parent = fixture.root / "linked-socket-parent"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            endpoint_path = real_parent / "target.sock"
            endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            endpoint.bind(str(endpoint_path))
            endpoint.listen(128)
            endpoint_path.chmod(0o600)
            try:
                self.assert_error_code(
                    "unsafe_inference_socket",
                    lambda: publish_inference_attachment(
                        fixture.contract,
                        linked_parent / "target.sock",
                        ttl_seconds=60,
                        clock=lambda: NOW,
                    ),
                )
            finally:
                endpoint.close()

        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            with fixture.unix_socket() as socket_path:
                with mock.patch(
                    "model_session.attachment.os.getuid",
                    return_value=os.getuid() + 1,
                ):
                    self.assert_error_code(
                        "unsafe_inference_socket",
                        lambda: publish_inference_attachment(
                            fixture.contract,
                            socket_path,
                            ttl_seconds=60,
                            clock=lambda: NOW,
                        ),
                    )

        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            non_listening_path = fixture.root / "non-listening.sock"
            non_listening = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            non_listening.bind(str(non_listening_path))
            non_listening_path.chmod(0o600)
            try:
                self.assert_error_code(
                    "inference_attachment_unavailable",
                    lambda: publish_inference_attachment(
                        fixture.contract,
                        non_listening_path,
                        ttl_seconds=60,
                        clock=lambda: NOW,
                    ),
                )
            finally:
                non_listening.close()

        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            datagram_path = fixture.root / "datagram.sock"
            datagram = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            datagram.bind(str(datagram_path))
            datagram_path.chmod(0o600)
            try:
                self.assert_error_code(
                    "inference_attachment_unavailable",
                    lambda: publish_inference_attachment(
                        fixture.contract,
                        datagram_path,
                        ttl_seconds=60,
                        clock=lambda: NOW,
                    ),
                )
            finally:
                datagram.close()

    def test_path_traversal_and_noncanonical_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            with fixture.unix_socket() as socket_path:
                traversing_path = (
                    f"{fixture.root}/unused/../{socket_path.name}"
                )
                self.assert_error_code(
                    "unsafe_inference_socket",
                    lambda: publish_inference_attachment(
                        fixture.contract,
                        traversing_path,
                        ttl_seconds=60,
                        clock=lambda: NOW,
                    ),
                )
                self.assert_error_code(
                    "unsafe_inference_socket",
                    lambda: publish_inference_attachment(
                        fixture.contract,
                        "relative.sock",
                        ttl_seconds=60,
                        clock=lambda: NOW,
                    ),
                )
                invalid_profile = dataclasses.replace(
                    fixture.contract,
                    profile_id="../escape",
                )
                self.assert_error_code(
                    "invalid_inference_attachment_binding",
                    lambda: publish_inference_attachment(
                        invalid_profile,
                        socket_path,
                        ttl_seconds=60,
                        clock=lambda: NOW,
                    ),
                )

    def test_replaced_socket_inode_and_wrong_boot_are_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            path = fixture.root / "replaceable.sock"
            first_endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            first_endpoint.bind(str(path))
            first_endpoint.listen(128)
            path.chmod(0o600)
            try:
                attachment = publish_inference_attachment(
                    fixture.contract,
                    path,
                    ttl_seconds=600,
                    clock=lambda: NOW,
                )
            finally:
                first_endpoint.close()
            path.unlink()

            second_endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            second_endpoint.bind(str(path))
            second_endpoint.listen(128)
            path.chmod(0o600)
            try:
                self.assertNotEqual(
                    attachment.socket_inode,
                    path.stat().st_ino,
                )
                self.assert_error_code(
                    "inference_attachment_unavailable",
                    lambda: load_inference_attachment(
                        fixture.contract,
                        clock=lambda: NOW,
                    ),
                )
            finally:
                second_endpoint.close()

        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            with fixture.unix_socket() as socket_path:
                publish_inference_attachment(
                    fixture.contract,
                    socket_path,
                    ttl_seconds=600,
                    clock=lambda: NOW,
                )
                with mock.patch(
                    "model_session.attachment._read_boot_id",
                    return_value="00000000-0000-0000-0000-000000000000",
                ):
                    self.assert_error_code(
                        "inference_attachment_wrong_boot",
                        lambda: load_inference_attachment(
                            fixture.contract,
                            clock=lambda: NOW,
                        ),
                    )

    def test_receipt_fifo_is_rejected_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            with fixture.unix_socket() as socket_path:
                attachment = publish_inference_attachment(
                    fixture.contract,
                    socket_path,
                    ttl_seconds=600,
                    clock=lambda: NOW,
                )
                attachment.receipt_path.unlink()
                os.mkfifo(attachment.receipt_path, mode=0o600)
                self.assert_error_code(
                    "unsafe_inference_attachment_state",
                    lambda: load_inference_attachment(
                        fixture.contract,
                        clock=lambda: NOW,
                    ),
                )

    def test_vanished_socket_makes_existing_receipt_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AttachmentFixture(pathlib.Path(directory))
            with fixture.unix_socket() as socket_path:
                publish_inference_attachment(
                    fixture.contract,
                    socket_path,
                    ttl_seconds=600,
                    clock=lambda: NOW,
                )
                socket_path.unlink()
                self.assert_error_code(
                    "inference_attachment_unavailable",
                    lambda: load_inference_attachment(
                        fixture.contract,
                        clock=lambda: NOW,
                    ),
                )


def stat_mode(path: pathlib.Path) -> int:
    return path.stat().st_mode & 0o777


if __name__ == "__main__":
    unittest.main()
