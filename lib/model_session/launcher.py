"""Minimal provider-neutral launcher for one external model profile."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from types import FrameType
from typing import IO, Any

from .agents import AGENTS_MD
from .errors import ModelSessionError
from .history import (
    SessionHistory,
    acquire_history_run_from_state,
    enumerate_history,
)
from .lease import RunLease, acquire_run_from_state
from .materialization import materialize_new_run
from .pi_runtime import INFERENCE_RELAY_PATH, SESSION_POLICY_PATH
from .profile import ProfileRoute, load_profile, load_profile_route
from .runs import SessionRun
from .sandbox import (
    INFERENCE_SOCKET_DESTINATION,
    SandboxPlan,
    build_sandbox_plan,
)


SUPPORTED_PI_VERSION = "0.82.1"
RELAY_LISTEN_PORT = 41111
HISTORY_SCHEMA = "model-session.history.v1"
ERROR_SCHEMA = "model-session.error.v1"
CHILD_SHUTDOWN_GRACE_SECONDS = 5.0


def _fail(message: str, *, code: str = "invalid_launcher_request") -> None:
    raise ModelSessionError(message, code=code)


def _parser(program: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=program,
        description=(
            "Start or resume an isolated Pi session from an external model "
            "profile."
        ),
    )
    parser.add_argument(
        "--profile",
        metavar="DIRECTORY",
        help=(
            "external profile directory; required when invoking "
            "`model-session` directly"
        ),
    )
    parser.add_argument(
        "--agents-md",
        action="store_true",
        help="print the agent operating contract and exit",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "new",
        help="snapshot the current profile and start a new session",
    )
    resume = subparsers.add_parser(
        "resume",
        help="resume an exact historical session",
    )
    resume.add_argument(
        "session_id",
        nargs="?",
        help="exact outer session ID; omit for an interactive picker",
    )
    status = subparsers.add_parser(
        "status",
        help="list retained sessions without launching one",
    )
    status.add_argument(
        "--json",
        action="store_true",
        help="emit the versioned machine-readable history",
    )
    return parser


def _invocation_path(argument_zero: str) -> pathlib.Path:
    if not argument_zero or "\x00" in argument_zero:
        _fail("launcher invocation path is invalid")
    candidate = pathlib.Path(argument_zero)
    if len(candidate.parts) == 1:
        discovered = shutil.which(argument_zero)
        if discovered is None:
            _fail(
                f"cannot locate launcher invocation {argument_zero!r}",
                code="invalid_launcher_invocation",
            )
        candidate = pathlib.Path(discovered)
    if not candidate.is_absolute():
        candidate = pathlib.Path.cwd() / candidate
    return pathlib.Path(os.path.abspath(os.fspath(candidate)))


def resolve_profile_root(
    argument_zero: str,
    explicit_profile: str | None,
) -> pathlib.Path:
    """Resolve either an external `pi` symlink or an explicit direct route."""

    invocation = _invocation_path(argument_zero)
    if invocation.name == "pi":
        if explicit_profile is not None:
            _fail(
                "--profile cannot override the directory owning a `pi` "
                "profile launcher",
                code="ambiguous_profile_route",
            )
        if not invocation.is_symlink():
            _fail(
                "a profile launcher named `pi` must be a symlink to the "
                "dotfiles model-session entry point",
                code="invalid_launcher_invocation",
            )
        entry_point = (
            pathlib.Path(__file__).resolve().parents[2]
            / "bin"
            / "model-session"
        )
        try:
            matches_entry_point = os.path.samefile(invocation, entry_point)
        except OSError as error:
            raise ModelSessionError(
                "cannot validate the external `pi` launcher symlink: "
                f"{error}",
                code="invalid_launcher_invocation",
            ) from error
        if not matches_entry_point:
            _fail(
                "the external `pi` launcher must resolve to this dotfiles "
                "repository's bin/model-session",
                code="invalid_launcher_invocation",
            )
        return invocation.parent
    if invocation.name != "model-session":
        _fail(
            "invoke this command as an external profile symlink named `pi`, "
            "or invoke `model-session --profile DIRECTORY` directly",
            code="invalid_launcher_invocation",
        )
    if explicit_profile is None:
        _fail(
            "direct model-session invocation requires --profile DIRECTORY",
            code="profile_required",
        )
    if "\x00" in explicit_profile:
        _fail("explicit profile path contains a NUL byte")
    expanded = pathlib.Path(explicit_profile).expanduser()
    if not expanded.is_absolute():
        expanded = pathlib.Path.cwd() / expanded
    return pathlib.Path(os.path.abspath(os.fspath(expanded)))


def _locked_profile_resource(
    run: SessionRun,
    role: str,
    configured_path: pathlib.PurePosixPath | None,
) -> str | None:
    resource = run.resource_for_role(role)
    if configured_path is None:
        if resource is not None:
            _fail(
                f"locked run has an unexpected {role} resource",
                code="invalid_session_state",
            )
        return None
    expected = pathlib.PurePosixPath("profile").joinpath(*configured_path.parts)
    if resource is None or resource.relative_path != expected:
        _fail(
            f"locked run is missing its exact {role} resource",
            code="invalid_session_state",
        )
    return pathlib.PurePosixPath("/profile").joinpath(
        *configured_path.parts
    ).as_posix()


def build_pi_command(run: SessionRun) -> tuple[str, ...]:
    """Render the complete, non-extensible Pi 0.82.1 sandbox command."""

    if run.profile.pi.version != SUPPORTED_PI_VERSION:
        _fail(
            "launcher policy is validated only for Pi "
            f"{SUPPORTED_PI_VERSION}; locked run requests "
            f"{run.profile.pi.version}",
            code="unsupported_pi_version",
        )
    relay = run.resource_for_role("inference_relay")
    if relay is None or relay.relative_path != INFERENCE_RELAY_PATH:
        _fail(
            "locked run is missing its exact inference relay",
            code="invalid_session_state",
        )
    policy = run.resource_for_role("session_policy")
    if policy is None or policy.relative_path != SESSION_POLICY_PATH:
        _fail(
            "locked run is missing its exact Pi session policy",
            code="invalid_session_state",
        )
    system_prompt = _locked_profile_resource(
        run,
        "system_prompt",
        run.profile.pi.system_prompt_file,
    )
    append_system_prompt = _locked_profile_resource(
        run,
        "append_system_prompt",
        run.profile.pi.append_system_prompt_file,
    )
    pi_executable = pathlib.PurePosixPath("/opt/pi").joinpath(
        *run.profile.pi.executable.parts
    )
    command = [
        "/usr/bin/python3",
        "/runtime/relay.py",
        "--socket",
        INFERENCE_SOCKET_DESTINATION,
        "--listen-port",
        str(RELAY_LISTEN_PORT),
        "--expected-command-version",
        SUPPORTED_PI_VERSION,
        "--",
        pi_executable.as_posix(),
        "--offline",
        "--provider",
        run.profile.runtime.provider,
        "--model",
        run.profile.runtime.model_id,
        "--session-dir",
        "/sessions",
        "--session-id",
        run.session_id,
        "--tools",
        ",".join(run.profile.pi.tools),
        "--no-extensions",
        "--extension",
        "/runtime/session-policy.js",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--approve",
    ]
    if system_prompt is not None:
        command.extend(("--system-prompt", system_prompt))
    if append_system_prompt is not None:
        command.extend(("--append-system-prompt", append_system_prompt))
    return tuple(command)


def _shell_exit_code(return_code: int) -> int:
    return return_code if return_code >= 0 else 128 - return_code


class _ReceivedSignal(BaseException):
    def __init__(self, signal_number: int) -> None:
        self.signal_number = signal_number


@contextlib.contextmanager
def _forward_lifecycle_signals(plan: SandboxPlan) -> Iterator[None]:
    """Deliver terminal signals to the exact child behind `--new-session`."""

    previous: dict[int, signal.Handlers] = {}

    def forward(signal_number: int, _frame: FrameType | None) -> None:
        plan.signal_sandbox_child(signal_number)
        if signal_number != getattr(signal, "SIGWINCH", None):
            raise _ReceivedSignal(signal_number)

    try:
        for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT", "SIGWINCH"):
            signal_number = getattr(signal, name, None)
            if signal_number is None:
                continue
            previous[signal_number] = signal.signal(signal_number, forward)
        yield
    finally:
        for signal_number, handler in previous.items():
            signal.signal(signal_number, handler)


def _stop_and_reap(
    process: subprocess.Popen[Any],
    *,
    plan: SandboxPlan | None,
    initial_signal: int = signal.SIGTERM,
    signal_already_delivered: bool = False,
) -> None:
    """Reap before releasing the lease, even when cleanup operations fail."""

    try:
        if process.poll() is not None:
            return
    except BaseException:
        pass
    if not signal_already_delivered:
        plan_signaled = False
        if plan is not None:
            try:
                plan.signal_sandbox_child(initial_signal)
                plan_signaled = True
            except BaseException:
                pass
        if not plan_signaled:
            try:
                process.send_signal(initial_signal)
            except BaseException:
                pass
    try:
        process.wait(timeout=CHILD_SHUTDOWN_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    except BaseException:
        try:
            if process.poll() is not None:
                return
        except BaseException:
            pass
    if plan is not None:
        try:
            plan.signal_sandbox_child(signal.SIGKILL)
        except BaseException:
            pass
    try:
        process.kill()
    except BaseException:
        pass
    while True:
        try:
            process.wait()
            return
        except InterruptedError:
            continue
        except BaseException:
            try:
                if process.poll() is not None:
                    return
            except BaseException:
                pass
            # A live or unknown child keeps the plan and its exclusive lease.
            # Releasing either after failed kill/wait operations would admit a
            # second model process into the same mutable session.
            time.sleep(0.05)


def launch_lease(lease: RunLease) -> int:
    """Transfer one exclusive lease into a plan for the whole child lifetime."""

    try:
        command = build_pi_command(lease.run)
        plan = build_sandbox_plan(lease, command=command)
    except BaseException:
        lease.close()
        raise
    with plan:
        try:
            process = subprocess.Popen(
                plan.argv,
                pass_fds=plan.pass_fds,
                close_fds=True,
            )
        except OSError as error:
            raise ModelSessionError(
                f"cannot start the model-session sandbox: {error}",
                code="sandbox_launch_failed",
            ) from error
        child_identity_ready = False
        try:
            plan.sandbox_child_pid(process)
            child_identity_ready = True
            with _forward_lifecycle_signals(plan):
                while True:
                    try:
                        return _shell_exit_code(process.wait())
                    except InterruptedError:
                        continue
        except _ReceivedSignal as interruption:
            _stop_and_reap(
                process,
                plan=plan,
                initial_signal=interruption.signal_number,
                signal_already_delivered=True,
            )
            return 128 + interruption.signal_number
        except BaseException:
            _stop_and_reap(
                process,
                plan=plan if child_identity_ready else None,
            )
            raise


def _history_value(
    route: ProfileRoute,
    entries: Sequence[SessionHistory],
) -> dict[str, Any]:
    return {
        "schema": HISTORY_SCHEMA,
        "profile_id": route.profile_id,
        "sessions": [
            {
                "session_id": entry.session_id,
                "created_at": entry.created_at,
                "updated_at": entry.updated_at,
                "project_id": entry.project_id,
                "title": entry.title,
                "prompt_fingerprint": entry.prompt_fingerprint,
                "active": entry.active,
                "pi_session_name": entry.pi_session_name,
                "history_error": entry.history_error,
            }
            for entry in entries
        ],
    }


def _history_state(entry: SessionHistory) -> tuple[str, str]:
    if entry.active:
        return "active", ""
    if entry.history_error is not None:
        return "invalid", f" [{entry.history_error}]"
    return "idle", ""


def _print_history(
    route: ProfileRoute,
    entries: Sequence[SessionHistory],
    *,
    output: IO[str],
    as_json: bool,
) -> None:
    if as_json:
        print(
            json.dumps(
                _history_value(route, entries),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=output,
        )
        return
    if not entries:
        print(f"{route.profile_id}: no sessions", file=output)
        return
    print(f"{route.profile_id} sessions:", file=output)
    for entry in entries:
        state, error_suffix = _history_state(entry)
        prompt_fingerprint = entry.prompt_fingerprint or "-"
        print(
            f"{entry.session_id}  {state:7}  {entry.updated_at}  "
            f"{prompt_fingerprint:12}  {entry.title}{error_suffix}",
            file=output,
        )


def _pick_session(
    entries: Sequence[SessionHistory],
    *,
    input_stream: IO[str],
    output: IO[str],
) -> str:
    if not entries:
        _fail("this profile has no sessions to resume", code="no_sessions")
    if not input_stream.isatty() or not output.isatty():
        _fail(
            "resume without a session ID requires an interactive terminal; "
            "pass an exact ID from `./pi status`",
            code="session_id_required",
        )
    for index, entry in enumerate(entries, start=1):
        state, error_suffix = _history_state(entry)
        print(
            f"{index:>2}. {entry.session_id}  {state:7}  "
            f"{entry.title}{error_suffix}",
            file=output,
        )
    print(
        f"Resume session [1-{len(entries)}]: ",
        end="",
        flush=True,
        file=output,
    )
    selection = input_stream.readline()
    if selection == "":
        _fail("resume picker reached end of input", code="resume_cancelled")
    normalized = selection.strip()
    if not normalized.isascii() or not normalized.isdecimal():
        _fail(
            "resume picker selection must be a displayed number",
            code="invalid_resume_selection",
        )
    index = int(normalized)
    if index < 1 or index > len(entries):
        _fail(
            "resume picker selection is outside the displayed range",
            code="invalid_resume_selection",
        )
    return entries[index - 1].session_id


def _new_session(profile_root: pathlib.Path) -> int:
    profile = load_profile(profile_root)
    run = materialize_new_run(profile)
    lease = acquire_run_from_state(
        profile.contract.state_root,
        profile.contract.profile_id,
        run.session_id,
    )
    return launch_lease(lease)


def _resume_session(
    profile_root: pathlib.Path,
    session_id: str | None,
    *,
    input_stream: IO[str],
    output: IO[str],
) -> int:
    route = load_profile_route(profile_root)
    if session_id is not None:
        lease = acquire_history_run_from_state(
            route.state_root,
            route.profile_id,
            session_id,
        )
        return launch_lease(lease)
    with enumerate_history(route.state_root, route.profile_id) as catalog:
        selected = _pick_session(
            catalog.entries,
            input_stream=input_stream,
            output=output,
        )
        lease = catalog.acquire(selected)
    return launch_lease(lease)


def _status(
    profile_root: pathlib.Path,
    *,
    output: IO[str],
    as_json: bool,
) -> int:
    route = load_profile_route(profile_root)
    with enumerate_history(route.state_root, route.profile_id) as catalog:
        _print_history(route, catalog.entries, output=output, as_json=as_json)
    return 0


def main(
    arguments: Sequence[str] | None = None,
    *,
    argument_zero: str | None = None,
    input_stream: IO[str] | None = None,
    output: IO[str] | None = None,
    error: IO[str] | None = None,
) -> int:
    """Run the launcher, returning an operator-facing process status."""

    invocation = sys.argv[0] if argument_zero is None else argument_zero
    stdin = sys.stdin if input_stream is None else input_stream
    stdout = sys.stdout if output is None else output
    stderr = sys.stderr if error is None else error
    parser = _parser(pathlib.Path(invocation).name or "model-session")
    parsed = parser.parse_args(
        list(sys.argv[1:] if arguments is None else arguments)
    )
    if parsed.agents_md:
        print(AGENTS_MD, end="", file=stdout)
        return 0
    try:
        profile_root = resolve_profile_root(invocation, parsed.profile)
        if parsed.command in (None, "new"):
            return _new_session(profile_root)
        if parsed.command == "resume":
            return _resume_session(
                profile_root,
                parsed.session_id,
                input_stream=stdin,
                output=stdout,
            )
        if parsed.command == "status":
            return _status(
                profile_root,
                output=stdout,
                as_json=parsed.json,
            )
        _fail(f"unsupported launcher command: {parsed.command}")
    except ModelSessionError as exception:
        if parsed.command == "status" and parsed.json:
            print(
                json.dumps(
                    {
                        "schema": ERROR_SCHEMA,
                        "error": {
                            "code": exception.code,
                            "message": str(exception),
                        },
                    },
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                file=stdout,
            )
        else:
            print(
                f"model-session: {exception.code}: {exception}",
                file=stderr,
            )
        return 2
