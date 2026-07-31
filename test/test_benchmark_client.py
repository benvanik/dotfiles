from __future__ import annotations

import errno
import io
import os
import pathlib
import signal
import subprocess
import tempfile
import unittest
from unittest import mock

from benchmark_lock.client import (
    AGENTS_MD_SNIPPET,
    LEASE_ENVIRONMENT_VARIABLE,
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
                ["--agents-md", "true"],
                2,
                "--agents-md cannot be combined",
            ),
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

    def test_agents_md_is_exact_concise_and_never_connects(self) -> None:
        output = RecordingStream()
        error = RecordingStream()
        with mock.patch("benchmark_lock.client._connect_broker") as connect:
            status = main(
                ["--agents-md"],
                output=output,
                error=error,
                environment={},
            )

        self.assertEqual(status, 0)
        connect.assert_not_called()
        self.assertEqual(output.getvalue(), f"{AGENTS_MD_SNIPPET}\n")
        self.assertEqual(error.getvalue(), "")
        self.assertLessEqual(len(AGENTS_MD_SNIPPET.split()), 85)
        for required_text in (
            "[--label LABEL]",
            "FIFO",
            "exits or crashes",
            "unwrapped host or GPU load",
            "never nest `benchmark-lock`",
            "--status",
        ):
            self.assertIn(required_text, output.getvalue())
        for administrative_text in ("benchmark-admin", "doctor", "install", "sudo"):
            self.assertNotIn(administrative_text, output.getvalue())

    def test_repository_launcher_isolated_from_cwd_and_pythonpath(self) -> None:
        repository_root = pathlib.Path(__file__).resolve().parents[1]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = "/untrusted/python/path"
        with tempfile.TemporaryDirectory() as working_directory:
            result = subprocess.run(
                [repository_root / "bin/benchmark-lock", "--agents-md"],
                cwd=working_directory,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            help_result = subprocess.run(
                [repository_root / "bin/benchmark-lock", "--help"],
                cwd=working_directory,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{AGENTS_MD_SNIPPET}\n")
        self.assertEqual(result.stderr, "")
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn(
            "Broker setup and repair: ~/.dotfiles/bin/benchmark-admin --help",
            help_result.stdout,
        )
        self.assertEqual(help_result.stderr, "")

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

        def restore_signals() -> None:
            call_order.append("signals")

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
                "benchmark_lock.client._restore_exec_signal_dispositions",
                side_effect=restore_signals,
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

        self.assertEqual(call_order, ["aslr", "signals", "exec"])
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

    def test_broker_and_process_control_failures_return_125(self) -> None:
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
                "benchmark_lock.client._restore_exec_signal_dispositions",
                side_effect=BenchmarkLockError(
                    "denied",
                    code="benchmark_signal_control_failed",
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


if __name__ == "__main__":
    unittest.main()
