from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import tempfile
import unittest

from benchmark_lock.errors import BenchmarkLockError
from benchmark_lock.policy import (
    AmdGpuIdentity,
    EpochJournal,
    FixedHostPolicy,
    FixedHostPolicyConfig,
    LinuxHostFilesystem,
    PowerProfileHold,
    PowerProfileStatus,
)


_BOOT_ID = "11111111-2222-4333-8444-555555555555"
_NEXT_BOOT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_APPLICATION_ID = "com.benchmark-lock.host-policy"
_REASON = "exclusive benchmark lease"


class FakePowerProfiles:
    def __init__(self) -> None:
        self.active_profile = "balanced"
        self.performance_degraded = ""
        self.profiles = ("power-saver", "balanced", "performance")
        self.holds: tuple[PowerProfileHold, ...] = ()
        self.hold_calls = 0
        self.release_calls = 0
        self.before_hold = lambda: None
        self._baseline = self.active_profile
        self._cookie = 41

    def status(self) -> PowerProfileStatus:
        return PowerProfileStatus(
            active_profile=self.active_profile,
            performance_degraded=self.performance_degraded,
            profiles=self.profiles,
            holds=self.holds,
        )

    def hold_performance(self, *, reason: str, application_id: str) -> object:
        self.before_hold()
        self.hold_calls += 1
        self._baseline = self.active_profile
        self.holds = (
            PowerProfileHold(
                profile="performance",
                application_id=application_id,
                reason=reason,
            ),
        )
        self.active_profile = "performance"
        return self._cookie

    def release(self, cookie: object) -> None:
        if cookie != self._cookie or not self.holds:
            raise BenchmarkLockError(
                "unknown fake PPD hold",
                code="benchmark_policy_restore_failed",
            )
        self.release_calls += 1
        self.holds = ()
        self.active_profile = self._baseline

    def manual_override(self, profile: str) -> None:
        # PPD's real contract releases every hold when the user explicitly
        # selects an ActiveProfile.
        self.holds = ()
        self.active_profile = profile


class NonRestoringPowerProfiles(FakePowerProfiles):
    def release(self, cookie: object) -> None:
        if cookie != self._cookie or not self.holds:
            raise BenchmarkLockError(
                "unknown fake PPD hold",
                code="benchmark_policy_restore_failed",
            )
        self.release_calls += 1
        self.holds = ()
        self.active_profile = "performance"


class LostHoldReplyPowerProfiles(FakePowerProfiles):
    def hold_performance(self, *, reason: str, application_id: str) -> object:
        super().hold_performance(
            reason=reason,
            application_id=application_id,
        )
        raise BenchmarkLockError(
            "injected lost hold reply",
            code="benchmark_policy_unavailable",
        )


class RecordingFilesystem:
    def __init__(
        self,
        delegate: LinuxHostFilesystem,
        *,
        journal_path: pathlib.Path,
    ) -> None:
        self.delegate = delegate
        self.journal_path = journal_path
        self.writes: list[tuple[str, str]] = []
        self.kfd_checks = 0
        self.fail_write_number: int | None = None
        self.fail_restore = False

    def boot_id(self) -> str:
        return self.delegate.boot_id()

    def assert_kfd_clean(self) -> None:
        self.kfd_checks += 1
        self.delegate.assert_kfd_clean()

    def assert_gpu_identity(self, expected: AmdGpuIdentity) -> None:
        self.delegate.assert_gpu_identity(expected)

    def read_gpu_level(self, expected: AmdGpuIdentity) -> str:
        return self.delegate.read_gpu_level(expected)

    def write_gpu_level(
        self,
        expected: AmdGpuIdentity,
        value: str,
    ) -> None:
        if not self.journal_path.exists():
            raise AssertionError("sysfs mutation preceded durable journal")
        self.writes.append((expected.bdf, value))
        self.delegate.write_gpu_level(expected, value)
        write_number = len(self.writes)
        if self.fail_write_number == write_number:
            self.fail_write_number = None
            raise BenchmarkLockError(
                "injected write failure after mutation",
                code="benchmark_policy_apply_failed",
            )
        if self.fail_restore and value != "high":
            self.fail_restore = False
            self.delegate.write_gpu_level(expected, "high")
            raise BenchmarkLockError(
                "injected restoration readback failure",
                code="benchmark_policy_restore_failed",
            )


