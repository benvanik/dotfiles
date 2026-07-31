"""Unprivileged benchmark-lock client and exec-in-place boundary."""

from __future__ import annotations

import argparse
import errno
import os
import pathlib
import re
import signal
import socket
import sys
from collections.abc import Mapping, Sequence
from typing import NoReturn, TextIO

from .control_channel import connect_broker
from .errors import BenchmarkLockError
from .linux import disable_aslr_for_exec
from .protocol import (
    AcquireRequest,
    ErrorEvent,
    GrantedEvent,
    QueuedEvent,
    StatusEvent,
    StatusRequest,
    WaitingEvent,
    encode_request,
    receive_event,
    send_request,
)


LEASE_ENVIRONMENT_VARIABLE = "BENCHMARK_LOCK_LEASE_ID"

AGENTS_MD_SNIPPET = """\
## Benchmark runs

Run every benchmark as
`~/.dotfiles/bin/benchmark-lock [--label LABEL] -- COMMAND [ARG ...]`.
The wrapper waits in FIFO order, applies benchmark policy, and releases its
lease automatically when the foreground process exits or crashes. The lease
serializes cooperating wrappers; unwrapped host or GPU load can still invalidate
a measurement. Wrap the outermost command once; never nest `benchmark-lock`.
Never run benchmark work outside the wrapper.
Use `~/.dotfiles/bin/benchmark-lock --status` to inspect the holder and queue."""

_DEFAULT_LABEL_CHARACTER = re.compile(r"[A-Za-z0-9._:+/@=-]")
_DEFAULT_LABEL_FIRST_CHARACTER = re.compile(r"[A-Za-z0-9]")
_EXEC_NOT_FOUND = frozenset({errno.ENOENT, errno.ENOTDIR})
_EXEC_NOT_INVOKABLE = frozenset(
    {
        errno.EACCES,
        errno.EISDIR,
        errno.ENOEXEC,
        errno.EPERM,
        errno.ETXTBSY,
    }
)
_PYTHON_IGNORED_EXEC_SIGNALS = tuple(
    getattr(signal, name)
    for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ")
    if hasattr(signal, name)
)


