"""Linux, NVIDIA, process, and loopback boundaries for the runtime controller."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import pathlib
import signal
import stat
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from runpod_local.errors import RunpodLocalError

from .layout import REMOTE_RUNTIME_CONTROL_ROOT, RuntimeLayout
from .state import ProcessState
from .vllm import LOCAL_API_KEY


MAX_RUNTIME_VERIFICATION_BYTES = 1024 * 1024
MAX_PROCESS_ENVIRONMENT_BYTES = 1024 * 1024


def _fail(message: str, *, code: str = "service_runtime_platform_error") -> None:
    raise RunpodLocalError(message, code=code)


def _safe_control_file(
    path: pathlib.Path,
    *,
    maximum_bytes: int,
) -> bytes:
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise RunpodLocalError(
            f"runtime control file is absent: {path}",
            code="runtime_verification_unavailable",
        ) from error
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or stat.S_ISLNK(path_stat.st_mode)
        or path_stat.st_uid != os.getuid()
        or path_stat.st_nlink != 1
        or stat.S_IMODE(path_stat.st_mode) != 0o600
        or not 1 <= path_stat.st_size <= maximum_bytes
    ):
        _fail(
            f"runtime control file has an unsafe identity: {path}",
            code="runtime_verification_unavailable",
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != path_stat.st_dev
            or opened.st_ino != path_stat.st_ino
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != path_stat.st_uid
            or opened.st_nlink != path_stat.st_nlink
            or stat.S_IMODE(opened.st_mode) != stat.S_IMODE(path_stat.st_mode)
            or opened.st_size != path_stat.st_size
        ):
            _fail(
                f"runtime control file changed while opening: {path}",
                code="runtime_verification_unavailable",
            )
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        final = os.fstat(descriptor)
        if (
            len(payload) > maximum_bytes
            or final.st_size != opened.st_size
            or final.st_mtime_ns != opened.st_mtime_ns
            or final.st_ctime_ns != opened.st_ctime_ns
            or final.st_uid != opened.st_uid
            or final.st_nlink != opened.st_nlink
            or stat.S_IMODE(final.st_mode) != stat.S_IMODE(opened.st_mode)
        ):
            _fail(
                f"runtime control file changed while reading: {path}",
                code="runtime_verification_unavailable",
            )
    finally:
        os.close(descriptor)
    return payload


@dataclass(frozen=True)
class ProcessObservation:
    pid: int
    process_group_id: int
    session_id: int
    start_ticks: int


@dataclass(frozen=True)
class SpawnedProcess:
    pid: int
    observation: ProcessObservation


@dataclass(frozen=True)
class ProbeResult:
    ready: bool
    health_status: int | None
    models_status: int | None
    served_model_ids: tuple[str, ...]
    detail: str


class SystemPlatform:
    """Production implementation of every mutable OS/process boundary."""

    def require_runtime_account(self) -> None:
        if os.geteuid() != 0:
            _fail(
                "service runtime must execute as root inside the private Pod",
                code="invalid_service_runtime_account",
            )

    def boot_id(self) -> str:
        try:
            value = (
                pathlib.Path("/proc/sys/kernel/random/boot_id")
                .read_text(encoding="ascii")
                .strip()
            )
            parsed = uuid.UUID(value)
        except (OSError, ValueError) as error:
            raise RunpodLocalError(
                "kernel boot identity is unavailable or malformed",
                code="service_runtime_platform_error",
            ) from error
        return str(parsed)

    def process_nonce(self) -> str:
        try:
            value = (
                pathlib.Path("/proc/sys/kernel/random/uuid")
                .read_text(encoding="ascii")
                .strip()
            )
            parsed = uuid.UUID(value)
        except (OSError, ValueError) as error:
            raise RunpodLocalError(
                "kernel process nonce source is unavailable or malformed",
                code="service_runtime_platform_error",
            ) from error
        return str(parsed)

    def observe_gpu(self) -> dict[str, Any]:
        arguments = [
            "/usr/bin/nvidia-smi",
            "--query-gpu=name,compute_cap,memory.total,driver_version",
            "--format=csv,noheader,nounits",
        ]
        try:
            completed = subprocess.run(
                arguments,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                env={
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "PATH": "/usr/bin:/bin",
                },
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RunpodLocalError(
                "NVIDIA GPU observation failed",
                code="gpu_observation_failed",
            ) from error
        rows = list(csv.reader(completed.stdout.splitlines()))
        if len(rows) != 1 or len(rows[0]) != 4:
            _fail(
                "service runtime requires exactly one NVIDIA GPU",
                code="gpu_observation_failed",
            )
        name, capability, memory, driver = (item.strip() for item in rows[0])
        capability_parts = capability.split(".")
        try:
            if len(capability_parts) != 2:
                raise ValueError
            capability_value = [
                int(capability_parts[0]),
                int(capability_parts[1]),
            ]
            memory_mib = int(memory)
        except ValueError as error:
            raise RunpodLocalError(
                "NVIDIA GPU observation is malformed",
                code="gpu_observation_failed",
            ) from error
        return {
            "name": name,
            "compute_capability": capability_value,
            "memory_mib": memory_mib,
            "driver_version": driver,
        }

    def verify_runtime(
        self,
        *,
        expected_runtime: dict[str, Any],
        layout: RuntimeLayout,
    ) -> dict[str, Any]:
        control_root = REMOTE_RUNTIME_CONTROL_ROOT
        verifier = layout.localize(control_root / "verify-runtime.py")
        manifest = layout.localize(control_root / "runtime-manifest.json")
        _safe_control_file(
            verifier,
            maximum_bytes=1024 * 1024,
        )
        manifest_payload = _safe_control_file(
            manifest,
            maximum_bytes=1024 * 1024,
        )
        expected_manifest_sha256 = expected_runtime["manifest"]["sha256"]
        if hashlib.sha256(manifest_payload).hexdigest() != expected_manifest_sha256:
            _fail(
                "runtime manifest does not match the deployment",
                code="runtime_verification_failed",
            )
        arguments = [
            "/usr/bin/python3.12",
            str(verifier),
            "--manifest",
            str(manifest),
            "--gpu",
        ]
        try:
            completed = subprocess.run(
                arguments,
                check=True,
                capture_output=True,
                timeout=180,
                env={
                    "DO_NOT_TRACK": "1",
                    "HF_HUB_DISABLE_TELEMETRY": "1",
                    "HF_HUB_DISABLE_UPDATE_CHECK": "1",
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "PATH": (
                        "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
                    ),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RunpodLocalError(
                "pinned upstream runtime verification failed",
                code="runtime_verification_failed",
            ) from error
        if len(completed.stdout) > MAX_RUNTIME_VERIFICATION_BYTES:
            _fail(
                "runtime verification report exceeds its size bound",
                code="runtime_verification_failed",
            )
        try:
            report = json.loads(completed.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RunpodLocalError(
                "runtime verification report is malformed",
                code="runtime_verification_failed",
            ) from error
        expected_fields = {
            "schema_version",
            "requested_image",
            "runtime_id",
            "versions",
            "executables",
            "gpu_verified",
            "gpu",
        }
        gpu = report.get("gpu") if isinstance(report, dict) else None
        if (
            not isinstance(report, dict)
            or set(report) != expected_fields
            or report["schema_version"] != "runpod.upstream-runtime-verification.v1"
            or report["requested_image"] != expected_runtime["image"]
            or report["runtime_id"] != expected_runtime["runtime_id"]
            or report["gpu_verified"] is not True
            or not isinstance(gpu, dict)
            or set(gpu) != {"name", "capability", "bytes"}
        ):
            _fail(
                "runtime verification report does not match the deployment",
                code="runtime_verification_failed",
            )
        return report

    def _process_environment(self, pid: int) -> dict[str, str] | None:
        path = pathlib.Path("/proc") / str(pid) / "environ"
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
            return None
        try:
            chunks: list[bytes] = []
            remaining = MAX_PROCESS_ENVIRONMENT_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
        finally:
            os.close(descriptor)
        if len(payload) > MAX_PROCESS_ENVIRONMENT_BYTES:
            return None
        environment: dict[str, str] = {}
        for entry in payload.split(b"\0"):
            if not entry or b"=" not in entry:
                continue
            name, raw_value = entry.split(b"=", 1)
            try:
                environment[name.decode("ascii")] = raw_value.decode("utf-8")
            except UnicodeDecodeError:
                continue
        return environment

    def observe_process(self, pid: int) -> ProcessObservation | None:
        try:
            payload = (pathlib.Path("/proc") / str(pid) / "stat").read_text(
                encoding="ascii"
            )
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
            return None
        close_parenthesis = payload.rfind(")")
        if close_parenthesis < 1:
            return None
        fields = payload[close_parenthesis + 2 :].split()
        try:
            process_group_id = int(fields[2])
            session_id = int(fields[3])
            start_ticks = int(fields[19])
        except (IndexError, ValueError):
            return None
        return ProcessObservation(
            pid=pid,
            process_group_id=process_group_id,
            session_id=session_id,
            start_ticks=start_ticks,
        )

    def process_is_owned(self, state: ProcessState) -> bool:
        observation = self.observe_process(state.pid)
        environment = self._process_environment(state.pid)
        return bool(
            observation is not None
            and observation.start_ticks == state.process_start_ticks
            and environment is not None
            and environment.get("RUNPOD_SERVICE_ID") == state.service_id
            and environment.get("RUNPOD_SERVICE_PROCESS_NONCE") == state.process_nonce
            and environment.get("RUNPOD_SERVICE_MANIFEST_SHA256")
            == state.manifest_sha256
        )

    def list_service_processes(
        self,
        *,
        service_id: str,
        process_nonce: str | None = None,
        manifest_sha256: str | None = None,
    ) -> list[ProcessObservation]:
        processes: list[ProcessObservation] = []
        try:
            entries = list(os.scandir("/proc"))
        except OSError as error:
            raise RunpodLocalError(
                "cannot enumerate service processes",
                code="service_process_observation_failed",
            ) from error
        for entry in entries:
            if not entry.name.isdecimal() or int(entry.name) <= 0:
                continue
            pid = int(entry.name)
            environment = self._process_environment(pid)
            if (
                environment is None
                or environment.get("RUNPOD_SERVICE_ID") != service_id
                or (
                    process_nonce is not None
                    and environment.get("RUNPOD_SERVICE_PROCESS_NONCE") != process_nonce
                )
                or (
                    manifest_sha256 is not None
                    and environment.get("RUNPOD_SERVICE_MANIFEST_SHA256")
                    != manifest_sha256
                )
            ):
                continue
            observation = self.observe_process(pid)
            if observation is not None:
                processes.append(observation)
        return sorted(processes, key=lambda item: item.pid)

    def _base_environment(self) -> dict[str, str]:
        environment = {
            "HOME": "/root",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "LOGNAME": "root",
            "PATH": ("/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
            "USER": "root",
        }
        for name in (
            "CUDA_VISIBLE_DEVICES",
            "LD_LIBRARY_PATH",
            "NVIDIA_DRIVER_CAPABILITIES",
            "NVIDIA_VISIBLE_DEVICES",
            "TZ",
        ):
            value = os.environ.get(name)
            if value is not None:
                environment[name] = value
        return environment

    @staticmethod
    def _reap_rejected_spawn(process: subprocess.Popen[bytes]) -> None:
        """Boundedly stop a child that cannot receive a process receipt."""

        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired as error:
            raise RunpodLocalError(
                "unaccepted vLLM child survived TERM and KILL",
                code="ambiguous_failed_service_start",
            ) from error

    def spawn(
        self,
        *,
        argv: tuple[str, ...],
        environment_additions: dict[str, str],
        log_path: pathlib.Path,
        serving_lease_descriptor: int,
    ) -> SpawnedProcess:
        environment = self._base_environment()
        environment.update(environment_additions)
        flags = os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            try:
                log_descriptor = os.open(
                    log_path,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                log_descriptor = os.open(log_path, flags)
        except OSError as error:
            raise RunpodLocalError(
                f"cannot open service log: {log_path}",
                code="service_start_failed",
            ) from error
        try:
            opened = os.fstat(log_descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.getuid()
                or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) != 0o600
            ):
                _fail(
                    f"service log has an unsafe identity: {log_path}",
                    code="service_start_failed",
                )
            os.ftruncate(log_descriptor, 0)
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log_descriptor,
                stderr=subprocess.STDOUT,
                close_fds=True,
                pass_fds=(serving_lease_descriptor,),
                start_new_session=True,
                env=environment,
                cwd="/",
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RunpodLocalError(
                "cannot start the typed vLLM process",
                code="service_start_failed",
            ) from error
        finally:
            os.close(log_descriptor)
        observation = self.observe_process(process.pid)
        if (
            observation is None
            or observation.process_group_id != process.pid
            or observation.session_id != process.pid
        ):
            self._reap_rejected_spawn(process)
            _fail(
                "vLLM did not enter its dedicated process group and session",
                code="service_start_failed",
            )
        return SpawnedProcess(pid=process.pid, observation=observation)

    def signal_processes(
        self,
        *,
        processes: Iterable[ProcessObservation],
        signal_number: signal.Signals,
    ) -> None:
        observations = list(processes)
        process_ids = {item.pid for item in observations}
        signaled_groups: set[int] = set()
        for item in observations:
            current = self.observe_process(item.pid)
            if current is None or current.start_ticks != item.start_ticks:
                continue
            if (
                item.process_group_id == item.pid
                and item.session_id == item.pid
                and item.pid not in signaled_groups
            ):
                try:
                    os.killpg(item.pid, signal_number)
                except ProcessLookupError:
                    pass
                signaled_groups.add(item.pid)
                continue
            if item.pid in process_ids:
                try:
                    os.kill(item.pid, signal_number)
                except ProcessLookupError:
                    pass

    def wait_for_exit(
        self,
        *,
        processes: Iterable[ProcessObservation],
        timeout_seconds: float,
    ) -> bool:
        identities = {item.pid: item.start_ticks for item in processes}
        deadline = time.monotonic() + timeout_seconds
        while identities:
            identities = {
                pid: start_ticks
                for pid, start_ticks in identities.items()
                if (
                    (observed := self.observe_process(pid)) is not None
                    and observed.start_ticks == start_ticks
                )
            }
            if not identities:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)
        return True

    def probe(
        self,
        *,
        port: int,
        expected_service_id: str,
    ) -> ProbeResult:
        statuses: dict[str, int | None] = {"health": None, "models": None}
        payloads: dict[str, bytes] = {}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for name, route in (("health", "/health"), ("models", "/v1/models")):
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}{route}",
                headers={"Authorization": f"Bearer {LOCAL_API_KEY}"},
                method="GET",
            )
            try:
                with opener.open(request, timeout=2.0) as response:
                    statuses[name] = response.status
                    payloads[name] = response.read(1024 * 1024 + 1)
            except urllib.error.HTTPError as error:
                statuses[name] = error.code
                return ProbeResult(
                    ready=False,
                    health_status=statuses["health"],
                    models_status=statuses["models"],
                    served_model_ids=(),
                    detail="loopback endpoint returned an HTTP error",
                )
            except (urllib.error.URLError, TimeoutError, OSError):
                return ProbeResult(
                    ready=False,
                    health_status=statuses["health"],
                    models_status=statuses["models"],
                    served_model_ids=(),
                    detail="loopback endpoint is not ready",
                )
        if any(len(payload) > 1024 * 1024 for payload in payloads.values()):
            return ProbeResult(
                ready=False,
                health_status=statuses["health"],
                models_status=statuses["models"],
                served_model_ids=(),
                detail="model inventory exceeds its response bound",
            )
        try:
            models = json.loads(payloads["models"])
            if (
                not isinstance(models, dict)
                or set(models) != {"object", "data"}
                or models["object"] != "list"
                or not isinstance(models["data"], list)
                or not models["data"]
                or any(
                    not isinstance(item, dict)
                    or not isinstance(item.get("id"), str)
                    or not item["id"]
                    for item in models["data"]
                )
            ):
                raise TypeError
            served_ids = tuple(sorted(item["id"] for item in models["data"]))
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
            return ProbeResult(
                ready=False,
                health_status=statuses["health"],
                models_status=statuses["models"],
                served_model_ids=(),
                detail="model inventory is malformed",
            )
        ready = (
            statuses["health"] == 200
            and statuses["models"] == 200
            and served_ids == (expected_service_id,)
        )
        return ProbeResult(
            ready=ready,
            health_status=statuses["health"],
            models_status=statuses["models"],
            served_model_ids=served_ids,
            detail=(
                "ready"
                if ready
                else "model inventory does not match the deployed service"
            ),
        )
