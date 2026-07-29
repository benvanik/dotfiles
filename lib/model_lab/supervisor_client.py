"""Client and safe autostart path for the boot-local model-lab supervisor."""

from __future__ import annotations

import dataclasses
import fcntl
import os
import pathlib
import socket
import stat
import subprocess
import time
from collections.abc import Callable, Sequence
from typing import Any

from .errors import ModelLabError
from .paths import ensure_private_directory
from .supervisor_protocol import (
    PI_PENDING_SCHEMA,
    SUPERVISOR_ERROR_SCHEMA,
    SUPERVISOR_REQUEST_SCHEMA,
    SUPERVISOR_RESULT_SCHEMA,
    peer_credentials,
    receive_document,
    require_exact_fields,
    require_identifier,
    require_opaque_identifier,
    require_sha256,
    send_document,
    supervisor_socket_path,
)


@dataclasses.dataclass(frozen=True)
class PendingPiUse:
    profile_id: str
    service_id: str
    workload_sha256: str
    deployment_id: str
    use_lease_id: str


@dataclasses.dataclass
class PiLeaseChannel:
    pending: PendingPiUse
    connection: socket.socket

    def close(self) -> None:
        self.connection.close()


SupervisorLauncher = Callable[
    [pathlib.Path, pathlib.Path, pathlib.Path],
    subprocess.Popen[bytes] | None,
]


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
                str(executable),
                "--root",
                str(authored_root),
                "--state-root",
                str(state_root),
                "--runtime-root",
                str(runtime_root),
            ],
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
    ) -> None:
        self.authored_root = authored_root
        self.state_root = state_root
        self.runtime_root = runtime_root
        self.launcher = launcher
        self.startup_timeout_seconds = startup_timeout_seconds

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

    def _connect_once(self) -> socket.socket:
        self._validate_socket_path()
        connection = socket.socket(
            socket.AF_UNIX,
            socket.SOCK_STREAM | getattr(socket, "SOCK_CLOEXEC", 0),
        )
        try:
            connection.connect(os.fspath(self.socket_path))
            _, peer_uid, _ = peer_credentials(connection)
            if hasattr(os, "getuid") and peer_uid != os.getuid():
                raise ModelLabError(
                    "model-lab supervisor belongs to a different user",
                    code="supervisor_peer_mismatch",
                )
            return connection
        except BaseException:
            connection.close()
            raise

    def connect(self) -> socket.socket:
        """Connect, starting exactly one convergent daemon when absent."""

        try:
            return self._connect_once()
        except (ConnectionRefusedError, FileNotFoundError):
            pass
        except ModelLabError as error:
            if error.code not in {"supervisor_unavailable"}:
                raise
        process = self.launcher(
            self.authored_root,
            self.state_root,
            self.runtime_root,
        )
        deadline = time.monotonic() + self.startup_timeout_seconds
        last_error: BaseException | None = None
        while time.monotonic() < deadline:
            try:
                return self._connect_once()
            except (ConnectionRefusedError, FileNotFoundError) as error:
                last_error = error
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
            time.sleep(0.05)
        raise ModelLabError(
            f"model-lab supervisor did not become ready: {last_error}",
            code="supervisor_start_timeout",
        )

    @staticmethod
    def _receive_result(
        connection: socket.socket,
        *,
        operation: str,
    ) -> dict[str, Any]:
        response = receive_document(connection)
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

    def acquire_pi(
        self,
        *,
        profile_id: str,
        host_name: str | None,
        stop_on_release: bool,
    ) -> PiLeaseChannel:
        require_identifier(profile_id, label="profile ID")
        connection = self.connect()
        try:
            send_document(
                connection,
                {
                    "schema": SUPERVISOR_REQUEST_SCHEMA,
                    "operation": "pi-acquire",
                    "profile_id": profile_id,
                    "host_name": host_name,
                    "stop_on_release": stop_on_release,
                },
            )
            response = receive_document(connection)
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
                        "service_id",
                        "workload_sha256",
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
                service_id=require_identifier(
                    response["service_id"],
                    label="pending service ID",
                ),
                workload_sha256=require_sha256(
                    response["workload_sha256"],
                    label="pending workload",
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
            if pending.profile_id != profile_id:
                raise ModelLabError(
                    "supervisor returned a different profile grant",
                    code="invalid_supervisor_protocol",
                )
            return PiLeaseChannel(pending=pending, connection=connection)
        except BaseException:
            connection.close()
            raise


def subprocess_model_session(
    profile_root: pathlib.Path,
    arguments: Sequence[str],
    channel: PiLeaseChannel,
) -> int:
    """Pass the connected lease channel to model-session, then relinquish it."""

    executable = pathlib.Path(__file__).resolve().parents[2] / "bin" / "model-session"
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
