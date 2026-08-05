"""Explicit command-line surface for benchmarkd administration."""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import NoReturn, TextIO

from .admin import BenchmarkAdmin
from .errors import BenchmarkLockError


class _ParserExit(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


class _ArgumentParser(argparse.ArgumentParser):
    def __init__(
        self,
        *arguments: object,
        output: TextIO | None = None,
        error: TextIO | None = None,
        **keywords: object,
    ) -> None:
        self._output = sys.stdout if output is None else output
        self._error = sys.stderr if error is None else error
        super().__init__(
            *arguments,
            **keywords,
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
    parser = _ArgumentParser(
        prog="benchmark-admin",
        description="explicit root installation and audit for benchmarkd",
        allow_abbrev=False,
        output=output,
        error=error,
    )
    subcommands = parser.add_subparsers(dest="operation", required=True)

    install = subcommands.add_parser(
        "install",
        help="publish one immutable release and activate its socket",
        allow_abbrev=False,
        output=output,
        error=error,
    )
    install.add_argument(
        "--gpu",
        action="append",
        default=[],
        dest="gpu_bdfs",
        metavar="BDF",
        help="benchmark GPU PCI BDF; required and repeatable on first install",
    )
    install.add_argument(
        "--user",
        help="unprivileged user granted access (defaults to SUDO_USER)",
    )

    doctor = subcommands.add_parser(
        "doctor",
        help="audit root ownership, release identity, policy, and live socket",
        allow_abbrev=False,
        output=output,
        error=error,
    )
    doctor.add_argument(
        "--user",
        help=(
            "require this user to belong to the benchmark group (defaults to SUDO_USER)"
        ),
    )

    subcommands.add_parser(
        "uninstall",
        help="remove software while retaining policy and state",
        allow_abbrev=False,
        output=output,
        error=error,
    )
    return parser


def main(
    arguments: Sequence[str] | None = None,
    *,
    source_root: pathlib.Path,
    output: TextIO | None = None,
    error: TextIO | None = None,
    environment: Mapping[str, str] | None = None,
    admin_factory: Callable[..., BenchmarkAdmin] = BenchmarkAdmin,
) -> int:
    """Run the explicit administration CLI."""

    stdout = sys.stdout if output is None else output
    stderr = sys.stderr if error is None else error
    active_environment = os.environ if environment is None else environment
    parser = _argument_parser(output=stdout, error=stderr)
    try:
        parsed = parser.parse_args(
            list(sys.argv[1:] if arguments is None else arguments)
        )
    except _ParserExit as exception:
        return exception.status
    try:
        admin = admin_factory(
            source_root=source_root,
            report=lambda message: print(message, file=stdout, flush=True),
        )
        if parsed.operation == "install":
            user_name = parsed.user or active_environment.get("SUDO_USER")
            if not user_name:
                raise BenchmarkLockError(
                    "install requires --user when SUDO_USER is unavailable",
                    code="benchmark_admin_user_required",
                )
            admin.install(
                gpu_bdfs=tuple(parsed.gpu_bdfs),
                user_name=user_name,
            )
        elif parsed.operation == "doctor":
            admin.doctor(user_name=parsed.user or active_environment.get("SUDO_USER"))
        elif parsed.operation == "uninstall":
            admin.uninstall()
        else:
            raise AssertionError(
                f"unknown administrator operation {parsed.operation!r}"
            )
        return 0
    except BenchmarkLockError as exception:
        print(
            f"benchmark-admin: {exception.code}: {exception}",
            file=stderr,
            flush=True,
        )
        return 1
