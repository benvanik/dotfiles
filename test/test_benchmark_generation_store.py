from __future__ import annotations

import contextlib
import os
import pathlib
import signal
import stat
import tempfile
import unittest
from collections.abc import Callable, Iterator
from unittest import mock

from benchmark_lock import generation_store
from benchmark_lock.errors import BenchmarkLockError
from benchmark_lock.generation_format import build_generation
from benchmark_lock.generation_store import GenerationStore


EXPECTED_FIXTURE_DIGEST = (
    "5469a7e8c7e3ba999cba2d393a51dc84520df3907ee9c90d47d73bdb5188ad38"
)


class InjectedInterruption(RuntimeError):
    pass


class StoreFixture:
    def __init__(self, test: unittest.TestCase) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        test.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.source = self.root / "source"
        self.generations = self.root / "generations"
        self.generations.mkdir(mode=0o755)
        self.uid = os.getuid()
        self.gid = os.getgid()
        self.messages: list[str] = []
        source_files = {
            "lib/benchmark_lock/__init__.py": b"init\n",
            "lib/benchmark_lock/client.py": b"client\n",
            "lib/benchmark_lock/daemon.py": b"daemon\n",
            "lib/benchmark_lock/extra.py": b"extra\n",
            "benchmarkd/bin/benchmark-lock": b"lock\n",
            "benchmarkd/bin/benchmarkd": b"broker\n",
            "benchmarkd/systemd/benchmarkd.service": b"service\n",
            "benchmarkd/systemd/benchmarkd.socket": b"socket\n",
            "benchmarkd/sysusers/benchmarkd.conf": b"sysusers\n",
        }
        for relative, content in source_files.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.generation = build_generation(self.source)
        self.store = self.new_store()

    def new_store(self) -> GenerationStore:
        return GenerationStore(
            generation_directory=self.generations,
            root_uid=self.uid,
            root_gid=self.gid,
            report=self.messages.append,
        )

    @property
    def live(self) -> pathlib.Path:
        return self.generations / self.generation.digest

    @property
    def intent(self) -> pathlib.Path:
        return self.generations / (f".remove-{self.generation.digest}.manifest")

    @property
    def retired(self) -> pathlib.Path:
        return self.generations / f".remove-{self.generation.digest}.tree"

    @property
    def publication_intent(self) -> pathlib.Path:
        return self.generations / f".publish-{self.generation.digest}.manifest"

    @property
    def publication_staging(self) -> pathlib.Path:
        return self.generations / f".publish-{self.generation.digest}.tree"

    def publish(self) -> None:
        self.store.publish(self.generation)

    def leave_prepared_removal(self) -> None:
        self.publish()

        def after_intent_fsync(path: pathlib.Path) -> bool:
            return (
                pathlib.Path(path) == self.generations
                and os.path.lexists(self.intent)
                and os.path.lexists(self.live)
                and not os.path.lexists(self.retired)
            )

        with self.interrupt_after(
            self.store,
            "_fsync_directory",
            after_intent_fsync,
        ):
            with self.test_interruption():
                self.store.remove(self.generation.digest)

    def leave_retired_removal(self) -> None:
        self.publish()

        def after_retirement(
            source: pathlib.Path,
            destination: pathlib.Path,
        ) -> bool:
            return (
                pathlib.Path(source) == self.live
                and pathlib.Path(destination) == self.retired
            )

        with self.interrupt_after(os, "rename", after_retirement):
            with self.test_interruption():
                self.store.remove(self.generation.digest)

    @staticmethod
    @contextlib.contextmanager
    def test_interruption() -> Iterator[None]:
        with unittest.TestCase().assertRaises(InjectedInterruption):
            yield

    @staticmethod
    @contextlib.contextmanager
    def interrupt_after(
        owner: object,
        attribute: str,
        predicate: Callable[..., bool],
    ) -> Iterator[None]:
        original = getattr(owner, attribute)
        interrupted = False

        def wrapped(*arguments: object, **keywords: object) -> object:
            nonlocal interrupted
            result = original(*arguments, **keywords)
            if not interrupted and predicate(*arguments, **keywords):
                interrupted = True
                raise InjectedInterruption(attribute)
            return result

        with mock.patch.object(owner, attribute, new=wrapped):
            yield
        if not interrupted:
            raise AssertionError(f"interruption point {attribute!r} was not reached")


