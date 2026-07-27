from __future__ import annotations

import ast
import errno
import fcntl
import json
import os
import pathlib
import select
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import model_session.storage_launch as launch_module
import model_session.storage_namespace as storage_module
from model_session.cgroup import (
    CGROUP_ROOT,
    WORKLOAD_CGROUP_NAME,
    SessionCgroup,
    delegated_scope_command,
)
from model_session.errors import ModelSessionError
from model_session.storage_namespace import (
    BWRAP_BINARY,
    PYTHON_BINARY,
    SETPRIV_BINARY,
    create_storage_namespace,
)


PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
WORK_BYTES = PAGE_SIZE * 16
WORK_INODES = 8
HISTORY_BYTES = PAGE_SIZE * 8
HISTORY_INODES = 5
REAL_CGROUP_PROBE_ENVIRONMENT = "MODEL_SESSION_STORAGE_CGROUP_REAL_PROBE"


def _scratch_names() -> frozenset[str]:
    return frozenset(
        entry.name
        for entry in pathlib.Path("/tmp").iterdir()
        if entry.name.startswith("model-session-storage.")
    )


def _assert_closed(test: unittest.TestCase, descriptor: int) -> None:
    with test.assertRaises(OSError) as caught:
        os.fstat(descriptor)
    test.assertEqual(caught.exception.errno, errno.EBADF)


def _wait_for_pidfd_exit(pid_descriptor: int) -> None:
    poller = select.poll()
    poller.register(
        pid_descriptor,
        select.POLLIN | select.POLLHUP | select.POLLERR,
    )
    while True:
        try:
            events = poller.poll()
        except InterruptedError:
            continue
        if any(
            descriptor == pid_descriptor
            for descriptor, _event in events
        ):
            return


SANDBOX_PROBE = r"""
import json
import os

with open("/workspace/from-sandbox", "w", encoding="utf-8") as stream:
    stream.write("work\n")
with open("/sessions/from-sandbox", "w", encoding="utf-8") as stream:
    stream.write("history\n")

work = os.statvfs("/workspace")
history = os.statvfs("/sessions")
print(
    json.dumps(
        {
            "user_namespace": os.readlink("/proc/self/ns/user"),
            "uid_map": open(
                "/proc/self/uid_map", encoding="ascii"
            ).read().strip(),
            "work_device": os.stat("/workspace").st_dev,
            "history_device": os.stat("/sessions").st_dev,
            "work_bytes": work.f_blocks * work.f_frsize,
            "history_bytes": history.f_blocks * history.f_frsize,
            "work_inodes": work.f_files,
            "history_inodes": history.f_files,
        },
        sort_keys=True,
    )
)
"""


def _production_bwrap_argv(
    *,
    usr_descriptor: int,
    work_descriptor: int,
    history_descriptor: int,
    block_descriptor: int | None = None,
) -> tuple[str, ...]:
    block_arguments = (
        ()
        if block_descriptor is None
        else ("--block-fd", str(block_descriptor))
    )
    return (
        os.fspath(BWRAP_BINARY),
        "--unshare-all",
        "--unshare-user",
        "--disable-userns",
        "--assert-userns-disabled",
        "--as-pid-1",
        "--new-session",
        "--die-with-parent",
        *block_arguments,
        "--hostname",
        "storage-namespace-test",
        "--clearenv",
        "--ro-bind-fd",
        str(usr_descriptor),
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/sbin",
        "/sbin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--bind-fd",
        str(work_descriptor),
        "/workspace",
        "--bind-fd",
        str(history_descriptor),
        "/sessions",
        "--remount-ro",
        "/",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--chdir",
        "/workspace",
        "--",
        "/usr/bin/python3",
        "-c",
        SANDBOX_PROBE,
    )


