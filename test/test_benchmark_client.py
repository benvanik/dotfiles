from __future__ import annotations

import errno
import io
import os
import signal
import socket
import unittest
from types import SimpleNamespace
from unittest import mock

from benchmark_lock.client import (
    LEASE_ENVIRONMENT_VARIABLE,
    _connect_broker,
    main,
)
from benchmark_lock.errors import BenchmarkLockError
from benchmark_lock.protocol import (
    ActiveLease,
    ErrorEvent,
    GrantedEvent,
    QueuedEvent,
    StatusEvent,
    StatusRequest,
    WaitingEvent,
)


LEASE_ID = "a" * 32


class RecordingStream(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flush_count = 0

    def flush(self) -> None:
        self.flush_count += 1
        super().flush()


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class ExecIntercept(BaseException):
    pass


class BenchmarkClientTest(unittest.TestCase):
    def test_help_and_usage_never_connect(self) -> None:
        cases = (
            (["--help"], 0, "usage: benchmark-lock"),
            ([], 2, "a foreground COMMAND is required"),
            (
                ["--status", "--label", "invalid"],
                2,
                "--status cannot be combined",
            ),
            (["--label", "line\nbreak", "true"], 2, "label is invalid"),
        )
        for arguments, expected_status, expected_text in cases:
            with self.subTest(arguments=arguments):
                output = RecordingStream()
                error = RecordingStream()
                with mock.patch("benchmark_lock.client._connect_broker") as connect:
                    status = main(
                        arguments,
                        output=output,
                        error=error,
                        environment={},
                    )
                self.assertEqual(status, expected_status)
                connect.assert_not_called()
                self.assertIn(
                    expected_text,
                    output.getvalue() + error.getvalue(),
                )

    def test_status_is_allowed_inside_an_active_lease(self) -> None:
        connection = FakeConnection()
        output = RecordingStream()
        error = RecordingStream()
        active = ActiveLease(
            lease_id=LEASE_ID,
            pid=1234,
            uid=1000,
            label="kernel",
            elapsed_seconds=67,
        )
        with (
            mock.patch(
                "benchmark_lock.client._connect_broker",
                return_value=connection,
            ),
            mock.patch("benchmark_lock.client.send_request") as send,
            mock.patch(
                "benchmark_lock.client.receive_event",
                return_value=StatusEvent(active, 2, "held"),
            ),
        ):
            status = main(
                ["--status"],
                output=output,
                error=error,
                environment={LEASE_ENVIRONMENT_VARIABLE: LEASE_ID},
            )

        self.assertEqual(status, 0)
        send.assert_called_once_with(connection, StatusRequest())
        self.assertIn("active pid 1234 (kernel) for 1m 7s", output.getvalue())
        self.assertIn("policy held; queued 2", output.getvalue())
        self.assertGreater(output.flush_count, 0)
        self.assertTrue(connection.closed)

    def test_wait_then_grant_execs_exact_argv_and_augmented_environment(
        self,
    ) -> None:
        connection = FakeConnection()
        error = RecordingStream()
        original_environment = {
            "PATH": "/exact/path",
            "PRESERVED": "yes",
        }
        active = ActiveLease(
            lease_id="b" * 32,
            pid=77,
            uid=1000,
            label="other kernel",
            elapsed_seconds=125,
        )
        events = (
            QueuedEvent(LEASE_ID, 2, active),
            WaitingEvent(LEASE_ID, 1, active),
            GrantedEvent(LEASE_ID, "performance"),
        )
        observed: dict[str, object] = {}
        call_order: list[str] = []
        prior_sigint = signal.getsignal(signal.SIGINT)
        original_cwd = os.getcwd()

        def disable_aslr() -> int:
            call_order.append("aslr")
            return 0

        def execvpe(
            executable: str,
            arguments: list[str],
            environment: dict[str, str],
        ) -> None:
            call_order.append("exec")
            observed["executable"] = executable
            observed["arguments"] = arguments
            observed["environment"] = environment
            observed["cwd"] = os.getcwd()
            observed["sigint"] = signal.getsignal(signal.SIGINT)
            observed["connection_closed"] = connection.closed
            raise ExecIntercept()

        with (
            mock.patch(
                "benchmark_lock.client._connect_broker",
                return_value=connection,
            ),
            mock.patch("benchmark_lock.client.send_request") as send,
            mock.patch(
                "benchmark_lock.client.receive_event",
                side_effect=events,
            ),
            mock.patch(
                "benchmark_lock.client.disable_aslr_for_exec",
                side_effect=disable_aslr,
            ),
            mock.patch(
                "benchmark_lock.client.os.execvpe",
                side_effect=execvpe,
            ),
        ):
            with self.assertRaises(ExecIntercept):
                main(
                    ["--label", "kernel run", "tool", "--flag", "value"],
                    output=RecordingStream(),
                    error=error,
                    environment=original_environment,
                )

        self.assertEqual(call_order, ["aslr", "exec"])
        self.assertEqual(observed["executable"], "tool")
        self.assertEqual(
            observed["arguments"],
            ["tool", "--flag", "value"],
        )
        self.assertEqual(
            observed["environment"],
            {
                "PATH": "/exact/path",
                "PRESERVED": "yes",
                LEASE_ENVIRONMENT_VARIABLE: LEASE_ID,
            },
        )
        self.assertEqual(
            original_environment,
            {"PATH": "/exact/path", "PRESERVED": "yes"},
        )
        self.assertEqual(observed["cwd"], original_cwd)
        self.assertIs(observed["sigint"], signal.SIG_DFL)
        self.assertFalse(observed["connection_closed"])
        self.assertIs(signal.getsignal(signal.SIGINT), prior_sigint)
        self.assertTrue(connection.closed)
        request = send.call_args.args[1]
        self.assertEqual(request.label, "kernel run")
        self.assertIsNone(request.inherited_lease_id)
        rendered = error.getvalue()
        self.assertIn(
            "held by pid 77 (other kernel) for 2m 5s; queue position 2",
            rendered,
        )
        self.assertIn("queue position 1", rendered)
        self.assertGreaterEqual(error.flush_count, 2)

    def test_acquire_sends_inherited_lease_for_nested_detection(self) -> None:
        connection = FakeConnection()
        with (
            mock.patch(
                "benchmark_lock.client._connect_broker",
                return_value=connection,
            ),
            mock.patch("benchmark_lock.client.send_request") as send,
            mock.patch(
                "benchmark_lock.client.receive_event",
                return_value=ErrorEvent(
                    "nested_lease",
                    "Nested benchmark lease refused.",
                ),
            ),
        ):
            status = main(
                ["true"],
                output=RecordingStream(),
                error=RecordingStream(),
                environment={LEASE_ENVIRONMENT_VARIABLE: LEASE_ID},
            )

        self.assertEqual(status, 125)
        request = send.call_args.args[1]
        self.assertEqual(request.inherited_lease_id, LEASE_ID)

    def test_broker_and_personality_failures_return_125(self) -> None:
        error = RecordingStream()
        with mock.patch(
            "benchmark_lock.client._connect_broker",
            side_effect=BenchmarkLockError(
                "offline",
                code="benchmark_broker_unavailable",
            ),
        ):
            self.assertEqual(
                main(
                    ["true"],
                    output=RecordingStream(),
                    error=error,
                    environment={},
                ),
                125,
            )
        self.assertIn("benchmark_broker_unavailable: offline", error.getvalue())

        connection = FakeConnection()
        with (
            mock.patch(
                "benchmark_lock.client._connect_broker",
                return_value=connection,
            ),
            mock.patch("benchmark_lock.client.send_request"),
            mock.patch(
                "benchmark_lock.client.receive_event",
                side_effect=(
                    QueuedEvent(LEASE_ID, 1, None),
                    GrantedEvent(LEASE_ID, "performance"),
                ),
            ),
            mock.patch(
                "benchmark_lock.client.disable_aslr_for_exec",
                side_effect=BenchmarkLockError(
                    "denied",
                    code="benchmark_aslr_control_failed",
                ),
            ),
            mock.patch("benchmark_lock.client.os.execvpe") as execute,
        ):
            self.assertEqual(
                main(
                    ["true"],
                    output=RecordingStream(),
                    error=RecordingStream(),
                    environment={},
                ),
                125,
            )
        execute.assert_not_called()
        self.assertTrue(connection.closed)

    def test_exec_failures_have_shell_compatible_status(self) -> None:
        failures = (
            (FileNotFoundError(errno.ENOENT, "missing"), 127),
            (NotADirectoryError(errno.ENOTDIR, "not a directory"), 127),
            (PermissionError(errno.EACCES, "denied"), 126),
            (OSError(errno.ENOEXEC, "invalid executable"), 126),
            (OSError(errno.EIO, "I/O failure"), 125),
            (ValueError("embedded null byte"), 126),
        )
        for failure, expected_status in failures:
            with self.subTest(failure=failure):
                connection = FakeConnection()
                with (
                    mock.patch(
                        "benchmark_lock.client._connect_broker",
                        return_value=connection,
                    ),
                    mock.patch("benchmark_lock.client.send_request"),
                    mock.patch(
                        "benchmark_lock.client.receive_event",
                        side_effect=(
                            QueuedEvent(LEASE_ID, 1, None),
                            GrantedEvent(LEASE_ID, "performance"),
                        ),
                    ),
                    mock.patch(
                        "benchmark_lock.client.disable_aslr_for_exec",
                        return_value=0,
                    ),
                    mock.patch(
                        "benchmark_lock.client.os.execvpe",
                        side_effect=failure,
                    ),
                ):
                    status = main(
                        ["command"],
                        output=RecordingStream(),
                        error=RecordingStream(),
                        environment={"PATH": "/bin"},
                    )
                self.assertEqual(status, expected_status)
                self.assertTrue(connection.closed)

    def test_broker_protocol_error_event_is_flushed(self) -> None:
        connection = FakeConnection()
        error = RecordingStream()
        with (
            mock.patch(
                "benchmark_lock.client._connect_broker",
                return_value=connection,
            ),
            mock.patch("benchmark_lock.client.send_request"),
            mock.patch(
                "benchmark_lock.client.receive_event",
                return_value=ErrorEvent(
                    "policy_failed",
                    "Policy did not verify.",
                ),
            ),
        ):
            status = main(
                ["true"],
                output=RecordingStream(),
                error=error,
                environment={},
            )
        self.assertEqual(status, 125)
        self.assertIn(
            "benchmark-lock: policy_failed: Policy did not verify.",
            error.getvalue(),
        )
        self.assertGreater(error.flush_count, 0)

    def test_connect_uses_validated_root_seqpacket_endpoint(self) -> None:
        connection = mock.Mock()
        group = SimpleNamespace(gr_gid=742)
        with (
            mock.patch(
                "benchmark_lock.client.grp.getgrnam",
                return_value=group,
            ) as get_group,
            mock.patch("benchmark_lock.client.validate_root_socket_path") as validate,
            mock.patch("benchmark_lock.client.require_root_peer") as require_root,
            mock.patch(
                "benchmark_lock.client.socket.socket",
                return_value=connection,
            ) as socket_factory,
        ):
            self.assertIs(_connect_broker(), connection)

        get_group.assert_called_once_with("benchmark")
        validate.assert_called_once()
        self.assertEqual(
            validate.call_args.kwargs["expected_group_id"],
            742,
        )
        socket_factory.assert_called_once_with(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET | getattr(socket, "SOCK_CLOEXEC", 0),
        )
        connection.set_inheritable.assert_called_once_with(False)
        connection.connect.assert_called_once()
        require_root.assert_called_once_with(connection)


if __name__ == "__main__":
    unittest.main()
