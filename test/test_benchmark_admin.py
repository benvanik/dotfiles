from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from benchmark_lock.admin import (
    ADMIN_LOCK_PATH,
    BENCHMARK_GROUP_NAME,
    CLIENT_PATH,
    CONFIG_PATH,
    CONTROL_SOCKET_PATH,
    CURRENT_SELECTOR,
    GENERATION_DIRECTORY,
    INSTALL_INTENT_PATH,
    INSTALL_PUBLISH_PATH,
    INSTALL_ROOT,
    INSTALL_TRANSITION_PATH,
    SERVICE_UNIT_PATH,
    SOCKET_UNIT_PATH,
    SOCKET_UNIT_STAGE_PATH,
    STATE_DIRECTORY,
    SYSUSERS_PATH,
    UNINSTALL_INTENT_PATH,
    UNINSTALL_PUBLISH_PATH,
    UNINSTALL_TRANSITION_PATH,
    BenchmarkAdmin,
    canonical_policy_configuration,
    parse_policy_configuration,
)
from benchmark_lock.admin_cli import main
from benchmark_lock.administration_state import AdministrationAdmissionFence
from benchmark_lock.errors import BenchmarkLockError
from benchmark_lock.fdstore import (
    CONTROL_DESCRIPTOR_NAME,
    FILE_DESCRIPTOR_STORE_MAX,
)


def _configuration(*, unique_id: str = "4610468131039e0") -> bytes:
    return json.dumps(
        {
            "schema": "benchmarkd.config.v1",
            "policy_identity": "amd-performance-v1",
            "gpus": [
                {
                    "bdf": "0000:23:00.0",
                    "vendor": "0x1002",
                    "device": "0x744c",
                    "subsystem_vendor": "0x1eae",
                    "subsystem_device": "0x7901",
                    "revision": "0xc8",
                    "unique_id": unique_id,
                    "device_class": "0x030000",
                }
            ],
        },
        indent=2,
    ).encode("ascii")


class FakeRunner:
    def __init__(self, timeline: list[object] | None = None) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.fail_command: tuple[str, ...] | None = None
        self.timeline = [] if timeline is None else timeline

    def run(self, command: tuple[str, ...]) -> None:
        self.commands.append(command)
        self.timeline.append(("command", command))
        if command == self.fail_command:
            self.fail_command = None
            raise BenchmarkLockError(
                "injected administrator command failure",
                code="injected_admin_command_failure",
            )


class KillAfterCommandRunner(FakeRunner):
    def __init__(self, command: tuple[str, ...]) -> None:
        super().__init__()
        self.command = command

    def run(self, command: tuple[str, ...]) -> None:
        super().run(command)
        if command == self.command:
            os.kill(os.getpid(), signal.SIGKILL)


class FailActivationThenKillRollbackRunner(FakeRunner):
    def __init__(self) -> None:
        super().__init__()
        self.activation_failed = False

    def run(self, command: tuple[str, ...]) -> None:
        self.commands.append(command)
        self.timeline.append(("command", command))
        if not self.activation_failed and command == (
            "/usr/bin/systemctl",
            "start",
            "benchmarkd.service",
        ):
            self.activation_failed = True
            raise BenchmarkLockError(
                "injected target activation failure",
                code="injected_admin_command_failure",
            )
        if self.activation_failed and command == (
            "/usr/bin/systemctl",
            "stop",
            "benchmarkd.socket",
        ):
            os.kill(os.getpid(), signal.SIGKILL)


class FakeMaintenance:
    def __init__(self, timeline: list[object] | None = None) -> None:
        self.events: list[tuple[str, bool]] = []
        self.timeline = [] if timeline is None else timeline

    @contextlib.contextmanager
    def hold(self, *, installed: bool):
        self.events.append(("enter", installed))
        self.timeline.append(("maintenance-enter", installed))
        try:
            yield
        finally:
            self.events.append(("exit", installed))
            self.timeline.append(("maintenance-exit", installed))


class RejectingMaintenance:
    def __init__(self) -> None:
        self.entries: list[bool] = []

    @contextlib.contextmanager
    def hold(self, *, installed: bool):
        self.entries.append(installed)
        raise BenchmarkLockError(
            "injected busy benchmark scheduler",
            code="maintenance_busy",
        )
        yield


class FakeAccounts:
    def __init__(self, group_id: int) -> None:
        self._group_id = group_id
        self.added_users: list[str] = []
        self.validated_users: list[str] = []
        self.required_users: list[str | None] = []

    @property
    def benchmark_group_id(self) -> int:
        return self._group_id

    def add_user(self, user_name: str) -> None:
        self.added_users.append(user_name)

    def validate_user(self, user_name: str) -> None:
        self.validated_users.append(user_name)

    def require_user(self, user_name: str | None) -> None:
        self.required_users.append(user_name)