class _ParserExit(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


class _ArgumentParser(argparse.ArgumentParser):
    """Argument parser whose output belongs to the caller-provided streams."""

    def __init__(self, *, output: TextIO, error: TextIO) -> None:
        self._output = output
        self._error = error
        super().__init__(
            prog="benchmark-lock",
            description=(
                "run one foreground command while holding the machine benchmark lease"
            ),
            epilog=("Broker setup and repair: ~/.dotfiles/bin/benchmark-admin --help"),
            allow_abbrev=False,
        )

    def _print_message(
        self,
        message: str,
        file: TextIO | None = None,
    ) -> None:
        if not message:
            return
        destination = self._output if file is sys.stdout else self._error
        destination.write(message)
        destination.flush()

    def exit(
        self,
        status: int = 0,
        message: str | None = None,
    ) -> NoReturn:
        if message:
            self._print_message(
                message,
                self._output if status == 0 else self._error,
            )
        raise _ParserExit(status)


def _argument_parser(*, output: TextIO, error: TextIO) -> _ArgumentParser:
    parser = _ArgumentParser(output=output, error=error)
    parser.add_argument(
        "--label",
        help="short printable holder label shown to queued users",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="show broker state without acquiring a lease",
    )
    parser.add_argument(
        "--agents-md",
        action="store_true",
        help="print a concise AGENTS.md usage contract and exit",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        metavar="COMMAND",
        help="foreground command and its arguments",
    )
    return parser


def _report(
    stream: TextIO,
    message: str,
) -> None:
    print(message, file=stream, flush=True)


# Kept as the command seam used by focused client tests.
_connect_broker = connect_broker


def _default_label(executable: str) -> str:
    basename = pathlib.PurePath(executable.rstrip("/")).name
    if not basename:
        return "benchmark"
    normalized = "".join(
        character
        if ord(character) < 128 and _DEFAULT_LABEL_CHARACTER.fullmatch(character)
        else "-"
        for character in basename
    )
    if not normalized or not _DEFAULT_LABEL_FIRST_CHARACTER.fullmatch(normalized[0]):
        normalized = f"command-{normalized}"
    return normalized[:128]


def _format_duration(seconds: int) -> str:
    minutes, remaining = divmod(seconds, 60)
    if minutes:
        return f"{minutes}m {remaining}s"
    return f"{remaining}s"


def _render_wait(
    event: QueuedEvent | WaitingEvent,
    *,
    error: TextIO,
) -> None:
    if event.active is None:
        holder = "no active holder"
    else:
        holder = (
            f"held by pid {event.active.pid} ({event.active.label}) for "
            f"{_format_duration(event.active.elapsed_seconds)}"
        )
    _report(
        error,
        f"benchmark-lock: waiting for lock, {holder}; queue position {event.position}",
    )


def _render_status(event: StatusEvent, *, output: TextIO) -> None:
    if event.active is None:
        holder = "idle"
    else:
        holder = (
            f"active pid {event.active.pid} ({event.active.label}) for "
            f"{_format_duration(event.active.elapsed_seconds)}"
        )
    _report(
        output,
        f"benchmark-lock: {holder}; policy {event.policy_state}; "
        f"queued {event.queue_depth}",
    )


def _render_broker_error(event: ErrorEvent, *, error: TextIO) -> int:
    _report(
        error,
        f"benchmark-lock: {event.code}: {event.message}",
    )
    return 125


def _exec_failure_status(error: OSError) -> int:
    if error.errno in _EXEC_NOT_FOUND:
        return 127
    if error.errno in _EXEC_NOT_INVOKABLE:
        return 126
    return 125


def _restore_exec_signal_dispositions() -> None:
    """Remove Python's inherited signal changes before replacing the client."""

    try:
        for signal_number in _PYTHON_IGNORED_EXEC_SIGNALS:
            signal.signal(signal_number, signal.SIG_DFL)
    except (OSError, ValueError) as error:
        raise BenchmarkLockError(
            f"cannot restore benchmark command signal dispositions: {error}",
            code="benchmark_signal_control_failed",
        ) from error


def _exec_command(
    command: tuple[str, ...],
    *,
    lease_id: str,
    environment: Mapping[str, str],
    error: TextIO,
) -> int:
    child_environment = dict(environment)
    child_environment[LEASE_ENVIRONMENT_VARIABLE] = lease_id
    try:
        os.execvpe(command[0], list(command), child_environment)
    except OSError as exception:
        status = _exec_failure_status(exception)
        _report(
            error,
            f"benchmark-lock: cannot execute {command[0]!r}: {exception}",
        )
        return status
    except ValueError as exception:
        _report(
            error,
            f"benchmark-lock: cannot execute {command[0]!r}: {exception}",
        )
        return 126
    raise BenchmarkLockError(
        "execvpe returned without replacing the benchmark client",
        code="benchmark_exec_failed",
    )


def _run_status(
    connection: socket.socket,
    *,
    output: TextIO,
    error: TextIO,
) -> int:
    send_request(connection, StatusRequest())
    event = receive_event(connection)
    if isinstance(event, ErrorEvent):
        return _render_broker_error(event, error=error)
    if not isinstance(event, StatusEvent):
        raise BenchmarkLockError(
            "broker returned a non-status event for a status request",
            code="invalid_benchmark_protocol",
        )
    _render_status(event, output=output)
    return 0


def _run_acquire(
    connection: socket.socket,
    *,
    command: tuple[str, ...],
    label: str,
    environment: Mapping[str, str],
    error: TextIO,
) -> int:
    inherited_lease_id = environment.get(LEASE_ENVIRONMENT_VARIABLE)
    request = AcquireRequest(
        label=label,
        inherited_lease_id=inherited_lease_id,
    )
    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    observed_lease_id: str | None = None
    try:
        send_request(connection, request)
        while True:
            event = receive_event(connection)
            if isinstance(event, ErrorEvent):
                return _render_broker_error(event, error=error)
            if isinstance(event, QueuedEvent):
                if (
                    observed_lease_id is not None
                    and event.lease_id != observed_lease_id
                ):
                    raise BenchmarkLockError(
                        "broker changed lease identity while queued",
                        code="invalid_benchmark_protocol",
                    )
                observed_lease_id = event.lease_id
                if event.active is not None or event.position != 1:
                    _render_wait(event, error=error)
                continue
            if isinstance(event, WaitingEvent):
                if observed_lease_id is None or event.lease_id != observed_lease_id:
                    raise BenchmarkLockError(
                        "broker sent waiting state for an unknown lease",
                        code="invalid_benchmark_protocol",
                    )
                _render_wait(event, error=error)
                continue
            if isinstance(event, GrantedEvent):
                if observed_lease_id is None or event.lease_id != observed_lease_id:
                    raise BenchmarkLockError(
                        "broker granted an unknown lease identity",
                        code="invalid_benchmark_protocol",
                    )
                disable_aslr_for_exec()
                _restore_exec_signal_dispositions()
                return _exec_command(
                    command,
                    lease_id=event.lease_id,
                    environment=environment,
                    error=error,
                )
            raise BenchmarkLockError(
                "broker returned an event invalid for lease acquisition",
                code="invalid_benchmark_protocol",
            )
    finally:
        signal.signal(signal.SIGINT, previous_sigint)


def main(
    arguments: Sequence[str] | None = None,
    *,
    output: TextIO | None = None,
    error: TextIO | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Run the client, returning only when no command was successfully execed."""

    stdout = sys.stdout if output is None else output
    stderr = sys.stderr if error is None else error
    parser = _argument_parser(output=stdout, error=stderr)
    try:
        parsed = parser.parse_args(
            list(sys.argv[1:] if arguments is None else arguments)
        )
        command = tuple(parsed.command)
        if command[:1] == ("--",):
            command = command[1:]
        if parsed.agents_md:
            if parsed.status or parsed.label is not None or command:
                parser.error(
                    "--agents-md cannot be combined with --status, --label, "
                    "or a command"
                )
            _report(stdout, AGENTS_MD_SNIPPET)
            return 0
        if parsed.status:
            if parsed.label is not None or command:
                parser.error("--status cannot be combined with a command or --label")
        elif not command:
            parser.error("a foreground COMMAND is required")
        label = parsed.label
        if not parsed.status:
            label = _default_label(command[0]) if label is None else label
            try:
                encode_request(AcquireRequest(label=label))
            except BenchmarkLockError as exception:
                parser.error(str(exception))
    except _ParserExit as exception:
        return exception.status

    active_environment = os.environ if environment is None else environment
    connection: socket.socket | None = None
    try:
        connection = _connect_broker()
        if parsed.status:
            return _run_status(
                connection,
                output=stdout,
                error=stderr,
            )
        if label is None:
            raise AssertionError("acquire argument parsing lost its label")
        return _run_acquire(
            connection,
            command=command,
            label=label,
            environment=active_environment,
            error=stderr,
        )
    except BenchmarkLockError as exception:
        _report(
            stderr,
            f"benchmark-lock: {exception.code}: {exception}",
        )
        return 125
    finally:
        if connection is not None:
            connection.close()
