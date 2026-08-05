"""Explicit, transactional installation surface for benchmarkd.

Normal dotfiles installation deliberately knows nothing about this module.
The administrator invokes ``benchmark-admin`` for the one operation that
publishes root-owned broker code, policy, and units.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import grp
import os
import pathlib
import pwd
import re
import stat
import subprocess
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Protocol

from .administration_state import (
    ADMIN_LOCK_PATH,
    INSTALL_INTENT_PATH,
    INSTALL_PUBLISH_PATH,
    INSTALL_ROOT,
    INSTALL_TRANSITION_PATH,
    STATE_DIRECTORY,
    UNINSTALL_INTENT_PATH,
    UNINSTALL_PUBLISH_PATH,
    UNINSTALL_TRANSITION_PATH,
)
from .administration_journal import (
    AdministrationJournal,
    InstallIntent,
    JournalPaths,
    UninstallIntent,
)
from .configuration import (
    CONFIG_PATH,
    MAX_CONFIG_BYTES,
    canonical_policy_configuration,
    parse_policy_configuration,
)
from .control_channel import BENCHMARK_GROUP_NAME
from .errors import BenchmarkLockError
from .generation_format import MAX_SOURCE_FILE_BYTES, build_generation
from .generation_store import GenerationStore
from .installation_projection import (
    InstallationProjection,
    RegularProjection,
    SymlinkProjection,
    publish_new_regular,
)
from .policy import AmdGpuIdentity, FixedHostPolicyConfig, LinuxHostFilesystem


EPOCH_PATH = STATE_DIRECTORY / "active-epoch.json"
GENERATION_DIRECTORY = INSTALL_ROOT / "generations"
CURRENT_SELECTOR = INSTALL_ROOT / "current"
SYSTEMD_UNIT_DIRECTORY = pathlib.Path("/usr/local/lib/systemd/system")
SOCKET_UNIT_PATH = SYSTEMD_UNIT_DIRECTORY / "benchmarkd.socket"
SERVICE_UNIT_PATH = SYSTEMD_UNIT_DIRECTORY / "benchmarkd.service"
SYSUSERS_PATH = pathlib.Path("/usr/local/lib/sysusers.d/benchmarkd.conf")
CONTROL_SOCKET_PATH = pathlib.Path("/run/benchmarkd/control.sock")
SOCKET_UNIT_STAGE_PATH = SYSTEMD_UNIT_DIRECTORY / ".benchmarkd.socket.install"
SERVICE_UNIT_STAGE_PATH = SYSTEMD_UNIT_DIRECTORY / ".benchmarkd.service.install"
SYSUSERS_STAGE_PATH = SYSUSERS_PATH.with_name(".benchmarkd.conf.install")
CURRENT_STAGE_PATH = INSTALL_ROOT / ".current.install"

_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
_USER_PATTERN = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
_LEGACY_STATE_DIRECTORY_PATTERN = re.compile(r"benchmark-lock-[A-Za-z0-9_.-]+")
_MANAGED_MARKER = b"# Managed by benchmark-admin.\n"


def _admin_error(message: str, *, code: str) -> BenchmarkLockError:
    return BenchmarkLockError(message, code=code)


class CommandRunner(Protocol):
    """Privileged host-command boundary."""

    def run(self, command: tuple[str, ...]) -> None:
        """Run one exact argv vector or raise."""


class GpuIdentityReader(Protocol):
    """Administrator-selected PCI identity discovery boundary."""

    def read_gpu_identity(self, bdf: str) -> AmdGpuIdentity:
        """Read the exact AMD display-controller identity at one PCI BDF."""


class SubprocessCommandRunner:
    """Production command runner with no shell interpretation."""

    def run(self, command: tuple[str, ...]) -> None:
        try:
            subprocess.run(command, check=True)
        except (OSError, subprocess.CalledProcessError) as error:
            raise _admin_error(
                f"administrator command failed: {' '.join(command)}: {error}",
                code="benchmark_admin_command_failed",
            ) from error


class MaintenanceCoordinator(Protocol):
    """Exclusive daemon maintenance boundary for active installations."""

    def hold(self, *, installed: bool) -> AbstractContextManager[None]:
        """Hold root maintenance authority through a mutating cutover."""

    def probe(self) -> None:
        """Require one healthy broker status exchange."""


class ProductionMaintenanceCoordinator:
    """Root control-channel maintenance fence, loaded only when needed."""

    def hold(self, *, installed: bool) -> AbstractContextManager[None]:
        if not installed:
            return contextlib.nullcontext()
        from .maintenance import hold_maintenance

        return hold_maintenance()

    def probe(self) -> None:
        from .maintenance import probe_status

        probe_status()


class AccountManager(Protocol):
    """System account database boundary."""

    @property
    def benchmark_group_id(self) -> int:
        """Return the installed benchmark group identity."""

    def add_user(self, user_name: str) -> None:
        """Add one validated user to the benchmark group if needed."""

    def validate_user(self, user_name: str) -> None:
        """Require one existing, non-root, canonical user."""

    def require_user(self, user_name: str | None) -> None:
        """Validate group existence and optional membership."""


class ProductionAccountManager:
    """NSS-backed group and membership management."""

    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    @property
    def benchmark_group_id(self) -> int:
        try:
            return grp.getgrnam(BENCHMARK_GROUP_NAME).gr_gid
        except KeyError as error:
            raise _admin_error(
                f"system group {BENCHMARK_GROUP_NAME!r} does not exist",
                code="benchmark_admin_group_missing",
            ) from error

    @staticmethod
    def _user(user_name: str) -> pwd.struct_passwd:
        if not _USER_PATTERN.fullmatch(user_name):
            raise _admin_error(
                f"benchmark user name {user_name!r} is not canonical",
                code="benchmark_admin_user_invalid",
            )
        try:
            user = pwd.getpwnam(user_name)
        except KeyError as error:
            raise _admin_error(
                f"benchmark user {user_name!r} does not exist",
                code="benchmark_admin_user_invalid",
            ) from error
        if user.pw_uid == 0:
            raise _admin_error(
                "root does not need membership in the benchmark group",
                code="benchmark_admin_user_invalid",
            )
        return user

    @staticmethod
    def _is_member(user: pwd.struct_passwd, group: grp.struct_group) -> bool:
        return user.pw_gid == group.gr_gid or user.pw_name in group.gr_mem

    def add_user(self, user_name: str) -> None:
        user = self._user(user_name)
        group = grp.getgrnam(BENCHMARK_GROUP_NAME)
        if self._is_member(user, group):
            return
        self._runner.run(
            (
                "/usr/sbin/usermod",
                "--append",
                "--groups",
                BENCHMARK_GROUP_NAME,
                user_name,
            )
        )
        updated_user = self._user(user_name)
        updated_group = grp.getgrnam(BENCHMARK_GROUP_NAME)
        if not self._is_member(updated_user, updated_group):
            raise _admin_error(
                f"user {user_name!r} was not added to {BENCHMARK_GROUP_NAME!r}",
                code="benchmark_admin_membership_missing",
            )

    def validate_user(self, user_name: str) -> None:
        self._user(user_name)

    def require_user(self, user_name: str | None) -> None:
        try:
            group = grp.getgrnam(BENCHMARK_GROUP_NAME)
        except KeyError as error:
            raise _admin_error(
                f"system group {BENCHMARK_GROUP_NAME!r} does not exist",
                code="benchmark_admin_group_missing",
            ) from error
        if user_name is None:
            return
        user = self._user(user_name)
        if not self._is_member(user, group):
            raise _admin_error(
                f"user {user_name!r} is not a member of {BENCHMARK_GROUP_NAME!r}",
                code="benchmark_admin_membership_missing",
            )


@dataclasses.dataclass(frozen=True)
class _Layout:
    destination_root: pathlib.Path

    def __post_init__(self) -> None:
        if not self.destination_root.is_absolute():
            raise ValueError("administrator destination root must be absolute")

    def map(self, path: pathlib.Path) -> pathlib.Path:
        if not path.is_absolute():
            raise ValueError("administrator path must be absolute")
        return self.destination_root / path.relative_to("/")

    @property
    def install_root(self) -> pathlib.Path:
        return self.map(INSTALL_ROOT)

    @property
    def generations(self) -> pathlib.Path:
        return self.map(GENERATION_DIRECTORY)

    @property
    def current(self) -> pathlib.Path:
        return self.map(CURRENT_SELECTOR)

    @property
    def config(self) -> pathlib.Path:
        return self.map(CONFIG_PATH)

    @property
    def state(self) -> pathlib.Path:
        return self.map(STATE_DIRECTORY)

    @property
    def epoch(self) -> pathlib.Path:
        return self.map(EPOCH_PATH)

    @property
    def socket_unit(self) -> pathlib.Path:
        return self.map(SOCKET_UNIT_PATH)

    @property
    def service_unit(self) -> pathlib.Path:
        return self.map(SERVICE_UNIT_PATH)

    @property
    def sysusers(self) -> pathlib.Path:
        return self.map(SYSUSERS_PATH)

    @property
    def control_socket(self) -> pathlib.Path:
        return self.map(CONTROL_SOCKET_PATH)

    @property
    def admin_lock(self) -> pathlib.Path:
        return self.map(ADMIN_LOCK_PATH)

    @property
    def install_intent(self) -> pathlib.Path:
        return self.map(INSTALL_INTENT_PATH)

    @property
    def install_publish(self) -> pathlib.Path:
        return self.map(INSTALL_PUBLISH_PATH)

    @property
    def install_transition(self) -> pathlib.Path:
        return self.map(INSTALL_TRANSITION_PATH)

    @property
    def uninstall_intent(self) -> pathlib.Path:
        return self.map(UNINSTALL_INTENT_PATH)

    @property
    def uninstall_publish(self) -> pathlib.Path:
        return self.map(UNINSTALL_PUBLISH_PATH)

    @property
    def uninstall_transition(self) -> pathlib.Path:
        return self.map(UNINSTALL_TRANSITION_PATH)

    @property
    def socket_unit_stage(self) -> pathlib.Path:
        return self.map(SOCKET_UNIT_STAGE_PATH)

    @property
    def service_unit_stage(self) -> pathlib.Path:
        return self.map(SERVICE_UNIT_STAGE_PATH)

    @property
    def sysusers_stage(self) -> pathlib.Path:
        return self.map(SYSUSERS_STAGE_PATH)

    @property
    def current_stage(self) -> pathlib.Path:
        return self.map(CURRENT_STAGE_PATH)

    @property
    def temporary(self) -> pathlib.Path:
        return self.map(pathlib.Path("/tmp"))


Reporter = Callable[[str], None]


def _read_source_file(path: pathlib.Path) -> bytes:
    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise _admin_error(
            f"cannot inspect installation source {path}: {error}",
            code="benchmark_admin_source_invalid",
        ) from error
    if not stat.S_ISREG(metadata.st_mode):
        raise _admin_error(
            f"installation source is not a regular file: {path}",
            code="benchmark_admin_source_invalid",
        )
    if metadata.st_size > MAX_SOURCE_FILE_BYTES:
        raise _admin_error(
            f"installation source is too large: {path}",
            code="benchmark_admin_source_invalid",
        )
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            content = os.read(descriptor, MAX_SOURCE_FILE_BYTES + 1)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise _admin_error(
            f"cannot read installation source {path}: {error}",
            code="benchmark_admin_source_invalid",
        ) from error
    if len(content) != metadata.st_size:
        raise _admin_error(
            f"installation source changed while being read: {path}",
            code="benchmark_admin_source_invalid",
        )
    return content


def _mode(metadata: os.stat_result) -> int:
    return stat.S_IMODE(metadata.st_mode)


class BenchmarkAdmin:
    """Root-only installer, auditor, and conservative software remover."""

    def __init__(
        self,
        *,
        source_root: pathlib.Path,
        runner: CommandRunner | None = None,
        maintenance: MaintenanceCoordinator | None = None,
        accounts: AccountManager | None = None,
        identity_reader: GpuIdentityReader | None = None,
        destination_root: pathlib.Path = pathlib.Path("/"),
        root_uid: int = 0,
        root_gid: int = 0,
        effective_uid: int | None = None,
        report: Reporter = print,
        check_runtime: bool = True,
    ) -> None:
        self.source_root = pathlib.Path(source_root)
        if not self.source_root.is_absolute():
            raise ValueError("benchmark source root must be absolute")
        self.runner = SubprocessCommandRunner() if runner is None else runner
        self.maintenance = (
            ProductionMaintenanceCoordinator() if maintenance is None else maintenance
        )
        self.accounts = (
            ProductionAccountManager(self.runner) if accounts is None else accounts
        )
        self.identity_reader = (
            LinuxHostFilesystem() if identity_reader is None else identity_reader
        )
        self.layout = _Layout(pathlib.Path(destination_root))
        self.root_uid = root_uid
        self.root_gid = root_gid
        self.effective_uid = os.geteuid() if effective_uid is None else effective_uid
        self.report = report
        self.check_runtime = check_runtime
        self.generation_store = GenerationStore(
            generation_directory=self.layout.generations,
            root_uid=self.root_uid,
            root_gid=self.root_gid,
            report=self.report,
        )
        self.journal = AdministrationJournal(
            install_paths=JournalPaths(
                publish=self.layout.install_publish,
                intent=self.layout.install_intent,
                transition=self.layout.install_transition,
            ),
            uninstall_paths=JournalPaths(
                publish=self.layout.uninstall_publish,
                intent=self.layout.uninstall_intent,
                transition=self.layout.uninstall_transition,
            ),
            root_uid=self.root_uid,
            root_gid=self.root_gid,
            report=self.report,
        )

    def _require_root(self) -> None:
        if self.effective_uid != self.root_uid:
            raise _admin_error(
                "benchmark-admin must run as root (use sudo)",
                code="benchmark_admin_requires_root",
            )

    def _require_secure_parent(self, path: pathlib.Path) -> None:
        try:
            metadata = os.lstat(path)
        except OSError as error:
            raise _admin_error(
                f"required installation directory is unavailable: {path}: {error}",
                code="benchmark_admin_layout_invalid",
            ) from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != self.root_uid
            or metadata.st_gid != self.root_gid
            or _mode(metadata) & 0o022
        ):
            raise _admin_error(
                f"required installation directory is not securely root-owned: {path}",
                code="benchmark_admin_layout_invalid",
            )

    def _require_directory(self, path: pathlib.Path, *, mode: int) -> None:
        try:
            metadata = os.lstat(path)
        except OSError as error:
            raise _admin_error(
                f"managed directory is unavailable: {path}: {error}",
                code="benchmark_admin_layout_invalid",
            ) from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != self.root_uid
            or metadata.st_gid != self.root_gid
            or _mode(metadata) != mode
        ):
            raise _admin_error(
                f"managed directory has unsafe ownership or mode: {path}",
                code="benchmark_admin_layout_invalid",
            )

    @contextlib.contextmanager
    def _hold_operation_lock(self, *, shared: bool = False):
        """Serialize mutations and hold coherent shared audit snapshots."""

        self._ensure_operation_lock_directory()
        flags = os.O_RDWR | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        created = False
        try:
            try:
                previous_mask = os.umask(0)
                try:
                    descriptor = os.open(
                        self.layout.admin_lock,
                        flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                finally:
                    os.umask(previous_mask)
                created = True
            except FileExistsError:
                descriptor = os.open(self.layout.admin_lock, flags)
        except OSError as error:
            raise _admin_error(
                f"cannot open benchmark administrator lock: {error}",
                code="benchmark_admin_lock_failed",
            ) from error
        locked = False
        try:
            if created:
                os.fchown(descriptor, self.root_uid, self.root_gid)
                os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != self.root_uid
                or metadata.st_gid != self.root_gid
                or _mode(metadata) != 0o600
                or metadata.st_nlink != 1
            ):
                raise _admin_error(
                    "benchmark administrator lock has unsafe ownership or mode",
                    code="benchmark_admin_lock_failed",
                )
            try:
                operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
                fcntl.flock(descriptor, operation)
            except OSError as error:
                raise _admin_error(
                    f"cannot acquire benchmark administrator lock: {error}",
                    code="benchmark_admin_lock_failed",
                ) from error
            locked = True
            yield descriptor
        finally:
            try:
                if locked:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _ensure_operation_lock_directory(self) -> None:
        """Converge first install and systemd onto one retained lock parent."""

        directory = self.layout.admin_lock.parent
        if directory != self.layout.state:
            raise AssertionError("administrator lock is outside benchmark state")
        self._require_secure_parent(directory.parent)
        try:
            previous_mask = os.umask(0)
            try:
                os.mkdir(directory, 0o700)
            finally:
                os.umask(previous_mask)
        except FileExistsError:
            pass
        except OSError as error:
            raise _admin_error(
                f"cannot create benchmark state directory: {error}",
                code="benchmark_admin_lock_failed",
            ) from error
        else:
            try:
                os.chown(directory, self.root_uid, self.root_gid)
                os.chmod(directory, 0o700)
                self._fsync_directory(directory.parent)
            except OSError as error:
                raise _admin_error(
                    f"cannot secure benchmark state directory: {error}",
                    code="benchmark_admin_lock_failed",
                ) from error
        try:
            self._require_directory(directory, mode=0o700)
        except BenchmarkLockError as error:
            raise _admin_error(
                f"cannot use benchmark state for administrator locking: {error}",
                code="benchmark_admin_lock_failed",
            ) from error

    def _ensure_directory(self, path: pathlib.Path, *, mode: int) -> None:
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            try:
                previous_mask = os.umask(0)
                try:
                    os.mkdir(path, mode)
                finally:
                    os.umask(previous_mask)
                os.chown(path, self.root_uid, self.root_gid)
                os.chmod(path, mode)
                self._fsync_directory(path.parent)
                return
            except OSError as error:
                raise _admin_error(
                    f"cannot create managed directory {path}: {error}",
                    code="benchmark_admin_install_failed",
                ) from error
        except OSError as error:
            raise _admin_error(
                f"cannot inspect managed directory {path}: {error}",
                code="benchmark_admin_layout_invalid",
            ) from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != self.root_uid
            or metadata.st_gid != self.root_gid
            or _mode(metadata) != mode
        ):
            raise _admin_error(
                f"managed directory has unsafe ownership or mode: {path}",
                code="benchmark_admin_layout_invalid",
            )

    @staticmethod
    def _fsync_directory(path: pathlib.Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _prepare_layout(self) -> None:
        parents = (
            self.layout.map(pathlib.Path("/")),
            self.layout.map(pathlib.Path("/usr")),
            self.layout.map(pathlib.Path("/usr/local")),
            self.layout.map(pathlib.Path("/usr/local/lib")),
            self.layout.map(pathlib.Path("/etc")),
            self.layout.map(pathlib.Path("/var")),
            self.layout.map(pathlib.Path("/var/lib")),
        )
        for parent in parents:
            self._require_secure_parent(parent)
        self._ensure_directory(
            self.layout.map(pathlib.Path("/usr/local/lib/systemd")),
            mode=0o755,
        )
        self._ensure_directory(
            self.layout.map(pathlib.Path("/usr/local/lib/systemd/system")),
            mode=0o755,
        )
        self._ensure_directory(
            self.layout.map(pathlib.Path("/usr/local/lib/sysusers.d")),
            mode=0o755,
        )
        self._ensure_directory(self.layout.install_root, mode=0o755)
        self._ensure_directory(self.layout.generations, mode=0o755)
        self._ensure_directory(self.layout.config.parent, mode=0o700)
        self._ensure_directory(self.layout.state, mode=0o700)

    def _require_existing_layout(self) -> None:
        """Validate every existing managed ancestor without creating paths."""

        for parent in (
            self.layout.map(pathlib.Path("/")),
            self.layout.map(pathlib.Path("/usr")),
            self.layout.map(pathlib.Path("/usr/local")),
            self.layout.map(pathlib.Path("/usr/local/lib")),
            self.layout.map(pathlib.Path("/etc")),
            self.layout.map(pathlib.Path("/var")),
            self.layout.map(pathlib.Path("/var/lib")),
            self.layout.map(pathlib.Path("/run")),
        ):
            self._require_secure_parent(parent)
        for path, mode in (
            (
                self.layout.map(pathlib.Path("/usr/local/lib/systemd")),
                0o755,
            ),
            (
                self.layout.map(pathlib.Path("/usr/local/lib/systemd/system")),
                0o755,
            ),
            (
                self.layout.map(pathlib.Path("/usr/local/lib/sysusers.d")),
                0o755,
            ),
            (self.layout.install_root, 0o755),
            (self.layout.generations, 0o755),
            (self.layout.config.parent, 0o700),
            (self.layout.state, 0o700),
        ):
            if os.path.lexists(path):
                self._require_directory(path, mode=mode)

    def _legacy_paths(self) -> tuple[pathlib.Path, ...]:
        try:
            with os.scandir(self.layout.temporary) as iterator:
                candidates = tuple(iterator)
        except FileNotFoundError:
            return ()
        except OSError as error:
            raise _admin_error(
                f"cannot inspect legacy benchmark state: {error}",
                code="benchmark_admin_legacy_check_failed",
            ) from error
        legacy: list[pathlib.Path] = []
        for entry in candidates:
            if not _LEGACY_STATE_DIRECTORY_PATTERN.fullmatch(
                entry.name
            ) or not entry.is_dir(follow_symlinks=False):
                continue
            state_path = pathlib.Path(entry.path) / "state.tsv"
            if not os.path.lexists(state_path):
                continue
            try:
                metadata = os.lstat(state_path)
            except OSError as error:
                raise _admin_error(
                    f"cannot inspect historical benchmark state {state_path}: {error}",
                    code="benchmark_admin_legacy_check_failed",
                ) from error
            if not stat.S_ISREG(metadata.st_mode):
                raise _admin_error(
                    "historical benchmark state artifact has an unsafe type at "
                    f"{state_path}; inspect it and recover with benchmark-unlock",
                    code="benchmark_admin_legacy_check_failed",
                )
            legacy.append(pathlib.Path(entry.path))
        return tuple(sorted(legacy, key=os.fspath))

    def _require_no_legacy_state(self) -> None:
        legacy = self._legacy_paths()
        if legacy:
            formatted = ", ".join(os.fspath(path) for path in legacy)
            raise _admin_error(
                "legacy benchmark lock state still exists at "
                f"{formatted}; run benchmark-unlock before installing benchmarkd",
                code="benchmark_admin_legacy_lock_active",
            )

    def _read_regular(
        self,
        path: pathlib.Path,
        *,
        maximum: int,
        expected_mode: int | None = None,
    ) -> bytes:
        try:
            metadata = os.lstat(path)
        except OSError as error:
            raise _admin_error(
                f"cannot inspect managed file {path}: {error}",
                code="benchmark_admin_layout_invalid",
            ) from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != self.root_uid
            or metadata.st_gid != self.root_gid
            or (expected_mode is not None and _mode(metadata) != expected_mode)
            or metadata.st_size > maximum
        ):
            raise _admin_error(
                f"managed file has unsafe type, ownership, mode, or size: {path}",
                code="benchmark_admin_layout_invalid",
            )
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            content = os.read(descriptor, maximum + 1)
        finally:
            os.close(descriptor)
        if len(content) != metadata.st_size:
            raise _admin_error(
                f"managed file changed while being read: {path}",
                code="benchmark_admin_layout_invalid",
            )
        return content

    def _configuration_for_gpus(self, gpu_bdfs: tuple[str, ...]) -> bytes:
        try:
            config = FixedHostPolicyConfig(
                tuple(self.identity_reader.read_gpu_identity(bdf) for bdf in gpu_bdfs)
            )
        except ValueError as error:
            raise _admin_error(
                f"benchmarkd GPU selection is invalid: {error}",
                code="invalid_benchmark_policy_configuration",
            ) from error
        return canonical_policy_configuration(config)

    def _install_configuration(
        self,
        requested_configuration: bytes | None,
    ) -> None:
        if os.path.lexists(self.layout.config):
            installed = self._read_regular(
                self.layout.config,
                maximum=MAX_CONFIG_BYTES,
                expected_mode=0o600,
            )
            config = parse_policy_configuration(installed)
            if installed != canonical_policy_configuration(config):
                raise _admin_error(
                    "installed benchmarkd policy configuration is not canonical",
                    code="benchmark_admin_layout_invalid",
                )
            if (
                requested_configuration is not None
                and requested_configuration != installed
            ):
                raise _admin_error(
                    "install preserves the existing benchmarkd policy; "
                    "benchmark-admin does not expose the fenced epoch-aware "
                    "replacement transaction required to change it",
                    code="benchmark_admin_config_preserved",
                )
            return
        if requested_configuration is None:
            raise _admin_error(
                "first installation requires at least one --gpu BDF",
                code="benchmark_admin_config_required",
            )
        publish_new_regular(
            self.layout.config,
            requested_configuration,
            mode=0o600,
            root_uid=self.root_uid,
            root_gid=self.root_gid,
        )

    def _require_epoch_security(self) -> None:
        if os.path.lexists(self.layout.epoch):
            self._read_regular(
                self.layout.epoch,
                maximum=1024 * 1024,
                expected_mode=0o600,
            )

    def _has_uninstall_state(self) -> bool:
        return self.journal.has_uninstall_state()

    def _external_file_payloads(
        self,
        generation_root: pathlib.Path,
    ) -> tuple[tuple[pathlib.Path, bytes, str], ...]:
        socket_unit = self._read_regular(
            generation_root / "share/systemd/benchmarkd.socket",
            maximum=MAX_SOURCE_FILE_BYTES,
            expected_mode=0o444,
        )
        service_unit = self._read_regular(
            generation_root / "share/systemd/benchmarkd.service",
            maximum=MAX_SOURCE_FILE_BYTES,
            expected_mode=0o444,
        )
        sysusers = self._read_regular(
            generation_root / "share/sysusers/benchmarkd.conf",
            maximum=MAX_SOURCE_FILE_BYTES,
            expected_mode=0o444,
        )
        for content, description in (
            (socket_unit, "socket unit"),
            (service_unit, "service unit"),
            (sysusers, "sysusers configuration"),
        ):
            if not content.startswith(_MANAGED_MARKER):
                raise _admin_error(
                    f"benchmarkd {description} lacks its ownership marker",
                    code="benchmark_admin_source_invalid",
                )
        return (
            (self.layout.socket_unit, socket_unit, "socket unit"),
            (self.layout.service_unit, service_unit, "service unit"),
            (self.layout.sysusers, sysusers, "sysusers configuration"),
        )

    def _install_stage_paths(self) -> tuple[pathlib.Path, ...]:
        return (
            self.layout.socket_unit_stage,
            self.layout.service_unit_stage,
            self.layout.sysusers_stage,
            self.layout.current_stage,
        )

    def _require_no_install_stages(self) -> None:
        unexpected = tuple(
            path for path in self._install_stage_paths() if os.path.lexists(path)
        )
        if unexpected:
            formatted = ", ".join(os.fspath(path) for path in unexpected)
            raise _admin_error(
                "fixed benchmark install stage exists without its journal: "
                f"{formatted}",
                code="benchmark_admin_install_invalid",
            )

    def _installation_projection(
        self,
        *,
        prior_digest: str | None,
        target_digest: str,
    ) -> InstallationProjection:
        target_root = self.generation_store.verify(target_digest).root
        target_external = {
            destination: content
            for destination, content, _description in self._external_file_payloads(
                target_root
            )
        }
        if prior_digest is None:
            prior_external: dict[pathlib.Path, bytes] = {}
        else:
            prior_root = self.generation_store.verify(prior_digest).root
            prior_external = {
                destination: content
                for destination, content, _description in self._external_file_payloads(
                    prior_root
                )
            }
        items = (
            RegularProjection(
                description="benchmarkd socket unit",
                destination=self.layout.socket_unit,
                stage=self.layout.socket_unit_stage,
                prior=prior_external.get(self.layout.socket_unit),
                target=target_external[self.layout.socket_unit],
            ),
            RegularProjection(
                description="benchmarkd service unit",
                destination=self.layout.service_unit,
                stage=self.layout.service_unit_stage,
                prior=prior_external.get(self.layout.service_unit),
                target=target_external[self.layout.service_unit],
            ),
            RegularProjection(
                description="benchmarkd sysusers configuration",
                destination=self.layout.sysusers,
                stage=self.layout.sysusers_stage,
                prior=prior_external.get(self.layout.sysusers),
                target=target_external[self.layout.sysusers],
            ),
            SymlinkProjection(
                description="benchmarkd current generation",
                destination=self.layout.current,
                stage=self.layout.current_stage,
                prior=(None if prior_digest is None else f"generations/{prior_digest}"),
                target=f"generations/{target_digest}",
            ),
        )
        return InstallationProjection(
            items=items,
            root_uid=self.root_uid,
            root_gid=self.root_gid,
            report=self.report,
        )

    def _projection_for_destinations(
        self,
        projection: InstallationProjection,
        destinations: tuple[pathlib.Path, ...],
    ) -> InstallationProjection:
        indexed = {item.destination: item for item in projection.items}
        if len(indexed) != len(projection.items) or any(
            destination not in indexed for destination in destinations
        ):
            raise AssertionError("install projection phases are incomplete")
        return InstallationProjection(
            items=tuple(indexed[destination] for destination in destinations),
            root_uid=self.root_uid,
            root_gid=self.root_gid,
            report=self.report,
        )

    def _current_selector_digest(self) -> str | None:
        """Read the validated current selector without traversing its generation."""

        if not os.path.lexists(self.layout.current):
            return None
        try:
            metadata = os.lstat(self.layout.current)
        except OSError as error:
            raise _admin_error(
                f"cannot inspect current benchmark generation: {error}",
                code="benchmark_admin_layout_invalid",
            ) from error
        target = (
            os.readlink(self.layout.current) if stat.S_ISLNK(metadata.st_mode) else ""
        )
        expected_prefix = "generations/"
        digest = target.removeprefix(expected_prefix)
        if (
            not stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != self.root_uid
            or metadata.st_gid != self.root_gid
            or not target.startswith(expected_prefix)
            or "/" in digest
            or not _DIGEST_PATTERN.fullmatch(digest)
        ):
            raise _admin_error(
                "current benchmark generation selector is invalid",
                code="benchmark_admin_layout_invalid",
            )
        return digest

    def _current_digest(self) -> str | None:
        digest = self._current_selector_digest()
        if digest is not None:
            self.generation_store.verify(digest)
        return digest

    def _has_live_projection(self) -> bool:
        return any(
            os.path.lexists(path)
            for path in (
                self.layout.socket_unit,
                self.layout.service_unit,
                self.layout.sysusers,
                self.layout.control_socket,
            )
        )

    def _require_managed_file(
        self,
        destination: pathlib.Path,
        source: pathlib.Path,
        *,
        mode: int,
    ) -> None:
        expected = _read_source_file(source)
        observed = self._read_regular(
            destination,
            maximum=max(len(expected), MAX_CONFIG_BYTES),
            expected_mode=mode,
        )
        if observed != expected:
            raise _admin_error(
                f"installed managed file differs from this release: {destination}",
                code="benchmark_admin_upgrade_required",
            )

    def _start_current_service(self) -> None:
        """Enable socket activation and start one freshly loaded broker."""

        self.runner.run(
            (
                "/usr/bin/systemctl",
                "enable",
                "--now",
                "benchmarkd.socket",
            )
        )
        self.runner.run(
            (
                "/usr/bin/systemctl",
                "start",
                "benchmarkd.service",
            )
        )

    def _stop_for_install(self) -> None:
        """Synchronously stop socket activation and the complete broker cgroup."""

        self.runner.run(("/usr/bin/systemctl", "stop", "benchmarkd.socket"))
        self.runner.run(("/usr/bin/systemctl", "stop", "benchmarkd.service"))

    def _resume_prepared_install(
        self,
        *,
        intent: InstallIntent,
        projection: InstallationProjection,
    ) -> InstallIntent:
        """Synchronously stop the prior under the recorded empty-scheduler fence."""

        if intent.phase != "prepared":
            raise AssertionError("prepared install recovery has the wrong phase")
        projection.require_exact_prior()
        if intent.prior_digest is not None:
            self._stop_for_install()
        return self.journal.transition_install(intent, phase="stopped")

    def _complete_install_rollback(
        self,
        *,
        intent: InstallIntent,
        projection: InstallationProjection,
    ) -> None:
        """Resume a durable target rollback to prior or inert first install."""

        if intent.phase != "rollback":
            raise AssertionError("install rollback has the wrong phase")
        if intent.prior_digest is None:
            projection.require_removal_prefix()
            self.runner.run(
                (
                    "/usr/bin/systemctl",
                    "disable",
                    "--now",
                    "benchmarkd.socket",
                )
            )
            self.runner.run(("/usr/bin/systemctl", "stop", "benchmarkd.service"))
            projection.remove_target()
            self.runner.run(("/usr/bin/systemctl", "daemon-reload"))
            self.journal.remove_install(intent)
            return

        projection.require_prior_prefix()
        self._stop_for_install()
        projection.converge_prior()
        self.runner.run(("/usr/bin/systemctl", "daemon-reload"))
        self.runner.run(
            (
                "/usr/bin/systemd-sysusers",
                os.fspath(self.layout.sysusers),
            )
        )
        self.accounts.add_user(intent.user_name)
        self._start_current_service()
        with self.maintenance.hold(installed=True):
            self.maintenance.probe()
            projection.require_exact_prior()
        self.journal.remove_install(intent)

    def _complete_stopped_install(
        self,
        *,
        intent: InstallIntent,
        projection: InstallationProjection,
    ) -> None:
        """Replay a stopped target, durably rolling back bad activation."""

        if intent.phase != "stopped":
            raise AssertionError("stopped install recovery has the wrong phase")
        projection.require_target_prefix()
        unit_projection = self._projection_for_destinations(
            projection,
            (self.layout.socket_unit, self.layout.service_unit),
        )
        stopped_tail_projection = self._projection_for_destinations(
            projection,
            (self.layout.sysusers, self.layout.current),
        )
        unit_projection.converge_target()
        self.runner.run(("/usr/bin/systemctl", "daemon-reload"))
        self._stop_for_install()
        stopped_tail_projection.converge_target()
        projection.require_exact_target()
        self.runner.run(
            (
                "/usr/bin/systemd-sysusers",
                os.fspath(self.layout.sysusers),
            )
        )
        self.accounts.add_user(intent.user_name)

        try:
            self._start_current_service()
            with self.maintenance.hold(installed=True):
                self.maintenance.probe()
                projection.require_exact_target()
        except BaseException as activation_error:
            try:
                rollback = self.journal.transition_install(
                    intent,
                    phase="rollback",
                )
                self._complete_install_rollback(
                    intent=rollback,
                    projection=projection,
                )
            except BaseException as rollback_error:
                raise _admin_error(
                    "benchmarkd target activation failed and its durable "
                    "rollback could not complete: "
                    f"activation={activation_error}; rollback={rollback_error}",
                    code="benchmark_admin_rollback_failed",
                ) from rollback_error
            raise
        self.journal.remove_install(intent)

    def _resume_install_transaction(
        self,
        intent: InstallIntent,
    ) -> str | None:
        """Complete a recorded target, or finish rollback before a new request."""

        self.accounts.validate_user(intent.user_name)
        self._install_configuration(None)
        self._require_epoch_security()
        projection = self._installation_projection(
            prior_digest=intent.prior_digest,
            target_digest=intent.target_digest,
        )
        if intent.phase == "prepared":
            intent = self._resume_prepared_install(
                intent=intent,
                projection=projection,
            )
        if intent.phase == "stopped":
            self._complete_stopped_install(
                intent=intent,
                projection=projection,
            )
            self.report(
                f"recovered and installed benchmarkd generation {intent.target_digest}"
            )
            return intent.target_digest
        if intent.phase == "rollback":
            self._complete_install_rollback(
                intent=intent,
                projection=projection,
            )
            self.report(
                "completed benchmarkd rollback before the requested installation"
            )
            return None
        raise AssertionError("install intent has an unknown recovery phase")

    def install(
        self,
        *,
        gpu_bdfs: tuple[str, ...],
        user_name: str,
    ) -> str:
        """Publish and activate one complete immutable release."""

        self._require_root()
        with self._hold_operation_lock():
            return self._install(
                gpu_bdfs=gpu_bdfs,
                user_name=user_name,
            )

    def _install(
        self,
        *,
        gpu_bdfs: tuple[str, ...],
        user_name: str,
    ) -> str:
        """Run one serialized installation transaction."""

        self._require_no_legacy_state()
        if os.path.lexists(self.layout.install_root):
            self._require_existing_layout()
            self.journal.require_mutually_exclusive()
            if self._has_uninstall_state():
                raise _admin_error(
                    "benchmarkd has a committed uninstall transaction; "
                    "run benchmark-admin uninstall to complete it",
                    code="benchmark_admin_uninstall_pending",
                )
            pending = self.journal.recover_install()
            if pending is not None:
                recovered = self._resume_install_transaction(pending)
                if recovered is not None:
                    return recovered
            self._require_no_install_stages()
        else:
            self._require_no_install_stages()
        self.accounts.validate_user(user_name)
        if not gpu_bdfs and not os.path.lexists(self.layout.config):
            raise _admin_error(
                "first installation requires at least one --gpu BDF",
                code="benchmark_admin_config_required",
            )
        requested_configuration = (
            None if not gpu_bdfs else self._configuration_for_gpus(gpu_bdfs)
        )
        selected_prior_digest = self._current_selector_digest()
        if selected_prior_digest is None and self._has_live_projection():
            raise _admin_error(
                "benchmarkd has a live projection without a current generation; "
                "restore the installed selector before installing",
                code="benchmark_admin_layout_invalid",
            )
        maintenance = (
            contextlib.nullcontext()
            if selected_prior_digest is None
            else self.maintenance.hold(installed=True)
        )
        with maintenance:
            self._prepare_layout()
            self.journal.require_mutually_exclusive()
            if self._has_uninstall_state():
                raise _admin_error(
                    "benchmarkd has a committed uninstall transaction; "
                    "run benchmark-admin uninstall to complete it",
                    code="benchmark_admin_uninstall_pending",
                )
            if self.journal.has_install_state():
                raise _admin_error(
                    "benchmarkd install state appeared while its administrator "
                    "lock was held",
                    code="benchmark_admin_install_invalid",
                )
            self._require_no_install_stages()
            prior_digest = self._current_digest()
            if prior_digest != selected_prior_digest:
                raise _admin_error(
                    "current benchmark generation changed while the "
                    "administrator lock was held",
                    code="benchmark_admin_layout_invalid",
                )
            generation = build_generation(self.source_root)
            self._install_configuration(requested_configuration)
            self._require_epoch_security()
            self.generation_store.publish(generation)
            if prior_digest == generation.digest:
                projection = self._installation_projection(
                    prior_digest=prior_digest,
                    target_digest=generation.digest,
                )
                projection.require_exact_target()
                self.runner.run(
                    (
                        "/usr/bin/systemd-sysusers",
                        os.fspath(self.layout.sysusers),
                    )
                )
                self.accounts.add_user(user_name)
                self.runner.run(("/usr/bin/systemctl", "daemon-reload"))
                self._start_current_service()
                self.maintenance.probe()
                projection.require_exact_target()
                self.report(
                    f"benchmarkd generation {generation.digest} is already current; "
                    f"new group membership for {user_name!r} takes effect at "
                    "next login"
                )
                return generation.digest

            intent = InstallIntent(
                prior_digest=prior_digest,
                target_digest=generation.digest,
                user_name=user_name,
                phase="prepared",
            )
            projection = self._installation_projection(
                prior_digest=prior_digest,
                target_digest=generation.digest,
            )
            projection.require_exact_prior()
            self.journal.publish_install(intent)
            if prior_digest is not None:
                self._stop_for_install()
            intent = self.journal.transition_install(
                intent,
                phase="stopped",
            )
        self._complete_stopped_install(
            intent=intent,
            projection=projection,
        )
        self.report(
            f"installed benchmarkd generation {generation.digest}; "
            f"new group membership for {user_name!r} takes effect at next login"
        )
        return generation.digest

    def _require_runtime_socket(self) -> None:
        try:
            metadata = os.lstat(self.layout.control_socket)
        except OSError as error:
            raise _admin_error(
                f"benchmark control socket is unavailable: {error}",
                code="benchmark_admin_runtime_unavailable",
            ) from error
        if (
            not stat.S_ISSOCK(metadata.st_mode)
            or metadata.st_uid != self.root_uid
            or metadata.st_gid != self.accounts.benchmark_group_id
            or _mode(metadata) != 0o660
        ):
            raise _admin_error(
                "benchmark control socket has the wrong type, owner, group, or mode",
                code="benchmark_admin_runtime_unavailable",
            )

    def doctor(self, *, user_name: str | None) -> str:
        """Audit the installed immutable closure and live socket."""

        self._require_root()
        self._require_directory(self.layout.state, mode=0o700)
        with self._hold_operation_lock(shared=True):
            return self._doctor(user_name=user_name)

    def _doctor(self, *, user_name: str | None) -> str:
        """Audit one installation while holding its shared admin snapshot."""

        self._require_directory(self.layout.install_root, mode=0o755)
        self._require_directory(self.layout.generations, mode=0o755)
        self._require_directory(self.layout.config.parent, mode=0o700)
        self._require_directory(self.layout.state, mode=0o700)
        self._require_directory(
            self.layout.map(pathlib.Path("/usr/local/lib/systemd")),
            mode=0o755,
        )
        self._require_directory(
            self.layout.map(pathlib.Path("/usr/local/lib/systemd/system")),
            mode=0o755,
        )
        self._require_directory(
            self.layout.map(pathlib.Path("/usr/local/lib/sysusers.d")),
            mode=0o755,
        )
        if self._has_uninstall_state():
            raise _admin_error(
                "benchmarkd has a committed uninstall transaction",
                code="benchmark_admin_uninstall_pending",
            )
        if self.journal.has_install_state():
            raise _admin_error(
                "benchmarkd has a committed install transaction",
                code="benchmark_admin_install_pending",
            )
        self._require_no_install_stages()
        self.generation_store.require_quiescent()
        digest = self._current_digest()
        if digest is None:
            raise _admin_error(
                "benchmarkd has no current generation",
                code="benchmark_admin_not_installed",
            )
        self._require_managed_file(
            self.layout.socket_unit,
            self.layout.generations / digest / "share/systemd/benchmarkd.socket",
            mode=0o644,
        )
        self._require_managed_file(
            self.layout.service_unit,
            self.layout.generations / digest / "share/systemd/benchmarkd.service",
            mode=0o644,
        )
        self._require_managed_file(
            self.layout.sysusers,
            self.layout.generations / digest / "share/sysusers/benchmarkd.conf",
            mode=0o644,
        )
        config_payload = self._read_regular(
            self.layout.config,
            maximum=MAX_CONFIG_BYTES,
            expected_mode=0o600,
        )
        config = parse_policy_configuration(config_payload)
        if config_payload != canonical_policy_configuration(config):
            raise _admin_error(
                "installed benchmarkd policy configuration is not canonical",
                code="benchmark_admin_layout_invalid",
            )
        self._require_epoch_security()
        self.accounts.require_user(user_name)
        if self.check_runtime:
            self.runner.run(
                (
                    "/usr/bin/systemctl",
                    "is-enabled",
                    "--quiet",
                    "benchmarkd.socket",
                )
            )
            self.runner.run(
                (
                    "/usr/bin/systemctl",
                    "is-active",
                    "--quiet",
                    "benchmarkd.socket",
                )
            )
            self._require_runtime_socket()
            self.maintenance.probe()
        self.report(f"benchmarkd generation {digest} is installed and healthy")
        return digest

    def _remove_external_regular(
        self,
        path: pathlib.Path,
        *,
        expected: bytes,
    ) -> None:
        if not os.path.lexists(path):
            return
        self._require_removable_external_regular(path, expected=expected)
        os.unlink(path)
        self._fsync_directory(path.parent)

    def _require_removable_external_regular(
        self,
        path: pathlib.Path,
        *,
        expected: bytes,
    ) -> None:
        content = self._read_regular(
            path,
            maximum=MAX_SOURCE_FILE_BYTES,
            expected_mode=0o644,
        )
        if not content.startswith(_MANAGED_MARKER):
            raise _admin_error(
                f"refusing to remove an unmanaged file: {path}",
                code="benchmark_admin_layout_invalid",
            )
        if content != expected:
            raise _admin_error(
                f"refusing to remove a managed file modified after installation: {path}",
                code="benchmark_admin_layout_invalid",
            )

    def uninstall(self) -> None:
        """Remove executable software while retaining policy and epoch state."""

        self._require_root()
        with self._hold_operation_lock():
            self._uninstall()

    def _stop_for_uninstall(self) -> None:
        """Disable activation and synchronously stop the complete broker cgroup."""

        self.runner.run(
            (
                "/usr/bin/systemctl",
                "disable",
                "--now",
                "benchmarkd.socket",
            )
        )
        self.runner.run(("/usr/bin/systemctl", "stop", "benchmarkd.service"))

    def _require_uninstall_source(
        self,
        intent: UninstallIntent,
    ) -> None:
        """Require the complete pre-stop closure recorded by a prepared intent."""

        current_digest = self._current_digest()
        if current_digest != intent.current_digest:
            raise _admin_error(
                "prepared benchmark uninstall intent no longer matches current",
                code="benchmark_admin_uninstall_invalid",
            )
        generations = self.generation_store.require_quiescent()
        observed_digests = tuple(generation.digest for generation in generations)
        if observed_digests != intent.generation_digests:
            raise _admin_error(
                "prepared benchmark uninstall generation inventory changed",
                code="benchmark_admin_uninstall_invalid",
            )
        if current_digest is None:
            if self._has_live_projection():
                raise _admin_error(
                    "prepared benchmark uninstall has no generation for its "
                    "live projection",
                    code="benchmark_admin_uninstall_invalid",
                )
            return
        generation_root = self.layout.generations / current_digest
        for destination, content, _description in self._external_file_payloads(
            generation_root
        ):
            self._require_removable_external_regular(
                destination,
                expected=content,
            )

    def _complete_uninstall(self, intent: UninstallIntent) -> None:
        """Resume the filesystem half of a durably stopped uninstall."""

        if intent.phase != "stopped":
            raise _admin_error(
                "benchmark uninstall cannot remove files before broker stop",
                code="benchmark_admin_uninstall_invalid",
            )
        current_digest = self._current_digest()
        if current_digest is not None and current_digest != intent.current_digest:
            raise _admin_error(
                "benchmark uninstall current generation changed after stop",
                code="benchmark_admin_uninstall_invalid",
            )
        if current_digest is None:
            observed_digests = self.generation_store.inventory_digests()
            inventory_matches = set(observed_digests).issubset(
                intent.generation_digests
            )
        else:
            generations = self.generation_store.require_quiescent()
            observed_digests = tuple(generation.digest for generation in generations)
            inventory_matches = observed_digests == intent.generation_digests
        if not inventory_matches:
            raise _admin_error(
                "benchmark uninstall generation inventory changed after stop",
                code="benchmark_admin_uninstall_invalid",
            )

        if current_digest is not None:
            generation_root = self.layout.generations / current_digest
            for destination, content, _description in self._external_file_payloads(
                generation_root
            ):
                self._remove_external_regular(destination, expected=content)
            self.runner.run(("/usr/bin/systemctl", "daemon-reload"))
            self._current_digest()
            os.unlink(self.layout.current)
            self._fsync_directory(self.layout.current.parent)
        elif self._has_live_projection():
            raise _admin_error(
                "benchmark uninstall lost its generation before removing the "
                "live projection",
                code="benchmark_admin_uninstall_invalid",
            )

        self.generation_store.recover_removals()
        for digest in intent.generation_digests:
            self.generation_store.remove(digest)
        if self.generation_store.inventory_digests():
            raise AssertionError(
                "benchmark generation store is not empty after removal"
            )
        os.unlink(self.layout.uninstall_intent)
        self._fsync_directory(self.layout.install_root)
        os.rmdir(self.layout.generations)
        os.rmdir(self.layout.install_root)
        self._fsync_directory(self.layout.install_root.parent)

    def _uninstall(self) -> None:
        """Run one serialized conservative removal transaction."""

        self._require_existing_layout()
        self.journal.require_mutually_exclusive()
        if self.journal.has_install_state():
            raise _admin_error(
                "benchmarkd has a committed install transaction; "
                "run benchmark-admin install to complete it",
                code="benchmark_admin_install_pending",
            )
        self._require_no_install_stages()
        intent = self.journal.recover_uninstall()
        if intent is not None:
            if intent.phase == "prepared":
                self._require_uninstall_source(intent)
                if intent.current_digest is not None:
                    self._stop_for_uninstall()
                intent = self.journal.transition_uninstall(
                    intent,
                    phase="stopped",
                )
            self._complete_uninstall(intent)
            self.report(
                "removed benchmarkd software; retained "
                f"{CONFIG_PATH}, {STATE_DIRECTORY}, and the benchmark system group"
            )
            return

        selected_current_digest = self._current_selector_digest()
        if selected_current_digest is None and self._has_live_projection():
            raise _admin_error(
                "benchmarkd has a live projection without a current generation; "
                "restore the installed selector before uninstalling",
                code="benchmark_admin_layout_invalid",
            )
        if selected_current_digest is None and not os.path.lexists(
            self.layout.install_root
        ):
            self.report(
                "benchmarkd software is already absent; retained "
                f"{CONFIG_PATH}, {STATE_DIRECTORY}, and the benchmark system group"
            )
            return
        if selected_current_digest is None and not os.path.lexists(
            self.layout.generations
        ):
            try:
                with os.scandir(self.layout.install_root) as iterator:
                    remaining = tuple(entry.name for entry in iterator)
            except OSError as error:
                raise _admin_error(
                    f"cannot inspect final benchmark install root: {error}",
                    code="benchmark_admin_layout_invalid",
                ) from error
            if remaining:
                raise _admin_error(
                    "benchmark install root contains unknown state without its "
                    "generation store",
                    code="benchmark_admin_layout_invalid",
                )
            os.rmdir(self.layout.install_root)
            self._fsync_directory(self.layout.install_root.parent)
            self.report(
                "completed benchmarkd software removal; retained "
                f"{CONFIG_PATH}, {STATE_DIRECTORY}, and the benchmark system group"
            )
            return
        installed = selected_current_digest is not None
        with self.maintenance.hold(installed=installed):
            current_digest = self._current_digest()
            if current_digest != selected_current_digest:
                raise _admin_error(
                    "current benchmark generation changed while the "
                    "administrator lock was held",
                    code="benchmark_admin_layout_invalid",
                )
            generations = self.generation_store.require_quiescent()
            expected_external = (
                ()
                if current_digest is None
                else self._external_file_payloads(
                    self.layout.generations / current_digest
                )
            )
            for destination, content, _description in expected_external:
                self._require_removable_external_regular(
                    destination,
                    expected=content,
                )
            intent = UninstallIntent(
                current_digest=current_digest,
                generation_digests=tuple(
                    generation.digest for generation in generations
                ),
                phase="prepared",
            )
            self.journal.publish_uninstall(intent)
            if installed:
                self._stop_for_uninstall()
            intent = self.journal.transition_uninstall(
                intent,
                phase="stopped",
            )
            self._complete_uninstall(intent)
        self.report(
            "removed benchmarkd software; retained "
            f"{CONFIG_PATH}, {STATE_DIRECTORY}, and the benchmark system group"
        )