def _wait_for_workload_membership(
    session: SessionCgroup,
    process: subprocess.Popen[str],
) -> None:
    pid_descriptor = os.pidfd_open(process.pid)
    poller = select.poll()
    poller.register(
        session._workload_events_descriptor,
        select.POLLPRI | select.POLLERR,
    )
    poller.register(
        pid_descriptor,
        select.POLLIN | select.POLLHUP | select.POLLERR,
    )
    try:
        while not session.workload_populated():
            events = poller.poll()
            if any(
                descriptor == pid_descriptor
                for descriptor, _event in events
            ):
                _stdout, stderr = process.communicate()
                raise AssertionError(
                    "storage trampoline exited before joining its workload "
                    f"cgroup: {stderr}"
                )
    finally:
        os.close(pid_descriptor)


class StorageLaunchContractTest(unittest.TestCase):
    def test_namespace_child_executor_is_bounded_and_stdlib_only(self) -> None:
        source_bytes = (
            launch_module.NAMESPACE_CHILD_SOURCE_PATH.read_bytes()
        )
        self.assertLessEqual(
            len(source_bytes),
            launch_module._MAX_NAMESPACE_CHILD_SOURCE_BYTES,
        )
        source = source_bytes.decode("utf-8")
        self.assertNotIn("model_session", source)
        tree = ast.parse(source)
        imported_roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(
                    alias.name.partition(".")[0]
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                if node.module != "__future__":
                    imported_roots.add(node.module.partition(".")[0])
        self.assertLessEqual(imported_roots, sys.stdlib_module_names)

    def test_trusted_helper_rejects_group_writable_source(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="model-session-helper-mode.",
            dir="/tmp",
        ) as temporary:
            helper = pathlib.Path(temporary) / "checkpoint-helper.py"
            helper.write_text("raise SystemExit(0)\n", encoding="utf-8")
            helper.chmod(0o664)
            command = (
                os.fspath(PYTHON_BINARY),
                "-I",
                "-B",
                os.fspath(helper),
            )
            with mock.patch.object(
                storage_module,
                "TRUSTED_NAMESPACE_HELPERS",
                frozenset({helper}),
            ):
                with self.assertRaises(ModelSessionError) as caught:
                    storage_module._lock_trusted_namespace_command(command)
        self.assertEqual(caught.exception.code, "invalid_storage_launch")

    def test_regular_file_is_not_cgroup_procs_authority(self) -> None:
        with create_storage_namespace(
            work_bytes=WORK_BYTES,
            work_inodes=WORK_INODES,
            history_bytes=HISTORY_BYTES,
            history_inodes=HISTORY_INODES,
        ) as storage:
            with tempfile.TemporaryDirectory(
                prefix="model-session-fake-cgroup.",
                dir="/tmp",
            ) as temporary:
                path = pathlib.Path(temporary) / "ordinary"
                descriptor = os.open(
                    path,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                try:
                    with self.assertRaises(ModelSessionError) as caught:
                        storage.wrap_bwrap_argv(
                            (os.fspath(BWRAP_BINARY), "--help"),
                            workload_cgroup_procs_fd=descriptor,
                        )
                finally:
                    os.close(descriptor)
        self.assertEqual(caught.exception.code, "invalid_storage_launch")


class StorageLaunchRealTest(unittest.TestCase):
    def create(self):
        return create_storage_namespace(
            work_bytes=WORK_BYTES,
            work_inodes=WORK_INODES,
            history_bytes=HISTORY_BYTES,
            history_inodes=HISTORY_INODES,
        )

    def test_trampoline_postarm_parent_death_kills_blocked_helper(
        self,
    ) -> None:
        scratch_before = _scratch_names()
        storage = self.create()
        report_read, report_write = os.pipe()
        ready_read, ready_write = os.pipe()
        control_read, control_write = os.pipe()
        supervisor_pid = -1
        helper_pidfd = -1
        try:
            with tempfile.TemporaryDirectory(
                prefix="model-session-parent-death.",
                dir="/tmp",
            ) as temporary:
                helper = pathlib.Path(temporary) / "checkpoint_worker.py"
                helper.write_text(
                    (
                        "import os,sys\n"
                        "os.write(int(sys.argv[1]),b'READY')\n"
                        "os.read(int(sys.argv[2]),1)\n"
                    ),
                    encoding="utf-8",
                )
                helper.chmod(0o644)
                with mock.patch.object(
                    storage_module,
                    "TRUSTED_NAMESPACE_HELPERS",
                    frozenset({helper}),
                ):
                    supervisor_pid = os.fork()
                    if supervisor_pid == 0:
                        try:
                            os.close(report_read)
                            os.close(ready_read)
                            os.close(control_write)
                            command = (
                                os.fspath(PYTHON_BINARY),
                                "-I",
                                "-B",
                                os.fspath(helper),
                                str(ready_write),
                                str(control_read),
                            )
                            launch = storage.wrap_namespace_command(
                                command,
                                pass_fds=(ready_write, control_read),
                            )
                            process = launch.spawn(
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                            )
                            os.close(ready_write)
                            os.close(control_read)
                            os.write(
                                report_write,
                                f"{process.pid}\n".encode("ascii"),
                            )
                            os.close(report_write)
                            while True:
                                signal.pause()
                        except BaseException:
                            os._exit(73)

                os.close(report_write)
                report_write = -1
                os.close(ready_write)
                ready_write = -1
                os.close(control_read)
                control_read = -1
                helper_pid_text = os.read(report_read, 64)
                self.assertTrue(helper_pid_text)
                helper_pid = int(helper_pid_text.strip())
                self.assertEqual(os.read(ready_read, 5), b"READY")
                helper_pidfd = os.pidfd_open(helper_pid)
                storage.close()
                os.kill(supervisor_pid, signal.SIGKILL)
                _pid, status = os.waitpid(supervisor_pid, 0)
                supervisor_pid = -1
                self.assertTrue(os.WIFSIGNALED(status))
                self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
                _wait_for_pidfd_exit(helper_pidfd)
        finally:
            storage.close()
            if supervisor_pid > 0:
                try:
                    os.kill(supervisor_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(supervisor_pid, 0)
            for descriptor in (
                report_read,
                report_write,
                ready_read,
                ready_write,
                control_read,
                control_write,
                helper_pidfd,
            ):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
        self.assertEqual(_scratch_names(), scratch_before)

    def test_nested_production_bwrap_is_rw_and_direct_child_exec_succeeds(
        self,
    ) -> None:
        before = _scratch_names()
        with self.create() as storage:
            u1_identity = os.readlink(
                f"/proc/self/fd/{storage.user_namespace_descriptor}"
            )
            usr_descriptor = os.open(
                "/usr",
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
            )
            try:
                bwrap_argv = _production_bwrap_argv(
                    usr_descriptor=usr_descriptor,
                    work_descriptor=storage.work_descriptor,
                    history_descriptor=storage.history_descriptor,
                )
                launch = storage.wrap_bwrap_argv(
                    bwrap_argv,
                    pass_fds=(usr_descriptor,),
                )
                self.assertEqual(
                    launch._argv[-len(bwrap_argv) :],
                    bwrap_argv,
                )
                self.assertEqual(
                    launch._argv[:4],
                    (
                        os.fspath(SETPRIV_BINARY),
                        "--pdeathsig",
                        "KILL",
                        os.fspath(PYTHON_BINARY),
                    ),
                )
                process = launch.spawn(
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                stdout, stderr = process.communicate()
                self.assertEqual(
                    process.returncode,
                    0,
                    msg=f"stdout={stdout!r}\nstderr={stderr!r}",
                )
            finally:
                os.close(usr_descriptor)

            value = json.loads(stdout)
            self.assertNotEqual(value["user_namespace"], u1_identity)
            self.assertEqual(value["uid_map"], "0          0          1")
            self.assertNotEqual(
                value["work_device"],
                value["history_device"],
            )
            self.assertEqual(value["work_bytes"], WORK_BYTES)
            self.assertEqual(value["history_bytes"], HISTORY_BYTES)
            self.assertEqual(value["work_inodes"], WORK_INODES)
            self.assertEqual(value["history_inodes"], HISTORY_INODES)
            work_result = os.open(
                "from-sandbox",
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=storage.work_descriptor,
            )
            history_result = os.open(
                "from-sandbox",
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=storage.history_descriptor,
            )
            try:
                self.assertEqual(os.read(work_result, 64), b"work\n")
                self.assertEqual(os.read(history_result, 64), b"history\n")
            finally:
                os.close(work_result)
                os.close(history_result)
        self.assertEqual(_scratch_names(), before)

    def test_trampoline_joins_the_real_delegated_workload_cgroup(self) -> None:
        if os.environ.get(REAL_CGROUP_PROBE_ENVIRONMENT) != "1":
            environment = dict(os.environ)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment[REAL_CGROUP_PROBE_ENVIRONMENT] = "1"
            result = subprocess.run(
                delegated_scope_command(
                    (
                        sys.executable,
                        os.fspath(pathlib.Path(__file__).resolve()),
                        (
                            "StorageLaunchRealTest."
                            "test_trampoline_joins_the_real_delegated_"
                            "workload_cgroup"
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

        process: subprocess.Popen[str] | None = None
        block_read, block_write = os.pipe()
        usr_descriptor = os.open(
            "/usr",
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        try:
            with (
                SessionCgroup.create(
                    memory_bytes=1024 * 1024 * 1024,
                    max_tasks=16,
                ) as session,
                self.create() as storage,
            ):
                workload_procs = session.duplicate_workload_procs()
                try:
                    bwrap_argv = _production_bwrap_argv(
                        usr_descriptor=usr_descriptor,
                        work_descriptor=storage.work_descriptor,
                        history_descriptor=storage.history_descriptor,
                        block_descriptor=block_read,
                    )
                    launch = storage.wrap_bwrap_argv(
                        bwrap_argv,
                        pass_fds=(usr_descriptor, block_read),
                        workload_cgroup_procs_fd=workload_procs,
                    )
                    process = launch.spawn(
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                finally:
                    os.close(workload_procs)
                os.close(block_read)
                block_read = -1

                if process is None:
                    raise AssertionError("storage trampoline did not start")
                _wait_for_workload_membership(session, process)
                workload_members = {
                    int(value)
                    for value in (
                        CGROUP_ROOT.joinpath(
                            *session.relative_path.parts[1:],
                            WORKLOAD_CGROUP_NAME,
                            "cgroup.procs",
                        )
                        .read_text(encoding="ascii")
                        .split()
                    )
                }
                self.assertIn(process.pid, workload_members)
                os.write(block_write, b"1")
                os.close(block_write)
                block_write = -1
                stdout, stderr = process.communicate()
                self.assertEqual(
                    process.returncode,
                    0,
                    msg=f"stdout={stdout!r}\nstderr={stderr!r}",
                )
                self.assertFalse(session.workload_populated())
        finally:
            if block_read >= 0:
                os.close(block_read)
            if block_write >= 0:
                os.close(block_write)
            os.close(usr_descriptor)
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()

    def test_closed_launch_rejects_stale_helper_fd_substitution(self) -> None:
        with (
            self.create() as storage,
            tempfile.TemporaryDirectory(
                prefix="model-session-stale-helper.",
                dir="/tmp",
            ) as temporary,
        ):
            helper = pathlib.Path(temporary) / "checkpoint_worker.py"
            helper.write_text("print('ORIGINAL')\n", encoding="utf-8")
            helper.chmod(0o644)
            child_source = pathlib.Path(temporary) / "namespace-child.py"
            original_child_source = (
                launch_module.NAMESPACE_CHILD_SOURCE_PATH.read_text(
                    encoding="utf-8",
                )
            )
            child_source.write_text(
                original_child_source,
                encoding="utf-8",
            )
            child_source.chmod(0o644)
            command = (
                os.fspath(PYTHON_BINARY),
                "-I",
                "-B",
                os.fspath(helper),
            )
            with (
                mock.patch.object(
                    launch_module,
                    "NAMESPACE_CHILD_SOURCE_PATH",
                    child_source,
                ),
                mock.patch.object(
                    storage_module,
                    "TRUSTED_NAMESPACE_HELPERS",
                    frozenset({helper}),
                ),
            ):
                launch = storage.wrap_namespace_command(command)
            stale_argv = launch._argv
            stale_pass_fds = launch._pass_fds
            self.assertEqual(stale_argv[6], "-c")
            self.assertEqual(stale_argv[7], original_child_source)
            self.assertNotIn(os.fspath(child_source), stale_argv)
            boundary = stale_argv.index("--")
            helper_descriptor = int(
                pathlib.PurePosixPath(
                    stale_argv[boundary + 4]
                ).name
            )
            self.assertFalse(hasattr(launch, "argv"))
            self.assertFalse(hasattr(launch, "pass_fds"))
            with self.assertRaises(ModelSessionError) as caught:
                launch.spawn(pass_fds=())
            self.assertEqual(caught.exception.code, "invalid_storage_launch")
            launch.close()
            self.assertEqual(launch._argv, ())
            self.assertEqual(launch._pass_fds, ())

            child_source.write_text(
                "raise RuntimeError('MUTATED_CHILD_SOURCE')\n",
                encoding="utf-8",
            )
            child_source.chmod(0o644)
            replacement = pathlib.Path(temporary) / "replacement.py"
            replacement.write_text(
                "print('REPLACEMENT')\n",
                encoding="utf-8",
            )
            replacement.chmod(0o644)
            replacement_source = os.open(
                replacement,
                os.O_RDONLY | os.O_CLOEXEC,
            )
            if replacement_source != helper_descriptor:
                os.dup2(
                    replacement_source,
                    helper_descriptor,
                    inheritable=False,
                )
                os.close(replacement_source)
            try:
                result = subprocess.run(
                    stale_argv,
                    pass_fds=stale_pass_fds,
                    close_fds=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
            finally:
                os.close(helper_descriptor)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("REPLACEMENT", result.stdout)
            self.assertNotIn("MUTATED_CHILD_SOURCE", result.stderr)

    def test_spawn_failure_consumes_launch_and_closes_helper_image(self) -> None:
        with (
            self.create() as storage,
            tempfile.TemporaryDirectory(
                prefix="model-session-spawn-failure.",
                dir="/tmp",
            ) as temporary,
        ):
            helper = pathlib.Path(temporary) / "checkpoint_worker.py"
            helper.write_text("raise SystemExit(0)\n", encoding="utf-8")
            helper.chmod(0o644)
            command = (
                os.fspath(PYTHON_BINARY),
                "-I",
                "-B",
                os.fspath(helper),
            )
            with mock.patch.object(
                storage_module,
                "TRUSTED_NAMESPACE_HELPERS",
                frozenset({helper}),
            ):
                launch = storage.wrap_namespace_command(command)
            boundary = launch._argv.index("--")
            helper_descriptor = int(
                pathlib.PurePosixPath(
                    launch._argv[boundary + 4]
                ).name
            )
            with (
                mock.patch.object(
                    storage_module.subprocess,
                    "Popen",
                    side_effect=OSError(errno.EIO, "injected"),
                ),
                self.assertRaises(OSError),
            ):
                launch.spawn()
            _assert_closed(self, helper_descriptor)
            with self.assertRaises(ModelSessionError) as caught:
                launch.spawn()
            self.assertEqual(caught.exception.code, "storage_launch_closed")

    def test_stale_launch_rejects_cross_session_fd_substitution(self) -> None:
        mapped_targets: list[int] = []
        source_duplicates: list[int] = []
        saved_helper = -1
        storage_a = self.create()
        try:
            with tempfile.TemporaryDirectory(
                prefix="model-session-cross-session.",
                dir="/tmp",
            ) as temporary:
                helper = pathlib.Path(temporary) / "checkpoint_worker.py"
                helper.write_text(
                    (
                        "import os,sys\n"
                        "descriptor=int(sys.argv[1])\n"
                        "source=os.open('marker',os.O_RDONLY,"
                        "dir_fd=descriptor)\n"
                        "try:\n"
                        " print(os.read(source,64).decode('ascii'))\n"
                        "finally:\n"
                        " os.close(source)\n"
                    ),
                    encoding="utf-8",
                )
                helper.chmod(0o644)
                command = (
                    os.fspath(PYTHON_BINARY),
                    "-I",
                    "-B",
                    os.fspath(helper),
                    str(storage_a.work_descriptor),
                )
                with mock.patch.object(
                    storage_module,
                    "TRUSTED_NAMESPACE_HELPERS",
                    frozenset({helper}),
                ):
                    launch = storage_a.wrap_namespace_command(command)
                stale_argv = launch._argv
                stale_pass_fds = launch._pass_fds
                boundary = stale_argv.index("--")
                helper_descriptor = int(
                    pathlib.PurePosixPath(
                        stale_argv[boundary + 4]
                    ).name
                )
                session_a_targets = (
                    storage_a.work_descriptor,
                    storage_a.history_descriptor,
                    storage_a.user_namespace_descriptor,
                    storage_a.mount_namespace_descriptor,
                )
                saved_helper = fcntl.fcntl(
                    helper_descriptor,
                    fcntl.F_DUPFD_CLOEXEC,
                    256,
                )
                launch.close()
                storage_a.close()

                storage_b = self.create()
                try:
                    marker = os.open(
                        "marker",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=storage_b.work_descriptor,
                    )
                    try:
                        os.write(marker, b"SESSION-B\n")
                    finally:
                        os.close(marker)
                    for descriptor in (
                        storage_b.work_descriptor,
                        storage_b.history_descriptor,
                        storage_b.user_namespace_descriptor,
                        storage_b.mount_namespace_descriptor,
                    ):
                        source_duplicates.append(
                            fcntl.fcntl(
                                descriptor,
                                fcntl.F_DUPFD_CLOEXEC,
                                256,
                            )
                        )
                finally:
                    storage_b.close()

                for target, source in zip(
                    session_a_targets,
                    source_duplicates,
                    strict=True,
                ):
                    os.dup2(source, target, inheritable=False)
                    mapped_targets.append(target)
                os.dup2(
                    saved_helper,
                    helper_descriptor,
                    inheritable=False,
                )
                mapped_targets.append(helper_descriptor)
                result = subprocess.run(
                    stale_argv,
                    pass_fds=stale_pass_fds,
                    close_fds=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("SESSION-B", result.stdout)
        finally:
            storage_a.close()
            for descriptor in reversed(mapped_targets):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            for descriptor in reversed(source_duplicates):
                os.close(descriptor)
            if saved_helper >= 0:
                os.close(saved_helper)

    def test_trusted_namespace_command_allowlist_is_narrow(self) -> None:
        with self.create() as storage:
            module_path = pathlib.Path(storage_module.__file__).resolve()
            with tempfile.TemporaryDirectory(
                prefix="model-session-allowed-helper.",
                dir="/tmp",
            ) as temporary:
                helper = pathlib.Path(temporary) / "checkpoint_worker.py"
                original = (
                    b"import json,os,sys\n"
                    b"descriptor=int(sys.argv[1])\n"
                    b"source=os.open('mode-zero',os.O_RDONLY,"
                    b"dir_fd=descriptor)\n"
                    b"try:\n"
                    b" print(json.dumps({'uid':os.getuid(),"
                    b"'payload':os.read(source,64).decode('ascii'),"
                    b"'user_namespace':os.readlink("
                    b"'/proc/self/ns/user')}))\n"
                    b"finally:\n"
                    b" os.close(source)\n"
                )
                helper.write_bytes(original)
                helper.chmod(0o644)
                command = (
                    os.fspath(PYTHON_BINARY),
                    "-I",
                    "-B",
                    os.fspath(helper),
                    str(storage.work_descriptor),
                )
                with mock.patch.object(
                    storage_module,
                    "TRUSTED_NAMESPACE_HELPERS",
                    frozenset({helper}),
                ):
                    launch = storage.wrap_namespace_command(
                        command,
                        parent_death_signal=signal.SIGTERM,
                    )
                boundary = launch._argv.index("--")
                locked_command = launch._argv[boundary + 1 :]
                self.assertEqual(locked_command[:3], command[:3])
                self.assertEqual(locked_command[4:], command[4:])
                helper_descriptor = int(
                    pathlib.PurePosixPath(locked_command[3]).name
                )
                self.assertIn(helper_descriptor, launch._pass_fds)
                helper.write_bytes(b"raise RuntimeError('mutation')\n")
                self.assertEqual(
                    os.pread(helper_descriptor, len(original), 0),
                    original,
                )
                helper.unlink()
                helper.write_bytes(b"raise RuntimeError('replacement')\n")
                helper.chmod(0o644)
                self.assertEqual(
                    os.pread(helper_descriptor, len(original), 0),
                    original,
                )
                self.assertEqual(launch._argv[2], "TERM")
                mode_zero = os.open(
                    "mode-zero",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=storage.work_descriptor,
                )
                try:
                    os.write(mode_zero, b"retained\n")
                finally:
                    os.close(mode_zero)
                os.fchmod(storage.work_descriptor, 0o000)
                try:
                    process = launch.spawn(
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    stdout, stderr = process.communicate()
                finally:
                    os.fchmod(storage.work_descriptor, 0o700)
                    os.unlink(
                        "mode-zero",
                        dir_fd=storage.work_descriptor,
                    )
                self.assertEqual(process.returncode, 0, stderr)
                helper_result = json.loads(stdout)
                self.assertEqual(helper_result["uid"], 0)
                self.assertEqual(helper_result["payload"], "retained\n")
                self.assertNotEqual(
                    helper_result["user_namespace"],
                    os.readlink("/proc/self/ns/user"),
                )
                _assert_closed(self, helper_descriptor)
                with self.assertRaises(ModelSessionError) as caught:
                    launch.spawn()
                self.assertEqual(
                    caught.exception.code,
                    "storage_launch_closed",
                )
                with mock.patch.object(
                    storage_module,
                    "TRUSTED_NAMESPACE_HELPERS",
                    frozenset({helper}),
                ):
                    with self.assertRaises(ModelSessionError) as caught:
                        storage.wrap_namespace_command(
                            command,
                            parent_death_signal=signal.SIGCHLD,
                        )
                self.assertEqual(
                    caught.exception.code,
                    "invalid_storage_launch",
                )
            for rejected in (
                (
                    os.fspath(PYTHON_BINARY),
                    "-c",
                    "print('not isolated')",
                    os.fspath(module_path),
                ),
                (
                    os.fspath(PYTHON_BINARY),
                    "-I",
                    "-B",
                    "/tmp/not-dotfiles.py",
                ),
                (
                    os.fspath(PYTHON_BINARY),
                    "-I",
                    "-B",
                    os.fspath(module_path),
                ),
                (os.fspath(BWRAP_BINARY), "--help"),
            ):
                with self.subTest(command=rejected):
                    with self.assertRaises(ModelSessionError) as caught:
                        storage.wrap_namespace_command(rejected)
                    self.assertEqual(
                        caught.exception.code,
                        "invalid_storage_launch",
                    )


if __name__ == "__main__":
    unittest.main()
