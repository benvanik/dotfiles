from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import pathlib
import signal
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from model_session.attachment import publish_inference_attachment
from model_session.errors import ModelSessionError
from model_session.launcher import (
    AGENTS_MD,
    ERROR_SCHEMA,
    HISTORY_SCHEMA,
    SUPPORTED_PI_VERSION,
    _ReceivedSignal,
    _forward_lifecycle_signals,
    build_pi_command,
    launch_lease,
    main,
    resolve_profile_root,
)
from model_session.lease import acquire_run_from_state
from model_session.materialization import materialize_new_run
from model_session.profile import load_profile


REVISION = "a" * 40
DOTFILES_ROOT = pathlib.Path(__file__).resolve().parents[1]
ENTRY_POINT = DOTFILES_ROOT / "bin" / "model-session"


def _private_directory(path: pathlib.Path) -> pathlib.Path:
    path.mkdir(mode=0o700, parents=True)
    path.chmod(0o700)
    return path


class LauncherFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="model-session-launcher.",
            dir="/tmp",
        )
        self.root = pathlib.Path(self.temporary.name)
        self.profile_root = self.root / "profiles" / "fixture"
        self.profile_root.mkdir(parents=True)
        self.profile_root.chmod(0o755)
        self.state_root = self.root / "state"
        self.project_root = _private_directory(self.root / "project")
        self.pi_root = _private_directory(self.root / "pi-0.82.1")
        pi_bin = _private_directory(self.pi_root / "bin")
        pi = pi_bin / "pi"
        pi.write_text(
            """#!/bin/sh
if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then
  printf '%s\n' '0.82.1'
  exit 0
fi
[ "$MODEL_SESSION_BASE_URL" = "http://127.0.0.1:41111/v1" ] || exit 31
[ "$MODEL_SESSION_INFERENCE_SOCKET" = \
"/run/model-session/inference.sock" ] || exit 32
[ -r /workspace/AGENTS.md ] || exit 33
printf '%s\n' "$@" > /workspace/pi-argv
""",
            encoding="utf-8",
        )
        pi.chmod(0o700)
        node = pi_bin / "node"
        node.write_text("#!/bin/sh\necho v24.11.1\n", encoding="utf-8")
        node.chmod(0o700)

        self.agents = self.profile_root / "AGENTS.md"
        self.system_prompt = self.profile_root / "SYSTEM.md"
        self.append_prompt = self.profile_root / "APPEND.md"
        self.agents.write_text("isolated fixture workspace\n", encoding="utf-8")
        self.system_prompt.write_text("system prompt v1\n", encoding="utf-8")
        self.append_prompt.write_text("append prompt v1\n", encoding="utf-8")
        for path in (self.agents, self.system_prompt, self.append_prompt):
            path.chmod(0o644)
        self.profile_file = self.profile_root / "profile.toml"
        self.profile_file.write_text(
            f"""schema = "model-session.profile.v1"
profile_id = "fixture"
project_id = "fixture-project"
state_root = "{self.state_root}"
project_root = "{self.project_root}"

[model]
repository = "fixture/model"
revision = "{REVISION}"
context_tokens = 65536
max_output_tokens = 8192
kv_cache_dtype = "bf16"
max_sequences = 1
weight_format = "bf16"

[runtime]
provider = "fixture-provider"
model_id = "fixture-model"
reasoning = false
input_modalities = ["text"]

[pi]
installation_root = "{self.pi_root}"
executable = "bin/pi"
version = "0.82.1"
tools = ["read", "write", "edit", "bash"]
system_prompt_file = "SYSTEM.md"
append_system_prompt_file = "APPEND.md"
""",
            encoding="utf-8",
        )
        self.profile_file.chmod(0o644)
        self.launcher = self.profile_root / "pi"
        self.launcher.symlink_to(ENTRY_POINT)

    def close(self) -> None:
        self.temporary.cleanup()

    def profile(self):
        return load_profile(self.profile_root)


