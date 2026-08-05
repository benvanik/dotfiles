from __future__ import annotations

import json
import os
import pathlib
import stat
import unittest
from unittest import mock

from benchmark_lock.admin import (
    ADMIN_LOCK_PATH,
    CONFIG_PATH,
    CURRENT_SELECTOR,
    GENERATION_DIRECTORY,
    INSTALL_ROOT,
    SERVICE_UNIT_PATH,
    SOCKET_UNIT_PATH,
    STATE_DIRECTORY,
    SYSUSERS_PATH,
    UNINSTALL_INTENT_PATH,
    UNINSTALL_PUBLISH_PATH,
    UNINSTALL_TRANSITION_PATH,
)
from benchmark_lock.errors import BenchmarkLockError
from test_benchmark_admin import (
    FORBIDDEN_SYSTEM_CLIENT_PATH,
    AdminFixture,
    RejectingMaintenance,
)


class BenchmarkAdminUninstallTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = AdminFixture(self)

    def test_uninstall_requires_empty_scheduler_before_generation_scan(
        self,
    ) -> None:
        self.fixture.install()
        maintenance = RejectingMaintenance()
        admin = self.fixture.new_admin(maintenance=maintenance)
        self.fixture.runner.commands.clear()

        with (
            mock.patch.object(
                admin.generation_store,
                "verify",
            ) as verify_generation,
            mock.patch.object(
                admin.generation_store,
                "require_quiescent",
            ) as scan_generations,
            self.assertRaisesRegex(
                BenchmarkLockError,
                "busy benchmark scheduler",
            ),
        ):
            admin.uninstall()

        verify_generation.assert_not_called()
        scan_generations.assert_not_called()
        self.assertEqual(maintenance.entries, [True])
        self.assertEqual(self.fixture.runner.commands, [])
        self.assertTrue(self.fixture.mapped(CURRENT_SELECTOR).exists())

    def test_uninstall_removes_only_verified_software_and_retains_state(self) -> None:
        self.fixture.install()
        admin_lock = self.fixture.mapped(ADMIN_LOCK_PATH)
        lock_metadata = os.lstat(admin_lock)
        lock_identity = (lock_metadata.st_dev, lock_metadata.st_ino)
        state_marker = self.fixture.mapped(STATE_DIRECTORY) / "operator-note"
        state_marker.write_text("retain\n")
        self.fixture.runner.commands.clear()
        self.fixture.maintenance.events.clear()

        self.fixture.admin.uninstall()

        self.assertFalse(self.fixture.mapped(INSTALL_ROOT).exists())
        self.assertFalse(
            os.path.lexists(self.fixture.mapped(FORBIDDEN_SYSTEM_CLIENT_PATH))
        )
        self.assertFalse(self.fixture.mapped(SOCKET_UNIT_PATH).exists())
        self.assertFalse(self.fixture.mapped(SERVICE_UNIT_PATH).exists())
        self.assertFalse(self.fixture.mapped(SYSUSERS_PATH).exists())
        self.assertTrue(self.fixture.mapped(CONFIG_PATH).exists())
        self.assertEqual(state_marker.read_text(), "retain\n")
        retained_lock_metadata = os.lstat(admin_lock)
        self.assertEqual(
            (retained_lock_metadata.st_dev, retained_lock_metadata.st_ino),
            lock_identity,
        )
        self.assertEqual(stat.S_IMODE(retained_lock_metadata.st_mode), 0o600)
        self.assertEqual(
            self.fixture.maintenance.events,
            [("enter", True), ("exit", True)],
        )
        self.assertIn(
            (
                "/usr/bin/systemctl",
                "disable",
                "--now",
                "benchmarkd.socket",
            ),
            self.fixture.runner.commands,
        )

    def test_stop_failure_leaves_a_prepared_transaction_for_exact_retry(
        self,
    ) -> None:
        self.fixture.install()
        self.fixture.runner.commands.clear()
        self.fixture.maintenance.events.clear()
        stop_command = (
            "/usr/bin/systemctl",
            "stop",
            "benchmarkd.service",
        )
        self.fixture.runner.fail_command = stop_command

        with self.assertRaisesRegex(
            BenchmarkLockError,
            "injected administrator command failure",
        ):
            self.fixture.admin.uninstall()

        intent_path = self.fixture.mapped(UNINSTALL_INTENT_PATH)
        self.assertEqual(json.loads(intent_path.read_bytes())["phase"], "prepared")
        self.assertTrue(self.fixture.mapped(CURRENT_SELECTOR).exists())
        self.assertTrue(self.fixture.mapped(SOCKET_UNIT_PATH).exists())
        self.assertEqual(
            self.fixture.maintenance.events,
            [("enter", True), ("exit", True)],
        )

        self.fixture.runner.commands.clear()
        self.fixture.timeline.clear()
        self.fixture.admin.uninstall()

        self.assertFalse(self.fixture.mapped(INSTALL_ROOT).exists())
        self.assertEqual(
            self.fixture.maintenance.events,
            [("enter", True), ("exit", True)],
        )
        self.assertIn(stop_command, self.fixture.runner.commands)
        self.assertNotIn(
            (
                "/usr/bin/systemctl",
                "start",
                "benchmarkd.service",
            ),
            self.fixture.runner.commands,
        )

    def test_uninstall_publication_interruption_promotes_the_fixed_journal(
        self,
    ) -> None:
        self.fixture.install()
        publish_path = self.fixture.mapped(UNINSTALL_PUBLISH_PATH)
        intent_path = self.fixture.mapped(UNINSTALL_INTENT_PATH)
        original_rename = os.rename

        def interrupt_publication(
            source: os.PathLike[str] | str,
            destination: os.PathLike[str] | str,
        ) -> None:
            if (
                pathlib.Path(source) == publish_path
                and pathlib.Path(destination) == intent_path
            ):
                raise OSError("injected uninstall publication interruption")
            original_rename(source, destination)

        self.fixture.maintenance.events.clear()
        with (
            mock.patch("os.rename", side_effect=interrupt_publication),
            self.assertRaisesRegex(OSError, "publication interruption"),
        ):
            self.fixture.admin.uninstall()

        self.assertTrue(publish_path.is_file())
        self.assertFalse(os.path.lexists(intent_path))
        self.assertTrue(self.fixture.mapped(CURRENT_SELECTOR).exists())
        self.assertEqual(
            self.fixture.maintenance.events,
            [("enter", True), ("exit", True)],
        )

        self.fixture.admin.uninstall()

        self.assertFalse(self.fixture.mapped(INSTALL_ROOT).exists())
        self.assertEqual(
            self.fixture.maintenance.events,
            [("enter", True), ("exit", True)],
        )

    def test_exact_partial_uninstall_journal_is_recoverable_under_strict_umask(
        self,
    ) -> None:
        self.fixture.install()
        publish_path = self.fixture.mapped(UNINSTALL_PUBLISH_PATH)
        previous_umask = os.umask(0o0777)
        try:
            production_umask = os.umask(0)
            try:
                descriptor = os.open(
                    publish_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o600,
                )
            finally:
                os.umask(production_umask)
        finally:
            os.umask(previous_umask)
        try:
            os.write(descriptor, b'{"partial":')
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.assertEqual(stat.S_IMODE(os.lstat(publish_path).st_mode), 0o600)

        self.fixture.maintenance.events.clear()
        self.fixture.admin.uninstall()

        self.assertFalse(self.fixture.mapped(INSTALL_ROOT).exists())
        self.assertTrue(
            any(
                "discarding incomplete benchmark uninstall journal" in message
                for message in self.fixture.messages
            )
        )

    def test_uninstall_never_discards_an_unsafe_fixed_intermediate(self) -> None:
        self.fixture.install()
        publish_path = self.fixture.mapped(UNINSTALL_PUBLISH_PATH)
        publish_path.write_bytes(b'{"partial":')
        publish_path.chmod(0o666)

        with self.assertRaises(BenchmarkLockError):
            self.fixture.admin.uninstall()

        self.assertTrue(publish_path.exists())
        self.assertTrue(self.fixture.mapped(CURRENT_SELECTOR).exists())

    def test_stopped_transition_interruption_never_reopens_maintenance(
        self,
    ) -> None:
        self.fixture.install()
        transition_path = self.fixture.mapped(UNINSTALL_TRANSITION_PATH)
        intent_path = self.fixture.mapped(UNINSTALL_INTENT_PATH)
        original_replace = os.replace

        def interrupt_transition(
            source: os.PathLike[str] | str,
            destination: os.PathLike[str] | str,
        ) -> None:
            if (
                pathlib.Path(source) == transition_path
                and pathlib.Path(destination) == intent_path
            ):
                raise OSError("injected uninstall transition interruption")
            original_replace(source, destination)

        self.fixture.runner.commands.clear()
        self.fixture.maintenance.events.clear()
        with (
            mock.patch("os.replace", side_effect=interrupt_transition),
            self.assertRaisesRegex(OSError, "transition interruption"),
        ):
            self.fixture.admin.uninstall()

        self.assertEqual(json.loads(intent_path.read_bytes())["phase"], "prepared")
        self.assertEqual(
            json.loads(transition_path.read_bytes())["phase"],
            "stopped",
        )
        self.assertEqual(
            self.fixture.maintenance.events,
            [("enter", True), ("exit", True)],
        )

        self.fixture.runner.commands.clear()
        self.fixture.admin.uninstall()

        self.assertFalse(self.fixture.mapped(INSTALL_ROOT).exists())
        self.assertEqual(
            self.fixture.maintenance.events,
            [("enter", True), ("exit", True)],
        )
        self.assertNotIn(
            (
                "/usr/bin/systemctl",
                "stop",
                "benchmarkd.service",
            ),
            self.fixture.runner.commands,
        )

    def test_projection_failure_resumes_only_after_the_durable_stop(
        self,
    ) -> None:
        self.fixture.install()
        self.fixture.runner.commands.clear()
        self.fixture.maintenance.events.clear()
        original_remove = self.fixture.admin._remove_external_regular
        removal_count = 0

        def interrupt_second_removal(
            path: pathlib.Path,
            *,
            expected: bytes,
        ) -> None:
            nonlocal removal_count
            removal_count += 1
            if removal_count == 2:
                raise OSError("injected projected-file interruption")
            original_remove(path, expected=expected)

        with (
            mock.patch.object(
                self.fixture.admin,
                "_remove_external_regular",
                side_effect=interrupt_second_removal,
            ),
            self.assertRaisesRegex(OSError, "projected-file interruption"),
        ):
            self.fixture.admin.uninstall()

        intent_path = self.fixture.mapped(UNINSTALL_INTENT_PATH)
        self.assertEqual(json.loads(intent_path.read_bytes())["phase"], "stopped")
        self.assertTrue(self.fixture.mapped(CURRENT_SELECTOR).exists())
        self.assertFalse(self.fixture.mapped(SOCKET_UNIT_PATH).exists())
        self.assertTrue(self.fixture.mapped(SERVICE_UNIT_PATH).exists())
        self.assertEqual(
            self.fixture.maintenance.events,
            [("enter", True), ("exit", True)],
        )

        self.fixture.admin.uninstall()

        self.assertFalse(self.fixture.mapped(INSTALL_ROOT).exists())
        self.assertEqual(
            self.fixture.maintenance.events,
            [("enter", True), ("exit", True)],
        )

    def test_committed_uninstall_blocks_install_and_doctor(self) -> None:
        self.fixture.install()
        self.fixture.runner.fail_command = (
            "/usr/bin/systemctl",
            "stop",
            "benchmarkd.service",
        )
        with self.assertRaises(BenchmarkLockError):
            self.fixture.admin.uninstall()

        with self.assertRaisesRegex(BenchmarkLockError, "committed uninstall"):
            self.fixture.admin.install(
                gpu_bdfs=(),
                user_name="ben",
            )
        with self.assertRaisesRegex(BenchmarkLockError, "committed uninstall"):
            self.fixture.admin.doctor(user_name=None)

    def test_completed_generation_is_recognized_as_uninstall_progress(self) -> None:
        self.fixture.install()
        launcher = self.fixture.source_root / "benchmarkd/bin/benchmarkd"
        launcher.write_bytes(launcher.read_bytes() + b"\n")
        self.fixture.admin.install(
            gpu_bdfs=(),
            user_name="ben",
        )
        self.fixture.runner.commands.clear()
        self.fixture.maintenance.events.clear()
        original_remove = self.fixture.admin.generation_store.remove
        removal_count = 0

        def interrupt_after_complete_removal(
            digest: str,
            *,
            protected_digest: str | None = None,
        ) -> bool:
            nonlocal removal_count
            removed = original_remove(
                digest,
                protected_digest=protected_digest,
            )
            removal_count += 1
            if removal_count == 1:
                raise OSError("injected completed-generation interruption")
            return removed

        with (
            mock.patch.object(
                self.fixture.admin.generation_store,
                "remove",
                side_effect=interrupt_after_complete_removal,
            ),
            self.assertRaisesRegex(OSError, "completed-generation interruption"),
        ):
            self.fixture.admin.uninstall()

        self.assertFalse(os.path.lexists(self.fixture.mapped(CURRENT_SELECTOR)))
        self.assertEqual(
            json.loads(self.fixture.mapped(UNINSTALL_INTENT_PATH).read_bytes())[
                "phase"
            ],
            "stopped",
        )
        self.assertEqual(
            self.fixture.maintenance.events,
            [("enter", True), ("exit", True)],
        )

        self.fixture.admin.uninstall()

        self.assertFalse(self.fixture.mapped(INSTALL_ROOT).exists())
        self.assertEqual(
            self.fixture.maintenance.events,
            [("enter", True), ("exit", True)],
        )

    def test_empty_install_root_is_finalized_after_intent_commit(self) -> None:
        self.fixture.install()
        install_root = self.fixture.mapped(INSTALL_ROOT)
        original_rmdir = os.rmdir
        interrupted = False

        def interrupt_install_root(path: os.PathLike[str] | str) -> None:
            nonlocal interrupted
            if pathlib.Path(path) == install_root and not interrupted:
                interrupted = True
                raise OSError("injected final-directory interruption")
            original_rmdir(path)

        with (
            mock.patch("os.rmdir", side_effect=interrupt_install_root),
            self.assertRaisesRegex(OSError, "final-directory interruption"),
        ):
            self.fixture.admin.uninstall()

        self.assertTrue(install_root.is_dir())
        self.assertEqual(tuple(install_root.iterdir()), ())

        self.fixture.admin.uninstall()

        self.assertFalse(install_root.exists())

    def test_uninstall_refuses_unknown_generation_content(self) -> None:
        self.fixture.install()
        generations = self.fixture.mapped(GENERATION_DIRECTORY)
        (generations / "unknown").mkdir()
        command_count = len(self.fixture.runner.commands)

        with self.assertRaisesRegex(
            BenchmarkLockError,
            "unknown benchmark generation-store entry",
        ):
            self.fixture.admin.uninstall()

        self.assertEqual(len(self.fixture.runner.commands), command_count)
        self.assertTrue(self.fixture.mapped(CURRENT_SELECTOR).exists())

    def test_uninstall_refuses_modified_managed_projection(self) -> None:
        self.fixture.install()
        socket_unit = self.fixture.mapped(SOCKET_UNIT_PATH)
        socket_unit.write_bytes(
            b"# Managed by benchmark-admin.\n[Socket]\nSocketMode=0666\n"
        )
        self.fixture.runner.commands.clear()
        self.fixture.maintenance.events.clear()

        with self.assertRaisesRegex(
            BenchmarkLockError,
            "modified after installation",
        ):
            self.fixture.admin.uninstall()

        self.assertEqual(self.fixture.runner.commands, [])
        self.assertEqual(
            self.fixture.maintenance.events,
            [("enter", True), ("exit", True)],
        )
        self.assertTrue(self.fixture.mapped(CURRENT_SELECTOR).exists())

    def test_uninstall_requires_a_complete_projection_before_commit(self) -> None:
        self.fixture.install()
        self.fixture.mapped(SOCKET_UNIT_PATH).unlink()
        self.fixture.runner.commands.clear()
        self.fixture.maintenance.events.clear()

        with self.assertRaisesRegex(BenchmarkLockError, "cannot inspect managed file"):
            self.fixture.admin.uninstall()

        self.assertEqual(self.fixture.runner.commands, [])
        self.assertEqual(
            self.fixture.maintenance.events,
            [("enter", True), ("exit", True)],
        )
        self.assertFalse(os.path.lexists(self.fixture.mapped(UNINSTALL_INTENT_PATH)))
        self.assertTrue(self.fixture.mapped(CURRENT_SELECTOR).exists())

    def test_uninstall_preflights_every_generation_before_stopping_service(
        self,
    ) -> None:
        first_digest = self.fixture.install()
        launcher = self.fixture.source_root / "benchmarkd/bin/benchmarkd"
        launcher.write_bytes(launcher.read_bytes() + b"\n")
        self.fixture.admin.install(
            gpu_bdfs=(),
            user_name="ben",
        )
        first_broker = (
            self.fixture.mapped(GENERATION_DIRECTORY) / first_digest / "bin/benchmarkd"
        )
        os.chmod(first_broker, 0o755)
        self.fixture.runner.commands.clear()
        self.fixture.maintenance.events.clear()

        with self.assertRaisesRegex(
            BenchmarkLockError,
            "unsafe metadata",
        ):
            self.fixture.admin.uninstall()

        self.assertEqual(self.fixture.runner.commands, [])
        self.assertEqual(
            self.fixture.maintenance.events,
            [("enter", True), ("exit", True)],
        )
        self.assertTrue(self.fixture.mapped(CURRENT_SELECTOR).exists())

    def test_uninstall_refuses_projection_without_current_generation(self) -> None:
        self.fixture.install()
        self.fixture.mapped(CURRENT_SELECTOR).unlink()
        self.fixture.runner.commands.clear()
        self.fixture.maintenance.events.clear()

        with self.assertRaisesRegex(
            BenchmarkLockError,
            "live projection without a current generation",
        ):
            self.fixture.admin.uninstall()

        self.assertEqual(self.fixture.runner.commands, [])
        self.assertEqual(self.fixture.maintenance.events, [])
        self.assertTrue(self.fixture.mapped(SOCKET_UNIT_PATH).exists())

    def test_uninstall_rejects_symlinked_install_root_before_commands(self) -> None:
        self.fixture.install()
        install_root = self.fixture.mapped(INSTALL_ROOT)
        redirected = install_root.with_name("benchmarkd-redirected")
        install_root.rename(redirected)
        install_root.symlink_to(redirected)
        self.fixture.runner.commands.clear()
        self.fixture.maintenance.events.clear()

        with self.assertRaisesRegex(
            BenchmarkLockError,
            "unsafe ownership or mode",
        ):
            self.fixture.admin.uninstall()

        self.assertEqual(self.fixture.runner.commands, [])
        self.assertEqual(self.fixture.maintenance.events, [])
