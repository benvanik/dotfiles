from __future__ import annotations

import contextlib
import fcntl
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


FORBIDDEN_SYSTEM_CLIENT_PATH = pathlib.Path("/usr/local/bin/benchmark-lock")


def _configuration(
    *,
    unique_id: str | None = "4610468131039e0",
    device_class: str = "0x030000",
) -> bytes:
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
                    "device_class": device_class,
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


class FakeGpuIdentityReader:
    def __init__(self, *identities) -> None:
        self.identities = {identity.bdf: identity for identity in identities}
        self.reads: list[str] = []

    def read_gpu_identity(self, bdf: str):
        self.reads.append(bdf)
        try:
            return self.identities[bdf]
        except KeyError as error:
            raise BenchmarkLockError(
                f"PCI BDF {bdf!r} is unavailable",
                code="benchmark_policy_unavailable",
            ) from error


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
        self.probes = 0
        self.fail_next_probe = False
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

    def probe(self) -> None:
        self.probes += 1
        self.timeline.append(("maintenance-probe",))
        if self.fail_next_probe:
            self.fail_next_probe = False
            raise BenchmarkLockError(
                "injected broker health probe failure",
                code="injected_broker_probe_failure",
            )


class KillOnProbeMaintenance(FakeMaintenance):
    def probe(self) -> None:
        super().probe()
        os.kill(os.getpid(), signal.SIGKILL)


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

    def probe(self) -> None:
        raise AssertionError("rejected maintenance must not probe the broker")


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
        self.identity = parse_policy_configuration(_configuration()).gpus[0]
        self.gpu_bdfs = (self.identity.bdf,)
        self.identity_reader = FakeGpuIdentityReader(self.identity)
        self.messages: list[str] = []
        repository_root = pathlib.Path(__file__).resolve().parents[1]
        self.source_root = pathlib.Path(self.temporary.name) / "source"
        for relative in (
            "bin",
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
            "bin/benchmark-lock",
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
            identity_reader=self.identity_reader,
            destination_root=self.root,
            root_uid=self.uid,
            root_gid=self.gid,
            effective_uid=self.uid,
            report=self.messages.append,
            check_runtime=False,
        )

    def mapped(self, absolute: pathlib.Path) -> pathlib.Path:
        return self.root / absolute.relative_to("/")

    def install(self) -> str:
        return self.admin.install(
            gpu_bdfs=self.gpu_bdfs,
            user_name="ben",
        )

    def new_admin(
        self,
        *,
        runner: FakeRunner | None = None,
        maintenance: FakeMaintenance | None = None,
        identity_reader: FakeGpuIdentityReader | None = None,
    ) -> BenchmarkAdmin:
        return BenchmarkAdmin(
            source_root=self.source_root,
            runner=self.runner if runner is None else runner,
            maintenance=(self.maintenance if maintenance is None else maintenance),
            accounts=self.accounts,
            identity_reader=(
                self.identity_reader if identity_reader is None else identity_reader
            ),
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

    def test_integrated_display_configuration_has_explicit_absent_serial(
        self,
    ) -> None:
        config = parse_policy_configuration(
            _configuration(unique_id=None, device_class="0x038000")
        )
        canonical = canonical_policy_configuration(config)

        self.assertIsNone(config.gpus[0].unique_id)
        self.assertEqual(config.gpus[0].device_class, "0x038000")
        self.assertIn(b'"unique_id":null', canonical)
        self.assertEqual(parse_policy_configuration(canonical), config)

    def test_processing_accelerator_configuration_is_canonical(self) -> None:
        config = parse_policy_configuration(
            _configuration(device_class="0x120000")
        )

        self.assertEqual(config.gpus[0].device_class, "0x120000")
        self.assertEqual(
            parse_policy_configuration(canonical_policy_configuration(config)),
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
        wrong_class = json.loads(_configuration())
        wrong_class["gpus"][0]["device_class"] = "0x020000"
        wrong_unique_id = json.loads(_configuration())
        wrong_unique_id["gpus"][0]["unique_id"] = 1

        for payload in (
            json.dumps(unknown).encode("ascii"),
            duplicate,
            json.dumps(wrong_vendor).encode("ascii"),
            json.dumps(wrong_class).encode("ascii"),
            json.dumps(wrong_unique_id).encode("ascii"),
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
        gpu_bdfs: tuple[str, ...],
        maintenance: FakeMaintenance | None = None,
    ) -> None:
        process_id = os.fork()
        if process_id == 0:
            child_admin = self.fixture.new_admin(
                runner=runner,
                maintenance=(FakeMaintenance() if maintenance is None else maintenance),
            )
            child_admin.install(
                gpu_bdfs=gpu_bdfs,
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
        self.assertFalse(
            os.path.lexists(self.fixture.mapped(FORBIDDEN_SYSTEM_CLIENT_PATH))
        )
        self.assertFalse((generation / "bin/benchmark-lock").exists())
        import_result = subprocess.run(
            [
                "/usr/bin/python3",
                "-I",
                "-c",
                (
                    "import sys; sys.dont_write_bytecode = True; "
                    f"sys.path.insert(0, {os.fspath(generation / 'lib')!r}); "
                    "import benchmark_lock.daemon"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(import_result.returncode, 0, import_result.stderr)
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
        self.assertEqual(
            self.fixture.identity_reader.reads,
            [self.fixture.identity.bdf],
        )
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
        self.assertEqual(self.fixture.maintenance.probes, 1)
        maintenance_enter = self.fixture.timeline.index(("maintenance-enter", True))
        maintenance_probe = self.fixture.timeline.index(("maintenance-probe",))
        maintenance_exit = self.fixture.timeline.index(("maintenance-exit", True))
        self.assertLess(maintenance_enter, maintenance_probe)
        self.assertLess(maintenance_probe, maintenance_exit)
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
            [self.fixture.source_root / "bin/benchmark-lock", "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(client_help.returncode, 0, client_help.stderr)
        self.assertIn("usage: benchmark-lock", client_help.stdout)

        self.assertEqual(self.fixture.admin.doctor(user_name="ben"), digest)
        self.assertEqual(self.fixture.accounts.required_users, ["ben"])
        self.assertEqual(self.fixture.maintenance.probes, 1)

    def test_fresh_install_discovers_unified_gpu_identity(self) -> None:
        identity = parse_policy_configuration(
            _configuration(unique_id=None, device_class="0x038000")
        ).gpus[0]
        identity_reader = FakeGpuIdentityReader(identity)

        self.fixture.new_admin(identity_reader=identity_reader).install(
            gpu_bdfs=(identity.bdf,),
            user_name="ben",
        )

        installed = parse_policy_configuration(
            self.fixture.mapped(CONFIG_PATH).read_bytes()
        )
        self.assertEqual(installed.gpus, (identity,))
        self.assertEqual(identity_reader.reads, [identity.bdf])

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

    def test_doctor_holds_one_shared_snapshot_against_install_cutover(self) -> None:
        digest = self.fixture.install()
        attempting_shared_lock = threading.Event()
        completed = threading.Event()
        results: list[str] = []
        failures: list[BaseException] = []
        original_flock = fcntl.flock

        def observe_flock(descriptor: int, operation: int) -> None:
            if operation == fcntl.LOCK_SH:
                attempting_shared_lock.set()
            original_flock(descriptor, operation)

        def run_doctor() -> None:
            try:
                results.append(self.fixture.admin.doctor(user_name=None))
            except BaseException as error:
                failures.append(error)
            finally:
                completed.set()

        with mock.patch(
            "benchmark_lock.admin.fcntl.flock",
            side_effect=observe_flock,
        ):
            with self.fixture.admin._hold_operation_lock():
                contender = threading.Thread(target=run_doctor)
                contender.start()
                attempting_shared_lock.wait()
                self.assertFalse(completed.is_set())

            contender.join()

        self.assertEqual(failures, [])
        self.assertEqual(results, [digest])

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
            "bin/benchmark-lock",
            "benchmarkd/bin/benchmarkd",
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
        self.fixture.maintenance.probes = 0
        self.fixture.timeline.clear()

        upgraded = self.fixture.admin.install(
            gpu_bdfs=(),
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
        self.assertEqual(self.fixture.maintenance.probes, 1)
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
        timeline_probe = self.fixture.timeline.index(("maintenance-probe",))
        self.assertLess(maintenance_enters[0], timeline_stop)
        self.assertLess(timeline_stop, maintenance_exits[0])
        self.assertLess(maintenance_exits[0], timeline_enable)
        self.assertLess(timeline_enable, maintenance_enters[1])
        self.assertLess(maintenance_enters[1], timeline_probe)
        self.assertLess(timeline_probe, maintenance_exits[1])
        self.assertFalse(
            any(
                "benchmark-lock" in argument
                for command in self.fixture.runner.commands
                for argument in command
            )
        )

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
                gpu_bdfs=(),
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
        self.fixture.maintenance.probes = 0
        self.fixture.timeline.clear()

        observed = self.fixture.admin.install(
            gpu_bdfs=(),
            user_name="ben",
        )

        self.assertEqual(observed, digest)
        self.assertEqual(
            self.fixture.maintenance.events,
            [("enter", True), ("exit", True)],
        )
        self.assertEqual(self.fixture.maintenance.probes, 1)
        maintenance_enter = self.fixture.timeline.index(("maintenance-enter", True))
        maintenance_probe = self.fixture.timeline.index(("maintenance-probe",))
        maintenance_exit = self.fixture.timeline.index(("maintenance-exit", True))
        self.assertLess(maintenance_enter, maintenance_probe)
        self.assertLess(maintenance_probe, maintenance_exit)
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

    def test_same_generation_probe_failure_never_stops_or_journals(self) -> None:
        digest = self.fixture.install()
        self.fixture.runner.commands.clear()
        self.fixture.maintenance.events.clear()
        self.fixture.maintenance.probes = 0
        self.fixture.maintenance.fail_next_probe = True

        with self.assertRaisesRegex(
            BenchmarkLockError,
            "injected broker health probe failure",
        ):
            self.fixture.admin.install(
                gpu_bdfs=(),
                user_name="ben",
            )

        self.assertEqual(
            os.readlink(self.fixture.mapped(CURRENT_SELECTOR)),
            f"generations/{digest}",
        )
        self.assertEqual(self.fixture.maintenance.probes, 1)
        self.assertFalse(self.fixture.admin.journal.has_install_state())
        self.assertNotIn(
            ("/usr/bin/systemctl", "stop", "benchmarkd.socket"),
            self.fixture.runner.commands,
        )
        self.assertNotIn(
            ("/usr/bin/systemctl", "stop", "benchmarkd.service"),
            self.fixture.runner.commands,
        )

    def test_upgrade_rejects_policy_replacement_without_mutating_config(self) -> None:
        self.fixture.install()
        installed = self.fixture.mapped(CONFIG_PATH)
        original = installed.read_bytes()
        replacement = parse_policy_configuration(
            _configuration(unique_id="4610468131039e1")
        ).gpus[0]
        command_count = len(self.fixture.runner.commands)

        with self.assertRaisesRegex(
            BenchmarkLockError,
            "does not expose the fenced epoch-aware replacement transaction",
        ):
            self.fixture.new_admin(
                identity_reader=FakeGpuIdentityReader(replacement)
            ).install(
                gpu_bdfs=(replacement.bdf,),
                user_name="ben",
            )

        self.assertEqual(installed.read_bytes(), original)
        self.assertEqual(len(self.fixture.runner.commands), command_count)

    def test_first_install_requires_policy_and_root(self) -> None:
        with self.assertRaisesRegex(BenchmarkLockError, "requires at least one --gpu"):
            self.fixture.admin.install(
                gpu_bdfs=(),
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
                gpu_bdfs=self.fixture.gpu_bdfs,
                user_name="ben",
            )

    def test_unavailable_gpu_is_rejected_before_installation_mutates_host(
        self,
    ) -> None:
        with self.assertRaises(BenchmarkLockError):
            self.fixture.admin.install(
                gpu_bdfs=("0000:ff:00.0",),
                user_name="ben",
            )

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
            gpu_bdfs=(),
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
            gpu_bdfs=(),
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
        self.assertEqual(retry_maintenance.probes, 1)

    def test_sigkill_during_target_probe_recovers_stopped_install(
        self,
    ) -> None:
        self._fork_killed_install(
            FakeRunner(),
            gpu_bdfs=self.fixture.gpu_bdfs,
            maintenance=KillOnProbeMaintenance(),
        )

        intent_path = self.fixture.mapped(INSTALL_INTENT_PATH)
        intent = json.loads(intent_path.read_text())
        self.assertEqual(intent["phase"], "stopped")
        self.assertEqual(intent["prior_digest"], None)

        recovered = self.fixture.new_admin().install(
            gpu_bdfs=(),
            user_name="ben",
        )

        self.assertEqual(recovered, intent["target_digest"])
        self.assertFalse(os.path.lexists(intent_path))
        self.assertEqual(self.fixture.maintenance.probes, 1)

    def test_sigkill_during_durable_rollback_resumes_prior(self) -> None:
        prior_digest = self.fixture.install()
        launcher = self.fixture.source_root / "benchmarkd/bin/benchmarkd"
        prior_launcher = launcher.read_bytes()
        launcher.write_bytes(prior_launcher + b"\n")
        self.fixture.maintenance.events.clear()
        self.fixture.maintenance.probes = 0
        self.fixture.timeline.clear()
        self._fork_killed_install(
            FailActivationThenKillRollbackRunner(),
            gpu_bdfs=(),
        )

        intent_path = self.fixture.mapped(INSTALL_INTENT_PATH)
        intent = json.loads(intent_path.read_text())
        self.assertEqual(intent["phase"], "rollback")
        self.assertEqual(intent["prior_digest"], prior_digest)
        launcher.write_bytes(prior_launcher)

        observed = self.fixture.new_admin().install(
            gpu_bdfs=(),
            user_name="ben",
        )

        self.assertEqual(observed, prior_digest)
        self.assertFalse(os.path.lexists(intent_path))
        self.assertEqual(
            os.readlink(self.fixture.mapped(CURRENT_SELECTOR)),
            f"generations/{prior_digest}",
        )
        self.assertEqual(self.fixture.maintenance.probes, 2)

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
        self.fixture.maintenance.events.clear()
        self.fixture.maintenance.probes = 0
        self.fixture.timeline.clear()

        with self.assertRaisesRegex(BenchmarkLockError, "injected"):
            self.fixture.admin.install(
                gpu_bdfs=(),
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
        self.assertEqual(self.fixture.maintenance.probes, 1)

    def test_target_probe_failure_rolls_back_to_a_proven_prior_generation(
        self,
    ) -> None:
        prior_digest = self.fixture.install()
        launcher = self.fixture.source_root / "benchmarkd/bin/benchmarkd"
        launcher.write_bytes(launcher.read_bytes() + b"\n")
        self.fixture.maintenance.events.clear()
        self.fixture.maintenance.probes = 0
        self.fixture.maintenance.fail_next_probe = True
        self.fixture.timeline.clear()

        with self.assertRaisesRegex(
            BenchmarkLockError,
            "injected broker health probe failure",
        ):
            self.fixture.admin.install(
                gpu_bdfs=(),
                user_name="ben",
            )

        self.assertEqual(
            os.readlink(self.fixture.mapped(CURRENT_SELECTOR)),
            f"generations/{prior_digest}",
        )
        self.assertEqual(self.fixture.maintenance.probes, 2)
        self.assertEqual(
            self.fixture.maintenance.events,
            [
                ("enter", True),
                ("exit", True),
                ("enter", True),
                ("exit", True),
                ("enter", True),
                ("exit", True),
            ],
        )
        probe_indices = [
            index
            for index, event in enumerate(self.fixture.timeline)
            if event == ("maintenance-probe",)
        ]
        self.assertEqual(len(probe_indices), 2)
        rollback_start = max(
            index
            for index, event in enumerate(self.fixture.timeline)
            if event
            == (
                "command",
                ("/usr/bin/systemctl", "start", "benchmarkd.service"),
            )
        )
        self.assertLess(rollback_start, probe_indices[1])

    def test_fresh_target_probe_failure_removes_the_live_projection(self) -> None:
        self.fixture.maintenance.fail_next_probe = True

        with self.assertRaisesRegex(
            BenchmarkLockError,
            "injected broker health probe failure",
        ):
            self.fixture.install()

        self.assertEqual(self.fixture.maintenance.probes, 1)
        self.assertFalse(self.fixture.admin.journal.has_install_state())
        for path in (
            CURRENT_SELECTOR,
            SOCKET_UNIT_PATH,
            SERVICE_UNIT_PATH,
            SYSUSERS_PATH,
        ):
            self.assertFalse(os.path.lexists(self.fixture.mapped(path)))

    def test_rollback_probe_failure_remains_durable_and_resumable(self) -> None:
        prior_digest = self.fixture.install()
        launcher = self.fixture.source_root / "benchmarkd/bin/benchmarkd"
        prior_launcher = launcher.read_bytes()
        launcher.write_bytes(prior_launcher + b"\n")
        self.fixture.runner.fail_command = (
            "/usr/bin/systemctl",
            "start",
            "benchmarkd.service",
        )
        self.fixture.maintenance.fail_next_probe = True

        with self.assertRaisesRegex(
            BenchmarkLockError,
            "rollback could not complete",
        ):
            self.fixture.admin.install(
                gpu_bdfs=(),
                user_name="ben",
            )

        intent_path = self.fixture.mapped(INSTALL_INTENT_PATH)
        self.assertEqual(json.loads(intent_path.read_bytes())["phase"], "rollback")
        self.assertEqual(
            os.readlink(self.fixture.mapped(CURRENT_SELECTOR)),
            f"generations/{prior_digest}",
        )

        launcher.write_bytes(prior_launcher)
        retry_maintenance = FakeMaintenance()
        recovered = self.fixture.new_admin(
            maintenance=retry_maintenance,
        ).install(
            gpu_bdfs=(),
            user_name="ben",
        )

        self.assertEqual(recovered, prior_digest)
        self.assertFalse(os.path.lexists(intent_path))
        self.assertEqual(retry_maintenance.probes, 2)

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
                gpu_bdfs=(),
                user_name="ben",
            )

        self.assertTrue(injected)
        self.assertTrue(self.fixture.mapped(SOCKET_UNIT_STAGE_PATH).exists())
        intent = json.loads(self.fixture.mapped(INSTALL_INTENT_PATH).read_text())
        self.assertEqual(intent["phase"], "stopped")

        recovered = self.fixture.new_admin().install(
            gpu_bdfs=(),
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
                gpu_bdfs=(),
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
            gpu_bdfs=(),
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
        self.assertFalse(
            os.path.lexists(self.fixture.mapped(FORBIDDEN_SYSTEM_CLIENT_PATH))
        )
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
            SOCKET_UNIT_PATH,
            SERVICE_UNIT_PATH,
            SYSUSERS_PATH,
        ):
            self.assertTrue(os.path.lexists(self.fixture.mapped(path)))
        self.assertFalse(
            os.path.lexists(self.fixture.mapped(FORBIDDEN_SYSTEM_CLIENT_PATH))
        )
        self.assertTrue(self.fixture.mapped(CONFIG_PATH).exists())
        self.assertTrue(self.fixture.mapped(GENERATION_DIRECTORY).exists())
        recovered = self.fixture.new_admin().install(
            gpu_bdfs=(),
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
                gpu_bdfs=(),
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
        self.fixture.maintenance.probes = 0

        self.assertEqual(admin.doctor(user_name=None), digest)
        self.assertEqual(self.fixture.maintenance.probes, 1)
        self.assertFalse(
            any(
                "benchmark-lock" in argument
                for command in self.fixture.runner.commands
                for argument in command
            )
        )
        os.chmod(socket_path, 0o666)
        with self.assertRaisesRegex(BenchmarkLockError, "wrong type"):
            admin.doctor(user_name=None)
        self.assertEqual(self.fixture.maintenance.probes, 1)


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

    def test_install_passes_each_selected_gpu_to_the_administrator(self) -> None:
        class RecordingAdmin:
            def __init__(self) -> None:
                self.install_arguments = None

            def install(self, *, gpu_bdfs, user_name) -> None:
                self.install_arguments = (gpu_bdfs, user_name)

        admin = RecordingAdmin()
        output = io.StringIO()
        error = io.StringIO()
        status = main(
            [
                "install",
                "--gpu",
                "0000:23:00.0",
                "--gpu",
                "0000:c2:00.0",
            ],
            source_root=pathlib.Path(__file__).resolve().parents[1],
            output=output,
            error=error,
            environment={"SUDO_USER": "ben"},
            admin_factory=lambda **_arguments: admin,
        )

        self.assertEqual(status, 0, error.getvalue())
        self.assertEqual(
            admin.install_arguments,
            (("0000:23:00.0", "0000:c2:00.0"), "ben"),
        )
