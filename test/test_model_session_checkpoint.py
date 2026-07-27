from __future__ import annotations

import dataclasses
import errno
import hashlib
import os
import pathlib
import socket
import stat
import tempfile
import unittest
from unittest import mock

import model_session.checkpoint_format as checkpoint_format
from model_session.checkpoint import (
    CHECKPOINT_SCHEMA,
    DEFAULT_CHECKPOINT_LIMITS,
    CheckpointLimits,
    hydrate_checkpoint,
    maximum_encoded_bytes,
    validate_checkpoint,
    write_checkpoint,
)
from model_session.errors import ModelSessionError


def _directory(path: pathlib.Path, mode: int = 0o700) -> pathlib.Path:
    path.mkdir(parents=True)
    path.chmod(mode)
    return path


def _open_directory(path: pathlib.Path) -> int:
    return os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )


class CheckpointFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="model-session-checkpoint.",
            dir="/tmp",
        )
        self.root = pathlib.Path(self.temporary.name)
        self.work = _directory(self.root / "work")
        self.sessions = _directory(self.root / "sessions")
        self.hydrated_work = _directory(self.root / "hydrated-work")
        self.hydrated_sessions = _directory(
            self.root / "hydrated-sessions"
        )

    def close(self) -> None:
        self.temporary.cleanup()

    def descriptors(
        self,
        work: pathlib.Path | None = None,
        sessions: pathlib.Path | None = None,
    ) -> tuple[int, int]:
        return (
            _open_directory(self.work if work is None else work),
            _open_directory(self.sessions if sessions is None else sessions),
        )

    def write_pack(
        self,
        name: str = "checkpoint.pack",
        *,
        limits: CheckpointLimits = DEFAULT_CHECKPOINT_LIMITS,
    ):
        path = self.root / name
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        work_descriptor, sessions_descriptor = self.descriptors()
        try:
            summary = write_checkpoint(
                work_descriptor,
                sessions_descriptor,
                descriptor,
                limits=limits,
            )
        finally:
            os.close(work_descriptor)
            os.close(sessions_descriptor)
            os.close(descriptor)
        return path, summary

    def hydrate(
        self,
        pack: pathlib.Path,
        *,
        limits: CheckpointLimits = DEFAULT_CHECKPOINT_LIMITS,
    ):
        input_descriptor = os.open(pack, os.O_RDONLY | os.O_NOFOLLOW)
        work_descriptor, sessions_descriptor = self.descriptors(
            self.hydrated_work,
            self.hydrated_sessions,
        )
        try:
            return hydrate_checkpoint(
                input_descriptor,
                work_descriptor,
                sessions_descriptor,
                limits=limits,
            )
        finally:
            os.close(work_descriptor)
            os.close(sessions_descriptor)
            os.close(input_descriptor)


