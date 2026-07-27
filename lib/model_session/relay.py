"""Transparent loopback TCP relay for a pinned Unix inference socket.

The relay is intentionally protocol agnostic. It copies bytes without parsing
HTTP, so streaming responses, SSE framing, and binary request bodies retain
their exact transport representation.
"""

from __future__ import annotations

import argparse
import errno
import os
import pathlib
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Sequence


LOOPBACK_HOST = "127.0.0.1"
COPY_BUFFER_BYTES = 64 * 1024
DEFAULT_CONNECT_TIMEOUT_SECONDS = 2.0
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 5.0
DEFAULT_COMMAND_VERSION_TIMEOUT_SECONDS = 5.0
MODEL_SESSION_BASE_URL = "MODEL_SESSION_BASE_URL"
MODEL_SESSION_INFERENCE_SOCKET = "MODEL_SESSION_INFERENCE_SOCKET"

_EXPECTED_CONNECTION_ERRNOS = frozenset(
    {
        errno.EBADF,
        errno.ECONNABORTED,
        errno.ECONNRESET,
        errno.ENOTCONN,
        errno.EPIPE,
        errno.ESHUTDOWN,
    }
)


class RelayError(RuntimeError):
    """Base class for relay failures."""


class RelayConfigurationError(RelayError):
    """The requested relay boundary cannot be established safely."""


class RelayRuntimeError(RelayError):
    """An established relay failed while serving traffic."""


class RelayShutdownError(RelayError):
    """Relay workers did not stop within the bounded shutdown period."""


@dataclass(eq=False)
class _Connection:
    client: socket.socket
    backend: socket.socket

    def abort(self) -> None:
        _shutdown_and_close(self.client)
        _shutdown_and_close(self.backend)


def _validate_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int):
        raise RelayConfigurationError(
            "listen port must be an integer from 1 through 65535"
        )
    if not 1 <= port <= 65535:
        raise RelayConfigurationError(
            "listen port must be an integer from 1 through 65535"
        )
    return port


def _shutdown(socket_value: socket.socket, direction: int) -> None:
    try:
        socket_value.shutdown(direction)
    except OSError as error:
        if error.errno not in _EXPECTED_CONNECTION_ERRNOS:
            raise


def _shutdown_and_close(socket_value: socket.socket) -> None:
    try:
        socket_value.shutdown(socket.SHUT_RDWR)
    except OSError:
        # Shutdown is only a wakeup mechanism here. The close below owns the
        # resource boundary even when a peer has already torn the socket down.
        pass
    try:
        socket_value.close()
    except OSError:
        pass


