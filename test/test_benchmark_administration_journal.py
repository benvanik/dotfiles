from __future__ import annotations

import dataclasses
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from benchmark_lock.administration_journal import (
    AdministrationJournal,
    InstallIntent,
    JournalPaths,
    UninstallIntent,
    canonical_install_intent,
    canonical_uninstall_intent,
    parse_install_intent,
    parse_uninstall_intent,
)
from benchmark_lock.errors import BenchmarkLockError


PRIOR_DIGEST = "1" * 64
TARGET_DIGEST = "2" * 64
OTHER_DIGEST = "3" * 64


class InjectedInterruption(RuntimeError):
    pass


class JournalFixture:
    def __init__(self, test: unittest.TestCase) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        test.addCleanup(self.temporary.cleanup)
        self.install_root = pathlib.Path(self.temporary.name) / "benchmarkd"
        self.install_root.mkdir(mode=0o755)
        self.uid = os.getuid()
        self.gid = os.getgid()
        self.messages: list[str] = []
        self.install_paths = JournalPaths(
            publish=self.install_root / ".install-publish.json",
            intent=self.install_root / "install.json",
            transition=self.install_root / ".install-transition.json",
        )
        self.uninstall_paths = JournalPaths(
            publish=self.install_root / ".uninstall-publish.json",
            intent=self.install_root / "uninstall.json",
            transition=self.install_root / ".uninstall-transition.json",
        )
        self.journal = self.new_journal()

    def new_journal(self) -> AdministrationJournal:
        return AdministrationJournal(
            install_paths=self.install_paths,
            uninstall_paths=self.uninstall_paths,
            root_uid=self.uid,
            root_gid=self.gid,
            report=self.messages.append,
        )

    @staticmethod
    def write(path: pathlib.Path, payload: bytes, *, mode: int = 0o444) -> None:
        path.write_bytes(payload)
        path.chmod(mode)

    def interrupt_after_staged_fsync(
        self,
        staged_path: pathlib.Path,
    ) -> mock._patch:
        original = self.journal._fsync_directory
        interrupted = False

        def fsync_then_interrupt(path: pathlib.Path) -> None:
            nonlocal interrupted
            original(path)
            if not interrupted and os.path.lexists(staged_path):
                interrupted = True
                raise InjectedInterruption(staged_path.name)

        return mock.patch.object(
            self.journal,
            "_fsync_directory",
            side_effect=fsync_then_interrupt,
        )


class InstallIntentSchemaTest(unittest.TestCase):
    def test_all_install_phases_and_nullable_prior_digest_round_trip(self) -> None:
        for prior_digest in (None, PRIOR_DIGEST):
            for phase in ("prepared", "stopped", "rollback"):
                with self.subTest(prior_digest=prior_digest, phase=phase):
                    intent = InstallIntent(
                        prior_digest=prior_digest,
                        target_digest=TARGET_DIGEST,
                        user_name="benchmark_user-1",
                        phase=phase,
                    )
                    payload = canonical_install_intent(intent)

                    self.assertEqual(payload[-1:], b"\n")
                    self.assertEqual(parse_install_intent(payload), intent)
                    self.assertEqual(
                        canonical_install_intent(parse_install_intent(payload)),
                        payload,
                    )

        self.assertEqual(
            canonical_install_intent(
                InstallIntent(
                    prior_digest=None,
                    target_digest=TARGET_DIGEST,
                    user_name="ben",
                    phase="prepared",
                )
            ),
            (
                b'{"phase":"prepared","prior_digest":null,'
                b'"schema":"benchmarkd.install.v1",'
                b'"target_digest":"' + TARGET_DIGEST.encode("ascii") + b'",'
                b'"user_name":"ben"}\n'
            ),
        )

    def test_install_digest_phase_and_user_fields_are_strict(self) -> None:
        valid = InstallIntent(
            prior_digest=PRIOR_DIGEST,
            target_digest=TARGET_DIGEST,
            user_name="ben",
            phase="prepared",
        )
        invalid_intents = (
            dataclasses.replace(valid, prior_digest="not-a-digest"),
            dataclasses.replace(valid, prior_digest=TARGET_DIGEST),
            dataclasses.replace(valid, target_digest="2" * 63),
            dataclasses.replace(valid, target_digest="A" * 64),
            dataclasses.replace(valid, target_digest=7),  # type: ignore[arg-type]
            dataclasses.replace(valid, user_name=""),
            dataclasses.replace(valid, user_name="Uppercase"),
            dataclasses.replace(valid, user_name="../ben"),
            dataclasses.replace(valid, user_name="a" * 33),
            dataclasses.replace(valid, phase="committed"),
        )

        for intent in invalid_intents:
            with self.subTest(intent=intent):
                with self.assertRaises(BenchmarkLockError) as caught:
                    parse_install_intent(canonical_install_intent(intent))
                self.assertEqual(
                    caught.exception.code,
                    "benchmark_admin_install_invalid",
                )

    def test_install_document_must_be_exact_canonical_json(self) -> None:
        intent = InstallIntent(
            prior_digest=None,
            target_digest=TARGET_DIGEST,
            user_name="ben",
            phase="prepared",
        )
        canonical = canonical_install_intent(intent)
        specimens = (
            canonical[:-1],
            canonical.replace(b',"schema"', b', "schema"'),
            canonical.replace(
                b'"schema":"benchmarkd.install.v1"',
                b'"schema":"benchmarkd.install.v2"',
            ),
            canonical.replace(
                b'"user_name":"ben"',
                b'"user_name":"ben","extra":false',
            ),
        )

        for payload in specimens:
            with self.subTest(payload=payload):
                with self.assertRaises(BenchmarkLockError) as caught:
                    parse_install_intent(payload)
                self.assertEqual(
                    caught.exception.code,
                    "benchmark_admin_install_invalid",
                )


class AdministrationJournalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = JournalFixture(self)
        self.prepared = InstallIntent(
            prior_digest=PRIOR_DIGEST,
            target_digest=TARGET_DIGEST,
            user_name="ben",
            phase="prepared",
        )

    def assert_only_intent(
        self,
        paths: JournalPaths,
        payload: bytes,
    ) -> None:
        self.assertFalse(os.path.lexists(paths.publish))
        self.assertTrue(os.path.lexists(paths.intent))
        self.assertFalse(os.path.lexists(paths.transition))
        self.assertEqual(paths.intent.read_bytes(), payload)
        self.assertEqual(os.lstat(paths.intent).st_mode & 0o777, 0o444)

    def test_publish_install_commits_one_exact_read_only_intent(self) -> None:
        self.fixture.journal.publish_install(self.prepared)

        self.assert_only_intent(
            self.fixture.install_paths,
            canonical_install_intent(self.prepared),
        )
        self.assertEqual(
            self.fixture.new_journal().recover_install(),
            self.prepared,
        )

    def test_recover_promotes_durable_install_publication(self) -> None:
        with self.fixture.interrupt_after_staged_fsync(
            self.fixture.install_paths.publish
        ):
            with self.assertRaises(InjectedInterruption):
                self.fixture.journal.publish_install(self.prepared)

        self.assertTrue(os.path.lexists(self.fixture.install_paths.publish))
        self.assertFalse(os.path.lexists(self.fixture.install_paths.intent))

        recovered = self.fixture.new_journal().recover_install()

        self.assertEqual(recovered, self.prepared)
        self.assert_only_intent(
            self.fixture.install_paths,
            canonical_install_intent(self.prepared),
        )

    def test_recover_completes_prepared_to_stopped_transition(self) -> None:
        self.fixture.journal.publish_install(self.prepared)
        with self.fixture.interrupt_after_staged_fsync(
            self.fixture.install_paths.transition
        ):
            with self.assertRaises(InjectedInterruption):
                self.fixture.journal.transition_install(
                    self.prepared,
                    phase="stopped",
                )

        self.assertEqual(
            self.fixture.install_paths.intent.read_bytes(),
            canonical_install_intent(self.prepared),
        )
        self.assertTrue(os.path.lexists(self.fixture.install_paths.transition))

        recovered = self.fixture.new_journal().recover_install()
        expected = dataclasses.replace(self.prepared, phase="stopped")

        self.assertEqual(recovered, expected)
        self.assert_only_intent(
            self.fixture.install_paths,
            canonical_install_intent(expected),
        )

    def test_recover_completes_stopped_to_rollback_transition(self) -> None:
        self.fixture.journal.publish_install(self.prepared)
        stopped = self.fixture.journal.transition_install(
            self.prepared,
            phase="stopped",
        )
        with self.fixture.interrupt_after_staged_fsync(
            self.fixture.install_paths.transition
        ):
            with self.assertRaises(InjectedInterruption):
                self.fixture.journal.transition_install(
                    stopped,
                    phase="rollback",
                )

        recovered = self.fixture.new_journal().recover_install()
        expected = dataclasses.replace(stopped, phase="rollback")

        self.assertEqual(recovered, expected)
        self.assert_only_intent(
            self.fixture.install_paths,
            canonical_install_intent(expected),
        )

    def test_incomplete_fixed_publish_is_discarded_before_commit(self) -> None:
        self.fixture.write(
            self.fixture.install_paths.publish,
            b'{"phase":"prepared"',
            mode=0o600,
        )

        self.assertIsNone(self.fixture.journal.recover_install())

        self.assertFalse(os.path.lexists(self.fixture.install_paths.publish))
        self.assertEqual(len(self.fixture.messages), 1)
        self.assertIn("discarding incomplete", self.fixture.messages[0])
        self.assertIn(".install-publish.json", self.fixture.messages[0])

    def test_incomplete_fixed_transition_preserves_prepared_intent(self) -> None:
        self.fixture.write(
            self.fixture.install_paths.intent,
            canonical_install_intent(self.prepared),
        )
        self.fixture.write(
            self.fixture.install_paths.transition,
            b"{",
            mode=0o600,
        )

        recovered = self.fixture.journal.recover_install()

        self.assertEqual(recovered, self.prepared)
        self.assert_only_intent(
            self.fixture.install_paths,
            canonical_install_intent(self.prepared),
        )
        self.assertEqual(len(self.fixture.messages), 1)
        self.assertIn(".install-transition.json", self.fixture.messages[0])

    def test_committed_partial_intent_fails_closed_without_discard(self) -> None:
        self.fixture.write(
            self.fixture.install_paths.intent,
            b"{",
            mode=0o600,
        )

        with self.assertRaises(BenchmarkLockError) as caught:
            self.fixture.journal.recover_install()

        self.assertEqual(
            caught.exception.code,
            "benchmark_admin_install_invalid",
        )
        self.assertTrue(os.path.lexists(self.fixture.install_paths.intent))

    def test_unsafe_fixed_publish_fails_closed_without_discard(self) -> None:
        self.fixture.install_paths.publish.symlink_to(self.fixture.install_paths.intent)

        with self.assertRaises(BenchmarkLockError) as caught:
            self.fixture.journal.recover_install()

        self.assertEqual(
            caught.exception.code,
            "benchmark_admin_install_invalid",
        )
        self.assertTrue(self.fixture.install_paths.publish.is_symlink())

    def test_malformed_read_only_stage_fails_closed_without_discard(self) -> None:
        self.fixture.write(
            self.fixture.install_paths.publish,
            b"{",
            mode=0o444,
        )

        with self.assertRaises(BenchmarkLockError) as caught:
            self.fixture.journal.recover_install()

        self.assertEqual(
            caught.exception.code,
            "benchmark_admin_install_invalid",
        )
        self.assertEqual(self.fixture.install_paths.publish.read_bytes(), b"{")
        self.assertEqual(
            os.lstat(self.fixture.install_paths.publish).st_mode & 0o777,
            0o444,
        )

    def test_overlapping_publication_and_intent_fail_closed(self) -> None:
        self.fixture.write(
            self.fixture.install_paths.publish,
            canonical_install_intent(self.prepared),
        )
        self.fixture.write(
            self.fixture.install_paths.intent,
            canonical_install_intent(self.prepared),
        )

        with self.assertRaises(BenchmarkLockError) as caught:
            self.fixture.journal.recover_install()

        self.assertEqual(
            caught.exception.code,
            "benchmark_admin_install_invalid",
        )
        self.assertTrue(os.path.lexists(self.fixture.install_paths.publish))
        self.assertTrue(os.path.lexists(self.fixture.install_paths.intent))

    def test_transition_without_prior_intent_fails_closed(self) -> None:
        stopped = dataclasses.replace(self.prepared, phase="stopped")
        self.fixture.write(
            self.fixture.install_paths.transition,
            canonical_install_intent(stopped),
        )

        with self.assertRaises(BenchmarkLockError) as caught:
            self.fixture.journal.recover_install()

        self.assertEqual(
            caught.exception.code,
            "benchmark_admin_install_invalid",
        )
        self.assertTrue(os.path.lexists(self.fixture.install_paths.transition))

    def test_transition_cannot_change_recorded_install_closure(self) -> None:
        stopped = dataclasses.replace(
            self.prepared,
            target_digest=OTHER_DIGEST,
            phase="stopped",
        )
        self.fixture.write(
            self.fixture.install_paths.intent,
            canonical_install_intent(self.prepared),
        )
        self.fixture.write(
            self.fixture.install_paths.transition,
            canonical_install_intent(stopped),
        )

        with self.assertRaises(BenchmarkLockError) as caught:
            self.fixture.journal.recover_install()

        self.assertEqual(
            caught.exception.code,
            "benchmark_admin_install_invalid",
        )
        self.assertTrue(os.path.lexists(self.fixture.install_paths.transition))

    def test_install_and_uninstall_journals_conflict(self) -> None:
        uninstall = UninstallIntent(
            current_digest=PRIOR_DIGEST,
            generation_digests=(PRIOR_DIGEST,),
            phase="prepared",
        )
        self.fixture.write(
            self.fixture.install_paths.intent,
            canonical_install_intent(self.prepared),
        )
        self.fixture.write(
            self.fixture.uninstall_paths.intent,
            canonical_uninstall_intent(uninstall),
        )

        for recover in (
            self.fixture.journal.recover_install,
            self.fixture.journal.recover_uninstall,
        ):
            with self.subTest(recover=recover.__name__):
                with self.assertRaises(BenchmarkLockError) as caught:
                    recover()
                self.assertEqual(
                    caught.exception.code,
                    "benchmark_admin_transaction_conflict",
                )

    def test_publish_refuses_an_opposite_family_journal(self) -> None:
        uninstall = UninstallIntent(
            current_digest=PRIOR_DIGEST,
            generation_digests=(PRIOR_DIGEST,),
            phase="prepared",
        )
        self.fixture.write(
            self.fixture.uninstall_paths.intent,
            canonical_uninstall_intent(uninstall),
        )

        with self.assertRaises(BenchmarkLockError) as caught:
            self.fixture.journal.publish_install(self.prepared)

        self.assertEqual(
            caught.exception.code,
            "benchmark_admin_transaction_conflict",
        )
        self.assertFalse(self.fixture.journal.has_install_state())
        self.assertTrue(self.fixture.journal.has_uninstall_state())

    def test_uninstall_publish_transition_and_recovery_regression(self) -> None:
        prepared = UninstallIntent(
            current_digest=PRIOR_DIGEST,
            generation_digests=(PRIOR_DIGEST, TARGET_DIGEST),
            phase="prepared",
        )

        self.fixture.journal.publish_uninstall(prepared)
        self.assertEqual(
            self.fixture.new_journal().recover_uninstall(),
            prepared,
        )
        stopped = self.fixture.journal.transition_uninstall(
            prepared,
            phase="stopped",
        )

        self.assertEqual(stopped, dataclasses.replace(prepared, phase="stopped"))
        self.assertEqual(
            self.fixture.new_journal().recover_uninstall(),
            stopped,
        )
        self.assert_only_intent(
            self.fixture.uninstall_paths,
            canonical_uninstall_intent(stopped),
        )
        self.assertEqual(
            parse_uninstall_intent(
                canonical_uninstall_intent(stopped),
            ),
            stopped,
        )

    def test_impossible_uninstall_stopped_transition_fails_closed(self) -> None:
        stopped = UninstallIntent(
            current_digest=PRIOR_DIGEST,
            generation_digests=(PRIOR_DIGEST,),
            phase="stopped",
        )
        payload = canonical_uninstall_intent(stopped)
        self.fixture.write(self.fixture.uninstall_paths.intent, payload)
        self.fixture.write(self.fixture.uninstall_paths.transition, payload)

        with self.assertRaises(BenchmarkLockError) as caught:
            self.fixture.journal.recover_uninstall()

        self.assertEqual(
            caught.exception.code,
            "benchmark_admin_uninstall_invalid",
        )
        self.assertTrue(os.path.lexists(self.fixture.uninstall_paths.transition))

    def test_new_journal_file_is_immediately_0600_under_restrictive_umask(
        self,
    ) -> None:
        real_fchown = os.fchown
        modes_before_fchown: list[int] = []

        def observe_initial_mode(
            descriptor: int,
            user_id: int,
            group_id: int,
        ) -> None:
            modes_before_fchown.append(os.fstat(descriptor).st_mode & 0o777)
            real_fchown(descriptor, user_id, group_id)

        previous_mask = os.umask(0o777)
        observed_mask = -1
        try:
            with mock.patch(
                "benchmark_lock.administration_journal.os.fchown",
                side_effect=observe_initial_mode,
            ):
                self.fixture.journal.publish_install(self.prepared)
            observed_mask = os.umask(0o777)
            os.umask(observed_mask)
        finally:
            os.umask(previous_mask)

        self.assertEqual(modes_before_fchown, [0o600])
        self.assertEqual(observed_mask, 0o777)
        self.assertEqual(
            os.lstat(self.fixture.install_paths.intent).st_mode & 0o777,
            0o444,
        )


if __name__ == "__main__":
    unittest.main()