class GenerationV1Test(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = StoreFixture(self)

    def test_generation_digest_and_manifest_keep_the_v1_wire_format(self) -> None:
        generation = self.fixture.generation

        self.assertEqual(generation.digest, EXPECTED_FIXTURE_DIGEST)
        self.assertTrue(generation.manifest.endswith(b"\n"))
        self.assertNotIn(b" ", generation.manifest)
        self.assertIn(
            b'"schema":"benchmarkd.generation.v1"',
            generation.manifest,
        )
        self.assertIn(
            f'"digest":"{EXPECTED_FIXTURE_DIGEST}"'.encode(),
            generation.manifest,
        )
        self.assertEqual(
            [entry.path.as_posix() for entry in generation.entries],
            sorted(entry.path.as_posix() for entry in generation.entries),
        )

    def test_publish_is_immutable_idempotent_and_fully_verified(self) -> None:
        first = self.fixture.store.publish(self.fixture.generation)
        first_inode = os.lstat(first.root).st_ino

        second = self.fixture.store.publish(self.fixture.generation)

        self.assertEqual(second.digest, EXPECTED_FIXTURE_DIGEST)
        self.assertEqual(os.lstat(second.root).st_ino, first_inode)
        self.assertEqual(stat.S_IMODE(os.lstat(second.root).st_mode), 0o555)
        for directory in (
            "bin",
            "lib",
            "lib/benchmark_lock",
            "share",
            "share/systemd",
            "share/sysusers",
        ):
            self.assertEqual(
                stat.S_IMODE(os.lstat(second.root / directory).st_mode),
                0o555,
            )
        self.assertEqual(
            self.fixture.store.require_quiescent(),
            (second,),
        )
        self.assertEqual(
            self.fixture.store.inventory_digests(),
            (second.digest,),
        )

    def test_publish_refuses_to_replace_modified_generation_bytes(self) -> None:
        self.fixture.publish()
        client = self.fixture.live / "bin/benchmark-lock"
        os.chmod(client, 0o755)

        with self.assertRaises(BenchmarkLockError):
            self.fixture.store.publish(self.fixture.generation)

        self.assertEqual(stat.S_IMODE(os.lstat(client).st_mode), 0o755)

    def test_publish_enforces_the_bounded_generation_inventory(self) -> None:
        self.fixture.publish()
        extra = self.fixture.source / "lib/benchmark_lock/second.py"
        extra.write_bytes(b"second\n")
        second = build_generation(self.fixture.source)

        with (
            mock.patch.object(generation_store, "MAX_GENERATIONS", 1),
            self.assertRaisesRegex(BenchmarkLockError, "generation limit"),
        ):
            self.fixture.store.publish(second)

        self.assertFalse((self.fixture.generations / second.digest).exists())


class GenerationPublicationRecoveryTest(unittest.TestCase):
    def test_every_persisted_publication_phase_resumes(self) -> None:
        phases = (
            "intent",
            "tree",
            "directory",
            "payload",
            "tree-manifest",
            "seal",
            "rename",
            "intent-unlink",
        )
        for phase in phases:
            with self.subTest(phase=phase):
                fixture = StoreFixture(self)
                first_payload = (
                    fixture.publication_staging / fixture.generation.entries[0].path
                )

                if phase == "intent":
                    owner = fixture.store
                    attribute = "_fsync_directory"

                    def predicate(path: pathlib.Path) -> bool:
                        return (
                            pathlib.Path(path) == fixture.generations
                            and fixture.publication_intent.is_file()
                            and not os.path.lexists(fixture.publication_staging)
                        )

                elif phase == "tree":
                    owner = os
                    attribute = "mkdir"

                    def predicate(path: pathlib.Path, _mode: int) -> bool:
                        return pathlib.Path(path) == fixture.publication_staging

                elif phase == "directory":
                    owner = os
                    attribute = "mkdir"
                    first_directory = fixture.publication_staging / "bin"

                    def predicate(path: pathlib.Path, _mode: int) -> bool:
                        return pathlib.Path(path) == first_directory

                elif phase == "payload":
                    owner = fixture.store
                    attribute = "_write_new_file"

                    def predicate(
                        path: pathlib.Path,
                        _content: bytes,
                        *,
                        mode: int,
                    ) -> bool:
                        return pathlib.Path(path) == first_payload and mode in {
                            0o444,
                            0o555,
                        }

                elif phase == "tree-manifest":
                    owner = os
                    attribute = "link"

                    def predicate(
                        _source: pathlib.Path,
                        destination: pathlib.Path,
                        *,
                        follow_symlinks: bool,
                    ) -> bool:
                        return (
                            pathlib.Path(destination)
                            == fixture.publication_staging / "manifest.json"
                            and not follow_symlinks
                        )

                elif phase == "seal":
                    owner = os
                    attribute = "chmod"

                    def predicate(path: pathlib.Path, mode: int) -> bool:
                        return (
                            fixture.publication_staging in pathlib.Path(path).parents
                            and mode == 0o555
                        )

                elif phase == "rename":
                    owner = os
                    attribute = "rename"

                    def predicate(
                        source: pathlib.Path,
                        destination: pathlib.Path,
                    ) -> bool:
                        return (
                            pathlib.Path(source) == fixture.publication_staging
                            and pathlib.Path(destination) == fixture.live
                        )

                else:
                    owner = os
                    attribute = "unlink"

                    def predicate(path: pathlib.Path) -> bool:
                        return pathlib.Path(path) == fixture.publication_intent

                with fixture.interrupt_after(owner, attribute, predicate):
                    with self.assertRaises(InjectedInterruption):
                        fixture.store.publish(fixture.generation)

                verified = fixture.new_store().publish(fixture.generation)
                self.assertEqual(verified.digest, fixture.generation.digest)
                self.assertEqual(
                    tuple(fixture.generations.iterdir()),
                    (fixture.live,),
                )

    def test_sigkill_during_build_and_commit_is_recovered(self) -> None:
        cases = ("payload", "rename")
        for case in cases:
            with self.subTest(case=case):
                fixture = StoreFixture(self)
                process_id = os.fork()
                if process_id == 0:
                    if case == "payload":
                        original = GenerationStore._write_new_file
                        target = (
                            fixture.publication_staging
                            / fixture.generation.entries[0].path
                        )

                        def write_then_kill(
                            store: GenerationStore,
                            path: pathlib.Path,
                            content: bytes,
                            *,
                            mode: int,
                        ) -> None:
                            original(store, path, content, mode=mode)
                            if pathlib.Path(path) == target:
                                os.kill(os.getpid(), signal.SIGKILL)

                        patch = mock.patch.object(
                            GenerationStore,
                            "_write_new_file",
                            new=write_then_kill,
                        )
                    else:
                        original_rename = os.rename

                        def rename_then_kill(
                            source: pathlib.Path,
                            destination: pathlib.Path,
                        ) -> None:
                            original_rename(source, destination)
                            if (
                                pathlib.Path(source) == fixture.publication_staging
                                and pathlib.Path(destination) == fixture.live
                            ):
                                os.kill(os.getpid(), signal.SIGKILL)

                        patch = mock.patch.object(
                            os,
                            "rename",
                            new=rename_then_kill,
                        )
                    with patch:
                        fixture.new_store().publish(fixture.generation)
                    os._exit(90)

                waited_process, status = os.waitpid(process_id, 0)
                self.assertEqual(waited_process, process_id)
                self.assertTrue(os.WIFSIGNALED(status))
                self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
                verified = fixture.new_store().publish(fixture.generation)
                self.assertEqual(verified.digest, fixture.generation.digest)
                self.assertEqual(
                    tuple(fixture.generations.iterdir()),
                    (fixture.live,),
                )

    def test_publication_nodes_are_exact_under_restrictive_umask(self) -> None:
        fixture = StoreFixture(self)
        prior_mask = os.umask(0o777)
        try:
            with fixture.interrupt_after(
                os,
                "mkdir",
                lambda path, mode: (
                    pathlib.Path(path) == fixture.publication_staging and mode == 0o700
                ),
            ):
                with self.assertRaises(InjectedInterruption):
                    fixture.store.publish(fixture.generation)
        finally:
            os.umask(prior_mask)

        metadata = os.lstat(fixture.publication_intent)
        self.assertTrue(stat.S_ISREG(metadata.st_mode))
        self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o444)
        self.assertEqual(metadata.st_uid, fixture.uid)
        self.assertEqual(metadata.st_gid, fixture.gid)
        self.assertEqual(
            fixture.publication_intent.read_bytes(),
            fixture.generation.manifest,
        )
        self.assertEqual(
            stat.S_IMODE(os.lstat(fixture.publication_staging).st_mode),
            0o700,
        )
        fixture.new_store().publish(fixture.generation)

    def test_hostile_staging_bytes_block_recovery_without_deletion(self) -> None:
        fixture = StoreFixture(self)
        target = fixture.publication_staging / fixture.generation.entries[0].path

        with fixture.interrupt_after(
            fixture.store,
            "_write_new_file",
            lambda path, _content, *, mode: (
                pathlib.Path(path) == target and mode in {0o444, 0o555}
            ),
        ):
            with self.assertRaises(InjectedInterruption):
                fixture.store.publish(fixture.generation)
        os.chmod(target, 0o644)
        target.write_bytes(b"hostile\n")
        os.chmod(target, fixture.generation.entries[0].mode)

        with self.assertRaises(BenchmarkLockError):
            fixture.new_store().publish(fixture.generation)

        self.assertEqual(target.read_bytes(), b"hostile\n")
        self.assertTrue(fixture.publication_intent.is_file())
        self.assertTrue(fixture.publication_staging.is_dir())

    def test_whole_store_is_preflighted_before_publication_recovery(self) -> None:
        fixture = StoreFixture(self)
        first = fixture.store.publish(fixture.generation)
        extra = fixture.source / "lib/benchmark_lock/second.py"
        extra.write_bytes(b"second\n")
        second = build_generation(fixture.source)
        second_staging = fixture.generations / f".publish-{second.digest}.tree"
        target = second_staging / second.entries[0].path

        with fixture.interrupt_after(
            fixture.store,
            "_write_new_file",
            lambda path, _content, *, mode: (
                pathlib.Path(path) == target and mode in {0o444, 0o555}
            ),
        ):
            with self.assertRaises(InjectedInterruption):
                fixture.store.publish(second)
        first_client = first.root / "bin/benchmark-lock"
        os.chmod(first_client, 0o755)

        with self.assertRaises(BenchmarkLockError):
            fixture.new_store().publish(second)

        self.assertTrue(target.is_file())
        self.assertTrue(
            (fixture.generations / f".publish-{second.digest}.manifest").is_file()
        )