def _validated_unix_socket(
    socket_path: pathlib.Path,
    *,
    connect_timeout_seconds: float,
) -> pathlib.Path:
    if not socket_path.is_absolute():
        raise RelayConfigurationError(
            f"inference socket path must be absolute: {socket_path}"
        )
    try:
        metadata = socket_path.lstat()
    except FileNotFoundError as error:
        raise RelayConfigurationError(
            f"inference socket does not exist: {socket_path}"
        ) from error
    except OSError as error:
        raise RelayConfigurationError(
            f"cannot inspect inference socket {socket_path}: {error}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode):
        raise RelayConfigurationError(
            f"inference socket path must not be a symlink: {socket_path}"
        )
    if not stat.S_ISSOCK(metadata.st_mode):
        raise RelayConfigurationError(
            f"inference socket path is not a Unix socket: {socket_path}"
        )

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(connect_timeout_seconds)
        probe.connect(os.fspath(socket_path))
        probe.shutdown(socket.SHUT_WR)
    except OSError as error:
        raise RelayConfigurationError(
            f"cannot connect to inference socket {socket_path}: {error}"
        ) from error
    finally:
        probe.close()
    return socket_path


class UnixSocketRelay:
    """Relay a fixed loopback TCP port to one exact filesystem Unix socket."""

    def __init__(
        self,
        socket_path: pathlib.Path | str,
        listen_port: int,
        *,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        self.socket_path = pathlib.Path(socket_path)
        self.listen_port = _validate_port(listen_port)
        if connect_timeout_seconds <= 0:
            raise RelayConfigurationError(
                "connect timeout must be greater than zero"
            )
        if shutdown_timeout_seconds <= 0:
            raise RelayConfigurationError(
                "shutdown timeout must be greater than zero"
            )
        self.connect_timeout_seconds = connect_timeout_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds

        self._state_lock = threading.Lock()
        self._state = "new"
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._workers: set[threading.Thread] = set()
        self._connections: set[_Connection] = set()
        self._stop_event = threading.Event()
        self._failure_event = threading.Event()
        self._failure: RelayError | None = None
        self._backend_preflight_complete = False

    @property
    def base_url(self) -> str:
        return f"http://{LOOPBACK_HOST}:{self.listen_port}/v1"

    @property
    def failure(self) -> RelayError | None:
        with self._state_lock:
            return self._failure

    @property
    def is_running(self) -> bool:
        with self._state_lock:
            return self._state == "running" and not self._stop_event.is_set()

    def start(self) -> "UnixSocketRelay":
        with self._state_lock:
            if self._state != "new":
                raise RelayConfigurationError(
                    f"relay cannot start from state {self._state!r}"
                )
            self._state = "starting"
            backend_preflight_complete = self._backend_preflight_complete
            self._backend_preflight_complete = False

        listener: socket.socket | None = None
        try:
            if not backend_preflight_complete:
                _validated_unix_socket(
                    self.socket_path,
                    connect_timeout_seconds=self.connect_timeout_seconds,
                )
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((LOOPBACK_HOST, self.listen_port))
            listener.listen()
            listener.settimeout(0.2)
        except RelayError:
            with self._state_lock:
                self._state = "failed"
            if listener is not None:
                listener.close()
            raise
        except OSError as error:
            with self._state_lock:
                self._state = "failed"
            if listener is not None:
                listener.close()
            raise RelayConfigurationError(
                f"cannot listen on {LOOPBACK_HOST}:{self.listen_port}: {error}"
            ) from error

        accept_thread = threading.Thread(
            target=self._accept_connections,
            name=f"model-session-relay-{self.listen_port}",
            daemon=True,
        )
        with self._state_lock:
            self._listener = listener
            self._accept_thread = accept_thread
            self._state = "running"
        accept_thread.start()
        return self

    def validate_backend(self) -> None:
        """Perform the one live-connect preflight consumed by the next start."""

        with self._state_lock:
            if self._state != "new":
                raise RelayConfigurationError(
                    f"relay cannot validate from state {self._state!r}"
                )
        _validated_unix_socket(
            self.socket_path,
            connect_timeout_seconds=self.connect_timeout_seconds,
        )
        with self._state_lock:
            if self._state != "new":
                raise RelayConfigurationError(
                    "relay state changed during backend validation"
                )
            self._backend_preflight_complete = True

    def wait_for_failure(self, timeout: float | None = None) -> bool:
        return self._failure_event.wait(timeout)

    def raise_if_failed(self) -> None:
        failure = self.failure
        if failure is not None:
            raise failure

    def close(self) -> None:
        with self._state_lock:
            if self._state == "closed":
                return
            self._state = "closing"
            self._stop_event.set()
            listener = self._listener
            accept_thread = self._accept_thread
            connections = tuple(self._connections)

        if listener is not None:
            listener.close()
        for connection in connections:
            connection.abort()

        deadline = time.monotonic() + self.shutdown_timeout_seconds
        if (
            accept_thread is not None
            and accept_thread is not threading.current_thread()
        ):
            accept_thread.join(max(0.0, deadline - time.monotonic()))

        while True:
            with self._state_lock:
                workers = tuple(
                    worker
                    for worker in self._workers
                    if worker is not threading.current_thread()
                )
            if not workers:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                names = ", ".join(sorted(worker.name for worker in workers))
                with self._state_lock:
                    self._state = "closed"
                raise RelayShutdownError(
                    f"relay workers did not stop during shutdown: {names}"
                )
            for worker in workers:
                worker.join(remaining)

        with self._state_lock:
            self._state = "closed"

    def __enter__(self) -> "UnixSocketRelay":
        return self.start()

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def _accept_connections(self) -> None:
        while not self._stop_event.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                client, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError as error:
                if self._stop_event.is_set() or error.errno == errno.EBADF:
                    return
                self._fail(
                    RelayRuntimeError(
                        f"loopback relay accept failed: {error}"
                    )
                )
                return

            worker = threading.Thread(
                target=self._serve_connection,
                args=(client,),
                name=f"model-session-connection-{self.listen_port}",
                daemon=True,
            )
            with self._state_lock:
                if self._stop_event.is_set():
                    client.close()
                    return
                self._workers.add(worker)
            worker.start()

    def _serve_connection(self, client: socket.socket) -> None:
        worker = threading.current_thread()
        backend = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection = _Connection(client=client, backend=backend)
        with self._state_lock:
            if self._stop_event.is_set():
                self._workers.discard(worker)
                connection.abort()
                return
            self._connections.add(connection)
        try:
            backend.settimeout(self.connect_timeout_seconds)
            backend.connect(os.fspath(self.socket_path))
            backend.settimeout(None)

            errors: list[OSError] = []
            client_to_backend = threading.Thread(
                target=self._copy_direction,
                args=(client, backend, connection, errors),
                name=f"{worker.name}-request",
                daemon=True,
            )
            backend_to_client = threading.Thread(
                target=self._copy_direction,
                args=(backend, client, connection, errors),
                name=f"{worker.name}-response",
                daemon=True,
            )
            client_to_backend.start()
            backend_to_client.start()
            client_to_backend.join()
            backend_to_client.join()

            unexpected = tuple(
                error
                for error in errors
                if error.errno not in _EXPECTED_CONNECTION_ERRNOS
            )
            if unexpected and not self._stop_event.is_set():
                self._fail(
                    RelayRuntimeError(
                        "inference relay transport failed: "
                        + "; ".join(str(error) for error in unexpected)
                    )
                )
        except OSError as error:
            if not self._stop_event.is_set():
                self._fail(
                    RelayRuntimeError(
                        "cannot connect accepted request to inference socket "
                        f"{self.socket_path}: {error}"
                    )
                )
        finally:
            connection.abort()
            with self._state_lock:
                self._connections.discard(connection)
                self._workers.discard(worker)

    def _copy_direction(
        self,
        source: socket.socket,
        destination: socket.socket,
        connection: _Connection,
        errors: list[OSError],
    ) -> None:
        try:
            while True:
                payload = source.recv(COPY_BUFFER_BYTES)
                if not payload:
                    _shutdown(destination, socket.SHUT_WR)
                    return
                destination.sendall(payload)
        except OSError as error:
            errors.append(error)
            connection.abort()

    def _fail(self, failure: RelayError) -> None:
        with self._state_lock:
            if self._failure is not None or self._state in {
                "closing",
                "closed",
            }:
                return
            self._failure = failure
            self._failure_event.set()
            self._stop_event.set()
            listener = self._listener
            connections = tuple(self._connections)
        if listener is not None:
            listener.close()
        for connection in connections:
            connection.abort()


def _signal_process_group(process_group: int, signal_number: int) -> None:
    try:
        os.killpg(process_group, signal_number)
    except ProcessLookupError:
        return


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    return True


def _stop_process_group(
    child: subprocess.Popen[bytes],
    *,
    timeout_seconds: float,
) -> None:
    process_group = child.pid
    if _process_group_exists(process_group):
        _signal_process_group(process_group, signal.SIGTERM)
    deadline = time.monotonic() + timeout_seconds
    while _process_group_exists(process_group) and time.monotonic() < deadline:
        if child.poll() is None:
            try:
                child.wait(timeout=min(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
        else:
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    if _process_group_exists(process_group):
        _signal_process_group(process_group, signal.SIGKILL)
    if child.poll() is None:
        child.wait()
    kill_deadline = time.monotonic() + timeout_seconds
    while (
        _process_group_exists(process_group)
        and time.monotonic() < kill_deadline
    ):
        time.sleep(min(0.05, max(0.0, kill_deadline - time.monotonic())))
    if _process_group_exists(process_group):
        raise RelayShutdownError(
            f"child process group {process_group} survived SIGKILL"
        )


def _shell_exit_code(return_code: int) -> int:
    if return_code >= 0:
        return return_code
    return 128 - return_code


def supervise_child(
    socket_path: pathlib.Path | str,
    listen_port: int,
    command: Sequence[str],
    *,
    expected_command_version: str | None = None,
    command_version_timeout_seconds: float = (
        DEFAULT_COMMAND_VERSION_TIMEOUT_SECONDS
    ),
    shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    environment: dict[str, str] | None = None,
) -> int:
    """Run one child behind the relay and return its shell-compatible status."""

    if not command:
        raise RelayConfigurationError("a child command is required")
    relay = UnixSocketRelay(
        socket_path,
        listen_port,
        shutdown_timeout_seconds=shutdown_timeout_seconds,
    )
    child_environment = dict(os.environ if environment is None else environment)
    child_environment[MODEL_SESSION_BASE_URL] = relay.base_url
    child_environment[MODEL_SESSION_INFERENCE_SOCKET] = os.fspath(
        relay.socket_path
    )
    relay.validate_backend()
    if expected_command_version is not None:
        _verify_command_version(
            command[0],
            expected_command_version,
            environment=child_environment,
            timeout_seconds=command_version_timeout_seconds,
            shutdown_timeout_seconds=shutdown_timeout_seconds,
        )
    relay.start()

    try:
        child = subprocess.Popen(
            tuple(command),
            env=child_environment,
            start_new_session=True,
            umask=0o077,
        )
    except OSError as error:
        relay.close()
        raise RelayRuntimeError(
            f"cannot launch model-session child {command[0]!r}: {error}"
        ) from error

    previous_handlers: dict[int, signal.Handlers] = {}

    def forward_signal(signal_number: int, _frame: object) -> None:
        _signal_process_group(child.pid, signal_number)

    try:
        forwarded_signals = tuple(
            signal_number
            for name in (
                "SIGINT",
                "SIGTERM",
                "SIGHUP",
                "SIGQUIT",
                "SIGWINCH",
            )
            if (signal_number := getattr(signal, name, None)) is not None
        )
        for signal_number in forwarded_signals:
            previous_handlers[signal_number] = signal.signal(
                signal_number, forward_signal
            )

        relay_failure: RelayError | None = None
        while True:
            return_code = child.poll()
            if return_code is not None:
                break
            if relay.wait_for_failure(0.1):
                relay_failure = relay.failure
                _stop_process_group(
                    child, timeout_seconds=shutdown_timeout_seconds
                )
                break

        if relay_failure is not None:
            raise relay_failure
        _stop_process_group(child, timeout_seconds=shutdown_timeout_seconds)
        return _shell_exit_code(return_code)
    finally:
        for signal_number, previous_handler in previous_handlers.items():
            signal.signal(signal_number, previous_handler)
        if child.poll() is None:
            _stop_process_group(
                child, timeout_seconds=shutdown_timeout_seconds
            )
        relay.close()


def _verify_command_version(
    command: str,
    expected_version: str,
    *,
    environment: dict[str, str],
    timeout_seconds: float,
    shutdown_timeout_seconds: float,
) -> None:
    normalized_expected = expected_version.strip()
    if not normalized_expected or normalized_expected != expected_version:
        raise RelayConfigurationError(
            "expected command version must be a non-empty normalized string"
        )
    if timeout_seconds <= 0:
        raise RelayConfigurationError(
            "command version timeout must be greater than zero"
        )
    try:
        version_process = subprocess.Popen(
            (command, "--version"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            start_new_session=True,
            umask=0o077,
        )
    except OSError as error:
        raise RelayConfigurationError(
            f"cannot execute {command!r} for version verification: {error}"
        ) from error
    try:
        stdout, _stderr = version_process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        _stop_process_group(
            version_process, timeout_seconds=shutdown_timeout_seconds
        )
        version_process.communicate()
        raise RelayConfigurationError(
            f"{command!r} --version did not finish within "
            f"{timeout_seconds:g} seconds"
        ) from error
    _stop_process_group(
        version_process, timeout_seconds=shutdown_timeout_seconds
    )
    if version_process.returncode != 0:
        raise RelayConfigurationError(
            f"{command!r} --version exited with status "
            f"{_shell_exit_code(version_process.returncode)}"
        )
    try:
        actual_version = stdout.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise RelayConfigurationError(
            f"{command!r} --version did not emit UTF-8"
        ) from error
    if actual_version != normalized_expected:
        raise RelayConfigurationError(
            f"{command!r} version mismatch: expected "
            f"{normalized_expected!r}, got {actual_version!r}"
        )


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "relay one fixed loopback TCP port to an exact Unix inference "
            "socket and supervise a child process"
        )
    )
    parser.add_argument(
        "--socket",
        required=True,
        type=pathlib.Path,
        help="absolute filesystem Unix socket exposed inside the sandbox",
    )
    parser.add_argument(
        "--listen-port",
        required=True,
        type=int,
        help="required loopback TCP port from 1 through 65535",
    )
    parser.add_argument(
        "--expected-command-version",
        help=(
            "require exact normalized stdout from COMMAND[0] --version before "
            "launch"
        ),
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="child command, conventionally separated with --",
    )
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parsed = _argument_parser().parse_args(arguments)
    command = tuple(parsed.command)
    if command and command[0] == "--":
        command = command[1:]
    try:
        return supervise_child(
            parsed.socket,
            parsed.listen_port,
            command,
            expected_command_version=parsed.expected_command_version,
        )
    except RelayError as error:
        print(f"model-session relay: {error}", file=sys.stderr)
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
