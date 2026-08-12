"""Root-owned, crash-recoverable host policy for benchmark leases.

The policy deliberately accepts hardware identities, not paths or requested
settings. Its GPU mutation is a class-specific, compiled-in AMD sysfs value:
``high`` for display GPUs and ``perf_determinism`` for processing
accelerators. All path selection belongs to the privileged process that
constructs the filesystem and journal backends.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import stat
import tempfile
from collections.abc import Mapping, Sequence
from typing import Protocol

from .errors import BenchmarkLockError


_PCI_BDF_PATTERN = re.compile(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]")
_HEX_ID_PATTERN = re.compile(r"0x[0-9a-f]{4}")
_HEX_REVISION_PATTERN = re.compile(r"0x[0-9a-f]{2}")
_PCI_CLASS_PATTERN = re.compile(r"0x[0-9a-f]{6}")
_AMD_GPU_CLASS_PATTERN = re.compile(r"0x(?:03|12)[0-9a-f]{4}")
_UNIQUE_ID_PATTERN = re.compile(r"[0-9a-f]{1,64}")
_KFD_NODE_PATTERN = re.compile(r"(?:0|[1-9][0-9]{0,9})")
_KFD_PROCESS_PATTERN = re.compile(r"[1-9][0-9]{0,9}")
_KFD_QUEUE_PATTERN = re.compile(r"(?:0|[1-9][0-9]{0,9})")
_KFD_DECIMAL_PATTERN = re.compile(r"(?:0|[1-9][0-9]{0,19})")
_CPU_FREQUENCY_POLICY_PATTERN = re.compile(r"policy(?:0|[1-9][0-9]{0,9})")
_CPU_CONTROL_TOKEN_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_BOOT_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}"
)
_GPU_LEVELS = frozenset(
    {
        "auto",
        "low",
        "high",
        "manual",
        "profile_standard",
        "profile_min_sclk",
        "profile_min_mclk",
        "profile_peak",
        "perf_determinism",
    }
)
_AMD_VENDOR_ID = "0x1002"
_DEFAULT_GPU_CLASS = "0x030000"
_HELD_DISPLAY_GPU_LEVEL = "high"
_HELD_PROCESSING_ACCELERATOR_LEVEL = "perf_determinism"
_POWER_PROFILE = "performance"
_POWER_PROFILE_APPLICATION_ID = "com.benchmark-lock.host-policy"
_POWER_PROFILE_REASON = "exclusive benchmark lease"
_CPU_AUTHORITY_POWER_PROFILES_DAEMON = "power-profiles-daemon"
_CPU_AUTHORITY_FIXED_CPU_FREQUENCY = "fixed-cpu-frequency"
_JOURNAL_SCHEMA = 1
_MAX_JOURNAL_BYTES = 1024 * 1024
_MAX_KFD_PROPERTIES_BYTES = 4096
_MAX_KFD_DECIMAL_BYTES = 21
_MAX_UINT32 = (1 << 32) - 1
_MAX_UINT64 = (1 << 64) - 1


def _policy_error(message: str, *, code: str) -> BenchmarkLockError:
    return BenchmarkLockError(message, code=code)


def _matches(pattern: re.Pattern[str], value: str) -> bool:
    return pattern.fullmatch(value) is not None


@dataclasses.dataclass(frozen=True)
class AmdGpuIdentity:
    """One exact AMD GPU identity selected by the administrator."""

    bdf: str
    vendor: str
    device: str
    subsystem_vendor: str
    subsystem_device: str
    revision: str
    unique_id: str | None
    device_class: str = _DEFAULT_GPU_CLASS

    def __post_init__(self) -> None:
        fields_and_patterns = (
            ("bdf", self.bdf, _PCI_BDF_PATTERN),
            ("vendor", self.vendor, _HEX_ID_PATTERN),
            ("device", self.device, _HEX_ID_PATTERN),
            ("subsystem_vendor", self.subsystem_vendor, _HEX_ID_PATTERN),
            ("subsystem_device", self.subsystem_device, _HEX_ID_PATTERN),
            ("revision", self.revision, _HEX_REVISION_PATTERN),
            ("device_class", self.device_class, _PCI_CLASS_PATTERN),
        )
        for name, value, pattern in fields_and_patterns:
            if not isinstance(value, str) or not _matches(pattern, value):
                raise ValueError(f"{name} is not a canonical hardware identity")
        if self.unique_id is not None and (
            not isinstance(self.unique_id, str)
            or not _matches(_UNIQUE_ID_PATTERN, self.unique_id)
        ):
            raise ValueError("unique_id is not a canonical hardware identity")
        if self.vendor != _AMD_VENDOR_ID:
            raise ValueError("benchmark GPUs must have AMD vendor ID 0x1002")
        if not _matches(_AMD_GPU_CLASS_PATTERN, self.device_class):
            raise ValueError(
                "benchmark GPUs must be PCI display controllers or "
                "processing accelerators"
            )


@dataclasses.dataclass(frozen=True)
class FixedHostPolicyConfig:
    """Administrator-selected identities for one immutable policy."""

    gpus: tuple[AmdGpuIdentity, ...]
    policy_identity: str = "amd-performance-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.gpus, tuple):
            raise ValueError("gpus must be an immutable tuple")
        if not self.gpus or len(self.gpus) > 64:
            raise ValueError("fixed host policy requires between one and 64 GPUs")
        bdfs = tuple(gpu.bdf for gpu in self.gpus)
        if len(set(bdfs)) != len(bdfs):
            raise ValueError("fixed host policy contains a duplicate PCI BDF")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", self.policy_identity):
            raise ValueError("policy_identity is not canonical")


@dataclasses.dataclass(frozen=True)
class PowerProfileHold:
    """One power-profiles-daemon hold visible through ActiveProfileHolds."""

    profile: str
    application_id: str
    reason: str


@dataclasses.dataclass(frozen=True)
class PowerProfileStatus:
    """The PPD state needed to establish and audit the benchmark policy."""

    active_profile: str
    performance_degraded: str
    profiles: tuple[str, ...]
    holds: tuple[PowerProfileHold, ...]


@dataclasses.dataclass(frozen=True)
class CpuFrequencyPolicyStatus:
    """Performance-relevant state of one Linux cpufreq policy."""

    # Canonical sysfs policy directory name.
    name: str
    # Kernel frequency-scaling driver bound to the policy.
    driver: str
    # Kernel frequency-scaling governor selected for the policy.
    governor: str
    # Configured lower frequency bound, in kHz.
    minimum_frequency_khz: int
    # Configured upper frequency bound, in kHz.
    maximum_frequency_khz: int
    # Optional driver-specific energy/performance preference.
    energy_performance_preference: str | None


@dataclasses.dataclass(frozen=True)
class CpuPerformanceStatus:
    """Exact kernel CPU performance state used as a fixed authority."""

    # CPU frequency policies ordered by their numeric kernel index.
    policies: tuple[CpuFrequencyPolicyStatus, ...]
    # Optional global CPU boost control.
    boost_enabled: bool | None


class PowerProfilesBackend(Protocol):
    """Privileged power-profiles-daemon operation boundary."""

    def status(self) -> PowerProfileStatus:
        """Read a coherent-enough PPD status snapshot."""

    def hold_performance(self, *, reason: str, application_id: str) -> object:
        """Acquire a process-lifetime performance-profile hold."""

    def release(self, cookie: object) -> None:
        """Release a previously acquired hold."""


class GioPowerProfilesBackend:
    """Production PPD backend using its canonical system-bus API."""

    _BUS_NAME = "org.freedesktop.UPower.PowerProfiles"
    _OBJECT_PATH = "/org/freedesktop/UPower/PowerProfiles"
    _INTERFACE = "org.freedesktop.UPower.PowerProfiles"
    _PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"

    def __init__(self) -> None:
        # Gio is intentionally imported only in the production backend.  Tests
        # and root policy parsing never acquire a dependency on a user Python
        # environment.
        try:
            import gi

            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib
        except (ImportError, ValueError) as error:
            raise _policy_error(
                f"Gio is unavailable for power profile control: {error}",
                code="benchmark_policy_unavailable",
            ) from error

        self._gio = Gio
        self._glib = GLib
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except Exception as error:
            raise _policy_error(
                f"cannot connect to the system bus: {error}",
                code="benchmark_policy_unavailable",
            ) from error

    def _call(
        self,
        interface: str,
        method: str,
        signature: str,
        values: tuple[object, ...],
    ) -> object:
        try:
            result = self._bus.call_sync(
                self._BUS_NAME,
                self._OBJECT_PATH,
                interface,
                method,
                self._glib.Variant(signature, values),
                None,
                self._gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            return result.unpack()
        except Exception as error:
            raise _policy_error(
                f"power-profiles-daemon {method} failed: {error}",
                code="benchmark_policy_unavailable",
            ) from error

    @staticmethod
    def _unwrap(value: object) -> object:
        while hasattr(value, "unpack"):
            value = value.unpack()
        return value

    def _properties(self) -> Mapping[str, object]:
        unpacked = self._call(
            self._PROPERTIES_INTERFACE,
            "GetAll",
            "(s)",
            (self._INTERFACE,),
        )
        if not isinstance(unpacked, tuple) or len(unpacked) != 1:
            raise _policy_error(
                "power-profiles-daemon returned malformed properties",
                code="benchmark_policy_unavailable",
            )
        properties = self._unwrap(unpacked[0])
        if not isinstance(properties, Mapping):
            raise _policy_error(
                "power-profiles-daemon returned malformed properties",
                code="benchmark_policy_unavailable",
            )
        return properties

    def status(self) -> PowerProfileStatus:
        properties = self._properties()
        try:
            active_profile = self._unwrap(properties["ActiveProfile"])
            degraded = self._unwrap(properties["PerformanceDegraded"])
            raw_profiles = self._unwrap(properties["Profiles"])
            raw_holds = self._unwrap(properties["ActiveProfileHolds"])
        except KeyError as error:
            raise _policy_error(
                f"power-profiles-daemon omitted {error.args[0]}",
                code="benchmark_policy_unavailable",
            ) from error
        if not isinstance(active_profile, str) or not isinstance(degraded, str):
            raise _policy_error(
                "power-profiles-daemon returned malformed scalar properties",
                code="benchmark_policy_unavailable",
            )
        if not isinstance(raw_profiles, Sequence) or isinstance(
            raw_profiles, (str, bytes)
        ):
            raise _policy_error(
                "power-profiles-daemon returned malformed Profiles",
                code="benchmark_policy_unavailable",
            )
        if not isinstance(raw_holds, Sequence) or isinstance(raw_holds, (str, bytes)):
            raise _policy_error(
                "power-profiles-daemon returned malformed ActiveProfileHolds",
                code="benchmark_policy_unavailable",
            )

        profiles: list[str] = []
        for raw_profile in raw_profiles:
            profile = self._unwrap(raw_profile)
            if not isinstance(profile, Mapping):
                raise _policy_error(
                    "power-profiles-daemon returned a malformed profile",
                    code="benchmark_policy_unavailable",
                )
            name = self._unwrap(profile.get("Profile"))
            if not isinstance(name, str):
                raise _policy_error(
                    "power-profiles-daemon returned a profile without a name",
                    code="benchmark_policy_unavailable",
                )
            profiles.append(name)

        holds: list[PowerProfileHold] = []
        for raw_hold in raw_holds:
            hold = self._unwrap(raw_hold)
            if not isinstance(hold, Mapping):
                raise _policy_error(
                    "power-profiles-daemon returned a malformed hold",
                    code="benchmark_policy_unavailable",
                )
            profile = self._unwrap(hold.get("Profile"))
            application_id = self._unwrap(hold.get("ApplicationId"))
            reason = self._unwrap(hold.get("Reason"))
            if not all(
                isinstance(value, str) for value in (profile, application_id, reason)
            ):
                raise _policy_error(
                    "power-profiles-daemon returned an incomplete hold",
                    code="benchmark_policy_unavailable",
                )
            holds.append(
                PowerProfileHold(
                    profile=profile,
                    application_id=application_id,
                    reason=reason,
                )
            )
        return PowerProfileStatus(
            active_profile=active_profile,
            performance_degraded=degraded,
            profiles=tuple(profiles),
            holds=tuple(holds),
        )

    def hold_performance(self, *, reason: str, application_id: str) -> object:
        unpacked = self._call(
            self._INTERFACE,
            "HoldProfile",
            "(sss)",
            (_POWER_PROFILE, reason, application_id),
        )
        if (
            not isinstance(unpacked, tuple)
            or len(unpacked) != 1
            or not isinstance(unpacked[0], int)
        ):
            raise _policy_error(
                "power-profiles-daemon returned a malformed hold cookie",
                code="benchmark_policy_unavailable",
            )
        return unpacked[0]

    def release(self, cookie: object) -> None:
        if not isinstance(cookie, int) or not 0 <= cookie <= 0xFFFFFFFF:
            raise _policy_error(
                "power profile hold cookie is invalid",
                code="benchmark_policy_restore_failed",
            )
        self._call(
            self._INTERFACE,
            "ReleaseProfile",
            "(u)",
            (cookie,),
        )


class HostFilesystem(Protocol):
    """Fixed-path host observation and AMD sysfs mutation boundary."""

    def boot_id(self) -> str:
        """Read the current kernel boot identity."""

    def cpu_performance_status(self) -> CpuPerformanceStatus:
        """Read the exact Linux cpufreq controls relevant to performance."""

    def assert_kfd_gpus_unowned(
        self,
        expected: Sequence[AmdGpuIdentity],
    ) -> None:
        """Fail when a KFD process owns a configured GPU."""

    def assert_gpu_identity(self, expected: AmdGpuIdentity) -> None:
        """Fail unless the selected BDF still names the exact GPU."""

    def read_gpu_level(self, expected: AmdGpuIdentity) -> str:
        """Read the selected GPU's current forced performance level."""

    def write_gpu_level(self, expected: AmdGpuIdentity, value: str) -> None:
        """Write and read back one fixed GPU performance level."""