class FailingJournal(EpochJournal):
    def commit(self, epoch) -> None:
        del epoch
        raise BenchmarkLockError(
            "injected journal commit failure",
            code="benchmark_policy_journal_failed",
        )


class PolicyFixture:
    def __init__(self, test: unittest.TestCase, *, gpu_count: int = 1) -> None:
        self.test = test
        self.temporary = tempfile.TemporaryDirectory()
        test.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.sysfs_root = self.root / "sys"
        self.devices_root = self.sysfs_root / "devices"
        self.pci_links = self.sysfs_root / "bus/pci/devices"
        self.kfd_processes = self.sysfs_root / "class/kfd/kfd/proc"
        self.boot_id_path = self.root / "boot_id"
        self.state_root = self.root / "state"
        self.journal_path = self.state_root / "active-epoch.json"
        self.devices_root.mkdir(parents=True)
        self.pci_links.mkdir(parents=True)
        self.kfd_processes.mkdir(parents=True)
        self.state_root.mkdir(mode=0o700)
        self.boot_id_path.write_text(f"{_BOOT_ID}\n", encoding="ascii")
        self.identities = tuple(self._create_gpu(index) for index in range(gpu_count))
        self.config = FixedHostPolicyConfig(self.identities)
        self.linux_filesystem = LinuxHostFilesystem(
            sysfs_root=self.sysfs_root,
            boot_id_path=self.boot_id_path,
        )
        self.filesystem = RecordingFilesystem(
            self.linux_filesystem,
            journal_path=self.journal_path,
        )
        self.journal = EpochJournal(
            self.journal_path,
            owner_uid=os.getuid(),
        )
        self.power_profiles = FakePowerProfiles()

    def _create_gpu(self, index: int) -> AmdGpuIdentity:
        bdf = f"0000:{index + 1:02x}:00.0"
        identity = AmdGpuIdentity(
            bdf=bdf,
            vendor="0x1002",
            device=f"0x{0x744C + index:04x}",
            subsystem_vendor="0x1eae",
            subsystem_device=f"0x{0x7901 + index:04x}",
            revision="0xc8",
            unique_id=f"4610468131039e{index:x}",
        )
        device = self.devices_root / f"pci0000:{index + 1:02x}" / bdf
        device.mkdir(parents=True)
        values = {
            "vendor": identity.vendor,
            "device": identity.device,
            "subsystem_vendor": identity.subsystem_vendor,
            "subsystem_device": identity.subsystem_device,
            "revision": identity.revision,
            "unique_id": identity.unique_id,
            "class": identity.device_class,
            "power_dpm_force_performance_level": "auto",
        }
        for name, value in values.items():
            (device / name).write_text(f"{value}\n", encoding="ascii")
        (self.pci_links / bdf).symlink_to(device)
        return identity

    def policy(
        self,
        *,
        journal: EpochJournal | None = None,
        power_profiles: FakePowerProfiles | None = None,
        filesystem: RecordingFilesystem | None = None,
    ) -> FixedHostPolicy:
        return FixedHostPolicy(
            self.config,
            power_profiles=(
                self.power_profiles if power_profiles is None else power_profiles
            ),
            filesystem=self.filesystem if filesystem is None else filesystem,
            journal=self.journal if journal is None else journal,
        )

    def level_path(self, identity: AmdGpuIdentity) -> pathlib.Path:
        return (
            self.pci_links / identity.bdf
        ).resolve() / "power_dpm_force_performance_level"

    def level(self, identity: AmdGpuIdentity) -> str:
        return self.level_path(identity).read_text(encoding="ascii").strip()

    def set_identity_field(
        self,
        identity: AmdGpuIdentity,
        field: str,
        value: str,
    ) -> None:
        filename = "class" if field == "device_class" else field
        ((self.pci_links / identity.bdf).resolve() / filename).write_text(
            f"{value}\n",
            encoding="ascii",
        )


