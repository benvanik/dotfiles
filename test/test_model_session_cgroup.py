from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import unittest

from model_session.cgroup import (
    CGROUP_ROOT,
    SessionCgroup,
    _current_cgroup_relative_path,
    delegated_scope_command,
)
from model_session.errors import ModelSessionError


REAL_PROBE_ENVIRONMENT = "MODEL_SESSION_CGROUP_REAL_PROBE"


class ModelSessionCgroupUnitTest(unittest.TestCase):
    def test_membership_parser_requires_one_canonical_v2_path(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="model-session-cgroup.",
            dir="/tmp",
        ) as temporary:
            path = pathlib.Path(temporary) / "cgroup"
            path.write_text(
                "0::/user.slice/example.scope\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _current_cgroup_relative_path(path),
                pathlib.PurePosixPath("/user.slice/example.scope"),
            )
            for malformed in (
                "",
                "2:memory:/legacy\n",
                "0::relative\n",
                "0::/one\n0::/two\n",
            ):
                path.write_text(malformed, encoding="utf-8")
                with self.assertRaises(ModelSessionError):
                    _current_cgroup_relative_path(path)

    def test_scope_wrapper_is_fixed_and_argument_preserving(self) -> None:
        command = ("/absolute/model-session", "resume", "session-id")
        wrapped = delegated_scope_command(command)
        self.assertEqual(wrapped[0], "/usr/bin/systemd-run")
        self.assertIn("--property=Delegate=yes", wrapped)
        self.assertEqual(wrapped[-len(command) :], command)
        with self.assertRaises(ModelSessionError):
            delegated_scope_command(("/absolute/model-session", ""))


class ModelSessionCgroupRealTest(unittest.TestCase):
    def test_delegated_scope_enforces_and_drains_one_workload_leaf(self) -> None:
        if os.environ.get(REAL_PROBE_ENVIRONMENT) != "1":
            environment = dict(os.environ)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment[REAL_PROBE_ENVIRONMENT] = "1"
            result = subprocess.run(
                delegated_scope_command(
                    (
                        sys.executable,
                        os.fspath(pathlib.Path(__file__).resolve()),
                        (
                            "ModelSessionCgroupRealTest."
                            "test_delegated_scope_enforces_and_drains_one_"
                            "workload_leaf"
                        ),
                    )
                ),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return

        ready_read, ready_write = os.pipe()
        control_read, control_write = os.pipe()
        process: subprocess.Popen[bytes] | None = None
        with SessionCgroup.create(
            memory_bytes=1024 * 1024 * 1024,
            max_processes=16,
        ) as session:
            parent = CGROUP_ROOT.joinpath(*session.relative_path.parts[1:])
            self.assertEqual(
                (parent / "memory.max").read_text(encoding="ascii").strip(),
                str(1024 * 1024 * 1024),
            )
            self.assertEqual(
                (parent / "memory.swap.max")
                .read_text(encoding="ascii")
                .strip(),
                "0",
            )
            self.assertEqual(
                (parent / "pids.max").read_text(encoding="ascii").strip(),
                "16",
            )
            workload_procs = session.duplicate_workload_procs()
            try:
                child_program = (
                    "import os,sys;"
                    "os.write(int(sys.argv[1]),b'0\\n');"
                    "os.write(int(sys.argv[2]),b'1');"
                    "os.read(int(sys.argv[3]),1)"
                )
                process = subprocess.Popen(
                    (
                        sys.executable,
                        "-c",
                        child_program,
                        str(workload_procs),
                        str(ready_write),
                        str(control_read),
                    ),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    pass_fds=(workload_procs, ready_write, control_read),
                    close_fds=True,
                )
            finally:
                os.close(workload_procs)
                os.close(ready_write)
                os.close(control_read)
            self.assertEqual(os.read(ready_read, 1), b"1")
            self.assertTrue(session.workload_populated())
            with self.assertRaises(ModelSessionError) as caught:
                session.close()
            self.assertEqual(caught.exception.code, "cgroup_workload_live")
            session.kill_and_wait_empty()
            self.assertFalse(session.workload_populated())
            self.assertTrue(
                all(delta >= 0 for delta in session.memory_event_delta().values())
            )

        os.close(ready_read)
        os.close(control_write)
        self.assertIsNotNone(process)
        return_code = process.wait()
        self.assertEqual(return_code, -signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