class LinuxHostFilesystem:
    """Linux implementation rooted only at administrator-selected system paths."""

    def __init__(
        self,
        *,
        sysfs_root: pathlib.Path = pathlib.Path("/sys"),
        boot_id_path: pathlib.Path = pathlib.Path("/proc/sys/kernel/random/boot_id"),
    ) -> None:
        self._sysfs_root = pathlib.Path(sysfs_root)
        self._boot_id_path = pathlib.Path(boot_id_path)

    @staticmethod
    def _read_bounded_file(
        path: pathlib.Path,
        *,
        maximum_bytes: int,
        description: str,
    ) -> bytes:
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise _policy_error(
                f"cannot read {description}: {error}",
                code="benchmark_policy_unavailable",
            ) from error
        try:
            try:
                payload = os.read(descriptor, maximum_bytes + 1)
            except OSError as error:
                raise _policy_error(
                    f"cannot read {description}: {error}",
                    code="benchmark_policy_unavailable",
                ) from error
        finally:
            os.close(descriptor)
        if len(payload) > maximum_bytes:
            raise _policy_error(
                f"{description} exceeds its fixed representation",
                code="benchmark_policy_unavailable",
            )
        return payload

    @classmethod
    def _read_single_line(cls, path: pathlib.Path, *, description: str) -> str:
        data = cls._read_bounded_file(
            path,
            maximum_bytes=256,
            description=description,
        )
        try:
            text = data.decode("ascii")
        except UnicodeDecodeError as error:
            raise _policy_error(
                f"{description} is not ASCII",
                code="benchmark_policy_unavailable",
            ) from error
        if not text.endswith("\n") or "\n" in text[:-1] or "\r" in text:
            raise _policy_error(
                f"{description} is not one canonical line",
                code="benchmark_policy_unavailable",
            )
        value = text[:-1]
        if not value or value.strip() != value:
            raise _policy_error(
                f"{description} is not one canonical token",
                code="benchmark_policy_unavailable",
            )
        return value

    @classmethod
    def _read_optional_single_line(
        cls,
        path: pathlib.Path,
        *,
        description: str,
    ) -> str | None:
        try:
            return cls._read_single_line(path, description=description)
        except BenchmarkLockError as error:
            if isinstance(error.__cause__, FileNotFoundError):
                return None
            raise

    def boot_id(self) -> str:
        value = self._read_single_line(
            self._boot_id_path,
            description="kernel boot ID",
        )
        if not _matches(_BOOT_ID_PATTERN, value):
            raise _policy_error(
                "kernel boot ID is not canonical",
                code="benchmark_policy_unavailable",
            )
        return value

    @classmethod
    def _read_cpu_control_token(
        cls,
        path: pathlib.Path,
        *,
        description: str,
    ) -> str:
        value = cls._read_single_line(path, description=description)
        if not _matches(_CPU_CONTROL_TOKEN_PATTERN, value):
            raise _policy_error(
                f"{description} is not a canonical control token",
                code="benchmark_policy_unavailable",
            )
        return value

    @classmethod
    def _read_optional_cpu_control_token(
        cls,
        path: pathlib.Path,
        *,
        description: str,
    ) -> str | None:
        try:
            return cls._read_cpu_control_token(path, description=description)
        except BenchmarkLockError as error:
            if isinstance(error.__cause__, FileNotFoundError):
                return None
            raise

    @classmethod
    def _read_cpu_frequency(
        cls,
        path: pathlib.Path,
        *,
        description: str,
    ) -> int:
        value = cls._read_single_line(path, description=description)
        if not _matches(_KFD_DECIMAL_PATTERN, value):
            raise _policy_error(
                f"{description} is not a canonical decimal integer",
                code="benchmark_policy_unavailable",
            )
        frequency = int(value)
        if frequency > _MAX_UINT64:
            raise _policy_error(
                f"{description} exceeds its kernel representation",
                code="benchmark_policy_unavailable",
            )
        return frequency

    def cpu_performance_status(self) -> CpuPerformanceStatus:
        policy_root = self._sysfs_root / "devices/system/cpu/cpufreq"
        try:
            with os.scandir(policy_root) as iterator:
                entries = tuple(
                    entry
                    for entry in iterator
                    if _matches(_CPU_FREQUENCY_POLICY_PATTERN, entry.name)
                )
        except OSError as error:
            raise _policy_error(
                f"cannot inspect CPU frequency policies: {error}",
                code="benchmark_policy_unavailable",
            ) from error
        if not entries:
            raise _policy_error(
                "the host exposes no CPU frequency policies",
                code="benchmark_policy_unavailable",
            )

        policies: list[CpuFrequencyPolicyStatus] = []
        for entry in sorted(entries, key=lambda item: int(item.name[6:])):
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError as error:
                raise _policy_error(
                    f"cannot inspect CPU frequency policy {entry.name}: {error}",
                    code="benchmark_policy_unavailable",
                ) from error
            if not is_directory:
                raise _policy_error(
                    f"CPU frequency policy {entry.name} is not a directory",
                    code="benchmark_policy_unavailable",
                )
            path = policy_root / entry.name
            description = f"CPU frequency policy {entry.name}"
            minimum_frequency_khz = self._read_cpu_frequency(
                path / "scaling_min_freq",
                description=f"{description} minimum frequency",
            )
            maximum_frequency_khz = self._read_cpu_frequency(
                path / "scaling_max_freq",
                description=f"{description} maximum frequency",
            )
            if minimum_frequency_khz > maximum_frequency_khz:
                raise _policy_error(
                    f"{description} minimum frequency exceeds its maximum",
                    code="benchmark_policy_unavailable",
                )
            policies.append(
                CpuFrequencyPolicyStatus(
                    name=entry.name,
                    driver=self._read_cpu_control_token(
                        path / "scaling_driver",
                        description=f"{description} driver",
                    ),
                    governor=self._read_cpu_control_token(
                        path / "scaling_governor",
                        description=f"{description} governor",
                    ),
                    minimum_frequency_khz=minimum_frequency_khz,
                    maximum_frequency_khz=maximum_frequency_khz,
                    energy_performance_preference=(
                        self._read_optional_cpu_control_token(
                            path / "energy_performance_preference",
                            description=(
                                f"{description} energy performance preference"
                            ),
                        )
                    ),
                )
            )

        boost = self._read_optional_single_line(
            policy_root / "boost",
            description="CPU frequency boost control",
        )
        if boost not in {None, "0", "1"}:
            raise _policy_error(
                "CPU frequency boost control is not zero or one",
                code="benchmark_policy_unavailable",
            )
        return CpuPerformanceStatus(
            policies=tuple(policies),
            boost_enabled=None if boost is None else boost == "1",
        )

    @classmethod
    def _read_kfd_decimal(
        cls,
        path: pathlib.Path,
        *,
        maximum: int,
        description: str,
    ) -> int:
        payload = cls._read_bounded_file(
            path,
            maximum_bytes=_MAX_KFD_DECIMAL_BYTES,
            description=description,
        )
        try:
            text = payload.decode("ascii")
        except UnicodeDecodeError as error:
            raise _policy_error(
                f"{description} is not ASCII",
                code="benchmark_policy_unavailable",
            ) from error
        if text.endswith("\n"):
            text = text[:-1]
        if not _matches(_KFD_DECIMAL_PATTERN, text):
            raise _policy_error(
                f"{description} is not a canonical decimal integer",
                code="benchmark_policy_unavailable",
            )
        value = int(text)
        if value > maximum:
            raise _policy_error(
                f"{description} exceeds its kernel representation",
                code="benchmark_policy_unavailable",
            )
        return value

    @classmethod
    def _read_optional_kfd_decimal(
        cls,
        path: pathlib.Path,
        *,
        maximum: int,
        description: str,
    ) -> int | None:
        try:
            return cls._read_kfd_decimal(
                path,
                maximum=maximum,
                description=description,
            )
        except BenchmarkLockError as error:
            if isinstance(error.__cause__, FileNotFoundError):
                return None
            raise

    @classmethod
    def _read_kfd_node_location(
        cls,
        path: pathlib.Path,
        *,
        node_name: str,
    ) -> tuple[int, int]:
        description = f"KFD topology node {node_name} properties"
        payload = cls._read_bounded_file(
            path,
            maximum_bytes=_MAX_KFD_PROPERTIES_BYTES,
            description=description,
        )
        try:
            text = payload.decode("ascii")
        except UnicodeDecodeError as error:
            raise _policy_error(
                f"{description} is not ASCII",
                code="benchmark_policy_unavailable",
            ) from error
        if not text.endswith("\n") or "\r" in text:
            raise _policy_error(
                f"{description} is not canonical",
                code="benchmark_policy_unavailable",
            )
        values: dict[str, int] = {}
        for line in text[:-1].split("\n"):
            key, separator, value_text = line.partition(" ")
            if key not in {"domain", "location_id"}:
                continue
            if (
                not separator
                or key in values
                or not _matches(_KFD_DECIMAL_PATTERN, value_text)
            ):
                raise _policy_error(
                    f"{description} has a malformed {key}",
                    code="benchmark_policy_unavailable",
                )
            value = int(value_text)
            if value > _MAX_UINT32:
                raise _policy_error(
                    f"{description} has an out-of-range {key}",
                    code="benchmark_policy_unavailable",
                )
            values[key] = value
        if values.keys() != {"domain", "location_id"}:
            raise _policy_error(
                f"{description} does not identify a PCI location",
                code="benchmark_policy_unavailable",
            )
        return values["domain"], values["location_id"]

    @staticmethod
    def _kfd_location(expected: AmdGpuIdentity) -> tuple[int, int]:
        domain_text, bus_text, device_function = expected.bdf.split(":")
        device_text, function_text = device_function.split(".")
        domain = int(domain_text, 16)
        location = (
            (int(bus_text, 16) << 8)
            | (int(device_text, 16) << 3)
            | int(function_text, 16)
        )
        return domain, location

    def _kfd_gpu_ids(
        self,
        expected: Sequence[AmdGpuIdentity],
    ) -> frozenset[int]:
        expected_locations = {
            self._kfd_location(identity): identity.bdf for identity in expected
        }
        matched_bdfs: set[str] = set()
        gpu_ids: set[int] = set()
        topology_root = self._sysfs_root / "class/kfd/kfd/topology/nodes"
        try:
            with os.scandir(topology_root) as iterator:
                entries = tuple(iterator)
        except OSError as error:
            raise _policy_error(
                f"cannot inspect KFD topology: {error}",
                code="benchmark_policy_unavailable",
            ) from error
        for entry in entries:
            if not _matches(_KFD_NODE_PATTERN, entry.name):
                raise _policy_error(
                    "KFD topology contains an unknown node",
                    code="benchmark_policy_unavailable",
                )
            node_path = topology_root / entry.name
            gpu_id = self._read_kfd_decimal(
                node_path / "gpu_id",
                maximum=_MAX_UINT32,
                description=f"KFD topology node {entry.name} GPU ID",
            )
            if gpu_id == 0:
                continue
            location = self._read_kfd_node_location(
                node_path / "properties",
                node_name=entry.name,
            )
            bdf = expected_locations.get(location)
            if bdf is None:
                continue
            matched_bdfs.add(bdf)
            gpu_ids.add(gpu_id)
        missing_bdfs = sorted(set(expected_locations.values()) - matched_bdfs)
        if missing_bdfs:
            raise _policy_error(
                "configured PCI GPUs are absent from KFD topology: "
                + ", ".join(missing_bdfs),
                code="benchmark_policy_unavailable",
            )
        return frozenset(gpu_ids)

    def _kfd_process_uses_gpu(
        self,
        process_path: pathlib.Path,
        *,
        process_id: str,
        gpu_ids: frozenset[int],
    ) -> bool:
        queues_path = process_path / "queues"
        try:
            with os.scandir(queues_path) as iterator:
                queue_entries = tuple(iterator)
        except FileNotFoundError:
            if process_path.exists():
                raise _policy_error(
                    f"KFD process {process_id} has no queue ledger",
                    code="benchmark_policy_unavailable",
                )
            return False
        except OSError as error:
            raise _policy_error(
                f"cannot inspect KFD process {process_id} queues: {error}",
                code="benchmark_policy_unavailable",
            ) from error
        for entry in queue_entries:
            if not _matches(_KFD_QUEUE_PATTERN, entry.name):
                raise _policy_error(
                    f"KFD process {process_id} has an unknown queue",
                    code="benchmark_policy_unavailable",
                )
            gpu_id = self._read_optional_kfd_decimal(
                queues_path / entry.name / "gpuid",
                maximum=_MAX_UINT32,
                description=(
                    f"KFD process {process_id} queue {entry.name} GPU ID"
                ),
            )
            if gpu_id in gpu_ids:
                return True
        for gpu_id in gpu_ids:
            vram_bytes = self._read_optional_kfd_decimal(
                process_path / f"vram_{gpu_id}",
                maximum=_MAX_UINT64,
                description=(
                    f"KFD process {process_id} GPU {gpu_id} VRAM usage"
                ),
            )
            if vram_bytes:
                return True
        return False

    def assert_kfd_gpus_unowned(
        self,
        expected: Sequence[AmdGpuIdentity],
    ) -> None:
        gpu_ids = self._kfd_gpu_ids(expected)
        process_root = self._sysfs_root / "class/kfd/kfd/proc"
        try:
            with os.scandir(process_root) as iterator:
                entries = tuple(iterator)
        except OSError as error:
            raise _policy_error(
                f"cannot inspect KFD process ownership: {error}",
                code="benchmark_policy_unavailable",
            ) from error
        owners: list[str] = []
        for entry in entries:
            if not _matches(_KFD_PROCESS_PATTERN, entry.name):
                raise _policy_error(
                    "KFD process ownership contains an unknown entry",
                    code="benchmark_policy_unavailable",
                )
            if self._kfd_process_uses_gpu(
                process_root / entry.name,
                process_id=entry.name,
                gpu_ids=gpu_ids,
            ):
                owners.append(entry.name)
        if owners:
            raise _policy_error(
                "configured KFD GPUs are already owned by process IDs "
                + ", ".join(sorted(owners, key=int)),
                code="benchmark_external_compute",
            )

    def _device_path(self, bdf: str) -> pathlib.Path:
        if not isinstance(bdf, str) or not _matches(_PCI_BDF_PATTERN, bdf):
            raise _policy_error(
                f"PCI BDF {bdf!r} is not canonical",
                code="benchmark_policy_unavailable",
            )
        selected = self._sysfs_root / "bus/pci/devices" / bdf
        devices_root = self._sysfs_root / "devices"
        try:
            canonical_devices_root = devices_root.resolve(strict=True)
            canonical_device = selected.resolve(strict=True)
            canonical_device.relative_to(canonical_devices_root)
        except (OSError, ValueError) as error:
            raise _policy_error(
                f"PCI BDF {bdf} does not resolve inside sysfs devices",
                code="benchmark_policy_unavailable",
            ) from error
        if not canonical_device.is_dir():
            raise _policy_error(
                f"PCI BDF {bdf} is not a device directory",
                code="benchmark_policy_unavailable",
            )
        return canonical_device

    def _read_gpu_identity_fields(
        self,
        bdf: str,
        device_path: pathlib.Path,
    ) -> dict[str, str]:
        fields = {
            "vendor": "vendor",
            "device": "device",
            "subsystem_vendor": "subsystem_vendor",
            "subsystem_device": "subsystem_device",
            "revision": "revision",
            "device_class": "class",
        }
        return {
            field: self._read_single_line(
                device_path / filename,
                description=f"{bdf} {filename}",
            )
            for field, filename in fields.items()
        }

    @staticmethod
    def _make_gpu_identity(
        bdf: str,
        fields: Mapping[str, str],
        unique_id: str | None,
    ) -> AmdGpuIdentity:
        try:
            return AmdGpuIdentity(
                bdf=bdf,
                vendor=fields["vendor"],
                device=fields["device"],
                subsystem_vendor=fields["subsystem_vendor"],
                subsystem_device=fields["subsystem_device"],
                revision=fields["revision"],
                unique_id=unique_id,
                device_class=fields["device_class"],
            )
        except ValueError as error:
            raise _policy_error(
                f"PCI BDF {bdf} has malformed hardware identity: {error}",
                code="benchmark_policy_unavailable",
            ) from error

    def read_gpu_identity(self, bdf: str) -> AmdGpuIdentity:
        """Discover one exact AMD GPU identity from sysfs."""

        device_path = self._device_path(bdf)
        fields = self._read_gpu_identity_fields(bdf, device_path)
        unique_id = self._read_optional_single_line(
            device_path / "unique_id",
            description=f"{bdf} unique_id",
        )
        return self._make_gpu_identity(bdf, fields, unique_id)

    def _observed_gpu_identity(
        self,
        expected: AmdGpuIdentity,
    ) -> AmdGpuIdentity:
        device_path = self._device_path(expected.bdf)
        fields = self._read_gpu_identity_fields(expected.bdf, device_path)
        unique_id = (
            None
            if expected.unique_id is None
            else self._read_single_line(
                device_path / "unique_id",
                description=f"{expected.bdf} unique_id",
            )
        )
        return self._make_gpu_identity(expected.bdf, fields, unique_id)

    def assert_gpu_identity(self, expected: AmdGpuIdentity) -> None:
        observed = self._observed_gpu_identity(expected)
        if observed != expected:
            raise _policy_error(
                f"PCI BDF {expected.bdf} hardware identity changed",
                code="benchmark_hardware_identity_changed",
            )

    def read_gpu_level(self, expected: AmdGpuIdentity) -> str:
        self.assert_gpu_identity(expected)
        value = self._read_single_line(
            self._device_path(expected.bdf) / "power_dpm_force_performance_level",
            description=f"{expected.bdf} forced performance level",
        )
        if value not in _GPU_LEVELS:
            raise _policy_error(
                f"PCI BDF {expected.bdf} has unsupported performance level {value!r}",
                code="benchmark_policy_unavailable",
            )
        return value

    def write_gpu_level(self, expected: AmdGpuIdentity, value: str) -> None:
        if value not in _GPU_LEVELS:
            raise _policy_error(
                f"refusing unsupported GPU performance level {value!r}",
                code="benchmark_policy_apply_failed",
            )
        self.assert_gpu_identity(expected)
        path = self._device_path(expected.bdf) / "power_dpm_force_performance_level"
        flags = os.O_WRONLY | os.O_CLOEXEC | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        payload = f"{value}\n".encode("ascii")
        try:
            descriptor = os.open(path, flags)
            try:
                written = 0
                while written < len(payload):
                    count = os.write(descriptor, payload[written:])
                    if count <= 0:
                        raise OSError("short sysfs write")
                    written += count
            finally:
                os.close(descriptor)
        except OSError as error:
            raise _policy_error(
                f"cannot set {expected.bdf} performance level: {error}",
                code="benchmark_policy_apply_failed",
            ) from error
        observed = self.read_gpu_level(expected)
        if observed != value:
            raise _policy_error(
                f"PCI BDF {expected.bdf} rejected performance level "
                f"{value!r}; read back {observed!r}",
                code="benchmark_policy_apply_failed",
            )


