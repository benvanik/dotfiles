"""Client and safe autostart path for the boot-local model-lab supervisor."""

from __future__ import annotations

import dataclasses
import datetime
import fcntl
import os
import pathlib
import pwd
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

from runpod_local.paths import (
    config_root as runpod_config_root,
    credentials_file as runpod_credentials_file,
    runpod_root,
    state_root as runpod_state_root,
)

from .errors import ModelLabError
from .huggingface_credentials import huggingface_token_path
from .lifecycle import format_timestamp, utc_now
from .paths import ensure_private_directory
from .supervisor_protocol import (
    PI_PENDING_SCHEMA,
    SUPERVISOR_ERROR_SCHEMA,
    SUPERVISOR_REQUEST_SCHEMA,
    SUPERVISOR_RESULT_SCHEMA,
    peer_credentials,
    receive_document,
    require_canonical_input_modalities,
    require_exact_fields,
    require_identifier,
    require_nullable_opaque_identifier,
    require_opaque_identifier,
    require_sha256,
    send_document,
    supervisor_socket_path,
)


@dataclasses.dataclass(frozen=True)
class PendingPiUse:
    profile_id: str
    project_id: str
    service_id: str
    service_sha256: str
    workload_sha256: str
    required_input_modalities: tuple[str, ...]
    session_id: str | None
    deployment_id: str
    use_lease_id: str


@dataclasses.dataclass
class PiLeaseChannel:
    pending: PendingPiUse
    connection: socket.socket
    startup_deadline: float

    def close(self) -> None:
        self.connection.close()


SupervisorLauncher = Callable[
    [pathlib.Path, pathlib.Path, pathlib.Path],
    subprocess.Popen[bytes] | None,
]


@dataclasses.dataclass(frozen=True)
class _SupervisorLaunchEnvironment:
    """Exact non-secret paths needed by the long-lived supervisor."""

    home: pathlib.Path
    runtime_parent: pathlib.Path
    runpod_authored_root: pathlib.Path
    runpod_state_root: pathlib.Path
    runpod_config_root: pathlib.Path
    runpod_credentials_file: pathlib.Path
    huggingface_token_file: pathlib.Path

    def normalized(self) -> dict[str, str]:
        return {
            "HOME": os.fspath(self.home),
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "XDG_RUNTIME_DIR": os.fspath(self.runtime_parent),
            "RUNPOD_ROOT": os.fspath(self.runpod_authored_root),
            "RUNPOD_STATE_HOME": os.fspath(self.runpod_state_root),
            "RUNPOD_CONFIG_HOME": os.fspath(self.runpod_config_root),
            "RUNPOD_CREDENTIALS_FILE": os.fspath(
                self.runpod_credentials_file
            ),
            "HF_TOKEN_PATH": os.fspath(self.huggingface_token_file),
        }


def _absolute_launch_path(value: pathlib.Path, *, label: str) -> pathlib.Path:
    path = value.expanduser().absolute()
    if any(ord(character) < 32 or ord(character) == 127 for character in str(path)):
        raise ModelLabError(
            f"{label} contains control characters",
            code="unsafe_supervisor_runtime",
        )
    return path


def _supervisor_launch_environment(
    runtime_root: pathlib.Path,
) -> dict[str, str]:
    """Resolve configuration once, without forwarding the caller environment."""

    try:
        account_home = pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError) as error:
        raise ModelLabError(
            "cannot resolve the supervisor account home directory",
            code="unsafe_supervisor_runtime",
        ) from error
    runtime = _absolute_launch_path(
        runtime_root,
        label="supervisor runtime root",
    )
    if runtime.name != "model-lab":
        raise ModelLabError(
            "supervisor runtime root must be the model-lab child of one "
            "boot-local runtime directory",
            code="noncanonical_supervisor_runtime",
        )
    paths = _SupervisorLaunchEnvironment(
        home=_absolute_launch_path(account_home, label="account home"),
        runtime_parent=runtime.parent,
        runpod_authored_root=_absolute_launch_path(
            runpod_root(),
            label="RunPod authored root",
        ),
        runpod_state_root=_absolute_launch_path(
            runpod_state_root(),
            label="RunPod state root",
        ),
        runpod_config_root=_absolute_launch_path(
            runpod_config_root(),
            label="RunPod config root",
        ),
        runpod_credentials_file=_absolute_launch_path(
            runpod_credentials_file(),
            label="RunPod credential file",
        ),
        huggingface_token_file=_absolute_launch_path(
            huggingface_token_path(),
            label="Hugging Face token file",
        ),
    )
    return paths.normalized()


