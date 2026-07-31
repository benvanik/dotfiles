from __future__ import annotations

import fcntl
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from benchmark_lock.administration_state import (
    ADMIN_LOCK_PATH,
    INSTALL_ROOT,
    INSTALL_STATE_PATHS,
    STATE_DIRECTORY,
    UNINSTALL_STATE_PATHS,
    AdministrationAdmissionFence,
)
from benchmark_lock.errors import BenchmarkLockError


class AdministrationAdmissionFenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.state_directory = self.root / STATE_DIRECTORY.relative_to("/")
        self.lock_path = self.root / ADMIN_LOCK_PATH.relative_to("/")
        self.install_root = self.root / "usr/local/lib/benchmarkd"
        self.state_directory.mkdir(parents=True, mode=0o700)
        self.install_root.mkdir(parents=True, mode=0o755)
        self.state_directory.chmod(0o700)
        self.install_root.chmod(0o755)
        self.install_paths = tuple(
            self.install_root / path.relative_to(INSTALL_ROOT)
            for path in INSTALL_STATE_PATHS
        )
        self.uninstall_paths = tuple(
            self.install_root / path.relative_to(INSTALL_ROOT)
            for path in UNINSTALL_STATE_PATHS
        )
        self.fences: list[AdministrationAdmissionFence] = []

    def tearDown(self) -> None:
        for fence in self.fences:
            fence.close()
        self.temporary.cleanup()

    def fence(self) -> AdministrationAdmissionFence:
        fence = AdministrationAdmissionFence(
            admin_lock_path=self.lock_path,
            install_root=self.install_root,
            install_state_paths=self.install_paths,
            uninstall_state_paths=self.uninstall_paths,
            root_uid=os.getuid(),
            root_gid=os.getgid(),
        )
        self.fences.append(fence)
        return fence

    def test_exclusive_administrator_lock_is_a_live_restart_fence(self) -> None:
        descriptor = os.open(
            self.lock_path,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        self.addCleanup(os.close, descriptor)
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        fence = self.fence()
        scans = 0
        original = fence._uninstall_state_present

        def record_scan() -> bool:
            nonlocal scans
            scans += 1
            return original()

        fence._uninstall_state_present = record_scan

        self.assertTrue(fence.refresh())
        self.assertEqual(scans, 0)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        self.assertFalse(fence.refresh())
        self.assertEqual(scans, 1)

    def test_each_fixed_install_name_is_a_dynamic_fence(self) -> None:
        self.assertEqual(
            tuple(path.name for path in self.install_paths),
            (
                "install.json",
                ".install-publish.json",
                ".install-transition.json",
            ),
        )
        for path in self.install_paths:
            with self.subTest(path=path.name):
                fence = self.fence()
                path.write_bytes(b"partial or complete journal")
                path.chmod(0o444)
                self.assertTrue(fence.refresh())
                path.unlink()
                self.assertFalse(fence.refresh())
                fence.close()

    def test_each_fixed_uninstall_name_is_a_permanent_fence(self) -> None:
        for path in self.uninstall_paths:
            with self.subTest(path=path.name):
                fence = self.fence()
                path.write_bytes(b"partial or complete journal")
                path.chmod(0o444)
                self.assertTrue(fence.refresh())
                path.unlink()
                self.assertTrue(fence.refresh())
                fence.close()

    def test_shared_probe_prevents_a_mid_scan_administrator_cutover(self) -> None:
        fence = self.fence()
        observed: list[bool] = []

        original = fence._uninstall_state_present

        def inspect_while_contending() -> bool:
            contender = os.open(self.lock_path, os.O_RDWR | os.O_CLOEXEC)
            try:
                try:
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    observed.append(True)
                else:
                    observed.append(False)
                    fcntl.flock(contender, fcntl.LOCK_UN)
            finally:
                os.close(contender)
            return original()

        fence._uninstall_state_present = inspect_while_contending
        self.assertFalse(fence.refresh())
        self.assertEqual(observed, [True])

    def test_missing_or_unsafe_installation_state_fails_closed(self) -> None:
        self.install_root.rmdir()
        fence = self.fence()
        self.assertTrue(fence.refresh())
        self.assertIn("cannot inspect benchmark installation root", fence.reason)

        self.install_root.mkdir(mode=0o755)
        unsafe = self.uninstall_paths[0]
        unsafe.symlink_to(self.lock_path)
        second = self.fence()
        self.assertTrue(second.refresh())
        self.assertIn("unsafe metadata", second.reason)

    def test_unsafe_install_metadata_latches_fail_closed(self) -> None:
        unsafe = self.install_paths[0]
        unsafe.symlink_to(self.lock_path)
        fence = self.fence()

        self.assertTrue(fence.refresh())
        reason = fence.reason
        self.assertIsNotNone(reason)
        self.assertIn("unsafe metadata", reason)

        unsafe.unlink()
        self.assertTrue(fence.refresh())
        self.assertEqual(fence.reason, reason)

    def test_lock_metadata_is_validated_before_flock_authority(self) -> None:
        self.lock_path.write_bytes(b"")
        self.lock_path.chmod(0o644)
        with self.assertRaises(BenchmarkLockError) as caught:
            self.fence()
        self.assertEqual(
            caught.exception.code,
            "invalid_benchmark_administration_state",
        )

    def test_missing_lock_is_created_as_the_shared_authority_inode(self) -> None:
        fence = self.fence()
        metadata = os.lstat(self.lock_path)

        self.assertEqual(metadata.st_uid, os.getuid())
        self.assertEqual(metadata.st_gid, os.getgid())
        self.assertEqual(metadata.st_mode & 0o777, 0o600)
        self.assertEqual(metadata.st_nlink, 1)
        self.assertFalse(fence.refresh())

    def test_missing_lock_is_immediately_exact_under_restrictive_umask(
        self,
    ) -> None:
        real_fchmod = os.fchmod
        modes_before_fchmod: list[int] = []

        def record_mode_before_fchmod(descriptor: int, mode: int) -> None:
            metadata = os.fstat(descriptor)
            modes_before_fchmod.append(metadata.st_mode & 0o777)
            real_fchmod(descriptor, mode)

        previous_mask = os.umask(0o777)
        try:
            with mock.patch(
                "benchmark_lock.administration_state.os.fchmod",
                side_effect=record_mode_before_fchmod,
            ):
                fence = self.fence()
            current_mask = os.umask(0o777)
            os.umask(current_mask)
        finally:
            os.umask(previous_mask)

        self.assertEqual(modes_before_fchmod, [0o600])
        self.assertEqual(current_mask, 0o777)
        self.assertEqual(os.lstat(self.lock_path).st_mode & 0o777, 0o600)
        self.assertFalse(fence.refresh())

    def test_daemon_restart_reopens_the_retained_authority_inode(self) -> None:
        first = self.fence()
        first_metadata = os.fstat(first._admin_lock_descriptor)
        first.close()

        second = self.fence()
        second_metadata = os.fstat(second._admin_lock_descriptor)
        path_metadata = os.lstat(self.lock_path)

        self.assertEqual(
            (first_metadata.st_dev, first_metadata.st_ino),
            (second_metadata.st_dev, second_metadata.st_ino),
        )
        self.assertEqual(
            (second_metadata.st_dev, second_metadata.st_ino),
            (path_metadata.st_dev, path_metadata.st_ino),
        )
        self.assertFalse(second.refresh())

    def test_production_authority_is_inside_the_systemd_state_directory(
        self,
    ) -> None:
        self.assertEqual(STATE_DIRECTORY, pathlib.Path("/var/lib/benchmarkd"))
        self.assertEqual(ADMIN_LOCK_PATH, STATE_DIRECTORY / "admin.lock")


if __name__ == "__main__":
    unittest.main()