@dataclasses.dataclass(frozen=True)
class _GpuBaseline:
    identity: AmdGpuIdentity
    level: str


@dataclasses.dataclass(frozen=True)
class _PolicyEpoch:
    boot_id: str
    policy_identity: str
    baseline_profile: str
    gpus: tuple[_GpuBaseline, ...]


class EpochJournal:
    """Canonical durable record that authorizes exact crash restoration."""

    def __init__(
        self,
        path: pathlib.Path = pathlib.Path("/var/lib/benchmarkd/active-epoch.json"),
        *,
        owner_uid: int = 0,
    ) -> None:
        self.path = pathlib.Path(path)
        self.owner_uid = owner_uid
        if not self.path.is_absolute():
            raise ValueError("epoch journal path must be absolute")
        if self.path.name in {"", ".", ".."}:
            raise ValueError("epoch journal path must name one file")
        if owner_uid < 0:
            raise ValueError("epoch journal owner UID is invalid")

    def _parent(self) -> pathlib.Path:
        parent = self.path.parent
        try:
            metadata = os.lstat(parent)
        except OSError as error:
            raise _policy_error(
                f"cannot inspect epoch journal directory: {error}",
                code="benchmark_policy_journal_failed",
            ) from error
        if not stat.S_ISDIR(metadata.st_mode):
            raise _policy_error(
                "epoch journal parent is not a directory",
                code="benchmark_policy_journal_failed",
            )
        if metadata.st_uid != self.owner_uid:
            raise _policy_error(
                "epoch journal directory has the wrong owner",
                code="benchmark_policy_journal_failed",
            )
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise _policy_error(
                "epoch journal directory is writable by another principal",
                code="benchmark_policy_journal_failed",
            )
        return parent

    def _validate_file(self) -> os.stat_result:
        try:
            metadata = os.lstat(self.path)
        except FileNotFoundError:
            raise
        except OSError as error:
            raise _policy_error(
                f"cannot inspect epoch journal: {error}",
                code="benchmark_policy_journal_failed",
            ) from error
        if not stat.S_ISREG(metadata.st_mode):
            raise _policy_error(
                "epoch journal is not a regular file",
                code="benchmark_policy_journal_failed",
            )
        if metadata.st_uid != self.owner_uid:
            raise _policy_error(
                "epoch journal has the wrong owner",
                code="benchmark_policy_journal_failed",
            )
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise _policy_error(
                "epoch journal mode is not 0600",
                code="benchmark_policy_journal_failed",
            )
        if metadata.st_size > _MAX_JOURNAL_BYTES:
            raise _policy_error(
                "epoch journal exceeds its fixed size limit",
                code="benchmark_policy_journal_failed",
            )
        return metadata

    def exists(self) -> bool:
        self._parent()
        try:
            self._validate_file()
        except FileNotFoundError:
            return False
        return True

    @staticmethod
    def _identity_json(identity: AmdGpuIdentity) -> dict[str, str | None]:
        return {
            "bdf": identity.bdf,
            "vendor": identity.vendor,
            "device": identity.device,
            "subsystem_vendor": identity.subsystem_vendor,
            "subsystem_device": identity.subsystem_device,
            "revision": identity.revision,
            "unique_id": identity.unique_id,
            "device_class": identity.device_class,
        }

    @classmethod
    def _encode(cls, epoch: _PolicyEpoch) -> bytes:
        document = {
            "schema": _JOURNAL_SCHEMA,
            "boot_id": epoch.boot_id,
            "policy_identity": epoch.policy_identity,
            "baseline_profile": epoch.baseline_profile,
            "gpus": [
                {
                    "identity": cls._identity_json(gpu.identity),
                    "baseline_level": gpu.level,
                }
                for gpu in epoch.gpus
            ],
        }
        return (
            json.dumps(
                document,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii")

    @staticmethod
    def _exact_keys(
        value: object,
        expected: frozenset[str],
        *,
        description: str,
    ) -> Mapping[str, object]:
        if not isinstance(value, Mapping) or set(value) != expected:
            raise _policy_error(
                f"epoch journal has malformed {description}",
                code="benchmark_policy_journal_failed",
            )
        return value

    @classmethod
    def _decode(cls, payload: bytes) -> _PolicyEpoch:
        try:
            text = payload.decode("ascii")
            if not text.endswith("\n") or "\n" in text[:-1]:
                raise ValueError("not one canonical line")
            document = json.loads(text)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            raise _policy_error(
                f"epoch journal is not canonical JSON: {error}",
                code="benchmark_policy_journal_failed",
            ) from error
        root = cls._exact_keys(
            document,
            frozenset(
                {
                    "schema",
                    "boot_id",
                    "policy_identity",
                    "baseline_profile",
                    "gpus",
                }
            ),
            description="root",
        )
        if root["schema"] != _JOURNAL_SCHEMA:
            raise _policy_error(
                "epoch journal schema is unsupported",
                code="benchmark_policy_journal_failed",
            )
        boot_id = root["boot_id"]
        policy_identity = root["policy_identity"]
        baseline_profile = root["baseline_profile"]
        raw_gpus = root["gpus"]
        if not isinstance(boot_id, str) or not _matches(_BOOT_ID_PATTERN, boot_id):
            raise _policy_error(
                "epoch journal boot ID is malformed",
                code="benchmark_policy_journal_failed",
            )
        if not isinstance(policy_identity, str) or not re.fullmatch(
            r"[a-z][a-z0-9-]{0,62}", policy_identity
        ):
            raise _policy_error(
                "epoch journal policy identity is malformed",
                code="benchmark_policy_journal_failed",
            )
        if not isinstance(baseline_profile, str) or not baseline_profile:
            raise _policy_error(
                "epoch journal baseline profile is malformed",
                code="benchmark_policy_journal_failed",
            )
        if not isinstance(raw_gpus, list) or not raw_gpus:
            raise _policy_error(
                "epoch journal GPU baselines are malformed",
                code="benchmark_policy_journal_failed",
            )
        gpus: list[_GpuBaseline] = []
        for raw_gpu in raw_gpus:
            gpu = cls._exact_keys(
                raw_gpu,
                frozenset({"identity", "baseline_level"}),
                description="GPU baseline",
            )
            raw_identity = cls._exact_keys(
                gpu["identity"],
                frozenset(
                    {
                        "bdf",
                        "vendor",
                        "device",
                        "subsystem_vendor",
                        "subsystem_device",
                        "revision",
                        "unique_id",
                        "device_class",
                    }
                ),
                description="GPU identity",
            )
            try:
                identity = AmdGpuIdentity(
                    bdf=raw_identity["bdf"],  # type: ignore[arg-type]
                    vendor=raw_identity["vendor"],  # type: ignore[arg-type]
                    device=raw_identity["device"],  # type: ignore[arg-type]
                    subsystem_vendor=raw_identity["subsystem_vendor"],  # type: ignore[arg-type]
                    subsystem_device=raw_identity["subsystem_device"],  # type: ignore[arg-type]
                    revision=raw_identity["revision"],  # type: ignore[arg-type]
                    unique_id=raw_identity["unique_id"],  # type: ignore[arg-type]
                    device_class=raw_identity["device_class"],  # type: ignore[arg-type]
                )
            except ValueError as error:
                raise _policy_error(
                    f"epoch journal GPU identity is malformed: {error}",
                    code="benchmark_policy_journal_failed",
                ) from error
            level = gpu["baseline_level"]
            if not isinstance(level, str) or level not in _GPU_LEVELS:
                raise _policy_error(
                    "epoch journal GPU baseline level is malformed",
                    code="benchmark_policy_journal_failed",
                )
            gpus.append(_GpuBaseline(identity=identity, level=level))
        return _PolicyEpoch(
            boot_id=boot_id,
            policy_identity=policy_identity,
            baseline_profile=baseline_profile,
            gpus=tuple(gpus),
        )

    def _fsync_parent(self, parent: pathlib.Path) -> None:
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        try:
            descriptor = os.open(parent, flags)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise _policy_error(
                f"cannot synchronize epoch journal directory: {error}",
                code="benchmark_policy_journal_failed",
            ) from error

    def commit(self, epoch: _PolicyEpoch) -> None:
        parent = self._parent()
        if self.exists():
            raise _policy_error(
                "an epoch journal is already active",
                code="benchmark_policy_recovery_required",
            )
        payload = self._encode(epoch)
        descriptor = -1
        temporary_path: pathlib.Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix=".active-epoch.",
                dir=parent,
            )
            temporary_path = pathlib.Path(raw_path)
            os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
            if metadata.st_uid != self.owner_uid:
                raise _policy_error(
                    "temporary epoch journal has the wrong owner",
                    code="benchmark_policy_journal_failed",
                )
            written = 0
            while written < len(payload):
                count = os.write(descriptor, payload[written:])
                if count <= 0:
                    raise OSError("short epoch journal write")
                written += count
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary_path, self.path)
            temporary_path = None
            self._validate_file()
            self._fsync_parent(parent)
        except BenchmarkLockError:
            raise
        except OSError as error:
            raise _policy_error(
                f"cannot commit epoch journal: {error}",
                code="benchmark_policy_journal_failed",
            ) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass

    def load(self) -> _PolicyEpoch:
        self._parent()
        metadata = self._validate_file()
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags)
            try:
                opened = os.fstat(descriptor)
                if opened.st_dev != metadata.st_dev or opened.st_ino != metadata.st_ino:
                    raise _policy_error(
                        "epoch journal changed while opening",
                        code="benchmark_policy_journal_failed",
                    )
                payload = os.read(descriptor, _MAX_JOURNAL_BYTES + 1)
            finally:
                os.close(descriptor)
        except BenchmarkLockError:
            raise
        except OSError as error:
            raise _policy_error(
                f"cannot read epoch journal: {error}",
                code="benchmark_policy_journal_failed",
            ) from error
        if len(payload) > _MAX_JOURNAL_BYTES:
            raise _policy_error(
                "epoch journal exceeds its fixed size limit",
                code="benchmark_policy_journal_failed",
            )
        epoch = self._decode(payload)
        if self._encode(epoch) != payload:
            raise _policy_error(
                "epoch journal JSON is not in canonical form",
                code="benchmark_policy_journal_failed",
            )
        return epoch

    def delete(self) -> None:
        parent = self._parent()
        self._validate_file()
        try:
            os.unlink(self.path)
        except OSError as error:
            raise _policy_error(
                f"cannot delete completed epoch journal: {error}",
                code="benchmark_policy_journal_failed",
            ) from error
        self._fsync_parent(parent)