class AdminFixture:
    def __init__(self, test: unittest.TestCase) -> None:
        self.test = test
        self.temporary = tempfile.TemporaryDirectory()
        test.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name) / "destination"
        self.root.mkdir(mode=0o700)
        self.uid = os.getuid()
        self.gid = os.getgid()
        for relative in (
            "usr",
            "usr/local",
            "usr/local/lib",
            "usr/local/bin",
            "etc",
            "var",
            "var/lib",
            "run",
            "run/benchmarkd",
            "tmp",
        ):
            path = self.root / relative
            path.mkdir(mode=0o755)
        self.timeline: list[object] = []
        self.runner = FakeRunner(self.timeline)
        self.maintenance = FakeMaintenance(self.timeline)
        self.accounts = FakeAccounts(self.gid)
        self.messages: list[str] = []
        repository_root = pathlib.Path(__file__).resolve().parents[1]
        self.source_root = pathlib.Path(self.temporary.name) / "source"
        for relative in (
            "lib/benchmark_lock",
            "benchmarkd/bin",
            "benchmarkd/systemd",
            "benchmarkd/sysusers",
        ):
            (self.source_root / relative).mkdir(parents=True, exist_ok=True)
        for source in sorted((repository_root / "lib/benchmark_lock").glob("*.py")):
            destination = self.source_root / "lib/benchmark_lock" / source.name
            destination.write_bytes(source.read_bytes())
        for relative in (
            "benchmarkd/bin/benchmark-lock",
            "benchmarkd/bin/benchmarkd",
            "benchmarkd/systemd/benchmarkd.socket",
            "benchmarkd/systemd/benchmarkd.service",
            "benchmarkd/sysusers/benchmarkd.conf",
        ):
            source = repository_root / relative
            destination = self.source_root / relative
            destination.write_bytes(source.read_bytes())
            os.chmod(destination, stat.S_IMODE(os.lstat(source).st_mode))
        self.admin = BenchmarkAdmin(
            source_root=self.source_root,
            runner=self.runner,
            maintenance=self.maintenance,
            accounts=self.accounts,
            destination_root=self.root,
            root_uid=self.uid,
            root_gid=self.gid,
            effective_uid=self.uid,
            report=self.messages.append,
            check_runtime=False,
        )
        self.config_source = self.root / "policy-source.json"
        self.config_source.write_bytes(_configuration())

    def mapped(self, absolute: pathlib.Path) -> pathlib.Path:
        return self.root / absolute.relative_to("/")

    def install(self) -> str:
        return self.admin.install(
            configuration_source=self.config_source,
            user_name="ben",
        )

    def new_admin(
        self,
        *,
        runner: FakeRunner | None = None,
        maintenance: FakeMaintenance | None = None,
    ) -> BenchmarkAdmin:
        return BenchmarkAdmin(
            source_root=self.source_root,
            runner=self.runner if runner is None else runner,
            maintenance=(self.maintenance if maintenance is None else maintenance),
            accounts=self.accounts,
            destination_root=self.root,
            root_uid=self.uid,
            root_gid=self.gid,
            effective_uid=self.uid,
            report=self.messages.append,
            check_runtime=False,
        )