class GenerationRemovalTest(unittest.TestCase):
    def test_complete_removal_leaves_no_store_artifacts(self) -> None:
        fixture = StoreFixture(self)
        fixture.publish()

        self.assertTrue(fixture.store.remove(fixture.generation.digest))

        self.assertEqual(tuple(fixture.generations.iterdir()), ())
        self.assertEqual(fixture.store.require_quiescent(), ())
        self.assertFalse(fixture.store.remove(fixture.generation.digest))

    def test_prepared_intent_is_the_exact_manifest_hard_link(self) -> None:
        fixture = StoreFixture(self)
        fixture.leave_prepared_removal()
        intent_metadata = os.lstat(fixture.intent)
        manifest_metadata = os.lstat(fixture.live / "manifest.json")

        self.assertEqual(
            (intent_metadata.st_dev, intent_metadata.st_ino),
            (manifest_metadata.st_dev, manifest_metadata.st_ino),
        )
        self.assertEqual(intent_metadata.st_nlink, 2)
        with self.assertRaisesRegex(
            BenchmarkLockError,
            "require recovery",
        ):
            fixture.store.require_quiescent()
        self.assertEqual(
            fixture.store.inventory_digests(),
            (fixture.generation.digest,),
        )

        self.assertEqual(
            fixture.new_store().recover_removals(),
            (fixture.generation.digest,),
        )
        self.assertEqual(tuple(fixture.generations.iterdir()), ())

    def test_selected_generation_is_never_retired_or_recovered(self) -> None:
        fixture = StoreFixture(self)
        fixture.leave_prepared_removal()

        with self.assertRaisesRegex(BenchmarkLockError, "selected"):
            fixture.new_store().recover_removals(
                protected_digest=fixture.generation.digest
            )

        self.assertTrue(fixture.live.is_dir())
        self.assertTrue(fixture.intent.is_file())
        self.assertFalse(os.path.lexists(fixture.retired))

    def test_every_persisted_removal_phase_resumes(self) -> None:
        phases = (
            "intent",
            "retire",
            "chmod",
            "payload",
            "tree-manifest",
            "directory",
            "tree",
            "intent-unlink",
        )
        for phase in phases:
            with self.subTest(phase=phase):
                fixture = StoreFixture(self)
                fixture.publish()
                first_payload = fixture.retired / fixture.generation.entries[0].path
                first_directory = fixture.retired / "lib/benchmark_lock"

                if phase == "intent":
                    owner = fixture.store
                    attribute = "_fsync_directory"

                    def predicate(path: pathlib.Path) -> bool:
                        return (
                            pathlib.Path(path) == fixture.generations
                            and os.path.lexists(fixture.intent)
                            and os.path.lexists(fixture.live)
                            and not os.path.lexists(fixture.retired)
                        )

                elif phase == "retire":
                    owner = os
                    attribute = "rename"

                    def predicate(
                        source: pathlib.Path,
                        destination: pathlib.Path,
                    ) -> bool:
                        return (
                            pathlib.Path(source) == fixture.live
                            and pathlib.Path(destination) == fixture.retired
                        )

                elif phase == "chmod":
                    owner = os
                    attribute = "chmod"

                    def predicate(path: pathlib.Path, mode: int) -> bool:
                        return (
                            fixture.retired in pathlib.Path(path).parents
                            and mode == 0o700
                        )

                elif phase == "payload":
                    owner = os
                    attribute = "unlink"

                    def predicate(path: pathlib.Path) -> bool:
                        return pathlib.Path(path) == first_payload

                elif phase == "tree-manifest":
                    owner = os
                    attribute = "unlink"

                    def predicate(path: pathlib.Path) -> bool:
                        return pathlib.Path(path) == (fixture.retired / "manifest.json")

                elif phase == "directory":
                    owner = os
                    attribute = "rmdir"

                    def predicate(path: pathlib.Path) -> bool:
                        return pathlib.Path(path) == first_directory

                elif phase == "tree":
                    owner = os
                    attribute = "rmdir"

                    def predicate(path: pathlib.Path) -> bool:
                        return pathlib.Path(path) == fixture.retired

                else:
                    owner = os
                    attribute = "unlink"

                    def predicate(path: pathlib.Path) -> bool:
                        return pathlib.Path(path) == fixture.intent

                with fixture.interrupt_after(owner, attribute, predicate):
                    with self.assertRaises(InjectedInterruption):
                        fixture.store.remove(fixture.generation.digest)

                fixture.new_store().recover_removals()
                self.assertEqual(tuple(fixture.generations.iterdir()), ())

    def test_multiple_generations_are_preflighted_before_recovery(self) -> None:
        fixture = StoreFixture(self)
        first = fixture.store.publish(fixture.generation)
        extra = fixture.source / "lib/benchmark_lock/second.py"
        extra.write_bytes(b"second\n")
        second_generation = build_generation(fixture.source)
        second = fixture.store.publish(second_generation)

        def after_retirement(
            source: pathlib.Path,
            destination: pathlib.Path,
        ) -> bool:
            return (
                pathlib.Path(source) == first.root
                and pathlib.Path(destination) == fixture.retired
            )

        with fixture.interrupt_after(os, "rename", after_retirement):
            with self.assertRaises(InjectedInterruption):
                fixture.store.remove(first.digest)
        second_client = second.root / "bin/benchmark-lock"
        os.chmod(second_client, 0o755)

        with self.assertRaises(BenchmarkLockError):
            fixture.new_store().recover_removals()

        self.assertTrue(fixture.retired.is_dir())
        self.assertTrue(fixture.intent.is_file())

    def test_empty_recovery_fsyncs_the_generation_directory(self) -> None:
        fixture = StoreFixture(self)
        synced: list[pathlib.Path] = []
        original = fixture.store._fsync_directory

        def record(path: pathlib.Path) -> None:
            original(path)
            synced.append(pathlib.Path(path))

        with mock.patch.object(fixture.store, "_fsync_directory", new=record):
            self.assertEqual(fixture.store.recover_removals(), ())

        self.assertEqual(synced, [fixture.generations])