class _TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class _FakeProcess:
    def __init__(
        self,
        *,
        return_code: int = 0,
        timeout_on_grace: bool = False,
    ) -> None:
        self.final_return_code = return_code
        self.timeout_on_grace = timeout_on_grace
        self.returncode: int | None = None
        self.sent_signals: list[int] = []
        self.killed = False
        self.wait_timeouts: list[float | None] = []

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if (
            timeout is not None
            and self.timeout_on_grace
            and not self.killed
        ):
            raise subprocess.TimeoutExpired("/fake/bwrap", timeout)
        if self.killed:
            return self.returncode
        self.returncode = self.final_return_code
        return self.returncode

    def send_signal(self, signal_number: int) -> None:
        self.sent_signals.append(signal_number)

    def kill(self) -> None:
        self.killed = True
        self.returncode = -signal.SIGKILL


class _FakePlan:
    def __init__(
        self,
        lease,
        *,
        process: _FakeProcess,
        on_close=None,
        sandbox_child_pid: int = 424242,
    ) -> None:
        self.argv = ("/fake/bwrap", "--", "/usr/bin/python3")
        self.pass_fds = ()
        self.lease = lease
        self.process = process
        self.on_close = on_close
        self.child_pid = sandbox_child_pid
        self.sent_signals: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        if self.on_close is not None:
            self.on_close()
        self.lease.close()

    def sandbox_child_pid(self, process: _FakeProcess) -> int:
        if process is not self.process:
            raise AssertionError("sandbox identity used another monitor")
        return self.child_pid

    def signal_sandbox_child(self, signal_number: int) -> None:
        self.sent_signals.append(signal_number)


class ModelSessionLauncherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = LauncherFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    @contextlib.contextmanager
    def fake_launch(self, *, return_code: int = 0):
        captured: dict[str, object] = {}
        process = _FakeProcess(return_code=return_code)

        def build(lease, *, command):
            captured["lease"] = lease
            captured["command"] = command
            return _FakePlan(lease, process=process)

        def popen(argv, **options):
            captured["argv"] = argv
            captured["popen_options"] = options
            return process

        with (
            mock.patch(
                "model_session.launcher.build_sandbox_plan",
                side_effect=build,
            ),
            mock.patch(
                "model_session.launcher.subprocess.Popen",
                side_effect=popen,
            ),
        ):
            yield captured, process

    def test_external_pi_symlink_is_the_profile_identity(self) -> None:
        self.assertEqual(
            resolve_profile_root(os.fspath(self.fixture.launcher), None),
            self.fixture.profile_root,
        )
        with self.assertRaises(ModelSessionError) as caught:
            resolve_profile_root(
                os.fspath(self.fixture.launcher),
                os.fspath(self.fixture.root / "other"),
            )
        self.assertEqual(caught.exception.code, "ambiguous_profile_route")

        fake_entry_point = self.fixture.root / "fake-model-session"
        fake_entry_point.write_text("#!/bin/sh\n", encoding="utf-8")
        wrong = self.fixture.root / "wrong-profile" / "pi"
        wrong.parent.mkdir()
        wrong.symlink_to(fake_entry_point)
        with self.assertRaises(ModelSessionError) as caught:
            resolve_profile_root(os.fspath(wrong), None)
        self.assertEqual(caught.exception.code, "invalid_launcher_invocation")

    def test_direct_entry_point_requires_an_explicit_external_profile(self) -> None:
        with self.assertRaises(ModelSessionError) as caught:
            resolve_profile_root(os.fspath(ENTRY_POINT), None)
        self.assertEqual(caught.exception.code, "profile_required")
        self.assertEqual(
            resolve_profile_root(
                os.fspath(ENTRY_POINT),
                os.fspath(self.fixture.profile_root),
            ),
            self.fixture.profile_root,
        )

    def test_fixed_command_contains_only_locked_pi_authority(self) -> None:
        run = materialize_new_run(self.fixture.profile())
        command = build_pi_command(run)

        self.assertEqual(
            command[:10],
            (
                "/usr/bin/python3",
                "/runtime/relay.py",
                "--socket",
                "/run/model-session/inference.sock",
                "--listen-port",
                "41111",
                "--expected-command-version",
                SUPPORTED_PI_VERSION,
                "--",
                "/opt/pi/bin/pi",
            ),
        )
        self.assertEqual(command[command.index("--provider") + 1], "fixture-provider")
        self.assertEqual(command[command.index("--model") + 1], "fixture-model")
        self.assertEqual(command[command.index("--session-id") + 1], run.session_id)
        self.assertEqual(
            command[command.index("--tools") + 1],
            "read,write,edit,bash",
        )
        self.assertEqual(
            command[command.index("--system-prompt") + 1],
            "/profile/SYSTEM.md",
        )
        self.assertEqual(
            command[command.index("--append-system-prompt") + 1],
            "/profile/APPEND.md",
        )
        for flag in (
            "--offline",
            "--no-extensions",
            "--extension",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--approve",
        ):
            self.assertIn(flag, command)
        rendered = "\0".join(command)
        for forbidden in (
            "RUNPOD_API_KEY",
            "HF_TOKEN",
            os.fspath(self.fixture.profile_root),
            os.fspath(self.fixture.state_root),
        ):
            self.assertNotIn(forbidden, rendered)

        unsupported = dataclasses.replace(
            run,
            profile=dataclasses.replace(
                run.profile,
                pi=dataclasses.replace(run.profile.pi, version="0.83.0"),
            ),
        )
        with self.assertRaises(ModelSessionError) as caught:
            build_pi_command(unsupported)
        self.assertEqual(caught.exception.code, "unsupported_pi_version")

    def test_bare_pi_creates_and_launches_one_external_session(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with self.fake_launch() as (captured, process):
            result = main(
                [],
                argument_zero=os.fspath(self.fixture.launcher),
                output=output,
                error=error,
            )

        self.assertEqual(result, 0)
        self.assertEqual(process.wait_timeouts, [None])
        self.assertEqual(error.getvalue(), "")
        command = captured["command"]
        self.assertIsInstance(command, tuple)
        session_id = command[command.index("--session-id") + 1]
        session_root = (
            self.fixture.state_root / "sessions" / "fixture" / session_id
        )
        self.assertTrue(session_root.is_dir())
        self.assertTrue((session_root / "workspace" / "AGENTS.md").is_file())
        self.assertFalse(str(session_root).startswith(str(DOTFILES_ROOT)))
        self.assertEqual(
            captured["popen_options"],
            {"pass_fds": (), "close_fds": True},
        )

    def test_resume_uses_locked_prompt_after_current_prompt_changes(self) -> None:
        run = materialize_new_run(self.fixture.profile())
        self.fixture.system_prompt.write_text(
            "current prompt v2\n",
            encoding="utf-8",
        )
        self.fixture.system_prompt.chmod(0o644)
        captured_prompt: list[bytes] = []
        process = _FakeProcess()

        def build(lease, *, command):
            descriptor = lease.duplicate_resource(
                pathlib.PurePosixPath("profile/SYSTEM.md")
            )
            try:
                captured_prompt.append(os.read(descriptor, 4096))
            finally:
                os.close(descriptor)
            return _FakePlan(lease, process=process)

        with (
            mock.patch(
                "model_session.launcher.build_sandbox_plan",
                side_effect=build,
            ),
            mock.patch(
                "model_session.launcher.subprocess.Popen",
                return_value=process,
            ),
        ):
            result = main(
                ["resume", run.session_id],
                argument_zero=os.fspath(self.fixture.launcher),
                output=io.StringIO(),
                error=io.StringIO(),
            )

        self.assertEqual(result, 0)
        self.assertEqual(captured_prompt, [b"system prompt v1\n"])

    def test_resume_picker_returns_only_the_selected_session_identity(self) -> None:
        older = materialize_new_run(self.fixture.profile())
        newer = materialize_new_run(self.fixture.profile())
        picker_input = _TTYBuffer("2\n")
        picker_output = _TTYBuffer()

        with self.fake_launch() as (captured, _process):
            result = main(
                ["resume"],
                argument_zero=os.fspath(self.fixture.launcher),
                input_stream=picker_input,
                output=picker_output,
                error=io.StringIO(),
            )

        self.assertEqual(result, 0)
        self.assertIn(older.session_id, picker_output.getvalue())
        self.assertIn(newer.session_id, picker_output.getvalue())
        command = captured["command"]
        self.assertEqual(
            command[command.index("--session-id") + 1],
            older.session_id,
        )

    def test_noninteractive_resume_requires_an_explicit_id(self) -> None:
        materialize_new_run(self.fixture.profile())
        error = io.StringIO()
        result = main(
            ["resume"],
            argument_zero=os.fspath(self.fixture.launcher),
            input_stream=io.StringIO(),
            output=io.StringIO(),
            error=error,
        )
        self.assertEqual(result, 2)
        self.assertIn("session_id_required", error.getvalue())

    def test_status_json_is_stable_and_does_not_launch(self) -> None:
        run = materialize_new_run(self.fixture.profile())
        output = io.StringIO()
        with mock.patch("model_session.launcher.subprocess.Popen") as popen:
            result = main(
                ["status", "--json"],
                argument_zero=os.fspath(self.fixture.launcher),
                output=output,
                error=io.StringIO(),
            )
        self.assertEqual(result, 0)
        popen.assert_not_called()
        value = json.loads(output.getvalue())
        self.assertEqual(value["schema"], HISTORY_SCHEMA)
        self.assertEqual(value["profile_id"], "fixture")
        self.assertEqual(len(value["sessions"]), 1)
        self.assertEqual(value["sessions"][0]["session_id"], run.session_id)
        self.assertEqual(value["sessions"][0]["title"], "(empty session)")
        self.assertIsNone(value["sessions"][0]["history_error"])

    def test_poisoned_sibling_does_not_block_status_or_exact_resume(self) -> None:
        healthy = materialize_new_run(self.fixture.profile())
        poisoned = materialize_new_run(self.fixture.profile())
        structurally_damaged = materialize_new_run(self.fixture.profile())
        poison = poisoned.pi_sessions / "foreign.jsonl"
        poison.write_text("not-json\n", encoding="utf-8")
        poison.chmod(0o600)
        structurally_damaged.workspace.chmod(0o777)

        output = io.StringIO()
        result = main(
            ["status", "--json"],
            argument_zero=os.fspath(self.fixture.launcher),
            output=output,
            error=io.StringIO(),
        )
        self.assertEqual(result, 0)
        by_id = {
            entry["session_id"]: entry
            for entry in json.loads(output.getvalue())["sessions"]
        }
        self.assertEqual(
            by_id[poisoned.session_id]["history_error"],
            "invalid_pi_session",
        )
        self.assertIsNone(by_id[healthy.session_id]["history_error"])
        self.assertEqual(
            by_id[structurally_damaged.session_id]["history_error"],
            "unsafe_session_permissions",
        )
        self.assertIsNone(
            by_id[structurally_damaged.session_id]["prompt_fingerprint"]
        )

        with self.fake_launch() as (captured, _process):
            result = main(
                ["resume", healthy.session_id],
                argument_zero=os.fspath(self.fixture.launcher),
                output=io.StringIO(),
                error=io.StringIO(),
            )
        self.assertEqual(result, 0)
        command = captured["command"]
        self.assertEqual(
            command[command.index("--session-id") + 1],
            healthy.session_id,
        )

    def test_status_json_failure_is_a_stable_json_error(self) -> None:
        profile_file = self.fixture.profile_file
        profile_file.write_text(
            profile_file.read_text(encoding="utf-8").replace(
                'profile_id = "fixture"',
                'profile_id = "INVALID/profile"',
            ),
            encoding="utf-8",
        )
        profile_file.chmod(0o644)
        output = io.StringIO()
        error = io.StringIO()
        result = main(
            ["status", "--json"],
            argument_zero=os.fspath(self.fixture.launcher),
            output=output,
            error=error,
        )
        self.assertEqual(result, 2)
        self.assertEqual(error.getvalue(), "")
        value = json.loads(output.getvalue())
        self.assertEqual(value["schema"], ERROR_SCHEMA)
        self.assertEqual(value["error"]["code"], "invalid_profile")
        self.assertIsInstance(value["error"]["message"], str)

    def test_child_is_reaped_before_plan_releases_lease_on_setup_failure(
        self,
    ) -> None:
        run = materialize_new_run(self.fixture.profile())
        lease = acquire_run_from_state(
            self.fixture.state_root,
            "fixture",
            run.session_id,
        )
        process = _FakeProcess(timeout_on_grace=True)
        close_observations: list[int | None] = []
        plans: list[_FakePlan] = []

        def build(acquired_lease, *, command):
            self.assertEqual(
                command[command.index("--session-id") + 1],
                run.session_id,
            )
            plan = _FakePlan(
                acquired_lease,
                process=process,
                on_close=lambda: close_observations.append(process.poll()),
            )
            plans.append(plan)
            return plan

        failures: list[BaseException] = []

        def worker() -> None:
            try:
                launch_lease(lease)
            except BaseException as exception:
                failures.append(exception)

        with (
            mock.patch(
                "model_session.launcher.build_sandbox_plan",
                side_effect=build,
            ),
            mock.patch(
                "model_session.launcher.subprocess.Popen",
                return_value=process,
            ),
        ):
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join()

        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], ValueError)
        self.assertEqual(len(plans), 1)
        self.assertEqual(
            plans[0].sent_signals,
            [signal.SIGTERM, signal.SIGKILL],
        )
        self.assertEqual(process.sent_signals, [])
        self.assertTrue(process.killed)
        self.assertEqual(close_observations, [-signal.SIGKILL])
        self.assertTrue(lease.closed)
        with acquire_run_from_state(
            self.fixture.state_root,
            "fixture",
            run.session_id,
        ) as reacquired:
            self.assertEqual(reacquired.run.session_id, run.session_id)

    def test_lifecycle_signal_handlers_target_the_exact_sandbox_child(
        self,
    ) -> None:
        handlers: dict[int, object] = {}
        plan = mock.Mock()

        def install(signal_number, handler):
            previous = handlers.get(signal_number, signal.SIG_DFL)
            handlers[signal_number] = handler
            return previous

        with (
            mock.patch(
                "model_session.launcher.signal.signal",
                side_effect=install,
            ),
            _forward_lifecycle_signals(plan),
        ):
            winch = handlers[signal.SIGWINCH]
            winch(signal.SIGWINCH, None)
            interrupt = handlers[signal.SIGINT]
            with self.assertRaises(_ReceivedSignal) as caught:
                interrupt(signal.SIGINT, None)

        self.assertEqual(caught.exception.signal_number, signal.SIGINT)
        self.assertEqual(
            [call.args for call in plan.signal_sandbox_child.call_args_list],
            [(signal.SIGWINCH,), (signal.SIGINT,)],
        )

    def test_cleanup_failures_cannot_release_a_live_session_lease(self) -> None:
        run = materialize_new_run(self.fixture.profile())
        lease = acquire_run_from_state(
            self.fixture.state_root,
            "fixture",
            run.session_id,
        )

        class HostileProcess(_FakeProcess):
            def __init__(self) -> None:
                super().__init__()
                self.final_wait_count = 0

            def send_signal(self, signal_number: int) -> None:
                raise OSError(f"send failed for {signal_number}")

            def kill(self) -> None:
                raise OSError("supervisor kill failed")

            def wait(self, timeout: float | None = None) -> int:
                if timeout is not None:
                    raise OSError("timed wait failed")
                self.final_wait_count += 1
                if self.final_wait_count == 1:
                    raise OSError("first terminal wait failed")
                self.returncode = 137
                return self.returncode

        class HostilePlan(_FakePlan):
            def signal_sandbox_child(self, signal_number: int) -> None:
                raise ModelSessionError(
                    f"pidfd signal failed for {signal_number}",
                    code="sandbox_signal_failed",
                )

        process = HostileProcess()
        close_observations: list[int | None] = []

        def build(acquired_lease, *, command):
            self.assertEqual(
                command[command.index("--session-id") + 1],
                run.session_id,
            )
            return HostilePlan(
                acquired_lease,
                process=process,
                on_close=lambda: close_observations.append(process.poll()),
            )

        failures: list[BaseException] = []

        def worker() -> None:
            try:
                launch_lease(lease)
            except BaseException as exception:
                failures.append(exception)

        with (
            mock.patch(
                "model_session.launcher.build_sandbox_plan",
                side_effect=build,
            ),
            mock.patch(
                "model_session.launcher.subprocess.Popen",
                return_value=process,
            ),
            mock.patch("model_session.launcher.time.sleep") as sleep,
        ):
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join()

        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], ValueError)
        self.assertEqual(process.final_wait_count, 2)
        self.assertEqual(close_observations, [137])
        sleep.assert_called_once_with(0.05)
        self.assertTrue(lease.closed)

    def test_agents_contract_needs_no_profile_or_state(self) -> None:
        output = io.StringIO()
        result = main(
            ["--agents-md"],
            argument_zero=os.fspath(ENTRY_POINT),
            output=output,
            error=io.StringIO(),
        )
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), AGENTS_MD)
        self.assertIn("./pi resume SESSION_ID", output.getvalue())

    def test_entry_point_executes_without_installing_profile_state(self) -> None:
        result = subprocess.run(
            (os.fspath(ENTRY_POINT), "--agents-md"),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, AGENTS_MD)

    def test_external_pi_help_is_self_contained_and_has_no_side_effects(
        self,
    ) -> None:
        result = subprocess.run(
            (os.fspath(self.fixture.launcher), "--help"),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("{new,resume,status}", result.stdout)
        self.assertIn("--agents-md", result.stdout)
        self.assertFalse(self.fixture.state_root.exists())

    def test_real_bwrap_relay_and_fixed_pi_command_vertical_slice(self) -> None:
        runtime_directory = _private_directory(self.fixture.root / "runtime")
        inference_socket = runtime_directory / "inference.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(os.fspath(inference_socket))
            listener.listen(16)
            inference_socket.chmod(0o600)
            with mock.patch.dict(
                os.environ,
                {"XDG_RUNTIME_DIR": os.fspath(runtime_directory)},
            ):
                publish_inference_attachment(
                    self.fixture.profile(),
                    inference_socket,
                    ttl_seconds=60,
                )
                result = main(
                    [],
                    argument_zero=os.fspath(self.fixture.launcher),
                    output=io.StringIO(),
                    error=io.StringIO(),
                )
        finally:
            listener.close()

        self.assertEqual(result, 0)
        session_roots = tuple(
            (self.fixture.state_root / "sessions" / "fixture").iterdir()
        )
        self.assertEqual(len(session_roots), 1)
        arguments = (
            session_roots[0] / "workspace" / "pi-argv"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            arguments[arguments.index("--session-id") + 1],
            session_roots[0].name,
        )
        self.assertEqual(
            arguments[arguments.index("--system-prompt") + 1],
            "/profile/SYSTEM.md",
        )
        self.assertIn("--offline", arguments)
        self.assertIn("--no-extensions", arguments)


if __name__ == "__main__":
    unittest.main()