class FixedHostPolicy:
    """Exact CPU authority plus AMD policy implementing broker.HostPolicy."""

    def __init__(
        self,
        config: FixedHostPolicyConfig,
        *,
        power_profiles: PowerProfilesBackend,
        filesystem: HostFilesystem | None = None,
        journal: EpochJournal | None = None,
    ) -> None:
        self._config = config
        self._power_profiles = power_profiles
        self._filesystem = LinuxHostFilesystem() if filesystem is None else filesystem
        self._journal = EpochJournal() if journal is None else journal
        self._state = "recovery_required" if self._journal.exists() else "idle"
        self._hold_cookie: object | None = None
        self._hold_release_attempted = False
        self._cpu_authority: str | None = None
        self._fixed_cpu_baseline: CpuPerformanceStatus | None = None
        self._fixed_power_profile_baseline: PowerProfileStatus | None = None

    @property
    def identity(self) -> str:
        return self._config.policy_identity

    @property
    def state(self) -> str:
        return self._state

    @staticmethod
    def _own_hold() -> PowerProfileHold:
        return PowerProfileHold(
            profile=_POWER_PROFILE,
            application_id=_POWER_PROFILE_APPLICATION_ID,
            reason=_POWER_PROFILE_REASON,
        )

    @staticmethod
    def _held_gpu_level(gpu: AmdGpuIdentity) -> str:
        if gpu.device_class.startswith("0x12"):
            return _HELD_PROCESSING_ACCELERATOR_LEVEL
        return _HELD_DISPLAY_GPU_LEVEL

    def _select_cpu_authority(
        self,
        status: PowerProfileStatus,
    ) -> tuple[str, CpuPerformanceStatus | None]:
        if status.holds:
            raise _policy_error(
                "another power profile hold is already active",
                code="benchmark_external_policy",
            )
        if _POWER_PROFILE in status.profiles:
            if status.performance_degraded:
                raise _policy_error(
                    "the performance power profile is degraded: "
                    f"{status.performance_degraded}",
                    code="benchmark_policy_unavailable",
                )
            return _CPU_AUTHORITY_POWER_PROFILES_DAEMON, None

        fixed_status = self._filesystem.cpu_performance_status()
        non_performance = tuple(
            policy
            for policy in fixed_status.policies
            if policy.governor != _POWER_PROFILE
        )
        if non_performance:
            details = ", ".join(
                f"{policy.name}={policy.governor}" for policy in non_performance
            )
            raise _policy_error(
                "power-profiles-daemon cannot hold performance and CPU "
                f"frequency governors are not fixed to performance: {details}",
                code="benchmark_policy_unavailable",
            )
        return _CPU_AUTHORITY_FIXED_CPU_FREQUENCY, fixed_status

    def _validate_epoch_matches_config(self, epoch: _PolicyEpoch) -> None:
        if epoch.policy_identity != self.identity:
            raise _policy_error(
                "epoch journal belongs to another fixed policy",
                code="benchmark_policy_recovery_required",
            )
        identities = tuple(gpu.identity for gpu in epoch.gpus)
        if identities != self._config.gpus:
            raise _policy_error(
                "epoch journal hardware set differs from fixed policy",
                code="benchmark_policy_recovery_required",
            )

    def _validate_recovery_hardware(self, epoch: _PolicyEpoch) -> None:
        self._validate_epoch_matches_config(epoch)
        try:
            for gpu in epoch.gpus:
                self._filesystem.assert_gpu_identity(gpu.identity)
        except BenchmarkLockError as error:
            raise _policy_error(
                f"refusing epoch replay against changed hardware: {error}",
                code="benchmark_policy_recovery_required",
            ) from error

    def _snapshot_epoch(
        self,
        power_profile: PowerProfileStatus,
    ) -> _PolicyEpoch:
        baselines: list[_GpuBaseline] = []
        for gpu in self._config.gpus:
            self._filesystem.assert_gpu_identity(gpu)
            baselines.append(
                _GpuBaseline(
                    identity=gpu,
                    level=self._filesystem.read_gpu_level(gpu),
                )
            )
        return _PolicyEpoch(
            boot_id=self._filesystem.boot_id(),
            policy_identity=self.identity,
            baseline_profile=power_profile.active_profile,
            gpus=tuple(baselines),
        )

    @staticmethod
    def _as_error(error: Exception, *, code: str) -> BenchmarkLockError:
        if isinstance(error, BenchmarkLockError):
            return error
        return _policy_error(str(error), code=code)

    def _verify_held(self) -> None:
        if not self._journal.exists():
            raise _policy_error(
                "the durable benchmark policy epoch disappeared",
                code="benchmark_policy_drift",
            )
        if self._cpu_authority == _CPU_AUTHORITY_POWER_PROFILES_DAEMON:
            self._verify_power_profile_held()
        elif self._cpu_authority == _CPU_AUTHORITY_FIXED_CPU_FREQUENCY:
            self._verify_fixed_cpu_held()
        else:
            raise _policy_error(
                "the benchmark CPU policy has no selected authority",
                code="benchmark_policy_drift",
            )

        for gpu in self._config.gpus:
            try:
                self._filesystem.assert_gpu_identity(gpu)
                level = self._filesystem.read_gpu_level(gpu)
            except BenchmarkLockError as error:
                raise _policy_error(
                    f"cannot audit GPU {gpu.bdf}: {error}",
                    code="benchmark_policy_drift",
                ) from error
            expected_level = self._held_gpu_level(gpu)
            if level != expected_level:
                raise _policy_error(
                    f"GPU {gpu.bdf} drifted from {expected_level!r} "
                    f"to {level!r}",
                    code="benchmark_policy_drift",
                )

    def _verify_power_profile_held(self) -> None:
        status = self._power_profiles.status()
        expected_hold = self._own_hold()
        if status.holds != (expected_hold,):
            raise _policy_error(
                "the benchmark power profile hold was released or replaced",
                code="benchmark_policy_drift",
            )
        if status.active_profile != _POWER_PROFILE:
            raise _policy_error(
                "the active power profile is no longer performance",
                code="benchmark_policy_drift",
            )
        if status.performance_degraded:
            raise _policy_error(
                "the performance power profile became degraded: "
                f"{status.performance_degraded}",
                code="benchmark_policy_drift",
            )

    def _verify_fixed_cpu_held(self) -> None:
        expected = self._fixed_cpu_baseline
        expected_power_profile = self._fixed_power_profile_baseline
        if expected is None or expected_power_profile is None:
            raise _policy_error(
                "the fixed CPU frequency authority has no baseline",
                code="benchmark_policy_drift",
            )
        try:
            observed_power_profile = self._power_profiles.status()
            observed = self._filesystem.cpu_performance_status()
        except BenchmarkLockError as error:
            raise _policy_error(
                f"cannot audit fixed CPU frequency state: {error}",
                code="benchmark_policy_drift",
            ) from error
        if observed_power_profile != expected_power_profile:
            raise _policy_error(
                "power-profiles-daemon state changed while fixed CPU "
                "frequency controls were authoritative",
                code="benchmark_policy_drift",
            )
        drift = self._describe_fixed_cpu_drift(expected, observed)
        if drift is not None:
            raise _policy_error(
                f"fixed CPU frequency state drifted: {drift}",
                code="benchmark_policy_drift",
            )

    @staticmethod
    def _describe_fixed_cpu_drift(
        expected: CpuPerformanceStatus,
        observed: CpuPerformanceStatus,
    ) -> str | None:
        expected_names = tuple(policy.name for policy in expected.policies)
        observed_names = tuple(policy.name for policy in observed.policies)
        if observed_names != expected_names:
            return "the CPU frequency policy set changed"
        controls = (
            ("driver", "driver"),
            ("governor", "governor"),
            ("minimum frequency", "minimum_frequency_khz"),
            ("maximum frequency", "maximum_frequency_khz"),
            (
                "energy performance preference",
                "energy_performance_preference",
            ),
        )
        for expected_policy, observed_policy in zip(
            expected.policies,
            observed.policies,
            strict=True,
        ):
            for label, attribute in controls:
                expected_value = getattr(expected_policy, attribute)
                observed_value = getattr(observed_policy, attribute)
                if observed_value != expected_value:
                    return (
                        f"{expected_policy.name} {label} changed from "
                        f"{expected_value!r} to {observed_value!r}"
                    )
        if observed.boost_enabled != expected.boost_enabled:
            return (
                "the global boost control changed from "
                f"{expected.boost_enabled!r} to {observed.boost_enabled!r}"
            )
        return None

    def preflight(self) -> None:
        """Require unowned configured GPUs immediately before a grant."""

        self._filesystem.assert_kfd_gpus_unowned(self._config.gpus)

    def verify(self) -> None:
        if self._state != "held":
            raise _policy_error(
                f"cannot verify policy while it is {self._state}",
                code="benchmark_policy_state",
            )
        try:
            self._verify_held()
        except BenchmarkLockError:
            self._state = "faulted"
            raise

    def enter(self) -> None:
        if self._state not in {"idle", "recovery_required"}:
            raise _policy_error(
                f"cannot enter policy while it is {self._state}",
                code="benchmark_policy_state",
            )
        if self._journal.exists():
            self.recover()
        if self._state != "idle":
            raise _policy_error(
                f"cannot enter policy while it is {self._state}",
                code="benchmark_policy_state",
            )
        self.preflight()
        power_profile = self._power_profiles.status()
        cpu_authority, fixed_cpu_baseline = self._select_cpu_authority(power_profile)
        epoch = self._snapshot_epoch(power_profile)
        self._journal.commit(epoch)
        self._cpu_authority = cpu_authority
        self._fixed_cpu_baseline = fixed_cpu_baseline
        self._fixed_power_profile_baseline = (
            power_profile
            if cpu_authority == _CPU_AUTHORITY_FIXED_CPU_FREQUENCY
            else None
        )
        self._state = "entering"
        try:
            if self._cpu_authority == _CPU_AUTHORITY_POWER_PROFILES_DAEMON:
                self._hold_cookie = self._power_profiles.hold_performance(
                    reason=_POWER_PROFILE_REASON,
                    application_id=_POWER_PROFILE_APPLICATION_ID,
                )
            for gpu in self._config.gpus:
                self._filesystem.write_gpu_level(gpu, self._held_gpu_level(gpu))
            self._verify_held()
        except Exception as error:
            original = self._as_error(
                error,
                code="benchmark_policy_apply_failed",
            )
            self._rollback_failed_enter(epoch, original)
        self._state = "held"

    def _power_profile_has_own_hold(
        self,
        status: PowerProfileStatus,
    ) -> bool:
        return self._own_hold() in status.holds

    @staticmethod
    def _validate_released_power_profile(
        epoch: _PolicyEpoch,
        status: PowerProfileStatus,
    ) -> None:
        if status.active_profile != epoch.baseline_profile:
            raise _policy_error(
                "power profile did not return to its baseline after release",
                code="benchmark_policy_restore_failed",
            )
        if status.holds:
            raise _policy_error(
                "power profile holds did not return to their empty baseline",
                code="benchmark_policy_restore_failed",
            )

    def _release_power_profile(
        self,
        epoch: _PolicyEpoch,
        errors: list[BenchmarkLockError],
    ) -> None:
        cookie = self._hold_cookie
        try:
            before = self._power_profiles.status()
            if cookie is None:
                if self._power_profile_has_own_hold(before):
                    raise _policy_error(
                        "the benchmark power profile hold has no release cookie",
                        code="benchmark_policy_restore_failed",
                    )
                self._validate_released_power_profile(epoch, before)
                self._hold_release_attempted = False
                return
            if not self._power_profile_has_own_hold(before):
                # PPD explicitly cancels holds on a manual profile override.
                # Absence is drift, but leave must never fight the operator by
                # setting or reacquiring a profile. After an attempted release,
                # however, absence is not evidence that baseline restoration
                # succeeded: verify the post-release state before forgetting
                # the durable epoch.
                if self._hold_release_attempted:
                    self._validate_released_power_profile(epoch, before)
                self._hold_cookie = None
                self._hold_release_attempted = False
                return
            self._hold_release_attempted = True
            self._power_profiles.release(cookie)
            after = self._power_profiles.status()
            self._validate_released_power_profile(epoch, after)
            self._hold_cookie = None
            self._hold_release_attempted = False
        except Exception as error:
            errors.append(
                self._as_error(
                    error,
                    code="benchmark_policy_restore_failed",
                )
            )

    def _release_cpu_authority(
        self,
        epoch: _PolicyEpoch,
        errors: list[BenchmarkLockError],
    ) -> None:
        if self._cpu_authority == _CPU_AUTHORITY_FIXED_CPU_FREQUENCY:
            if self._hold_cookie is not None:
                errors.append(
                    _policy_error(
                        "fixed CPU frequency authority unexpectedly owns a "
                        "power profile hold",
                        code="benchmark_policy_restore_failed",
                    )
                )
            return
        if self._cpu_authority not in {
            None,
            _CPU_AUTHORITY_POWER_PROFILES_DAEMON,
        }:
            errors.append(
                _policy_error(
                    "benchmark CPU policy authority is unknown",
                    code="benchmark_policy_restore_failed",
                )
            )
            return
        self._release_power_profile(epoch, errors)

    def _restore_epoch(
        self,
        epoch: _PolicyEpoch,
        *,
        release_cpu_authority: bool,
    ) -> None:
        self._validate_epoch_matches_config(epoch)
        errors: list[BenchmarkLockError] = []
        for gpu in epoch.gpus:
            try:
                self._filesystem.assert_gpu_identity(gpu.identity)
                self._filesystem.write_gpu_level(gpu.identity, gpu.level)
                observed = self._filesystem.read_gpu_level(gpu.identity)
                if observed != gpu.level:
                    raise _policy_error(
                        f"GPU {gpu.identity.bdf} restored as {observed!r}, "
                        f"not {gpu.level!r}",
                        code="benchmark_policy_restore_failed",
                    )
            except Exception as error:
                errors.append(
                    self._as_error(
                        error,
                        code="benchmark_policy_restore_failed",
                    )
                )
        if release_cpu_authority:
            self._release_cpu_authority(epoch, errors)
        if errors:
            details = "; ".join(str(error) for error in errors)
            raise _policy_error(
                f"host policy restoration was incomplete: {details}",
                code="benchmark_policy_restore_failed",
            )

    def _rollback_failed_enter(
        self,
        epoch: _PolicyEpoch,
        original: BenchmarkLockError,
    ) -> None:
        self._state = "leaving"
        try:
            self._restore_epoch(epoch, release_cpu_authority=True)
            self._journal.delete()
        except BenchmarkLockError as rollback_error:
            self._state = "faulted"
            raise _policy_error(
                f"policy application failed ({original}); "
                f"rollback failed ({rollback_error})",
                code="benchmark_policy_restore_failed",
            ) from original
        self._cpu_authority = None
        self._fixed_cpu_baseline = None
        self._fixed_power_profile_baseline = None
        self._state = "idle"
        raise original

    def recover(self) -> None:
        if self._state not in {"idle", "recovery_required"}:
            raise _policy_error(
                f"cannot recover policy while it is {self._state}",
                code="benchmark_policy_state",
            )
        if not self._journal.exists():
            self._state = "idle"
            return
        epoch = self._journal.load()
        current_boot_id = self._filesystem.boot_id()
        if epoch.boot_id != current_boot_id:
            # Sysfs state is boot-scoped.  A stale epoch can authorize no write
            # against a new kernel's potentially different device topology.
            self._journal.delete()
            self._hold_cookie = None
            self._hold_release_attempted = False
            self._cpu_authority = None
            self._fixed_cpu_baseline = None
            self._fixed_power_profile_baseline = None
            self._state = "idle"
            return
        self._validate_recovery_hardware(epoch)
        self._state = "leaving"
        try:
            self._restore_epoch(epoch, release_cpu_authority=False)
            self._journal.delete()
        except BenchmarkLockError:
            self._state = "faulted"
            raise
        self._hold_cookie = None
        self._hold_release_attempted = False
        self._cpu_authority = None
        self._fixed_cpu_baseline = None
        self._fixed_power_profile_baseline = None
        self._state = "idle"

    def leave(self) -> None:
        if not self._journal.exists():
            if self._state != "idle" or self._hold_cookie is not None:
                self._state = "faulted"
                raise _policy_error(
                    "cannot restore host policy without its durable epoch",
                    code="benchmark_policy_restore_failed",
                )
            self._hold_cookie = None
            self._hold_release_attempted = False
            self._cpu_authority = None
            self._fixed_cpu_baseline = None
            self._fixed_power_profile_baseline = None
            self._state = "idle"
            return
        epoch = self._journal.load()
        if epoch.boot_id != self._filesystem.boot_id():
            self._journal.delete()
            self._hold_cookie = None
            self._hold_release_attempted = False
            self._cpu_authority = None
            self._fixed_cpu_baseline = None
            self._fixed_power_profile_baseline = None
            self._state = "idle"
            return
        self._state = "leaving"
        try:
            self._restore_epoch(epoch, release_cpu_authority=True)
            self._journal.delete()
        except BenchmarkLockError:
            self._state = "faulted"
            raise
        self._hold_cookie = None
        self._hold_release_attempted = False
        self._cpu_authority = None
        self._fixed_cpu_baseline = None
        self._fixed_power_profile_baseline = None
        self._state = "idle"