class GenerationRemovalHostileStateTest(unittest.TestCase):
    def test_retired_tree_without_intent_is_never_traversed(self) -> None:
        fixture = StoreFixture(self)
        fixture.publish()
        os.rename(fixture.live, fixture.retired)
        sentinel = fixture.retired / "bin/benchmark-lock"

        with self.assertRaisesRegex(BenchmarkLockError, "lacks its removal intent"):
            fixture.store.recover_removals()

        self.assertTrue(sentinel.is_file())

    def test_unknown_store_entry_blocks_recovery(self) -> None:
        fixture = StoreFixture(self)
        fixture.publish()
        unknown = fixture.generations / "operator-file"
        unknown.write_text("retain\n")

        with self.assertRaisesRegex(BenchmarkLockError, "unknown"):
            fixture.store.recover_removals()

        self.assertEqual(unknown.read_text(), "retain\n")
        self.assertTrue(fixture.live.is_dir())

    def test_partial_canonical_generation_is_not_deletion_progress(self) -> None:
        fixture = StoreFixture(self)
        fixture.publish()
        payload = fixture.live / fixture.generation.entries[0].path
        os.chmod(payload.parent, 0o700)
        payload.unlink()

        with self.assertRaises(BenchmarkLockError):
            fixture.store.recover_removals()

        self.assertFalse(os.path.lexists(fixture.intent))
        self.assertTrue(fixture.live.is_dir())

    def test_symlink_or_modified_file_in_retired_tree_blocks_recovery(self) -> None:
        cases = ("symlink", "modified")
        for case in cases:
            with self.subTest(case=case):
                fixture = StoreFixture(self)
                fixture.leave_retired_removal()
                if case == "symlink":
                    os.chmod(fixture.retired, 0o700)
                    (fixture.retired / "unrecorded").symlink_to("/tmp")
                else:
                    payload = fixture.retired / fixture.generation.entries[0].path
                    os.chmod(payload, 0o644)
                    payload.write_bytes(b"changed\n")
                    os.chmod(payload, fixture.generation.entries[0].mode)

                with self.assertRaises(BenchmarkLockError):
                    fixture.new_store().recover_removals()

                self.assertTrue(fixture.retired.is_dir())
                self.assertTrue(fixture.intent.is_file())

    def test_unsafe_intent_type_or_mode_blocks_recovery(self) -> None:
        cases = ("symlink", "independent-copy", "mode")
        for case in cases:
            with self.subTest(case=case):
                fixture = StoreFixture(self)
                fixture.leave_prepared_removal()
                if case == "symlink":
                    fixture.intent.unlink()
                    fixture.intent.symlink_to(fixture.live / "manifest.json")
                elif case == "independent-copy":
                    payload = fixture.intent.read_bytes()
                    fixture.intent.unlink()
                    fixture.intent.write_bytes(payload)
                    os.chmod(fixture.intent, 0o444)
                else:
                    os.chmod(fixture.intent, 0o644)

                with self.assertRaises(BenchmarkLockError):
                    fixture.new_store().recover_removals()

                self.assertTrue(fixture.live.is_dir())

    def test_missing_payload_is_accepted_only_in_removal_order(self) -> None:
        fixture = StoreFixture(self)
        fixture.leave_retired_removal()
        for directory in (
            fixture.retired / "bin",
            fixture.retired / "lib",
            fixture.retired / "lib/benchmark_lock",
            fixture.retired / "share",
            fixture.retired / "share/systemd",
            fixture.retired / "share/sysusers",
            fixture.retired,
        ):
            os.chmod(directory, 0o700)
        out_of_order = fixture.retired / fixture.generation.entries[1].path
        out_of_order.unlink()

        with self.assertRaisesRegex(BenchmarkLockError, "not a prefix"):
            fixture.new_store().recover_removals()

        self.assertTrue(
            (fixture.retired / fixture.generation.entries[0].path).is_file()
        )
