from __future__ import annotations

import contextlib
import json
import os
import pathlib
import socket
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from model_session.errors import ModelSessionError
from model_session.launch_authority import (
    SESSION_USE_ACCEPTED_SCHEMA,
    SESSION_USE_ADMISSION_SCHEMA,
    SESSION_USE_ERROR_SCHEMA,
    attest_workload,
    process_start_time,
    read_session_use_authority,
)


def _canonical(value) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("ascii")


class SessionUseAuthorityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.runtime_root = pathlib.Path(self.temporary.name) / "runtime"
        self.model_lab_runtime = self.runtime_root / "model-lab"
        self.model_lab_runtime.mkdir(parents=True, mode=0o700)
        self.supervisor_path = self.model_lab_runtime / "supervisor.sock"
        self.previous_runtime = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = os.fspath(self.runtime_root)
        self.route = SimpleNamespace(
            profile_id="chat",
            service_id="qwen-chat",
        )

    def tearDown(self) -> None:
        if self.previous_runtime is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = self.previous_runtime
        self.temporary.cleanup()

    def _accepted(self, **overrides):
        value = {
            "schema": SESSION_USE_ACCEPTED_SCHEMA,
            "profile_id": "chat",
            "service_id": "qwen-chat",
            "workload_sha256": "a" * 64,
            "deployment_id": "deployment-one",
            "use_lease_id": "use-one",
            "supervisor_pid": os.getpid(),
            "supervisor_start_time": process_start_time(os.getpid()),
            "session_pid": os.getpid(),
            "session_start_time": process_start_time(os.getpid()),
        }
        value.update(overrides)
        return value

    @contextlib.contextmanager
    def _channel(
        self,
        response: bytes,
        *,
        response_delay_seconds: float = 0.0,
        response_byte_delay_seconds: float = 0.0,
    ):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(os.fspath(self.supervisor_path))
        self.supervisor_path.chmod(0o600)
        listener.listen(1)
        requests: list[dict] = []
        failures: list[BaseException] = []

        def serve() -> None:
            try:
                connection, _address = listener.accept()
                with connection:
                    payload = bytearray()
                    while not payload.endswith(b"\n"):
                        chunk = connection.recv(4096)
                        if not chunk:
                            return
                        payload.extend(chunk)
                    requests.append(json.loads(payload))
                    if response_delay_seconds:
                        time.sleep(response_delay_seconds)
                    if response_byte_delay_seconds:
                        for byte in response:
                            connection.sendall(bytes((byte,)))
                            time.sleep(response_byte_delay_seconds)
                    else:
                        connection.sendall(response)
                    if not response.endswith(b"\n"):
                        connection.shutdown(socket.SHUT_WR)
                    while connection.recv(1):
                        raise AssertionError(
                            "client sent unsupported post-admission bytes"
                        )
            except (BrokenPipeError, ConnectionResetError):
                pass
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=serve)
        thread.start()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(os.fspath(self.supervisor_path))
        descriptor = client.detach()
        try:
            yield descriptor, requests
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
            listener.close()
            thread.join()
            self.supervisor_path.unlink(missing_ok=True)
            if failures:
                raise failures[0]

    def test_canonical_supervisor_connection_admits_exact_session(self) -> None:
        with self._channel(_canonical(self._accepted())) as (descriptor, requests):
            authority = read_session_use_authority(
                descriptor,
                self.route,
                startup_deadline=time.monotonic() + 5,
            )
            self.assertEqual(authority.profile_id, "chat")
            self.assertEqual(authority.service_id, "qwen-chat")
            self.assertEqual(authority.workload_sha256, "a" * 64)
            attest_workload(
                authority,
                service_id="qwen-chat",
                workload_sha256="a" * 64,
            )
            authority.close()

        self.assertEqual(
            requests,
            [
                {
                    "schema": SESSION_USE_ADMISSION_SCHEMA,
                    "profile_id": "chat",
                    "service_id": "qwen-chat",
                    "pid": os.getpid(),
                    "start_time": process_start_time(os.getpid()),
                }
            ],
        )

    def test_missing_descriptor_fails_with_operator_surface(self) -> None:
        with self.assertRaises(ModelSessionError) as caught:
            read_session_use_authority(
                None,
                self.route,
                startup_deadline=time.monotonic() + 5,
            )

        self.assertEqual(
            caught.exception.code,
            "model_lab_use_authority_required",
        )

    def test_pipe_regular_file_and_socketpair_are_not_supervisor(self) -> None:
        read_descriptor, write_descriptor = os.pipe2(os.O_CLOEXEC)
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            path = pathlib.Path(temporary) / "authority.json"
            path.write_bytes(_canonical(self._accepted()))
            regular_descriptor = os.open(path, os.O_RDONLY)
            local, peer = socket.socketpair(
                socket.AF_UNIX,
                socket.SOCK_STREAM,
            )
            local_descriptor = local.detach()
            try:
                for descriptor in (
                    read_descriptor,
                    regular_descriptor,
                    local_descriptor,
                ):
                    with self.subTest(descriptor=descriptor):
                        with self.assertRaises(ModelSessionError) as caught:
                            read_session_use_authority(
                                descriptor,
                                self.route,
                                startup_deadline=time.monotonic() + 5,
                            )
                        self.assertEqual(
                            caught.exception.code,
                            "invalid_model_lab_use_authority",
                        )
            finally:
                os.close(write_descriptor)
                for descriptor in (
                    read_descriptor,
                    regular_descriptor,
                    local_descriptor,
                ):
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                peer.close()

    def test_response_is_strict_canonical_bounded_json(self) -> None:
        canonical = _canonical(self._accepted())
        responses = (
            json.dumps(self._accepted()).encode("ascii") + b"\n",
            canonical[:-1],
            canonical + b"trailing",
            b'{"schema":"one","schema":"two"}\n',
            b"x" * (16 * 1024 + 1),
        )

        for response in responses:
            with self.subTest(size=len(response)):
                with self._channel(response) as (descriptor, _requests):
                    with self.assertRaises(ModelSessionError) as caught:
                        read_session_use_authority(
                            descriptor,
                            self.route,
                            startup_deadline=time.monotonic() + 5,
                        )
                self.assertEqual(
                    caught.exception.code,
                    "invalid_model_lab_use_authority",
                )

    def test_supervisor_error_preserves_its_operator_surface(self) -> None:
        response = {
            "schema": SESSION_USE_ERROR_SCHEMA,
            "code": "service_start_failed",
            "message": "remote vLLM did not become ready",
        }
        with self._channel(_canonical(response)) as (descriptor, _requests):
            with self.assertRaises(ModelSessionError) as caught:
                read_session_use_authority(
                    descriptor,
                    self.route,
                    startup_deadline=time.monotonic() + 5,
                )

        self.assertEqual(caught.exception.code, "service_start_failed")
        self.assertEqual(
            str(caught.exception),
            "remote vLLM did not become ready",
        )

    def test_pending_admission_is_bounded_by_the_original_startup_deadline(
        self,
    ) -> None:
        with self._channel(
            _canonical(self._accepted()),
            response_delay_seconds=0.1,
        ) as (descriptor, _requests):
            with self.assertRaises(ModelSessionError) as caught:
                read_session_use_authority(
                    descriptor,
                    self.route,
                    startup_deadline=time.monotonic() + 0.02,
                )

        self.assertEqual(caught.exception.code, "service_startup_timeout")

    def test_slow_bytes_cannot_reset_the_absolute_admission_deadline(
        self,
    ) -> None:
        started = time.monotonic()
        with self._channel(
            _canonical(self._accepted()),
            response_byte_delay_seconds=0.01,
        ) as (descriptor, _requests):
            with self.assertRaises(ModelSessionError) as caught:
                read_session_use_authority(
                    descriptor,
                    self.route,
                    startup_deadline=started + 0.05,
                )
        elapsed = time.monotonic() - started

        self.assertEqual(caught.exception.code, "service_startup_timeout")
        self.assertLess(elapsed, 0.25)

    def test_accepted_frame_processed_after_deadline_cannot_launch_pi(
        self,
    ) -> None:
        observed_times = iter((0.0, 0.0, 0.0, 2.0))
        with self._channel(_canonical(self._accepted())) as (
            descriptor,
            _requests,
        ):
            with self.assertRaises(ModelSessionError) as caught:
                read_session_use_authority(
                    descriptor,
                    self.route,
                    startup_deadline=1.0,
                    monotonic=lambda: next(observed_times),
                )

        self.assertEqual(caught.exception.code, "service_startup_timeout")

    def test_malformed_supervisor_error_is_not_trusted(self) -> None:
        responses = (
            {
                "schema": SESSION_USE_ERROR_SCHEMA,
                "code": "UPPERCASE",
                "message": "not a valid error code",
            },
            {
                "schema": SESSION_USE_ERROR_SCHEMA,
                "code": "service_start_failed",
                "message": "",
            },
            {
                "schema": SESSION_USE_ERROR_SCHEMA,
                "code": "service_start_failed",
                "message": "failure",
                "extra": True,
            },
        )
        for response in responses:
            with self.subTest(response=response):
                with self._channel(_canonical(response)) as (
                    descriptor,
                    _requests,
                ):
                    with self.assertRaises(ModelSessionError) as caught:
                        read_session_use_authority(
                            descriptor,
                            self.route,
                            startup_deadline=time.monotonic() + 5,
                        )
                self.assertEqual(
                    caught.exception.code,
                    "invalid_model_lab_use_authority",
                )

    def test_profile_and_process_generations_are_exact(self) -> None:
        cases = (
            (
                self._accepted(profile_id="other"),
                "model_lab_use_authority_mismatch",
            ),
            (
                self._accepted(supervisor_pid=os.getpid() + 100000),
                "model_lab_use_authority_parent_mismatch",
            ),
            (
                self._accepted(session_start_time="9999"),
                "model_lab_use_authority_parent_mismatch",
            ),
        )

        for response, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                with self._channel(_canonical(response)) as (descriptor, _requests):
                    with self.assertRaises(ModelSessionError) as caught:
                        read_session_use_authority(
                            descriptor,
                            self.route,
                            startup_deadline=time.monotonic() + 5,
                        )
                self.assertEqual(caught.exception.code, expected_code)

    def test_workload_attestation_rejects_service_or_hash_drift(self) -> None:
        with self._channel(_canonical(self._accepted())) as (descriptor, _requests):
            authority = read_session_use_authority(
                descriptor,
                self.route,
                startup_deadline=time.monotonic() + 5,
            )
            try:
                for service_id, workload_sha256 in (
                    ("other", "a" * 64),
                    ("qwen-chat", "c" * 64),
                ):
                    with self.subTest(service_id=service_id):
                        with self.assertRaises(ModelSessionError) as caught:
                            attest_workload(
                                authority,
                                service_id=service_id,
                                workload_sha256=workload_sha256,
                            )
                        self.assertEqual(
                            caught.exception.code,
                            "model_lab_use_authority_workload_mismatch",
                        )
            finally:
                authority.close()


if __name__ == "__main__":
    unittest.main()