class BenchmarkConfigurationTest(unittest.TestCase):
    def test_strict_configuration_is_normalized_for_installation(self) -> None:
        config = parse_policy_configuration(_configuration())
        canonical = canonical_policy_configuration(config)

        self.assertEqual(canonical[-1:], b"\n")
        self.assertNotIn(b" ", canonical)
        self.assertEqual(
            parse_policy_configuration(canonical),
            config,
        )

    def test_unknown_duplicate_and_wrong_hardware_fields_are_rejected(self) -> None:
        unknown = json.loads(_configuration())
        unknown["fallback"] = True
        duplicate = (
            b'{"gpus":[],"policy_identity":"amd-performance-v1",'
            b'"schema":"benchmarkd.config.v1","schema":"benchmarkd.config.v1"}'
        )
        wrong_vendor = json.loads(_configuration())
        wrong_vendor["gpus"][0]["vendor"] = "0x10de"

        for payload in (
            json.dumps(unknown).encode("ascii"),
            duplicate,
            json.dumps(wrong_vendor).encode("ascii"),
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(BenchmarkLockError):
                    parse_policy_configuration(payload)


class BenchmarkAdminInstallTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = AdminFixture(self)

    def _fork_killed_install(
        self,
        runner: FakeRunner,
        *,
        configuration_source: pathlib.Path | None,
    ) -> None:
        process_id = os.fork()
        if process_id == 0:
            child_admin = self.fixture.new_admin(
                runner=runner,
                maintenance=FakeMaintenance(),
            )
            child_admin.install(
                configuration_source=configuration_source,
                user_name="ben",
            )
            os._exit(97)
        waited_id, wait_status = os.waitpid(process_id, 0)
        self.assertEqual(waited_id, process_id)
        self.assertTrue(os.WIFSIGNALED(wait_status))
        self.assertEqual(os.WTERMSIG(wait_status), signal.SIGKILL)

    def test_fresh_install_publishes_one_immutable_generation(self) -> None:
        state_directory = self.fixture.mapped(STATE_DIRECTORY)
        self.assertFalse(state_directory.exists())

        digest = self.fixture.install()

        generation = self.fixture.mapped(GENERATION_DIRECTORY) / digest
        admin_lock_metadata = os.lstat(self.fixture.mapped(ADMIN_LOCK_PATH))
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(
            stat.S_IMODE(os.lstat(state_directory).st_mode),
            0o700,
        )
        self.assertTrue(stat.S_ISREG(admin_lock_metadata.st_mode))
        self.assertEqual(admin_lock_metadata.st_uid, self.fixture.uid)
        self.assertEqual(admin_lock_metadata.st_gid, self.fixture.gid)
        self.assertEqual(stat.S_IMODE(admin_lock_metadata.st_mode), 0o600)
        self.assertEqual(admin_lock_metadata.st_nlink, 1)
        self.assertTrue(generation.is_dir())
        self.assertEqual(stat.S_IMODE(os.lstat(generation).st_mode), 0o555)
        self.assertEqual(
            os.readlink(self.fixture.mapped(CURRENT_SELECTOR)),
            f"generations/{digest}",
        )
        self.assertEqual(
            os.readlink(self.fixture.mapped(CLIENT_PATH)),
            "/usr/local/lib/benchmarkd/current/bin/benchmark-lock",
        )
        self.assertEqual(
            self.fixture.mapped(SOCKET_UNIT_PATH).read_bytes(),
            (generation / "share/systemd/benchmarkd.socket").read_bytes(),
        )
        self.assertEqual(
            self.fixture.mapped(SERVICE_UNIT_PATH).read_bytes(),
            (generation / "share/systemd/benchmarkd.service").read_bytes(),
        )
        installed_config = self.fixture.mapped(CONFIG_PATH)
        self.assertEqual(
            installed_config.read_bytes(),
            canonical_policy_configuration(
                parse_policy_configuration(_configuration())
            ),
        )
        self.assertEqual(stat.S_IMODE(os.lstat(installed_config).st_mode), 0o600)
        for relative in (
            "usr/local/lib/systemd",
            "usr/local/lib/systemd/system",
            "usr/local/lib/sysusers.d",
        ):
            self.assertEqual(
                stat.S_IMODE(os.lstat(self.fixture.root / relative).st_mode),
                0o755,
            )
        self.assertEqual(self.fixture.accounts.added_users, ["ben"])
        self.assertEqual(self.fixture.accounts.validated_users, ["ben"])
        self.assertEqual(
            self.fixture.maintenance.events,
            [("enter", True), ("exit", True)],
        )
        self.assertIn(
            (
                "/usr/bin/systemd-sysusers",
                os.fspath(self.fixture.mapped(SYSUSERS_PATH)),
            ),
            self.fixture.runner.commands,
        )
        self.assertIn(
            (
                "/usr/bin/systemctl",
                "enable",
                "--now",
                "benchmarkd.socket",
            ),
            self.fixture.runner.commands,
        )
        client_help = subprocess.run(
            [generation / "bin/benchmark-lock", "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(client_help.returncode, 0, client_help.stderr)
        self.assertIn("usage: benchmark-lock", client_help.stdout)

        self.assertEqual(self.fixture.admin.doctor(user_name="ben"), digest)
        self.assertEqual(self.fixture.accounts.required_users, ["ben"])

    def test_operation_lock_serializes_mutating_administrators(self) -> None:
        attempting = threading.Event()
        acquired = threading.Event()
        identities: list[tuple[int, int]] = []

        def contend() -> None:
            attempting.set()
            with self.fixture.admin._hold_operation_lock() as descriptor:
                metadata = os.fstat(descriptor)
                identities.append((metadata.st_dev, metadata.st_ino))
                acquired.set()

        with self.fixture.admin._hold_operation_lock() as descriptor:
            metadata = os.fstat(descriptor)
            identities.append((metadata.st_dev, metadata.st_ino))
            contender = threading.Thread(target=contend)
            contender.start()
            attempting.wait()
            self.assertFalse(acquired.wait(0.05))

        acquired.wait()
        contender.join()
        self.assertEqual(len(identities), 2)
        self.assertEqual(identities[1], identities[0])

    def test_operation_lock_is_exact_under_restrictive_umask(self) -> None:
        observed_directory_modes: list[int] = []
        observed_lock_modes: list[int] = []
        original_chmod = os.chmod
        original_fchmod = os.fchmod

        def observe_directory(path: os.PathLike[str] | str, mode: int) -> None:
            if pathlib.Path(path) == self.fixture.mapped(STATE_DIRECTORY):
                observed_directory_modes.append(stat.S_IMODE(os.lstat(path).st_mode))
            original_chmod(path, mode)

        def observe_lock(descriptor: int, mode: int) -> None:
            observed_lock_modes.append(stat.S_IMODE(os.fstat(descriptor).st_mode))
            original_fchmod(descriptor, mode)

        previous_mask = os.umask(0o0777)
        try:
            with (
                mock.patch(
                    "benchmark_lock.admin.os.chmod",
                    side_effect=observe_directory,
                ),
                mock.patch(
                    "benchmark_lock.admin.os.fchmod",
                    side_effect=observe_lock,
                ),
                self.fixture.admin._hold_operation_lock(),
            ):
                pass
        finally:
            os.umask(previous_mask)

        self.assertEqual(observed_directory_modes, [0o700])
        self.assertEqual(observed_lock_modes, [0o600])
        self.assertEqual(
            stat.S_IMODE(os.lstat(self.fixture.mapped(STATE_DIRECTORY)).st_mode),
            0o700,
        )
        self.assertEqual(
            stat.S_IMODE(os.lstat(self.fixture.mapped(ADMIN_LOCK_PATH)).st_mode),
            0o600,
        )

    def test_managed_directories_are_immediately_exact_under_restrictive_umask(
        self,
    ) -> None:
        expected_modes = {
            self.fixture.mapped(path): mode
            for path, mode in (
                (pathlib.Path("/usr/local/lib/systemd"), 0o755),
                (pathlib.Path("/usr/local/lib/systemd/system"), 0o755),
                (pathlib.Path("/usr/local/lib/sysusers.d"), 0o755),
                (INSTALL_ROOT, 0o755),
                (GENERATION_DIRECTORY, 0o755),
                (CONFIG_PATH.parent, 0o700),
                (STATE_DIRECTORY, 0o700),
            )
        }
        observed_modes: dict[pathlib.Path, int] = {}
        original_chown = os.chown

        def observe_before_chown(
            path: os.PathLike[str] | str,
            user_id: int,
            group_id: int,
        ) -> None:
            observed_path = pathlib.Path(path)
            if observed_path in expected_modes:
                observed_modes[observed_path] = stat.S_IMODE(
                    os.lstat(observed_path).st_mode
                )
            original_chown(path, user_id, group_id)

        previous_mask = os.umask(0o777)
        try:
            with mock.patch(
                "benchmark_lock.admin.os.chown",
                side_effect=observe_before_chown,
            ):
                self.fixture.admin._prepare_layout()
            observed_mask = os.umask(0o777)
            os.umask(observed_mask)
        finally:
            os.umask(previous_mask)

        self.assertEqual(observed_mask, 0o777)
        self.assertEqual(observed_modes, expected_modes)

    def test_operation_lock_is_the_broker_restart_fence(self) -> None:
        self.fixture.install()
        fence = AdministrationAdmissionFence(
            admin_lock_path=self.fixture.mapped(ADMIN_LOCK_PATH),
            install_root=self.fixture.mapped(INSTALL_ROOT),
            install_state_paths=(
                self.fixture.mapped(INSTALL_INTENT_PATH),
                self.fixture.mapped(INSTALL_PUBLISH_PATH),
                self.fixture.mapped(INSTALL_TRANSITION_PATH),
            ),
            uninstall_state_paths=(
                self.fixture.mapped(UNINSTALL_INTENT_PATH),
                self.fixture.mapped(UNINSTALL_PUBLISH_PATH),
                self.fixture.mapped(UNINSTALL_TRANSITION_PATH),
            ),
            root_uid=self.fixture.uid,
            root_gid=self.fixture.gid,
        )
        self.addCleanup(fence.close)

        self.assertFalse(fence.refresh())
        with self.fixture.admin._hold_operation_lock():
            self.assertTrue(fence.refresh())
        self.assertFalse(fence.refresh())

    def test_units_encode_socket_credentials_and_unbounded_recovery(self) -> None:
        self.fixture.install()
        socket_unit = self.fixture.mapped(SOCKET_UNIT_PATH).read_text()
        service_unit = self.fixture.mapped(SERVICE_UNIT_PATH).read_text()

        for directive in (
            "ListenSequentialPacket=/run/benchmarkd/control.sock",
            "SocketMode=0660",
            "SocketGroup=benchmark",
            "Accept=no",
            "PassCredentials=yes",
            "FlushPending=no",
            f"FileDescriptorName={CONTROL_DESCRIPTOR_NAME}",
        ):
            self.assertIn(directive, socket_unit)
        for directive in (
            "Type=notify",
            "Restart=on-failure",
            "RestartPreventExitStatus=78",
            f"FileDescriptorStoreMax={FILE_DESCRIPTOR_STORE_MAX}",
            "FileDescriptorStorePreserve=restart",
            "KillMode=control-group",
            "StartLimitIntervalSec=0",
            "TimeoutStartSec=infinity",
            "TimeoutStopSec=infinity",
            "StateDirectory=benchmarkd",
            "StateDirectoryMode=0700",
            "UMask=0077",
            "NoNewPrivileges=yes",
            "CapabilityBoundingSet=CAP_KILL",
            "ProtectHome=yes",
            "ProtectSystem=strict",
            "PrivateTmp=yes",
            "ProtectClock=yes",
            "ProtectControlGroups=yes",
            "ProtectHostname=yes",
            "ProtectKernelLogs=yes",
            "ProtectKernelModules=yes",
            "RestrictAddressFamilies=AF_UNIX",
            "RestrictNamespaces=yes",
            "RestrictRealtime=yes",
            "RestrictSUIDSGID=yes",
            "LockPersonality=yes",
            "SystemCallArchitectures=native",
        ):
            self.assertIn(directive, service_unit)

    def test_entry_points_use_fixed_isolated_python(self) -> None:
        repository_root = pathlib.Path(__file__).resolve().parents[1]
        for relative in (
            "bin/benchmark-admin",
            "benchmarkd/bin/benchmarkd",
            "benchmarkd/bin/benchmark-lock",
        ):
            with self.subTest(relative=relative):
                self.assertEqual(
                    (repository_root / relative).read_bytes().splitlines()[0],
                    b"#!/usr/bin/python3 -I",
                )

    def test_upgrade_preserves_config_and_holds_maintenance_before_stop(self) -> None:
        digest = self.fixture.install()
        launcher = self.fixture.source_root / "benchmarkd/bin/benchmarkd"
        launcher.write_bytes(launcher.read_bytes() + b"\n")
        self.fixture.runner.commands.clear()
        self.fixture.maintenance.events.clear()
        self.fixture.timeline.clear()

        upgraded = self.fixture.admin.install(
            configuration_source=None,
            user_name="ben",
        )

        self.assertNotEqual(upgraded, digest)
        self.assertEqual(
            self.fixture.maintenance.events,
            [
                ("enter", True),
                ("exit", True),
                ("enter", True),
                ("exit", True),
            ],
        )
        stop_socket = self.fixture.runner.commands.index(
            ("/usr/bin/systemctl", "stop", "benchmarkd.socket")
        )
        stop_service = self.fixture.runner.commands.index(
            ("/usr/bin/systemctl", "stop", "benchmarkd.service")
        )
        sysusers = self.fixture.runner.commands.index(
            (
                "/usr/bin/systemd-sysusers",
                os.fspath(self.fixture.mapped(SYSUSERS_PATH)),
            )
        )
        enable = self.fixture.runner.commands.index(
            (
                "/usr/bin/systemctl",
                "enable",
                "--now",
                "benchmarkd.socket",
            )
        )
        self.assertLess(stop_socket, stop_service)
        self.assertLess(stop_service, sysusers)
        self.assertLess(stop_service, enable)
        maintenance_enters = [
            index
            for index, event in enumerate(self.fixture.timeline)
            if event == ("maintenance-enter", True)
        ]
        maintenance_exits = [
            index
            for index, event in enumerate(self.fixture.timeline)
            if event == ("maintenance-exit", True)
        ]
        timeline_stop = self.fixture.timeline.index(
            (
                "command",
                ("/usr/bin/systemctl", "stop", "benchmarkd.socket"),
            )
        )
        timeline_enable = self.fixture.timeline.index(
            (
                "command",
                (
                    "/usr/bin/systemctl",
                    "enable",
                    "--now",
                    "benchmarkd.socket",
                ),
            )
        )
        timeline_status = self.fixture.timeline.index(
            (
                "command",
                (
                    os.fspath(
                        self.fixture.mapped(GENERATION_DIRECTORY)
                        / upgraded
                        / "bin/benchmark-lock"
                    ),
                    "--status",
                ),
            )
        )
        self.assertLess(maintenance_enters[0], timeline_stop)
        self.assertLess(timeline_stop, maintenance_exits[0])
        self.assertLess(maintenance_exits[0], timeline_enable)
        self.assertLess(timeline_enable, maintenance_enters[1])
        self.assertLess(maintenance_enters[1], timeline_status)
        self.assertLess(timeline_status, maintenance_exits[1])

    def test_upgrade_requires_empty_scheduler_before_building_generation(
        self,
    ) -> None:
        digest = self.fixture.install()
        launcher = self.fixture.source_root / "benchmarkd/bin/benchmarkd"
        launcher.write_bytes(launcher.read_bytes() + b"\n")
        maintenance = RejectingMaintenance()
        admin = self.fixture.new_admin(maintenance=maintenance)

        with (
            mock.patch("benchmark_lock.admin.build_generation") as build,
            mock.patch.object(
                admin.generation_store,
                "verify",
            ) as verify_generation,
            mock.patch.object(
                admin,
                "_prepare_layout",
            ) as prepare_layout,
            self.assertRaisesRegex(
                BenchmarkLockError,
                "busy benchmark scheduler",
            ),
        ):
            admin.install(
                configuration_source=None,
                user_name="ben",
            )

        build.assert_not_called()
        verify_generation.assert_not_called()
        prepare_layout.assert_not_called()
        self.assertEqual(maintenance.entries, [True])
        self.assertEqual(
            self.fixture.admin.generation_store.inventory_digests(),
            (digest,),
        )

    def test_reinstalling_current_generation_does_not_interrupt_service(
        self,
    ) -> None:
        digest = self.fixture.install()
        self.fixture.runner.commands.clear()
        self.fixture.maintenance.events.clear()

        observed = self.fixture.admin.install(
            configuration_source=None,
            user_name="ben",
        )

        self.assertEqual(observed, digest)
        self.assertEqual(
            self.fixture.maintenance.events,
            [("enter", True), ("exit", True)],
        )
        self.assertNotIn(
            ("/usr/bin/systemctl", "stop", "benchmarkd.socket"),
            self.fixture.runner.commands,
        )
        self.assertIn(
            (
                "/usr/bin/systemctl",
                "enable",
                "--now",
                "benchmarkd.socket",
            ),
            self.fixture.runner.commands,
        )

    def test_upgrade_rejects_policy_replacement_without_mutating_config(self) -> None:
        self.fixture.install()
        installed = self.fixture.mapped(CONFIG_PATH)
        original = installed.read_bytes()
        replacement = self.fixture.root / "replacement.json"
        replacement.write_bytes(_configuration(unique_id="4610468131039e1"))
        command_count = len(self.fixture.runner.commands)

        with self.assertRaisesRegex(
            BenchmarkLockError,
            "does not expose the fenced epoch-aware replacement transaction",
        ):
            self.fixture.admin.install(
                configuration_source=replacement,
                user_name="ben",
            )

        self.assertEqual(installed.read_bytes(), original)
        self.assertEqual(len(self.fixture.runner.commands), command_count)

    def test_first_install_requires_policy_and_root(self) -> None:
        with self.assertRaisesRegex(BenchmarkLockError, "requires --config"):
            self.fixture.admin.install(
                configuration_source=None,
                user_name="ben",
            )

        unprivileged = BenchmarkAdmin(
            source_root=self.fixture.source_root,
            runner=self.fixture.runner,
            maintenance=self.fixture.maintenance,
            accounts=self.fixture.accounts,
            destination_root=self.fixture.root,
            root_uid=self.fixture.uid,
            root_gid=self.fixture.gid,
            effective_uid=self.fixture.uid + 1,
            check_runtime=False,
        )
        with self.assertRaisesRegex(BenchmarkLockError, "must run as root"):
            unprivileged.install(
                configuration_source=self.fixture.config_source,
                user_name="ben",
            )

    def test_invalid_policy_is_rejected_before_installation_mutates_host(
        self,
    ) -> None:
        self.fixture.config_source.write_text('{"schema":"wrong"}\n')

        with self.assertRaises(BenchmarkLockError):
            self.fixture.install()

        self.assertEqual(self.fixture.runner.commands, [])
        self.assertFalse(self.fixture.mapped(INSTALL_ROOT).exists())

    def test_insecure_projection_ancestor_is_rejected_before_mutation(self) -> None:
        insecure_parent = self.fixture.root / "usr/local"
        insecure_parent.chmod(0o777)

        with self.assertRaisesRegex(BenchmarkLockError, "securely root-owned"):
            self.fixture.install()

        self.assertEqual(self.fixture.runner.commands, [])
        self.assertFalse(self.fixture.mapped(INSTALL_ROOT).exists())

    def test_legacy_tmp_state_blocks_install_before_host_mutation(self) -> None:
        legacy = self.fixture.root / "tmp/benchmark-lock-ben"
        legacy.mkdir()
        (legacy / "state.tsv").write_text("sysctl\t/path\tvalue\n")

        with self.assertRaisesRegex(BenchmarkLockError, "benchmark-unlock"):
            self.fixture.install()

        self.assertEqual(self.fixture.runner.commands, [])
        self.assertFalse(self.fixture.mapped(INSTALL_ROOT).exists())

    def test_legacy_guard_and_empty_state_directory_do_not_block_install(self) -> None:
        (self.fixture.root / "tmp/benchmark-lock-ben.guard").write_text("")
        (self.fixture.root / "tmp/benchmark-lock-ben").mkdir()

        digest = self.fixture.install()

        self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_malformed_legacy_state_artifact_fails_closed(self) -> None:
        legacy = self.fixture.root / "tmp/benchmark-lock-ben"
        legacy.mkdir()
        (legacy / "state.tsv").mkdir()

        with self.assertRaisesRegex(BenchmarkLockError, "unsafe type"):
            self.fixture.install()

        self.assertEqual(self.fixture.runner.commands, [])

    def test_sigkill_after_prior_stop_recovers_prepared_upgrade(self) -> None:
        prior_digest = self.fixture.install()
        launcher = self.fixture.source_root / "benchmarkd/bin/benchmarkd"
        launcher.write_bytes(launcher.read_bytes() + b"\n")
        self._fork_killed_install(
            KillAfterCommandRunner(
                (
                    "/usr/bin/systemctl",
                    "stop",
                    "benchmarkd.service",
                )
            ),
            configuration_source=None,
        )

        intent_path = self.fixture.mapped(INSTALL_INTENT_PATH)
        intent = json.loads(intent_path.read_text())
        self.assertEqual(intent["phase"], "prepared")
        self.assertEqual(intent["prior_digest"], prior_digest)

        retry_runner = FakeRunner()
        retry_maintenance = FakeMaintenance()
        recovered = self.fixture.new_admin(
            runner=retry_runner,
            maintenance=retry_maintenance,
        ).install(
            configuration_source=None,
            user_name="ben",
        )

        self.assertEqual(recovered, intent["target_digest"])
        self.assertFalse(os.path.lexists(intent_path))
        self.assertEqual(
            os.readlink(self.fixture.mapped(CURRENT_SELECTOR)),
            f"generations/{recovered}",
        )
        stop_index = retry_runner.commands.index(
            (
                "/usr/bin/systemctl",
                "stop",
                "benchmarkd.service",
            )
        )
        start_index = retry_runner.commands.index(
            (
                "/usr/bin/systemctl",
                "start",
                "benchmarkd.service",
            )
        )
        self.assertLess(stop_index, start_index)
        self.assertEqual(
            retry_maintenance.events,
            [("enter", True), ("exit", True)],
        )

    def test_sigkill_after_fresh_target_start_recovers_stopped_install(
        self,
    ) -> None:
        self._fork_killed_install(
            KillAfterCommandRunner(
                (
                    "/usr/bin/systemctl",
                    "enable",
                    "--now",
                    "benchmarkd.socket",
                )
            ),
            configuration_source=self.fixture.config_source,
        )

        intent_path = self.fixture.mapped(INSTALL_INTENT_PATH)
        intent = json.loads(intent_path.read_text())
        self.assertEqual(intent["phase"], "stopped")
        self.assertEqual(intent["prior_digest"], None)

        recovered = self.fixture.new_admin().install(
            configuration_source=None,
            user_name="ben",
        )

        self.assertEqual(recovered, intent["target_digest"])
        self.assertFalse(os.path.lexists(intent_path))

    def test_sigkill_during_durable_rollback_resumes_prior(self) -> None:
        prior_digest = self.fixture.install()
        launcher = self.fixture.source_root / "benchmarkd/bin/benchmarkd"
        prior_launcher = launcher.read_bytes()
        launcher.write_bytes(prior_launcher + b"\n")
        self._fork_killed_install(
            FailActivationThenKillRollbackRunner(),
            configuration_source=None,
        )

        intent_path = self.fixture.mapped(INSTALL_INTENT_PATH)
        intent = json.loads(intent_path.read_text())
        self.assertEqual(intent["phase"], "rollback")
        self.assertEqual(intent["prior_digest"], prior_digest)
        launcher.write_bytes(prior_launcher)

        observed = self.fixture.new_admin().install(
            configuration_source=None,
            user_name="ben",
        )

        self.assertEqual(observed, prior_digest)
        self.assertFalse(os.path.lexists(intent_path))
        self.assertEqual(
            os.readlink(self.fixture.mapped(CURRENT_SELECTOR)),
            f"generations/{prior_digest}",
        )

    def test_activation_failure_restores_prior_generation(self) -> None:
        digest = self.fixture.install()
        original_build = self.fixture.source_root / "benchmarkd/bin/benchmarkd"
        original_content = original_build.read_bytes()
        original_build.write_bytes(original_content + b"\n")
        service_source = (
            self.fixture.source_root / "benchmarkd/systemd/benchmarkd.service"
        )
        service_source.write_bytes(service_source.read_bytes() + b"# changed\n")
        self.fixture.runner.fail_command = (
            "/usr/bin/systemctl",
            "start",
            "benchmarkd.service",
        )

        with self.assertRaisesRegex(BenchmarkLockError, "injected"):
            self.fixture.admin.install(
                configuration_source=None,
                user_name="ben",
            )

        self.assertEqual(
            os.readlink(self.fixture.mapped(CURRENT_SELECTOR)),
            f"generations/{digest}",
        )
        self.assertEqual(
            self.fixture.mapped(SERVICE_UNIT_PATH).read_bytes(),
            (
                self.fixture.mapped(GENERATION_DIRECTORY)
                / digest
                / "share/systemd/benchmarkd.service"
            ).read_bytes(),
        )
        enable_calls = [
            command
            for command in self.fixture.runner.commands
            if command[1:3] == ("enable", "--now")
        ]
        self.assertGreaterEqual(len(enable_calls), 3)

    def test_fixed_projection_stage_is_replayed_after_interruption(self) -> None:
        self.fixture.install()
        launcher = self.fixture.source_root / "benchmarkd/bin/benchmarkd"
        launcher.write_bytes(launcher.read_bytes() + b"\n")
        socket_source = (
            self.fixture.source_root / "benchmarkd/systemd/benchmarkd.socket"
        )
        socket_source.write_bytes(socket_source.read_bytes() + b"# changed\n")
        socket_unit = self.fixture.mapped(SOCKET_UNIT_PATH)
        original_replace = os.replace
        injected = False

        def interrupt_before_projection(
            source: os.PathLike[str] | str,
            destination: os.PathLike[str] | str,
        ) -> None:
            nonlocal injected
            if not injected and pathlib.Path(destination) == socket_unit:
                injected = True
                raise OSError("injected projection interruption")
            original_replace(source, destination)

        with (
            mock.patch(
                "benchmark_lock.installation_projection.os.replace",
                side_effect=interrupt_before_projection,
            ),
            self.assertRaisesRegex(OSError, "injected projection"),
        ):
            self.fixture.admin.install(
                configuration_source=None,
                user_name="ben",
            )

        self.assertTrue(injected)
        self.assertTrue(self.fixture.mapped(SOCKET_UNIT_STAGE_PATH).exists())
        intent = json.loads(self.fixture.mapped(INSTALL_INTENT_PATH).read_text())
        self.assertEqual(intent["phase"], "stopped")

        recovered = self.fixture.new_admin().install(
            configuration_source=None,
            user_name="ben",
        )

        self.assertEqual(
            os.readlink(self.fixture.mapped(CURRENT_SELECTOR)),
            f"generations/{recovered}",
        )
        self.assertFalse(os.path.lexists(self.fixture.mapped(SOCKET_UNIT_STAGE_PATH)))
        self.assertFalse(os.path.lexists(self.fixture.mapped(INSTALL_INTENT_PATH)))

    def test_pre_activation_failure_leaves_stopped_target_for_retry(self) -> None:
        self.fixture.install()
        for relative in (
            "benchmarkd/systemd/benchmarkd.socket",
            "benchmarkd/systemd/benchmarkd.service",
        ):
            source = self.fixture.source_root / relative
            source.write_bytes(source.read_bytes() + b"# changed\n")
        self.fixture.runner.commands.clear()
        self.fixture.runner.fail_command = (
            "/usr/bin/systemd-sysusers",
            os.fspath(self.fixture.mapped(SYSUSERS_PATH)),
        )

        with self.assertRaisesRegex(BenchmarkLockError, "injected"):
            self.fixture.admin.install(
                configuration_source=None,
                user_name="ben",
            )

        intent_path = self.fixture.mapped(INSTALL_INTENT_PATH)
        intent = json.loads(intent_path.read_text())
        target_digest = intent["target_digest"]
        self.assertEqual(intent["phase"], "stopped")
        self.assertEqual(
            os.readlink(self.fixture.mapped(CURRENT_SELECTOR)),
            f"generations/{target_digest}",
        )
        self.assertEqual(
            self.fixture.mapped(SOCKET_UNIT_PATH).read_bytes(),
            (
                self.fixture.mapped(GENERATION_DIRECTORY)
                / target_digest
                / "share/systemd/benchmarkd.socket"
            ).read_bytes(),
        )
        self.assertEqual(
            self.fixture.mapped(SERVICE_UNIT_PATH).read_bytes(),
            (
                self.fixture.mapped(GENERATION_DIRECTORY)
                / target_digest
                / "share/systemd/benchmarkd.service"
            ).read_bytes(),
        )
        self.assertIn(
            ("/usr/bin/systemctl", "stop", "benchmarkd.socket"),
            self.fixture.runner.commands,
        )

        recovered = self.fixture.new_admin().install(
            configuration_source=None,
            user_name="ben",
        )
        self.assertEqual(recovered, target_digest)
        self.assertFalse(os.path.lexists(intent_path))

    def test_fresh_activation_failure_leaves_no_current_selector(self) -> None:
        self.fixture.runner.fail_command = (
            "/usr/bin/systemctl",
            "start",
            "benchmarkd.service",
        )

        with self.assertRaisesRegex(BenchmarkLockError, "injected"):
            self.fixture.install()

        self.assertFalse(os.path.lexists(self.fixture.mapped(CURRENT_SELECTOR)))
        self.assertFalse(os.path.lexists(self.fixture.mapped(CLIENT_PATH)))
        self.assertFalse(self.fixture.mapped(SOCKET_UNIT_PATH).exists())
        self.assertFalse(self.fixture.mapped(SERVICE_UNIT_PATH).exists())
        self.assertFalse(self.fixture.mapped(SYSUSERS_PATH).exists())
        self.assertIn(
            (
                "/usr/bin/systemctl",
                "disable",
                "--now",
                "benchmarkd.socket",
            ),
            self.fixture.runner.commands,
        )

    def test_fresh_pre_activation_failure_is_recoverable_from_stopped(self) -> None:
        self.fixture.runner.fail_command = (
            "/usr/bin/systemd-sysusers",
            os.fspath(self.fixture.mapped(SYSUSERS_PATH)),
        )

        with self.assertRaisesRegex(BenchmarkLockError, "injected"):
            self.fixture.install()

        intent_path = self.fixture.mapped(INSTALL_INTENT_PATH)
        intent = json.loads(intent_path.read_text())
        self.assertEqual(intent["phase"], "stopped")
        for path in (
            CURRENT_SELECTOR,
            CLIENT_PATH,
            SOCKET_UNIT_PATH,
            SERVICE_UNIT_PATH,
            SYSUSERS_PATH,
        ):
            self.assertTrue(os.path.lexists(self.fixture.mapped(path)))
        self.assertTrue(self.fixture.mapped(CONFIG_PATH).exists())
        self.assertTrue(self.fixture.mapped(GENERATION_DIRECTORY).exists())
        recovered = self.fixture.new_admin().install(
            configuration_source=None,
            user_name="ben",
        )
        self.assertEqual(recovered, intent["target_digest"])
        self.assertFalse(os.path.lexists(intent_path))

    def test_projection_preflight_prevents_partial_unit_replacement(self) -> None:
        digest = self.fixture.install()
        old_socket_unit = self.fixture.mapped(SOCKET_UNIT_PATH).read_bytes()
        socket_source = (
            self.fixture.source_root / "benchmarkd/systemd/benchmarkd.socket"
        )
        socket_source.write_bytes(socket_source.read_bytes() + b"# changed\n")
        service_unit = self.fixture.mapped(SERVICE_UNIT_PATH)
        service_unit.write_text("[Unit]\nDescription=operator file\n")
        command_count = len(self.fixture.runner.commands)
        self.fixture.maintenance.events.clear()

        with self.assertRaisesRegex(BenchmarkLockError, "exact prior projection"):
            self.fixture.admin.install(
                configuration_source=None,
                user_name="ben",
            )

        self.assertEqual(
            os.readlink(self.fixture.mapped(CURRENT_SELECTOR)),
            f"generations/{digest}",
        )
        self.assertEqual(
            self.fixture.mapped(SOCKET_UNIT_PATH).read_bytes(),
            old_socket_unit,
        )
        self.assertEqual(len(self.fixture.runner.commands), command_count)
        self.assertEqual(
            self.fixture.maintenance.events,
            [("enter", True), ("exit", True)],
        )

    def test_fresh_install_refuses_projection_without_current_generation(
        self,
    ) -> None:
        socket_unit = self.fixture.mapped(SOCKET_UNIT_PATH)
        socket_unit.parent.mkdir(parents=True, exist_ok=True)
        socket_unit.parent.parent.chmod(0o755)
        socket_unit.parent.chmod(0o755)
        socket_unit.write_text("# Managed by benchmark-admin.\n[Socket]\n")
        original = socket_unit.read_bytes()

        with self.assertRaisesRegex(
            BenchmarkLockError,
            "live projection without a current generation",
        ):
            self.fixture.install()

        self.assertEqual(socket_unit.read_bytes(), original)
        self.assertEqual(self.fixture.runner.commands, [])
        self.assertEqual(self.fixture.maintenance.events, [])

    def test_doctor_fails_on_insecure_policy_mode(self) -> None:
        self.fixture.install()
        os.chmod(self.fixture.mapped(CONFIG_PATH), 0o644)

        with self.assertRaisesRegex(BenchmarkLockError, "unsafe"):
            self.fixture.admin.doctor(user_name=None)

    def test_doctor_audits_the_live_socket_identity(self) -> None:
        digest = self.fixture.install()
        socket_path = self.fixture.mapped(CONTROL_SOCKET_PATH)
        control = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(control.close)
        control.bind(os.fspath(socket_path))
        os.chmod(socket_path, 0o660)
        admin = BenchmarkAdmin(
            source_root=self.fixture.source_root,
            runner=self.fixture.runner,
            maintenance=self.fixture.maintenance,
            accounts=self.fixture.accounts,
            destination_root=self.fixture.root,
            root_uid=self.fixture.uid,
            root_gid=self.fixture.gid,
            effective_uid=self.fixture.uid,
            report=self.fixture.messages.append,
            check_runtime=True,
        )

        self.assertEqual(admin.doctor(user_name=None), digest)
        self.assertIn(
            (os.fspath(self.fixture.mapped(CLIENT_PATH)), "--status"),
            self.fixture.runner.commands,
        )
        os.chmod(socket_path, 0o666)
        with self.assertRaisesRegex(BenchmarkLockError, "wrong type"):
            admin.doctor(user_name=None)


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
        self.assertFalse(os.path.lexists(self.fixture.mapped(CLIENT_PATH)))
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
                configuration_source=None,
                user_name="ben",
            )
        with self.assertRaisesRegex(BenchmarkLockError, "committed uninstall"):
            self.fixture.admin.doctor(user_name=None)

    def test_completed_generation_is_recognized_as_uninstall_progress(self) -> None:
        self.fixture.install()
        launcher = self.fixture.source_root / "benchmarkd/bin/benchmarkd"
        launcher.write_bytes(launcher.read_bytes() + b"\n")
        self.fixture.admin.install(
            configuration_source=None,
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
            configuration_source=None,
            user_name="ben",
        )
        first_client = (
            self.fixture.mapped(GENERATION_DIRECTORY)
            / first_digest
            / "bin/benchmark-lock"
        )
        os.chmod(first_client, 0o755)
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


class BenchmarkAdminCliTest(unittest.TestCase):
    def test_normal_dotfiles_flows_do_not_invoke_privileged_admin(self) -> None:
        source_root = pathlib.Path(__file__).resolve().parents[1]
        for relative in ("bin/dotfiles", "install-deps.sh"):
            content = (source_root / relative).read_text()
            with self.subTest(relative=relative):
                self.assertNotIn("benchmark-admin", content)
                self.assertNotIn("benchmarkd.service", content)
                self.assertNotIn("benchmarkd.socket", content)

    def test_cli_exposes_only_explicit_operations_and_requires_user(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        source_root = pathlib.Path(__file__).resolve().parents[1]

        status = main(
            ["install"],
            source_root=source_root,
            output=output,
            error=error,
            environment={},
            admin_factory=lambda **_arguments: object(),
        )

        self.assertEqual(status, 1)
        self.assertIn("benchmark_admin_user_required", error.getvalue())
        self.assertEqual(BENCHMARK_GROUP_NAME, "benchmark")
        self.assertEqual(
            CONTROL_SOCKET_PATH, pathlib.Path("/run/benchmarkd/control.sock")
        )
