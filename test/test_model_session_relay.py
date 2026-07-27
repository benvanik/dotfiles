from __future__ import annotations

import os
import pathlib
import platform
import socket
import sys
import tempfile
import threading
import unittest

from model_session.relay import (
    LOOPBACK_HOST,
    MODEL_SESSION_BASE_URL,
    MODEL_SESSION_INFERENCE_SOCKET,
    RelayConfigurationError,
    UnixSocketRelay,
    supervise_child,
)


def available_loopback_port() -> int:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind((LOOPBACK_HOST, 0))
        return listener.getsockname()[1]
    finally:
        listener.close()


def receive_all(connection: socket.socket) -> bytes:
    payload = bytearray()
    while True:
        chunk = connection.recv(64 * 1024)
        if not chunk:
            return bytes(payload)
        payload.extend(chunk)


class UnixBackend:
    def __init__(
        self,
        socket_path: pathlib.Path,
        handler,
        *,
        probe_connections: int = 1,
    ):
        self.socket_path = socket_path
        self.handler = handler
        self.probe_connections = probe_connections
        self.errors: list[BaseException] = []
        self.connection_count = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._connections: set[socket.socket] = set()
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(os.fspath(socket_path))
        self._listener.listen()
        self._listener.settimeout(0.1)
        self._thread = threading.Thread(
            target=self._serve,
            name="model-session-test-backend",
            daemon=True,
        )
        self._thread.start()

    def close(self):
        self._stop.set()
        self._listener.close()
        with self._lock:
            connections = tuple(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        self._thread.join(2)
        if self._thread.is_alive():
            raise AssertionError("Unix backend did not stop")

    def assert_clean(self, test_case: unittest.TestCase):
        test_case.assertEqual(self.errors, [])

    def _serve(self):
        connection_count = 0
        while not self._stop.is_set():
            try:
                connection, _address = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                raise
            connection_count += 1
            self.connection_count = connection_count
            with self._lock:
                self._connections.add(connection)
            try:
                if connection_count <= self.probe_connections:
                    receive_all(connection)
                else:
                    self.handler(connection)
            except BaseException as error:
                if not self._stop.is_set():
                    self.errors.append(error)
            finally:
                with self._lock:
                    self._connections.discard(connection)
                connection.close()


class ModelSessionRelayTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)

    def backend(self, handler, *, probe_connections=1):
        backend = UnixBackend(
            self.root / "inference.sock",
            handler,
            probe_connections=probe_connections,
        )
        self.addCleanup(backend.close)
        return backend

    def relay(self, socket_path):
        relay = UnixSocketRelay(socket_path, available_loopback_port())
        relay.start()
        self.addCleanup(relay.close)
        return relay

    def connect(self, relay):
        client = socket.create_connection(
            (LOOPBACK_HOST, relay.listen_port), timeout=2
        )
        client.settimeout(2)
        self.addCleanup(client.close)
        return client

    def test_streaming_and_binary_bytes_are_transparent(self):
        request = (
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Content-Type: application/octet-stream\r\n\r\n"
            + bytes(range(256)) * 257
        )
        response_chunks = (
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n",
            b"data: {\"delta\":\"one\"}\n\n",
            bytes(reversed(range(256))),
            b"\x00\xffdata: [DONE]\n\n",
        )
        received_request: list[bytes] = []

        def handle(connection):
            received_request.append(receive_all(connection))
            for chunk in response_chunks:
                connection.sendall(chunk)
            connection.shutdown(socket.SHUT_WR)

        backend = self.backend(handle)
        relay = self.relay(backend.socket_path)
        client = self.connect(relay)

        client.sendall(request)
        client.shutdown(socket.SHUT_WR)
        response = receive_all(client)

        self.assertEqual(received_request, [request])
        self.assertEqual(response, b"".join(response_chunks))
        relay.raise_if_failed()
        backend.assert_clean(self)

    def test_backend_half_close_does_not_close_client_request_direction(self):
        greeting = b"HTTP/1.1 100 Continue\r\n\r\n"
        request_after_response = b"request bytes after response half-close"
        received = []
        received_event = threading.Event()

        def handle(connection):
            connection.sendall(greeting)
            connection.shutdown(socket.SHUT_WR)
            received.append(receive_all(connection))
            received_event.set()

        backend = self.backend(handle)
        relay = self.relay(backend.socket_path)
        client = self.connect(relay)

        self.assertEqual(receive_all(client), greeting)
        client.sendall(request_after_response)
        client.shutdown(socket.SHUT_WR)

        self.assertTrue(received_event.wait(2))
        self.assertEqual(received, [request_after_response])
        relay.raise_if_failed()
        backend.assert_clean(self)

    def test_invalid_or_unreachable_socket_fails_before_child_launch(self):
        regular_file = self.root / "regular"
        regular_file.write_text("not a socket")
        stale_socket = self.root / "stale.sock"
        stale_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale_listener.bind(os.fspath(stale_socket))
        stale_listener.close()

        for socket_path in (
            pathlib.Path("relative.sock"),
            self.root / "missing.sock",
            regular_file,
            stale_socket,
        ):
            with self.subTest(socket_path=socket_path):
                relay = UnixSocketRelay(
                    socket_path, available_loopback_port()
                )
                with self.assertRaises(RelayConfigurationError):
                    relay.start()

        marker = self.root / "child-launched"
        command = (
            sys.executable,
            "-c",
            (
                "import pathlib; "
                f"pathlib.Path({os.fspath(marker)!r}).write_text('launched')"
            ),
        )
        with self.assertRaises(RelayConfigurationError):
            supervise_child(
                stale_socket, available_loopback_port(), command
            )
        self.assertFalse(marker.exists())

    def test_close_terminates_an_active_connection_and_listener(self):
        backend_started = threading.Event()
        backend_eof = threading.Event()

        def handle(connection):
            backend_started.set()
            receive_all(connection)
            backend_eof.set()

        backend = self.backend(handle)
        relay = UnixSocketRelay(
            backend.socket_path, available_loopback_port()
        )
        relay.start()
        client = self.connect(relay)
        client.sendall(b"open request")
        self.assertTrue(backend_started.wait(2))

        relay.close()

        self.assertTrue(backend_eof.wait(2))
        self.assertFalse(relay.is_running)
        with self.assertRaises(OSError):
            socket.create_connection(
                (LOOPBACK_HOST, relay.listen_port), timeout=0.2
            )
        backend.assert_clean(self)

    def test_supervisor_verifies_version_exports_boundary_and_returns_status(
        self,
    ):
        backend = self.backend(
            lambda connection: receive_all(connection),
            probe_connections=1,
        )
        expected_version = f"Python {platform.python_version()}"
        child_program = (
            "import os, sys; "
            f"assert os.environ[{MODEL_SESSION_BASE_URL!r}].startswith("
            f"'http://{LOOPBACK_HOST}:'); "
            f"assert os.environ[{MODEL_SESSION_BASE_URL!r}].endswith('/v1'); "
            f"assert os.environ[{MODEL_SESSION_INFERENCE_SOCKET!r}] == "
            f"{os.fspath(backend.socket_path)!r}; "
            "sys.exit(17)"
        )

        return_code = supervise_child(
            backend.socket_path,
            available_loopback_port(),
            (sys.executable, "-c", child_program),
            expected_command_version=expected_version,
            environment={"PATH": os.environ["PATH"]},
        )

        self.assertEqual(return_code, 17)
        self.assertEqual(backend.connection_count, 1)
        backend.assert_clean(self)

    def test_supervisor_rejects_version_mismatch_before_interactive_child(self):
        backend = self.backend(
            lambda connection: receive_all(connection),
            probe_connections=1,
        )
        marker = self.root / "child-launched"
        command = (
            sys.executable,
            "-c",
            (
                "import pathlib; "
                f"pathlib.Path({os.fspath(marker)!r}).write_text('launched')"
            ),
        )

        with self.assertRaisesRegex(
            RelayConfigurationError, "version mismatch"
        ):
            supervise_child(
                backend.socket_path,
                available_loopback_port(),
                command,
                expected_command_version="definitely-not-python",
                environment={"PATH": os.environ["PATH"]},
            )
        self.assertFalse(marker.exists())
        backend.assert_clean(self)

    def test_version_probe_has_a_bounded_timeout(self):
        backend = self.backend(
            lambda connection: receive_all(connection),
            probe_connections=1,
        )
        command = self.root / "slow-version"
        command.write_text(
            "#!/usr/bin/python3\n"
            "import sys\n"
            "import time\n"
            "if sys.argv[1:] == ['--version']:\n"
            "    time.sleep(60)\n"
        )
        command.chmod(0o700)

        with self.assertRaisesRegex(
            RelayConfigurationError, "did not finish within"
        ):
            supervise_child(
                backend.socket_path,
                available_loopback_port(),
                (os.fspath(command),),
                expected_command_version="fixture-version",
                command_version_timeout_seconds=0.1,
                shutdown_timeout_seconds=0.5,
                environment={"PATH": os.environ["PATH"]},
            )
        self.assertEqual(backend.connection_count, 1)
        backend.assert_clean(self)

    def test_normal_child_exit_terminates_surviving_process_group(self):
        backend = self.backend(
            lambda connection: receive_all(connection),
            probe_connections=1,
        )
        process_ids = self.root / "process-ids"
        child_program = (
            "import os, pathlib, subprocess; "
            "grandchild = subprocess.Popen(['/usr/bin/sleep', '60']); "
            f"pathlib.Path({os.fspath(process_ids)!r}).write_text("
            "f'{os.getpid()} {grandchild.pid}')"
        )

        return_code = supervise_child(
            backend.socket_path,
            available_loopback_port(),
            (sys.executable, "-c", child_program),
            shutdown_timeout_seconds=1,
            environment={"PATH": os.environ["PATH"]},
        )

        process_group, _grandchild = (
            int(value) for value in process_ids.read_text().split()
        )
        self.assertEqual(return_code, 0)
        with self.assertRaises(ProcessLookupError):
            os.killpg(process_group, 0)
        backend.assert_clean(self)

    def test_supervised_child_uses_private_file_creation_mask(self):
        backend = self.backend(
            lambda connection: receive_all(connection),
            probe_connections=1,
        )
        created_file = self.root / "child-created"
        child_program = (
            "import os; "
            f"descriptor = os.open({os.fspath(created_file)!r}, "
            "os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666); "
            "os.close(descriptor)"
        )

        return_code = supervise_child(
            backend.socket_path,
            available_loopback_port(),
            (sys.executable, "-c", child_program),
            environment={"PATH": os.environ["PATH"]},
        )

        self.assertEqual(return_code, 0)
        self.assertEqual(created_file.stat().st_mode & 0o777, 0o600)
        backend.assert_clean(self)


if __name__ == "__main__":
    unittest.main()