def _default_launcher(
    authored_root: pathlib.Path,
    state_root: pathlib.Path,
    runtime_root: pathlib.Path,
) -> subprocess.Popen[bytes]:
    executable = (
        pathlib.Path(__file__).resolve().parents[2]
        / "bin"
        / "model-lab-supervisor"
    )
    ensure_private_directory(state_root)
    log_path = state_root / "supervisor.log"
    descriptor = os.open(
        log_path,
        os.O_WRONLY
        | os.O_APPEND
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o077
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            raise ModelLabError(
                "supervisor log has an unsafe identity",
                code="unsafe_supervisor_runtime",
            )
        return subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-B",
                str(executable),
                "--root",
                str(authored_root),
                "--state-root",
                str(state_root),
                "--runtime-root",
                str(runtime_root),
            ],
            env=_supervisor_launch_environment(runtime_root),
            stdin=subprocess.DEVNULL,
            stdout=descriptor,
            stderr=descriptor,
            close_fds=True,
            start_new_session=True,
        )
    finally:
        os.close(descriptor)


class SupervisorClient:
    """Typed client; Pi acquisition deliberately keeps its RPC stream open."""

    def __init__(
        self,
        *,
        authored_root: pathlib.Path,
        state_root: pathlib.Path,
        runtime_root: pathlib.Path,
        launcher: SupervisorLauncher = _default_launcher,
        startup_timeout_seconds: float = 10.0,
        clock: Callable[[], datetime.datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.authored_root = authored_root
        self.state_root = state_root
        self.runtime_root = runtime_root
        self.launcher = launcher
        self.startup_timeout_seconds = startup_timeout_seconds
        self.clock = clock
        self.monotonic = monotonic

    @property
    def socket_path(self) -> pathlib.Path:
        return supervisor_socket_path(self.runtime_root)

    def _validate_socket_path(self) -> None:
        try:
            metadata = self.socket_path.lstat()
        except FileNotFoundError as error:
            raise ModelLabError(
                "model-lab supervisor socket is absent",
                code="supervisor_unavailable",
            ) from error
        if (
            not stat.S_ISSOCK(metadata.st_mode)
            or self.socket_path.is_symlink()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            raise ModelLabError(
                "model-lab supervisor socket has an unsafe identity",
                code="unsafe_supervisor_runtime",
            )

    def _connect_once(
        self,
        *,
        deadline: float | None = None,
    ) -> socket.socket:
        self._validate_socket_path()
        connection = socket.socket(
            socket.AF_UNIX,
            socket.SOCK_STREAM | getattr(socket, "SOCK_CLOEXEC", 0),
        )
        try:
            if deadline is not None:
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    raise ModelLabError(
                        "model-lab supervisor connection exceeded its "
                        "absolute deadline",
                        code="service_startup_timeout",
                    )
                connection.settimeout(remaining)
            connection.connect(os.fspath(self.socket_path))
            _, peer_uid, _ = peer_credentials(connection)
            if hasattr(os, "getuid") and peer_uid != os.getuid():
                raise ModelLabError(
                    "model-lab supervisor belongs to a different user",
                    code="supervisor_peer_mismatch",
                )
            connection.settimeout(None)
            return connection
        except BaseException:
            connection.close()
            raise

    def connect(self, *, deadline: float | None = None) -> socket.socket:
        """Connect, starting exactly one convergent daemon when absent."""

        if deadline is None:
            deadline = self.monotonic() + self.startup_timeout_seconds
        try:
            return self._connect_once(deadline=deadline)
        except (ConnectionRefusedError, FileNotFoundError):
            pass
        except TimeoutError as error:
            raise ModelLabError(
                "model-lab supervisor connection exceeded its absolute "
                "deadline",
                code="service_startup_timeout",
            ) from error
        except ModelLabError as error:
            if error.code not in {"supervisor_unavailable"}:
                raise
        if self.monotonic() >= deadline:
            raise ModelLabError(
                "model-lab supervisor connection exceeded its absolute "
                "deadline",
                code="service_startup_timeout",
            )
        process = self.launcher(
            self.authored_root,
            self.state_root,
            self.runtime_root,
        )
        last_error: BaseException | None = None
        while self.monotonic() < deadline:
            try:
                return self._connect_once(deadline=deadline)
            except (ConnectionRefusedError, FileNotFoundError) as error:
                last_error = error
            except TimeoutError as error:
                raise ModelLabError(
                    "model-lab supervisor connection exceeded its absolute "
                    "deadline",
                    code="service_startup_timeout",
                ) from error
            except ModelLabError as error:
                if error.code != "supervisor_unavailable":
                    raise
                last_error = error
            if process is not None:
                return_code = process.poll()
                if return_code not in {None, 0, 2}:
                    raise ModelLabError(
                        "model-lab supervisor exited during startup; inspect "
                        f"{self.state_root / 'supervisor.log'}",
                        code="supervisor_start_failed",
                    )
            time.sleep(min(0.05, max(0.0, deadline - self.monotonic())))
        raise ModelLabError(
            f"model-lab supervisor did not become ready: {last_error}",
            code="service_startup_timeout",
        )

    def _receive_result(
        self,
        connection: socket.socket,
        *,
        operation: str,
        deadline: float | None = None,
        deadline_error_code: str = "supervisor_channel_closed",
    ) -> dict[str, Any]:
        response = receive_document(
            connection,
            deadline=deadline,
            monotonic=self.monotonic,
            deadline_error_code=deadline_error_code,
        )
        if response.get("schema") == SUPERVISOR_ERROR_SCHEMA:
            require_exact_fields(
                response,
                schema=SUPERVISOR_ERROR_SCHEMA,
                fields=frozenset({"code", "message"}),
            )
            if not isinstance(response["code"], str) or not isinstance(
                response["message"], str
            ):
                raise ModelLabError(
                    "supervisor error response is invalid",
                    code="invalid_supervisor_protocol",
                )
            raise ModelLabError(response["message"], code=response["code"])
        require_exact_fields(
            response,
            schema=SUPERVISOR_RESULT_SCHEMA,
            fields=frozenset({"operation", "result"}),
        )
        if response["operation"] != operation or not isinstance(
            response["result"], dict
        ):
            raise ModelLabError(
                "supervisor response does not match its request",
                code="invalid_supervisor_protocol",
            )
        return response["result"]

    def request(
        self,
        operation: str,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        connection = self.connect()
        try:
            send_document(
                connection,
                {
                    "schema": SUPERVISOR_REQUEST_SCHEMA,
                    "operation": operation,
                    **fields,
                },
            )
            return self._receive_result(connection, operation=operation)
        finally:
            connection.close()

    def request_up(
        self,
        *,
        service_id: str,
        host_name: str | None,
        startup_timeout_seconds: int,
    ) -> dict[str, Any]:
        """Ensure one service under the same absolute command deadline."""

        require_identifier(service_id, label="service ID")
        if (
            not isinstance(startup_timeout_seconds, int)
            or isinstance(startup_timeout_seconds, bool)
            or startup_timeout_seconds <= 0
        ):
            raise ModelLabError(
                "service startup timeout must be a positive integer",
                code="invalid_supervisor_protocol",
            )
        expires_at = self.clock() + datetime.timedelta(
            seconds=startup_timeout_seconds
        )
        startup_deadline = self.monotonic() + startup_timeout_seconds
        connection = self.connect(deadline=startup_deadline)
        try:
            send_document(
                connection,
                {
                    "schema": SUPERVISOR_REQUEST_SCHEMA,
                    "operation": "up",
                    "service_id": service_id,
                    "host_name": host_name,
                    "startup_expires_at": format_timestamp(expires_at),
                    "startup_deadline": startup_deadline,
                },
                deadline=startup_deadline,
                monotonic=self.monotonic,
                deadline_error_code="service_startup_timeout",
            )
            result = self._receive_result(
                connection,
                operation="up",
                deadline=startup_deadline,
                deadline_error_code="service_startup_timeout",
            )
            if self.monotonic() >= startup_deadline:
                raise ModelLabError(
                    "service endpoint did not become usable within the "
                    "configured startup budget",
                    code="service_startup_timeout",
                )
            return result
        finally:
            connection.close()

    def acquire_pi(
        self,
        *,
        profile_id: str,
        project_id: str,
        service_id: str,
        service_sha256: str,
        workload_sha256: str,
        required_input_modalities: tuple[str, ...],
        session_id: str | None,
        host_name: str | None,
        stop_on_release: bool,
        startup_timeout_seconds: int = 300,
    ) -> PiLeaseChannel:
        profile_id = require_identifier(profile_id, label="profile ID")
        project_id = require_identifier(project_id, label="project ID")
        service_id = require_identifier(service_id, label="service ID")
        service_sha256 = require_sha256(
            service_sha256,
            label="service document",
        )
        workload_sha256 = require_sha256(
            workload_sha256,
            label="service workload",
        )
        required_input_modalities = require_canonical_input_modalities(
            required_input_modalities,
            label="required input modalities",
        )
        session_id = require_nullable_opaque_identifier(
            session_id,
            label="session ID",
        )
        if (
            not isinstance(startup_timeout_seconds, int)
            or isinstance(startup_timeout_seconds, bool)
            or startup_timeout_seconds <= 0
        ):
            raise ModelLabError(
                "Pi startup timeout must be a positive integer",
                code="invalid_supervisor_protocol",
            )
        expires_at = self.clock() + datetime.timedelta(
            seconds=startup_timeout_seconds
        )
        expires_text = format_timestamp(expires_at)
        startup_deadline = self.monotonic() + startup_timeout_seconds
        connection = self.connect(deadline=startup_deadline)
        try:
            send_document(
                connection,
                {
                    "schema": SUPERVISOR_REQUEST_SCHEMA,
                    "operation": "pi-acquire",
                    "profile_id": profile_id,
                    "project_id": project_id,
                    "service_id": service_id,
                    "service_sha256": service_sha256,
                    "workload_sha256": workload_sha256,
                    "required_input_modalities": list(
                        required_input_modalities
                    ),
                    "session_id": session_id,
                    "host_name": host_name,
                    "stop_on_release": stop_on_release,
                    "startup_expires_at": expires_text,
                    "startup_deadline": startup_deadline,
                },
                deadline=startup_deadline,
                monotonic=self.monotonic,
                deadline_error_code="service_startup_timeout",
            )
            response = receive_document(
                connection,
                deadline=startup_deadline,
                monotonic=self.monotonic,
                deadline_error_code="service_startup_timeout",
            )
            if response.get("schema") == SUPERVISOR_ERROR_SCHEMA:
                require_exact_fields(
                    response,
                    schema=SUPERVISOR_ERROR_SCHEMA,
                    fields=frozenset({"code", "message"}),
                )
                raise ModelLabError(
                    str(response["message"]),
                    code=str(response["code"]),
                )
            require_exact_fields(
                response,
                schema=PI_PENDING_SCHEMA,
                fields=frozenset(
                    {
                        "profile_id",
                        "project_id",
                        "service_id",
                        "service_sha256",
                        "workload_sha256",
                        "required_input_modalities",
                        "session_id",
                        "deployment_id",
                        "use_lease_id",
                    }
                ),
            )
            pending = PendingPiUse(
                profile_id=require_identifier(
                    response["profile_id"],
                    label="pending profile ID",
                ),
                project_id=require_identifier(
                    response["project_id"],
                    label="pending project ID",
                ),
                service_id=require_identifier(
                    response["service_id"],
                    label="pending service ID",
                ),
                service_sha256=require_sha256(
                    response["service_sha256"],
                    label="pending service document",
                ),
                workload_sha256=require_sha256(
                    response["workload_sha256"],
                    label="pending workload",
                ),
                required_input_modalities=(
                    require_canonical_input_modalities(
                        response["required_input_modalities"],
                        label="pending required input modalities",
                    )
                ),
                session_id=require_nullable_opaque_identifier(
                    response["session_id"],
                    label="pending session ID",
                ),
                deployment_id=require_opaque_identifier(
                    response["deployment_id"],
                    label="pending deployment ID",
                ),
                use_lease_id=require_opaque_identifier(
                    response["use_lease_id"],
                    label="pending use lease ID",
                ),
            )
            if (
                pending.profile_id,
                pending.project_id,
                pending.service_id,
                pending.service_sha256,
                pending.workload_sha256,
                pending.required_input_modalities,
                pending.session_id,
            ) != (
                profile_id,
                project_id,
                service_id,
                service_sha256,
                workload_sha256,
                required_input_modalities,
                session_id,
            ):
                raise ModelLabError(
                    "supervisor returned a different exact Pi identity grant",
                    code="invalid_supervisor_protocol",
                )
            if self.monotonic() >= startup_deadline:
                raise ModelLabError(
                    "Pi endpoint did not become usable within the configured "
                    "startup budget",
                    code="service_startup_timeout",
                )
            connection.settimeout(None)
            return PiLeaseChannel(
                pending=pending,
                connection=connection,
                startup_deadline=startup_deadline,
            )
        except BaseException:
            connection.close()
            raise


def subprocess_model_session(
    profile_root: pathlib.Path,
    arguments: Sequence[str],
    channel: PiLeaseChannel,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Pass the connected lease channel to model-session, then relinquish it."""

    executable = pathlib.Path(__file__).resolve().parents[2] / "bin" / "model-session"
    if monotonic() >= channel.startup_deadline:
        channel.close()
        raise ModelLabError(
            "Pi session admission did not complete within the configured "
            "startup budget",
            code="service_startup_timeout",
        )
    source_descriptor = channel.connection.fileno()
    try:
        inherited_descriptor = fcntl.fcntl(
            source_descriptor,
            fcntl.F_DUPFD_CLOEXEC,
            3,
        )
    except OSError as error:
        channel.close()
        raise ModelLabError(
            f"cannot duplicate model-session lease channel: {error}",
            code="session_use_channel_unavailable",
        ) from error
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            [
                str(executable),
                "--model-lab-use-fd",
                str(inherited_descriptor),
                "--model-lab-use-deadline",
                format(channel.startup_deadline, ".17g"),
                "--profile",
                str(profile_root),
                *arguments,
            ],
            close_fds=True,
            pass_fds=(inherited_descriptor,),
        )
    finally:
        os.close(inherited_descriptor)
        channel.close()
    if process is None:
        raise AssertionError("model-session process was not created")
    return process.wait()
