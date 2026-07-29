"""Foreground entry point for the boot-local model-lab supervisor."""

from __future__ import annotations

import argparse
import signal
import sys
from collections.abc import Sequence
from typing import Any

from .configuration import load_lab_configuration
from .errors import ModelLabError
from .paths import authored_root, runtime_root, state_root
from .supervisor import ModelLabSupervisor, SupervisorFailure


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="model-lab-supervisor",
        description="Own model-lab services, claims, leases, and idle retirement.",
        allow_abbrev=False,
    )
    parser.add_argument("--root")
    parser.add_argument("--state-root")
    parser.add_argument("--runtime-root")
    return parser


def main(
    arguments: Sequence[str] | None = None,
    *,
    output: Any = None,
    error: Any = None,
) -> int:
    stdout = sys.stdout if output is None else output
    stderr = sys.stderr if error is None else error
    parsed = _parser().parse_args(
        list(sys.argv[1:] if arguments is None else arguments)
    )
    root = authored_root(parsed.root)
    machine_state = state_root(parsed.state_root)
    boot_runtime = runtime_root(parsed.runtime_root)
    try:
        canonical_runtime = runtime_root()
        if boot_runtime != canonical_runtime:
            raise ModelLabError(
                "supervisor runtime must be the canonical "
                "$XDG_RUNTIME_DIR/model-lab path",
                code="noncanonical_supervisor_runtime",
            )
        from .system import build_controller

        lab = load_lab_configuration(root / "lab.toml")
        controller = build_controller(
            authored_root=root,
            state_root=machine_state,
            runtime_root=boot_runtime,
            lab=lab,
        )

        def report(failure: SupervisorFailure) -> None:
            print(
                "model-lab-supervisor: "
                f"{failure.operation}: {failure.code}: {failure.message}",
                file=stderr,
                flush=True,
            )

        supervisor = ModelLabSupervisor(
            controller=controller,
            authored_root=root,
            state_root=machine_state,
            runtime_root=boot_runtime,
            report_failure=report,
        )

        def stop(_signum: int, _frame: object) -> None:
            supervisor.stop()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        print(
            f"model-lab-supervisor: serving {supervisor.socket_path}",
            file=stdout,
            flush=True,
        )
        supervisor.serve_forever()
        return 0
    except ModelLabError as exception:
        print(
            f"model-lab-supervisor: {exception.code}: {exception}",
            file=stderr,
        )
        return 2