def _write_document(path: pathlib.Path, document: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        position = 0
        while position < len(document):
            position += os.write(descriptor, document[position:])
    finally:
        os.close(descriptor)


def _rewrite_footer(document: bytes) -> bytes:
    footer_size = checkpoint_format._FOOTER.size
    prefix = document[:-footer_size]
    footer = checkpoint_format._FOOTER.pack(
        checkpoint_format._FOOTER_MAGIC,
        len(prefix),
        hashlib.sha256(prefix).digest(),
    )
    return prefix + footer


def _cross_root_hardlink_pack() -> bytes:
    def record(
        entry_type: int,
        root: int,
        mode: int,
        component: bytes | None,
        *,
        payload: bytes = b"",
        hardlink_index: int = checkpoint_format._NO_LINK,
    ) -> bytes:
        encoded_path = (
            b""
            if component is None
            else len(component).to_bytes(2, "little") + component
        )
        return (
            checkpoint_format._RECORD.pack(
                entry_type,
                root,
                mode,
                0 if component is None else 1,
                0,
                len(encoded_path),
                0,
                len(payload),
                len(payload),
                hardlink_index,
            )
            + encoded_path
            + payload
            + hashlib.sha256(payload).digest()
        )

    records = (
        record(
            checkpoint_format._ENTRY_DIRECTORY,
            checkpoint_format._ROOT_WORK,
            0o700,
            None,
        ),
        record(
            checkpoint_format._ENTRY_REGULAR,
            checkpoint_format._ROOT_WORK,
            0o600,
            b"source",
            payload=b"x",
        ),
        record(
            checkpoint_format._ENTRY_DIRECTORY,
            checkpoint_format._ROOT_SESSIONS,
            0o700,
            None,
        ),
        record(
            checkpoint_format._ENTRY_HARDLINK,
            checkpoint_format._ROOT_SESSIONS,
            0o600,
            b"alias",
            hardlink_index=1,
        ),
    )
    prefix = checkpoint_format._HEADER.pack(
        checkpoint_format._PACK_MAGIC,
        checkpoint_format._FORMAT_VERSION,
        checkpoint_format._FORMAT_FLAGS,
        checkpoint_format._HEADER.size,
        len(records),
        1,
        1,
    ) + b"".join(records)
    return prefix + checkpoint_format._FOOTER.pack(
        checkpoint_format._FOOTER_MAGIC,
        len(prefix),
        hashlib.sha256(prefix).digest(),
    )


class CheckpointCodecTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = CheckpointFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_deterministic_roundtrip_preserves_representation(self) -> None:
        self.fixture.work.chmod(0o750)
        self.fixture.sessions.chmod(0o710)
        nested = _directory(self.fixture.work / "nested", 0o711)
        alpha = nested / "alpha.txt"
        alpha.write_bytes(b"alpha payload\n")
        alpha.chmod(0o640)
        unicode_file = self.fixture.work / "caf\u00e9"
        unicode_file.write_bytes(b"unicode\n")
        unicode_file.chmod(0o600)
        os.link(alpha, self.fixture.work / "z-hardlink")
        os.link(alpha, self.fixture.sessions / "cross-root-hardlink")
        (self.fixture.work / "relative-link").symlink_to(
            "nested/alpha.txt"
        )
        outside = self.fixture.root / "outside-secret"
        outside.write_bytes(b"DO_NOT_COPY_THIS_PAYLOAD")
        (self.fixture.sessions / "escaping-link").symlink_to(
            "../outside-secret"
        )

        first, first_summary = self.fixture.write_pack("first.pack")
        second, second_summary = self.fixture.write_pack("second.pack")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(first_summary, second_summary)
        self.assertEqual(first_summary.schema, CHECKPOINT_SCHEMA)
        self.assertEqual(
            first_summary.pack_sha256,
            hashlib.sha256(first.read_bytes()).hexdigest(),
        )
        self.assertNotIn(b"DO_NOT_COPY_THIS_PAYLOAD", first.read_bytes())

        hydrated_summary = self.fixture.hydrate(first)
        self.assertEqual(hydrated_summary, first_summary)
        hydrated_alpha = (
            self.fixture.hydrated_work / "nested" / "alpha.txt"
        )
        self.assertEqual(hydrated_alpha.read_bytes(), b"alpha payload\n")
        self.assertEqual(
            (self.fixture.hydrated_work / "caf\u00e9").read_bytes(),
            b"unicode\n",
        )
        self.assertEqual(
            os.readlink(self.fixture.hydrated_work / "relative-link"),
            "nested/alpha.txt",
        )
        self.assertEqual(
            os.readlink(self.fixture.hydrated_sessions / "escaping-link"),
            "../outside-secret",
        )
        alpha_inode = hydrated_alpha.stat().st_ino
        self.assertEqual(
            (self.fixture.hydrated_work / "z-hardlink").stat().st_ino,
            alpha_inode,
        )
        cross_root = (
            self.fixture.hydrated_sessions / "cross-root-hardlink"
        )
        self.assertEqual(cross_root.read_bytes(), b"alpha payload\n")
        self.assertNotEqual(
            cross_root.stat().st_ino,
            alpha_inode,
        )
        self.assertEqual(stat.S_IMODE(hydrated_alpha.stat().st_mode), 0o640)
        self.assertEqual(
            stat.S_IMODE(
                (self.fixture.hydrated_work / "nested").stat().st_mode
            ),
            0o711,
        )
        self.assertEqual(
            stat.S_IMODE(self.fixture.hydrated_work.stat().st_mode),
            0o750,
        )
        self.assertEqual(
            stat.S_IMODE(self.fixture.hydrated_sessions.stat().st_mode),
            0o710,
        )

    def test_hardlinked_symlink_is_preserved_when_supported(self) -> None:
        source = self.fixture.work / "source-link"
        source.symlink_to("nowhere")
        alias = self.fixture.work / "alias-link"
        try:
            os.link(source, alias, follow_symlinks=False)
        except OSError as error:
            self.skipTest(f"filesystem cannot hardlink symlinks: {error}")
        if os.lstat(source).st_ino != os.lstat(alias).st_ino:
            self.skipTest("os.link followed the symlink on this platform")

        pack, _summary = self.fixture.write_pack()
        self.fixture.hydrate(pack)
        hydrated_source = self.fixture.hydrated_work / "source-link"
        hydrated_alias = self.fixture.hydrated_work / "alias-link"
        self.assertTrue(hydrated_source.is_symlink())
        self.assertTrue(hydrated_alias.is_symlink())
        self.assertEqual(os.readlink(hydrated_alias), "nowhere")
        self.assertEqual(
            os.lstat(hydrated_source).st_ino,
            os.lstat(hydrated_alias).st_ino,
        )

    def test_cross_root_host_link_encodes_as_independent_files(self) -> None:
        payload = b"root-scoped hardlink identity"
        source = self.fixture.work / "source"
        source.write_bytes(payload)
        alias = self.fixture.sessions / "alias"
        os.link(source, alias)

        checkpoint_path, summary = self.fixture.write_pack()
        self.assertEqual(summary.logical_bytes, 2 * len(payload))
        self.assertEqual(summary.payload_bytes, 2 * len(payload))
        if not pathlib.Path("/dev/shm").is_dir():
            self.skipTest("/dev/shm is unavailable for a distinct-filesystem target")
        try:
            other_filesystem = tempfile.TemporaryDirectory(
                prefix="model-session-checkpoint-target.",
                dir="/dev/shm",
            )
        except OSError as error:
            self.skipTest(f"cannot create distinct-filesystem target: {error}")
        with other_filesystem:
            sessions_target = pathlib.Path(other_filesystem.name)
            if (
                sessions_target.stat().st_dev
                == self.fixture.hydrated_work.stat().st_dev
            ):
                self.skipTest("/dev/shm and /tmp are the same filesystem")
            input_descriptor = os.open(checkpoint_path, os.O_RDONLY)
            work_descriptor = _open_directory(self.fixture.hydrated_work)
            sessions_descriptor = _open_directory(sessions_target)
            try:
                hydrate_checkpoint(
                    input_descriptor,
                    work_descriptor,
                    sessions_descriptor,
                )
            finally:
                os.close(sessions_descriptor)
                os.close(work_descriptor)
                os.close(input_descriptor)
            hydrated_source = self.fixture.hydrated_work / "source"
            hydrated_alias = sessions_target / "alias"
            self.assertEqual(hydrated_source.read_bytes(), payload)
            self.assertEqual(hydrated_alias.read_bytes(), payload)
            self.assertNotEqual(
                hydrated_source.stat().st_dev,
                hydrated_alias.stat().st_dev,
            )

    def test_source_hardlinks_must_be_closed_over_both_roots(self) -> None:
        source = self.fixture.work / "source"
        source.write_bytes(b"payload")
        os.link(source, self.fixture.root / "unscanned-alias")
        with self.assertRaises(ModelSessionError) as caught:
            self.fixture.write_pack()
        self.assertEqual(
            caught.exception.code,
            "unsupported_checkpoint_entry",
        )

    def test_cross_root_hardlink_pack_is_rejected_before_mutation(self) -> None:
        checkpoint_path = self.fixture.root / "cross-root-hardlink.pack"
        _write_document(checkpoint_path, _cross_root_hardlink_pack())
        self.fixture.hydrated_work.chmod(0o750)
        self.fixture.hydrated_sessions.chmod(0o710)

        with self.assertRaises(ModelSessionError) as caught:
            self.fixture.hydrate(checkpoint_path)
        self.assertEqual(caught.exception.code, "invalid_checkpoint_pack")
        self.assertEqual(tuple(self.fixture.hydrated_work.iterdir()), ())
        self.assertEqual(tuple(self.fixture.hydrated_sessions.iterdir()), ())
        self.assertEqual(
            stat.S_IMODE(self.fixture.hydrated_work.stat().st_mode),
            0o750,
        )
        self.assertEqual(
            stat.S_IMODE(self.fixture.hydrated_sessions.stat().st_mode),
            0o710,
        )

    def test_sparse_file_uses_only_clean_extents_or_full_payload(self) -> None:
        sparse = self.fixture.work / "sparse.bin"
        descriptor = os.open(
            sparse,
            os.O_RDWR | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        logical_size = 2 * 1024 * 1024
        try:
            os.ftruncate(descriptor, logical_size)
            os.pwrite(descriptor, b"front", 4096)
            os.pwrite(descriptor, b"back", logical_size - 4096)
        finally:
            os.close(descriptor)
        source_metadata = sparse.stat()

        pack, summary = self.fixture.write_pack()
        self.assertEqual(summary.logical_bytes, logical_size)
        self.assertLessEqual(summary.payload_bytes, logical_size)
        if source_metadata.st_blocks * 512 < logical_size:
            self.assertLess(summary.payload_bytes, logical_size)
        self.fixture.hydrate(pack)
        hydrated = self.fixture.hydrated_work / "sparse.bin"
        self.assertEqual(hydrated.stat().st_size, logical_size)
        descriptor = os.open(hydrated, os.O_RDONLY)
        try:
            self.assertEqual(os.pread(descriptor, 5, 4096), b"front")
            self.assertEqual(
                os.pread(descriptor, 4, logical_size - 4096),
                b"back",
            )
            self.assertEqual(os.pread(descriptor, 32, 1024 * 1024), b"\0" * 32)
        finally:
            os.close(descriptor)

    def test_streaming_reads_never_exceed_the_configured_chunk(self) -> None:
        (self.fixture.work / "payload").write_bytes(b"x" * 4096)
        limits = dataclasses.replace(
            DEFAULT_CHECKPOINT_LIMITS,
            io_chunk_bytes=37,
        )
        original_pread = os.pread
        requests: list[int] = []

        def bounded_pread(descriptor, length, offset):
            requests.append(length)
            return original_pread(descriptor, length, offset)

        with mock.patch("os.pread", side_effect=bounded_pread):
            pack, _summary = self.fixture.write_pack(limits=limits)
            descriptor = os.open(pack, os.O_RDONLY)
            try:
                validate_checkpoint(descriptor, limits=limits)
            finally:
                os.close(descriptor)
            self.fixture.hydrate(pack, limits=limits)
        self.assertTrue(requests)
        self.assertLessEqual(max(requests), limits.io_chunk_bytes)

    def test_all_resource_limits_fail_closed(self) -> None:
        cases = (
            (
                "entries",
                lambda fixture: (fixture.work / "one").write_bytes(b"x"),
                {"max_entries": 2},
            ),
            (
                "depth",
                lambda fixture: _directory(fixture.work / "a" / "b"),
                {"max_depth": 1},
            ),
            (
                "component",
                lambda fixture: (fixture.work / "long").write_bytes(b"x"),
                {"max_component_bytes": 3},
            ),
            (
                "path",
                lambda fixture: (
                    _directory(fixture.work / "abcd")
                    / "efgh"
                ).write_bytes(b"x"),
                {"max_path_bytes": 7},
            ),
            (
                "file-logical",
                lambda fixture: (fixture.work / "large").write_bytes(b"12345"),
                {"max_file_logical_bytes": 4},
            ),
            (
                "aggregate-logical",
                lambda fixture: (
                    (fixture.work / "a").write_bytes(b"123"),
                    (fixture.sessions / "b").write_bytes(b"456"),
                ),
                {"max_logical_bytes": 5},
            ),
            (
                "payload",
                lambda fixture: (fixture.work / "payload").write_bytes(b"12345"),
                {"max_payload_bytes": 4},
            ),
            (
                "pack",
                lambda fixture: (fixture.work / "payload").write_bytes(b"x"),
                {"max_pack_bytes": 100},
            ),
        )
        for name, populate, overrides in cases:
            with self.subTest(name=name):
                fixture = CheckpointFixture()
                try:
                    populate(fixture)
                    limits = dataclasses.replace(
                        DEFAULT_CHECKPOINT_LIMITS,
                        **overrides,
                    )
                    with self.assertRaises(ModelSessionError) as caught:
                        fixture.write_pack(limits=limits)
                    self.assertEqual(
                        caught.exception.code,
                        "checkpoint_limit_exceeded",
                    )
                finally:
                    fixture.close()

    def test_limit_contract_rejects_unrepresentable_values(self) -> None:
        for updates in (
            {"max_entries": 1},
            {"max_depth": 0},
            {"max_component_bytes": 0},
            {"io_chunk_bytes": 0},
            {"max_depth": 257},
            {"max_depth": 1 << 16},
            {"max_component_bytes": 1 << 16},
            {"max_sparse_extents_per_file": 1 << 32},
            {"max_sparse_extents": 1 << 64},
        ):
            with self.subTest(updates=updates):
                with self.assertRaises(ValueError):
                    dataclasses.replace(DEFAULT_CHECKPOINT_LIMITS, **updates)

    def test_encoded_bound_and_aggregate_sparse_extent_limit(self) -> None:
        limits = dataclasses.replace(
            DEFAULT_CHECKPOINT_LIMITS,
            max_entries=7,
            max_depth=3,
            max_component_bytes=4,
            max_path_bytes=9,
            max_payload_bytes=123,
            max_sparse_extents=5,
        )
        expected = (
            checkpoint_format._HEADER.size
            + checkpoint_format._FOOTER.size
            + 7
            * (
                checkpoint_format._RECORD.size
                + len(checkpoint_format._EMPTY_DIGEST)
            )
            + 5 * 13
            + 5 * checkpoint_format._EXTENT.size
            + 123
        )
        self.assertEqual(maximum_encoded_bytes(limits), expected)
        self.assertEqual(
            maximum_encoded_bytes(
                dataclasses.replace(limits, max_pack_bytes=1)
            ),
            expected,
        )

        (self.fixture.work / "sparse").write_bytes(b"abc")
        extents = ((0, 1), (2, 1))
        encoding_limits = dataclasses.replace(
            DEFAULT_CHECKPOINT_LIMITS,
            max_sparse_extents=2,
        )
        with mock.patch(
            "model_session.checkpoint_tree._sparse_extents",
            return_value=extents,
        ):
            encoded, _summary = self.fixture.write_pack(
                "aggregate-extents.pack",
                limits=encoding_limits,
            )
        descriptor = os.open(encoded, os.O_RDONLY)
        try:
            with self.assertRaises(ModelSessionError) as caught:
                validate_checkpoint(
                    descriptor,
                    limits=dataclasses.replace(
                        encoding_limits,
                        max_sparse_extents=1,
                    ),
                )
            self.assertEqual(
                caught.exception.code,
                "checkpoint_limit_exceeded",
            )
        finally:
            os.close(descriptor)

        with mock.patch(
            "model_session.checkpoint_tree._sparse_extents",
            return_value=extents,
        ):
            with self.assertRaises(ModelSessionError) as caught:
                self.fixture.write_pack(
                    "encode-extent-overflow.pack",
                    limits=dataclasses.replace(
                        encoding_limits,
                        max_sparse_extents=1,
                    ),
                )
        self.assertEqual(
            caught.exception.code,
            "checkpoint_limit_exceeded",
        )

    def test_source_rejects_special_objects_without_blocking(self) -> None:
        fifo = self.fixture.work / "fifo"
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(ModelSessionError) as caught:
            self.fixture.write_pack("fifo.pack")
        self.assertEqual(caught.exception.code, "unsupported_checkpoint_entry")
        fifo.unlink()

        endpoint = self.fixture.work / "socket"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(os.fspath(endpoint))
            with self.assertRaises(ModelSessionError) as caught:
                self.fixture.write_pack("socket.pack")
            self.assertEqual(
                caught.exception.code,
                "unsupported_checkpoint_entry",
            )
        finally:
            listener.close()

    def test_source_rejects_special_permissions_and_xattrs(self) -> None:
        special = self.fixture.work / "special"
        special.write_bytes(b"x")
        special.chmod(0o4600)
        with self.assertRaises(ModelSessionError) as caught:
            self.fixture.write_pack("setid.pack")
        self.assertEqual(caught.exception.code, "unsupported_checkpoint_entry")
        special.chmod(0o600)

        sticky = _directory(self.fixture.work / "sticky")
        sticky.chmod(0o1700)
        with self.assertRaises(ModelSessionError) as caught:
            self.fixture.write_pack("sticky.pack")
        self.assertEqual(caught.exception.code, "unsupported_checkpoint_entry")
        sticky.chmod(0o700)

        attributed = self.fixture.work / "attributed"
        attributed.write_bytes(b"x")
        try:
            os.setxattr(attributed, "user.checkpoint-test", b"x")
        except OSError as error:
            if error.errno in {
                errno.ENOTSUP,
                errno.EPERM,
                getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
            }:
                self.skipTest(f"filesystem xattrs unavailable: {error}")
            raise
        with self.assertRaises(ModelSessionError) as caught:
            self.fixture.write_pack("xattr.pack")
        self.assertEqual(caught.exception.code, "unsupported_checkpoint_entry")

    def test_source_rejects_noncanonical_filesystem_text(self) -> None:
        decomposed = self.fixture.work / "e\u0301"
        decomposed.write_bytes(b"x")
        with self.assertRaises(ModelSessionError) as caught:
            self.fixture.write_pack("nfd.pack")
        self.assertEqual(caught.exception.code, "invalid_checkpoint_path")
        decomposed.unlink()

        work_descriptor = _open_directory(self.fixture.work)
        try:
            bad_descriptor = os.open(
                b"\xff",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=work_descriptor,
            )
            os.close(bad_descriptor)
        finally:
            os.close(work_descriptor)
        with self.assertRaises(ModelSessionError) as caught:
            self.fixture.write_pack("non-utf8.pack")
        self.assertEqual(caught.exception.code, "invalid_checkpoint_path")

    def test_source_rejects_non_utf8_symlink_target(self) -> None:
        work_descriptor = _open_directory(self.fixture.work)
        try:
            os.symlink(b"\xff", b"bad-link", dir_fd=work_descriptor)
        finally:
            os.close(work_descriptor)
        with self.assertRaises(ModelSessionError) as caught:
            self.fixture.write_pack()
        self.assertEqual(caught.exception.code, "invalid_checkpoint_path")

    def test_truncation_hash_corruption_and_trailing_bytes_are_rejected(
        self,
    ) -> None:
        payload = b"unique-checkpoint-payload"
        (self.fixture.work / "payload").write_bytes(payload)
        pack, _summary = self.fixture.write_pack()
        document = pack.read_bytes()

        mutations = {
            "empty": b"",
            "header": document[: checkpoint_format._HEADER.size - 1],
            "middle": document[: len(document) // 2],
            "footer": document[:-1],
            "trailing": document + b"x",
        }
        payload_corrupt = bytearray(document)
        payload_offset = document.index(payload)
        payload_corrupt[payload_offset] ^= 0x01
        mutations["payload-hash"] = bytes(payload_corrupt)
        footer_corrupt = bytearray(document)
        footer_corrupt[-1] ^= 0x01
        mutations["footer-hash"] = bytes(footer_corrupt)

        for name, malformed in mutations.items():
            with self.subTest(name=name):
                path = self.fixture.root / f"malformed-{name}.pack"
                _write_document(path, malformed)
                descriptor = os.open(path, os.O_RDONLY)
                try:
                    with self.assertRaises(ModelSessionError):
                        validate_checkpoint(descriptor)
                finally:
                    os.close(descriptor)

    def test_decoder_rejects_canonical_path_and_mode_violations(self) -> None:
        (self.fixture.work / "alpha").write_bytes(b"x")
        pack, _summary = self.fixture.write_pack()
        original = pack.read_bytes()

        invalid_path = bytearray(original)
        marker = invalid_path.index(b"\x05\x00alpha")
        invalid_path[marker + 2 : marker + 7] = b"a/bcd"
        invalid_path = bytearray(_rewrite_footer(bytes(invalid_path)))
        invalid_path_pack = self.fixture.root / "invalid-path.pack"
        _write_document(invalid_path_pack, bytes(invalid_path))

        invalid_mode = bytearray(original)
        root_mode_offset = checkpoint_format._HEADER.size + 2
        invalid_mode[root_mode_offset : root_mode_offset + 2] = (
            0o4755
        ).to_bytes(2, "little")
        invalid_mode = bytearray(_rewrite_footer(bytes(invalid_mode)))
        invalid_mode_pack = self.fixture.root / "invalid-mode.pack"
        _write_document(invalid_mode_pack, bytes(invalid_mode))

        for path, code in (
            (invalid_path_pack, "invalid_checkpoint_path"),
            (invalid_mode_pack, "invalid_checkpoint_pack"),
        ):
            with self.subTest(path=path.name):
                descriptor = os.open(path, os.O_RDONLY)
                try:
                    with self.assertRaises(ModelSessionError) as caught:
                        validate_checkpoint(descriptor)
                    self.assertEqual(caught.exception.code, code)
                finally:
                    os.close(descriptor)

    def test_declared_totals_are_bounded_before_payload_read(self) -> None:
        (self.fixture.work / "payload").write_bytes(b"x")
        pack, _summary = self.fixture.write_pack()
        document = bytearray(pack.read_bytes())
        fields = list(
            checkpoint_format._HEADER.unpack(
                document[: checkpoint_format._HEADER.size]
            )
        )
        fields[5] = 5
        document[: checkpoint_format._HEADER.size] = (
            checkpoint_format._HEADER.pack(*fields)
        )
        document = bytearray(_rewrite_footer(bytes(document)))
        path = self.fixture.root / "oversized-declaration.pack"
        _write_document(path, bytes(document))
        limits = dataclasses.replace(
            DEFAULT_CHECKPOINT_LIMITS,
            max_logical_bytes=4,
        )
        descriptor = os.open(path, os.O_RDONLY)
        try:
            with self.assertRaises(ModelSessionError) as caught:
                validate_checkpoint(descriptor, limits=limits)
            self.assertEqual(
                caught.exception.code,
                "checkpoint_limit_exceeded",
            )
        finally:
            os.close(descriptor)

    def test_complete_pack_validation_precedes_any_target_mutation(self) -> None:
        (self.fixture.work / "payload").write_bytes(b"payload")
        pack, _summary = self.fixture.write_pack()
        corrupt = bytearray(pack.read_bytes())
        corrupt[-1] ^= 0x01
        corrupt_path = self.fixture.root / "corrupt.pack"
        _write_document(corrupt_path, bytes(corrupt))
        self.fixture.hydrated_work.chmod(0o750)
        self.fixture.hydrated_sessions.chmod(0o710)

        with self.assertRaises(ModelSessionError):
            self.fixture.hydrate(corrupt_path)
        self.assertEqual(tuple(self.fixture.hydrated_work.iterdir()), ())
        self.assertEqual(tuple(self.fixture.hydrated_sessions.iterdir()), ())
        self.assertEqual(
            stat.S_IMODE(self.fixture.hydrated_work.stat().st_mode),
            0o750,
        )
        self.assertEqual(
            stat.S_IMODE(self.fixture.hydrated_sessions.stat().st_mode),
            0o710,
        )

    def test_hydration_rejects_nonempty_or_attributed_roots(self) -> None:
        pack, _summary = self.fixture.write_pack()
        (self.fixture.hydrated_work / "occupied").write_bytes(b"x")
        with self.assertRaises(ModelSessionError) as caught:
            self.fixture.hydrate(pack)
        self.assertEqual(caught.exception.code, "unsafe_checkpoint_target")
        (self.fixture.hydrated_work / "occupied").unlink()

        try:
            os.setxattr(
                self.fixture.hydrated_sessions,
                "user.checkpoint-test",
                b"x",
            )
        except OSError as error:
            if error.errno in {
                errno.ENOTSUP,
                errno.EPERM,
                getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
            }:
                self.skipTest(f"filesystem xattrs unavailable: {error}")
            raise
        with self.assertRaises(ModelSessionError) as caught:
            self.fixture.hydrate(pack)
        self.assertEqual(
            caught.exception.code,
            "unsupported_checkpoint_entry",
        )

    def test_pack_descriptors_must_be_regular_and_output_must_be_empty(
        self,
    ) -> None:
        nonempty = self.fixture.root / "nonempty.pack"
        nonempty.write_bytes(b"x")
        output_descriptor = os.open(nonempty, os.O_RDWR)
        work_descriptor, sessions_descriptor = self.fixture.descriptors()
        try:
            with self.assertRaises(ModelSessionError) as caught:
                write_checkpoint(
                    work_descriptor,
                    sessions_descriptor,
                    output_descriptor,
                )
            self.assertEqual(
                caught.exception.code,
                "invalid_checkpoint_descriptor",
            )
        finally:
            os.close(work_descriptor)
            os.close(sessions_descriptor)
            os.close(output_descriptor)

        read_descriptor, write_descriptor = os.pipe()
        try:
            with self.assertRaises(ModelSessionError) as caught:
                validate_checkpoint(read_descriptor)
            self.assertEqual(
                caught.exception.code,
                "invalid_checkpoint_descriptor",
            )
        finally:
            os.close(read_descriptor)
            os.close(write_descriptor)


if __name__ == "__main__":
    unittest.main()