class FixedHostPolicyConfigTest(unittest.TestCase):
    def test_config_contains_only_fixed_hardware_identity(self) -> None:
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(FixedHostPolicyConfig)),
            ("gpus", "policy_identity"),
        )
        with self.assertRaises(ValueError):
            AmdGpuIdentity(
                bdf="/sys/class/drm/card0",
                vendor="0x1002",
                device="0x744c",
                subsystem_vendor="0x1eae",
                subsystem_device="0x7901",
                revision="0xc8",
                unique_id="1",
            )
        with self.assertRaises(ValueError):
            AmdGpuIdentity(
                bdf="0000:01:00.0",
                vendor="0x10de",
                device="0x744c",
                subsystem_vendor="0x1eae",
                subsystem_device="0x7901",
                revision="0xc8",
                unique_id="1",
            )

    def test_policy_identity_matches_the_grant_protocol(self) -> None:
        identity = AmdGpuIdentity(
            bdf="0000:01:00.0",
            vendor="0x1002",
            device="0x744c",
            subsystem_vendor="0x1eae",
            subsystem_device="0x7901",
            revision="0xc8",
            unique_id="1",
        )
        self.assertEqual(
            FixedHostPolicyConfig(
                (identity,),
                policy_identity="a" + "b" * 62,
            ).policy_identity,
            "a" + "b" * 62,
        )
        for invalid in ("1numeric", "has_underscore", "has.dot", "a" * 64):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                FixedHostPolicyConfig((identity,), policy_identity=invalid)


