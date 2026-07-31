from __future__ import annotations

import os
import pathlib
import stat
import tempfile
import unittest

from benchmark_lock.errors import BenchmarkLockError
from benchmark_lock.installation_projection import (
    InstallationProjection,
    RegularProjection,
    SymlinkProjection,
    publish_new_regular,
)


class InstallationProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.uid = os.getuid()
        self.gid = os.getgid()
        self.regular_a = RegularProjection(
            description="unit a",
            destination=self.root / "a",
            stage=self.root / ".a.install",
            prior=b"old-a\n",
            target=b"new-a\n",
        )
        self.regular_b = RegularProjection(
            description="unit b",
            destination=self.root / "b",
            stage=self.root / ".b.install",
            prior=b"old-b\n",
            target=b"new-b\n",
        )
        self.selector = SymlinkProjection(
            description="selector",
            destination=self.root / "current",
            stage=self.root / ".current.install",
            prior="generations/old",
            target="generations/new",
        )

    def _projection(
        self,
        *items: RegularProjection | SymlinkProjection,
    ) -> InstallationProjection:
        return InstallationProjection(
            items=items,
            root_uid=self.uid,
            root_gid=self.gid,
            report=lambda _message: None,
        )

    @staticmethod
    def _write(path: pathlib.Path, payload: bytes) -> None:
        path.write_bytes(payload)
        path.chmod(0o644)

    def _install_prior(self) -> InstallationProjection:
        self._write(self.regular_a.destination, self.regular_a.prior)
        self._write(self.regular_b.destination, self.regular_b.prior)
        self.selector.destination.symlink_to(self.selector.prior)
        return self._projection(
            self.regular_a,
            self.regular_b,
            self.selector,
        )

    def _install_target(self) -> InstallationProjection:
        self._write(self.regular_a.destination, self.regular_a.target)
        self._write(self.regular_b.destination, self.regular_b.target)
        self.selector.destination.symlink_to(self.selector.target)
        return self._projection(
            self.regular_a,
            self.regular_b,
            self.selector,
        )

    def test_existing_projection_converges_target_then_prior(self) -> None:
        projection = self._install_prior()

        projection.converge_target()
        projection.require_exact_target()
        projection.converge_prior()
        projection.require_exact_prior()

        self.assertFalse(any(os.path.lexists(path) for path in projection.stage_paths))

    def test_complete_fixed_regular_stage_is_replayed(self) -> None:
        projection = self._install_prior()
        publish_new_regular(
            self.regular_a.stage,
            self.regular_a.target,
            mode=0o644,
            root_uid=self.uid,
            root_gid=self.gid,
        )

        projection.require_target_prefix()
        projection.converge_target()

        self.assertEqual(
            self.regular_a.destination.read_bytes(),
            self.regular_a.target,
        )
        self.assertFalse(os.path.lexists(self.regular_a.stage))

    def test_complete_fixed_symlink_stage_is_replayed(self) -> None:
        projection = self._install_prior()
        self._write(self.regular_a.destination, self.regular_a.target)
        self._write(self.regular_b.destination, self.regular_b.target)
        self.selector.stage.symlink_to(self.selector.target)

        projection.require_target_prefix()
        projection.converge_target()

        self.assertEqual(
            os.readlink(self.selector.destination),
            self.selector.target,
        )
        self.assertFalse(os.path.lexists(self.selector.stage))

    def test_first_install_converges_and_removes_in_reverse_prefix(self) -> None:
        items = (
            RegularProjection(
                description="unit",
                destination=self.root / "fresh-unit",
                stage=self.root / ".fresh-unit.install",
                prior=None,
                target=b"target\n",
            ),
            SymlinkProjection(
                description="selector",
                destination=self.root / "fresh-current",
                stage=self.root / ".fresh-current.install",
                prior=None,
                target="generations/target",
            ),
        )
        projection = self._projection(*items)

        projection.converge_target()
        os.unlink(items[-1].destination)
        projection.require_removal_prefix()
        projection.remove_target()

        projection.require_exact_prior()

    def test_nonprefix_projection_fails_closed(self) -> None:
        projection = self._install_prior()
        self._write(self.regular_b.destination, self.regular_b.target)

        with self.assertRaisesRegex(BenchmarkLockError, "not a deterministic"):
            projection.require_target_prefix()

    def test_more_than_one_stage_fails_closed(self) -> None:
        projection = self._install_prior()
        for item in (self.regular_a, self.regular_b):
            publish_new_regular(
                item.stage,
                item.target,
                mode=0o644,
                root_uid=self.uid,
                root_gid=self.gid,
            )

        with self.assertRaisesRegex(BenchmarkLockError, "more than one"):
            projection.require_target_prefix()

    def test_wrong_stage_content_and_target_fail_closed(self) -> None:
        projection = self._install_prior()
        publish_new_regular(
            self.regular_a.stage,
            b"bad\n",
            mode=0o644,
            root_uid=self.uid,
            root_gid=self.gid,
        )
        with self.assertRaisesRegex(BenchmarkLockError, "wrong content"):
            projection.require_target_prefix()

        os.unlink(self.regular_a.stage)
        self.selector.stage.symlink_to("generations/foreign")
        with self.assertRaisesRegex(BenchmarkLockError, "wrong content"):
            projection.require_target_prefix()

    def test_unsafe_destination_metadata_fails_closed(self) -> None:
        projection = self._install_prior()
        self.regular_a.destination.chmod(0o666)

        with self.assertRaisesRegex(BenchmarkLockError, "unsafe metadata"):
            projection.require_exact_prior()

    def test_restrictive_umask_cannot_weaken_visible_publication(self) -> None:
        destination = self.root / "published"
        previous_mask = os.umask(0o0777)
        try:
            publish_new_regular(
                destination,
                b"complete\n",
                mode=0o644,
                root_uid=self.uid,
                root_gid=self.gid,
            )
        finally:
            os.umask(previous_mask)

        metadata = os.lstat(destination)
        self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o644)
        self.assertEqual(metadata.st_nlink, 1)
        self.assertEqual(destination.read_bytes(), b"complete\n")


if __name__ == "__main__":
    unittest.main()