class FixedHostPolicyTest(unittest.TestCase):
    def test_enter_and_leave_are_exact_and_journal_precedes_mutation(
        self,
    ) -> None:
        fixture = PolicyFixture(self)
        policy = fixture.policy()
        fixture.power_profiles.before_hold = lambda: self.assertTrue(
            fixture.journal_path.exists()
        )

        policy.enter()

        self.assertEqual(policy.identity, "amd-performance-v1")
        self.assertEqual(policy.state, "held")
        self.assertEqual(fixture.level(fixture.identities[0]), "high")
        self.assertEqual(fixture.power_profiles.active_profile, "performance")
        self.assertEqual(fixture.power_profiles.hold_calls, 1)
        self.assertEqual(fixture.filesystem.writes, [("0000:01:00.0", "high")])
        journal_payload = fixture.journal_path.read_bytes()
        self.assertTrue(journal_payload.endswith(b"\n"))
        self.assertNotIn(b" ", journal_payload)
        document = json.loads(journal_payload)
        self.assertEqual(document["gpus"][0]["baseline_level"], "auto")
        self.assertEqual(
            fixture.journal_path.stat().st_mode & 0o777,
            0o600,
        )

        policy.verify()
        policy.leave()

        self.assertEqual(policy.state, "idle")
        self.assertEqual(fixture.level(fixture.identities[0]), "auto")
        self.assertEqual(fixture.power_profiles.active_profile, "balanced")
        self.assertEqual(fixture.power_profiles.release_calls, 1)
        self.assertFalse(fixture.journal_path.exists())

    def test_kfd_is_clean_at_preflight_not_during_a_running_lease(self) -> None:
        fixture = PolicyFixture(self)
        policy = fixture.policy()
        policy.enter()
        (fixture.kfd_processes / "1234").mkdir()

        policy.verify()
        with self.assertRaises(BenchmarkLockError) as raised:
            policy.preflight()

        self.assertEqual(raised.exception.code, "benchmark_external_compute")
        self.assertEqual(policy.state, "held")

    def test_kfd_owner_rejects_enter_before_journal_or_mutation(self) -> None:
        fixture = PolicyFixture(self)
        (fixture.kfd_processes / "321").mkdir()
        policy = fixture.policy()

        with self.assertRaises(BenchmarkLockError) as raised:
            policy.enter()

        self.assertEqual(raised.exception.code, "benchmark_external_compute")
        self.assertEqual(policy.state, "idle")
        self.assertFalse(fixture.journal_path.exists())
        self.assertEqual(fixture.power_profiles.hold_calls, 0)
        self.assertEqual(fixture.filesystem.writes, [])

    def test_partial_apply_restores_every_gpu_and_releases_hold(self) -> None:
        fixture = PolicyFixture(self, gpu_count=2)
        fixture.filesystem.fail_write_number = 2
        policy = fixture.policy()

        with self.assertRaises(BenchmarkLockError) as raised:
            policy.enter()

        self.assertEqual(
            raised.exception.code,
            "benchmark_policy_apply_failed",
        )
        self.assertEqual(policy.state, "idle")
        self.assertEqual(
            [fixture.level(identity) for identity in fixture.identities],
            ["auto", "auto"],
        )
        self.assertEqual(
            fixture.filesystem.writes,
            [
                ("0000:01:00.0", "high"),
                ("0000:02:00.0", "high"),
                ("0000:01:00.0", "auto"),
                ("0000:02:00.0", "auto"),
            ],
        )
        self.assertEqual(fixture.power_profiles.release_calls, 1)
        self.assertFalse(fixture.journal_path.exists())

    def test_journal_failure_prevents_every_policy_mutation(self) -> None:
        fixture = PolicyFixture(self)
        failing_journal = FailingJournal(
            fixture.journal_path,
            owner_uid=os.getuid(),
        )
        policy = fixture.policy(journal=failing_journal)

        with self.assertRaises(BenchmarkLockError) as raised:
            policy.enter()

        self.assertEqual(
            raised.exception.code,
            "benchmark_policy_journal_failed",
        )
        self.assertEqual(policy.state, "idle")
        self.assertEqual(fixture.power_profiles.hold_calls, 0)
        self.assertEqual(fixture.filesystem.writes, [])
        self.assertEqual(fixture.level(fixture.identities[0]), "auto")

    def test_rollback_failure_keeps_epoch_for_recovery(self) -> None:
        fixture = PolicyFixture(self)
        fixture.filesystem.fail_write_number = 1
        fixture.filesystem.fail_restore = True
        policy = fixture.policy()

        with self.assertRaises(BenchmarkLockError) as raised:
            policy.enter()

        self.assertEqual(
            raised.exception.code,
            "benchmark_policy_restore_failed",
        )
        self.assertEqual(policy.state, "faulted")
        self.assertTrue(fixture.journal_path.exists())
        self.assertEqual(fixture.power_profiles.release_calls, 1)

    def test_manual_override_is_drift_and_is_never_fought(self) -> None:
        fixture = PolicyFixture(self)
        policy = fixture.policy()
        policy.enter()
        fixture.power_profiles.manual_override("power-saver")

        with self.assertRaises(BenchmarkLockError) as raised:
            policy.verify()

        self.assertEqual(raised.exception.code, "benchmark_policy_drift")
        self.assertEqual(policy.state, "faulted")
        self.assertEqual(fixture.power_profiles.hold_calls, 1)

        policy.leave()

        self.assertEqual(policy.state, "idle")
        self.assertEqual(fixture.power_profiles.active_profile, "power-saver")
        self.assertEqual(fixture.power_profiles.release_calls, 0)
        self.assertEqual(fixture.level(fixture.identities[0]), "auto")
        self.assertFalse(fixture.journal_path.exists())

    def test_degraded_profile_and_gpu_drift_fail_held_audit(self) -> None:
        fixture = PolicyFixture(self)
        policy = fixture.policy()
        policy.enter()
        fixture.power_profiles.performance_degraded = "lap-detected"

        with self.assertRaises(BenchmarkLockError) as degraded:
            policy.verify()

        self.assertEqual(degraded.exception.code, "benchmark_policy_drift")
        policy.leave()

        second_fixture = PolicyFixture(self)
        second_policy = second_fixture.policy()
        second_policy.enter()
        second_fixture.level_path(second_fixture.identities[0]).write_text(
            "auto\n",
            encoding="ascii",
        )

        with self.assertRaises(BenchmarkLockError) as gpu_drift:
            second_policy.verify()

        self.assertEqual(gpu_drift.exception.code, "benchmark_policy_drift")
        second_policy.leave()

    def test_same_boot_recovery_restores_exact_baseline(self) -> None:
        fixture = PolicyFixture(self)
        first_policy = fixture.policy()
        first_policy.enter()
        self.assertEqual(fixture.level(fixture.identities[0]), "high")

        # A process crash releases the D-Bus hold.  A fresh service instance
        # then sees only the durable sysfs epoch.
        recovered_profiles = FakePowerProfiles()
        recovered_filesystem = RecordingFilesystem(
            fixture.linux_filesystem,
            journal_path=fixture.journal_path,
        )
        recovered_policy = fixture.policy(
            power_profiles=recovered_profiles,
            filesystem=recovered_filesystem,
        )
        self.assertEqual(recovered_policy.state, "recovery_required")

        recovered_policy.recover()

        self.assertEqual(recovered_policy.state, "idle")
        self.assertEqual(fixture.level(fixture.identities[0]), "auto")
        self.assertEqual(
            recovered_filesystem.writes,
            [("0000:01:00.0", "auto")],
        )
        self.assertFalse(fixture.journal_path.exists())
        self.assertEqual(recovered_profiles.hold_calls, 0)

    def test_new_boot_discards_epoch_without_replaying_sysfs(self) -> None:
        fixture = PolicyFixture(self)
        fixture.policy().enter()
        fixture.boot_id_path.write_text(f"{_NEXT_BOOT_ID}\n", encoding="ascii")
        recovered_filesystem = RecordingFilesystem(
            fixture.linux_filesystem,
            journal_path=fixture.journal_path,
        )
        recovered_policy = fixture.policy(
            power_profiles=FakePowerProfiles(),
            filesystem=recovered_filesystem,
        )

        recovered_policy.recover()

        self.assertEqual(recovered_policy.state, "idle")
        self.assertEqual(recovered_filesystem.writes, [])
        self.assertEqual(fixture.level(fixture.identities[0]), "high")
        self.assertFalse(fixture.journal_path.exists())

    def test_changed_hardware_identity_blocks_recovery_without_a_write(
        self,
    ) -> None:
        fixture = PolicyFixture(self)
        fixture.policy().enter()
        fixture.set_identity_field(
            fixture.identities[0],
            "device",
            "0x7550",
        )
        recovered_filesystem = RecordingFilesystem(
            fixture.linux_filesystem,
            journal_path=fixture.journal_path,
        )
        recovered_policy = fixture.policy(
            power_profiles=FakePowerProfiles(),
            filesystem=recovered_filesystem,
        )

        with self.assertRaises(BenchmarkLockError) as raised:
            recovered_policy.recover()

        self.assertEqual(
            raised.exception.code,
            "benchmark_policy_recovery_required",
        )
        self.assertEqual(recovered_policy.state, "recovery_required")
        self.assertEqual(recovered_filesystem.writes, [])
        self.assertTrue(fixture.journal_path.exists())

    def test_restore_readback_failure_is_faulted_and_retains_journal(
        self,
    ) -> None:
        fixture = PolicyFixture(self)
        policy = fixture.policy()
        policy.enter()
        fixture.filesystem.fail_restore = True

        with self.assertRaises(BenchmarkLockError) as raised:
            policy.leave()

        self.assertEqual(
            raised.exception.code,
            "benchmark_policy_restore_failed",
        )
        self.assertEqual(policy.state, "faulted")
        self.assertTrue(fixture.journal_path.exists())
        self.assertEqual(fixture.power_profiles.release_calls, 1)

    def test_failed_power_profile_restore_is_not_forgotten_on_retry(
        self,
    ) -> None:
        fixture = PolicyFixture(self)
        power_profiles = NonRestoringPowerProfiles()
        policy = fixture.policy(power_profiles=power_profiles)
        policy.enter()

        for _attempt in range(2):
            with self.assertRaises(BenchmarkLockError) as raised:
                policy.leave()
            self.assertEqual(
                raised.exception.code,
                "benchmark_policy_restore_failed",
            )
            self.assertEqual(policy.state, "faulted")
            self.assertTrue(fixture.journal_path.exists())

        self.assertEqual(power_profiles.release_calls, 1)
        power_profiles.active_profile = "balanced"
        policy.leave()
        self.assertEqual(policy.state, "idle")
        self.assertFalse(fixture.journal_path.exists())

    def test_lost_hold_reply_retains_epoch_until_process_recovery(
        self,
    ) -> None:
        fixture = PolicyFixture(self)
        power_profiles = LostHoldReplyPowerProfiles()
        policy = fixture.policy(power_profiles=power_profiles)

        with self.assertRaises(BenchmarkLockError) as raised:
            policy.enter()

        self.assertEqual(
            raised.exception.code,
            "benchmark_policy_restore_failed",
        )
        self.assertEqual(policy.state, "faulted")
        self.assertTrue(fixture.journal_path.exists())
        self.assertEqual(power_profiles.hold_calls, 1)
        self.assertEqual(power_profiles.release_calls, 0)
        self.assertEqual(
            power_profiles.holds,
            (
                PowerProfileHold(
                    profile="performance",
                    application_id=_APPLICATION_ID,
                    reason=_REASON,
                ),
            ),
        )


if __name__ == "__main__":
    unittest.main()
