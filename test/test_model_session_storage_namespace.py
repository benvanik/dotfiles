from __future__ import annotations

import errno
import gc
import os
import pathlib
import select
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import model_session.storage_namespace as storage_module
import model_session.storage_launch as launch_module
from model_session.errors import ModelSessionError
from model_session.storage_namespace import (
    PYTHON_BINARY,
    SETPRIV_BINARY,
    UNSHARE_BINARY,
    create_storage_namespace,
)
from model_session.storage_limits import StoragePoolLimits


PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
WORK_BYTES = PAGE_SIZE * 16
WORK_INODES = 8
HISTORY_BYTES = PAGE_SIZE * 8
HISTORY_INODES = 5

HIDDEN_FORK_CREATOR = r"""
import array
import os
import socket
import struct
import sys

socket_descriptor = int(sys.argv[1])
claimed_pid = os.getpid()
payload = struct.pack(
    "!24s32sQQQQq",
    bytes.fromhex(sys.argv[2]),
    bytes.fromhex(sys.argv[3]),
    int(sys.argv[4]),
    int(sys.argv[5]),
    int(sys.argv[6]),
    int(sys.argv[7]),
    claimed_pid,
)
if os.fork() != 0:
    os._exit(0)

sock = socket.socket(fileno=socket_descriptor)
descriptors = [
    os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)
    for _index in range(4)
]
rights = array.array("i", descriptors)
sock.sendmsg(
    [payload],
    [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights.tobytes())],
)
for descriptor in descriptors:
    os.close(descriptor)
sock.close()
os._exit(0)
"""

PREARM_SUPERVISOR = r"""
import os
import signal
import sys

arguments = tuple(
    str(os.getpid()) if value == "EXPECTED_PARENT_PID" else value
    for value in sys.argv[1:]
)
child_pid = os.fork()
if child_pid == 0:
    os.kill(os.getpid(), signal.SIGSTOP)
    os.execve(
        arguments[0],
        arguments,
        {"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
    )
    os._exit(72)
os.write(1, f"{child_pid}\n".encode("ascii"))
os._exit(0)
"""


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


def _exhaust_bytes(descriptor: int, expected_bytes: int) -> None:
    file_descriptor = os.open(
        "capacity",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
        dir_fd=descriptor,
    )
    written = 0
    try:
        block = b"x" * PAGE_SIZE
        while True:
            try:
                written += os.write(file_descriptor, block)
            except OSError as error:
                if error.errno != errno.ENOSPC:
                    raise
                break
    finally:
        os.close(file_descriptor)
        os.unlink("capacity", dir_fd=descriptor)
    if written != expected_bytes:
        raise AssertionError(
            f"tmpfs accepted {written} bytes, expected {expected_bytes}"
        )


def _exhaust_inodes(descriptor: int, expected_inodes: int) -> None:
    created: list[str] = []
    try:
        while True:
            name = f"inode-{len(created)}"
            try:
                file_descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=descriptor,
                )
            except OSError as error:
                if error.errno != errno.ENOSPC:
                    raise
                break
            else:
                os.close(file_descriptor)
                created.append(name)
    finally:
        for name in created:
            os.unlink(name, dir_fd=descriptor)
    if len(created) != expected_inodes - 1:
        raise AssertionError(
            f"tmpfs admitted {len(created)} children, "
            f"expected {expected_inodes - 1}"
        )



class StorageNamespaceContractTest(unittest.TestCase):
    def test_direct_construction_fails_without_destructor_error(self) -> None:
        unraisable: list[object] = []
        previous_hook = sys.unraisablehook
        sys.unraisablehook = unraisable.append
        try:
            with self.assertRaises(ModelSessionError) as caught:
                storage_module.StorageNamespace(
                    object(),
                    work_descriptor=-1,
                    history_descriptor=-1,
                    user_namespace_descriptor=-1,
                    mount_namespace_descriptor=-1,
                    work_limits=StoragePoolLimits(
                        WORK_BYTES,
                        WORK_INODES,
                    ),
                    history_limits=StoragePoolLimits(
                        HISTORY_BYTES,
                        HISTORY_INODES,
                    ),
                )
            gc.collect()
        finally:
            sys.unraisablehook = previous_hook
        self.assertEqual(caught.exception.code, "invalid_storage_namespace")
        self.assertEqual(unraisable, [])

    def test_limits_reject_non_page_aligned_and_boolean_values(self) -> None:
        for value in (True, PAGE_SIZE + 1):
            with self.subTest(value=value):
                with self.assertRaises(ModelSessionError) as caught:
                    StoragePoolLimits(value, 2)
                self.assertEqual(caught.exception.code, "invalid_storage_limit")
        with self.assertRaises(ModelSessionError):
            StoragePoolLimits(PAGE_SIZE, True)
        with self.assertRaises(ModelSessionError) as caught:
            storage_module._parent_death_signal_name(True)
        self.assertEqual(caught.exception.code, "invalid_storage_launch")

    def test_creator_command_is_fixed_and_scratch_is_removed_on_failure(
        self,
    ) -> None:
        before = _scratch_names()
        observed: list[tuple[str, ...]] = []

        def reject(
            command,
            *,
            child_socket,
            parent_socket,
        ):
            del child_socket, parent_socket
            observed.append(command)
            raise ModelSessionError("injected", code="injected")

        with mock.patch.object(storage_module, "_run_creator", side_effect=reject):
            with self.assertRaises(ModelSessionError) as caught:
                create_storage_namespace(
                    work_bytes=WORK_BYTES,
                    work_inodes=WORK_INODES,
                    history_bytes=HISTORY_BYTES,
                    history_inodes=HISTORY_INODES,
                )
        self.assertEqual(caught.exception.code, "injected")
        self.assertEqual(_scratch_names(), before)
        command = observed[0]
        self.assertEqual(
            command[:12],
            (
                os.fspath(SETPRIV_BINARY),
                "--pdeathsig",
                "KILL",
                os.fspath(UNSHARE_BINARY),
                "--user",
                "--map-root-user",
                "--mount",
                "--propagation",
                "private",
                os.fspath(PYTHON_BINARY),
                "-I",
                "-B",
            ),
        )
        self.assertEqual(command[12], "-c")
        self.assertEqual(
            command[13],
            launch_module.snapshot_namespace_child_source(),
        )
        self.assertEqual(command[14], storage_module._CREATOR_COMMAND)

    def test_creator_executes_an_immutable_child_source_snapshot(self) -> None:
        source = launch_module.NAMESPACE_CHILD_SOURCE_PATH.read_text(
            encoding="utf-8",
        )
        original_run_creator = storage_module._run_creator
        observed: list[tuple[str, ...]] = []
        with tempfile.TemporaryDirectory(
            prefix="model-session-creator-source.",
            dir="/tmp",
        ) as temporary:
            child_source = pathlib.Path(temporary) / "namespace-child.py"
            child_source.write_text(source, encoding="utf-8")
            child_source.chmod(0o644)

            def mutate_after_command_construction(
                command,
                *,
                child_socket,
                parent_socket,
            ):
                observed.append(command)
                child_source.write_text(
                    "raise RuntimeError('MUTATED_CHILD_SOURCE')\n",
                    encoding="utf-8",
                )
                child_source.chmod(0o644)
                return original_run_creator(
                    command,
                    child_socket=child_socket,
                    parent_socket=parent_socket,
                )

            with (
                mock.patch.object(
                    launch_module,
                    "NAMESPACE_CHILD_SOURCE_PATH",
                    child_source,
                ),
                mock.patch.object(
                    storage_module,
                    "_run_creator",
                    side_effect=mutate_after_command_construction,
                ),
                create_storage_namespace(
                    work_bytes=WORK_BYTES,
                    work_inodes=WORK_INODES,
                    history_bytes=HISTORY_BYTES,
                    history_inodes=HISTORY_INODES,
                ),
            ):
                pass
        self.assertEqual(observed[0][12], "-c")
        self.assertEqual(observed[0][13], source)
        self.assertNotIn(os.fspath(child_source), observed[0])

    def test_protocol_rejects_mismatched_creator_pid_and_closes_fds(
        self,
    ) -> None:
        nonce = bytes(range(32))
        descriptors = tuple(os.open("/dev/null", os.O_RDONLY) for _ in range(4))
        payload = storage_module._PROTOCOL.pack(
            storage_module._PROTOCOL_MAGIC,
            nonce,
            WORK_BYTES,
            WORK_INODES,
            HISTORY_BYTES,
            HISTORY_INODES,
            777,
        )
        with (
            mock.patch.object(
                storage_module.secrets,
                "token_bytes",
                return_value=nonce,
            ),
            mock.patch.object(
                storage_module,
                "_run_creator",
                return_value=(778, payload, descriptors),
            ),
        ):
            with self.assertRaises(ModelSessionError) as caught:
                create_storage_namespace(
                    work_bytes=WORK_BYTES,
                    work_inodes=WORK_INODES,
                    history_bytes=HISTORY_BYTES,
                    history_inodes=HISTORY_INODES,
                )
        self.assertEqual(caught.exception.code, "invalid_storage_protocol")
        for descriptor in descriptors:
            _assert_closed(self, descriptor)

    def test_protocol_rejects_wrong_fd_types_and_closes_fds(self) -> None:
        nonce = bytes(reversed(range(32)))
        descriptors = tuple(os.open("/dev/null", os.O_RDONLY) for _ in range(4))
        process_id = os.getpid()
        payload = storage_module._PROTOCOL.pack(
            storage_module._PROTOCOL_MAGIC,
            nonce,
            WORK_BYTES,
            WORK_INODES,
            HISTORY_BYTES,
            HISTORY_INODES,
            process_id,
        )
        with (
            mock.patch.object(
                storage_module.secrets,
                "token_bytes",
                return_value=nonce,
            ),
            mock.patch.object(
                storage_module,
                "_run_creator",
                return_value=(process_id, payload, descriptors),
            ),
        ):
            with self.assertRaises(ModelSessionError) as caught:
                create_storage_namespace(
                    work_bytes=WORK_BYTES,
                    work_inodes=WORK_INODES,
                    history_bytes=HISTORY_BYTES,
                    history_inodes=HISTORY_INODES,
                )
        self.assertEqual(caught.exception.code, "invalid_storage_namespace")
        for descriptor in descriptors:
            _assert_closed(self, descriptor)

    def test_owner_construction_failure_closes_all_transferred_fds(self) -> None:
        nonce = bytes(reversed(range(32)))
        descriptors = tuple(os.open("/dev/null", os.O_RDONLY) for _ in range(4))
        process_id = os.getpid()
        payload = storage_module._PROTOCOL.pack(
            storage_module._PROTOCOL_MAGIC,
            nonce,
            WORK_BYTES,
            WORK_INODES,
            HISTORY_BYTES,
            HISTORY_INODES,
            process_id,
        )
        with (
            mock.patch.object(
                storage_module.secrets,
                "token_bytes",
                return_value=nonce,
            ),
            mock.patch.object(
                storage_module,
                "_run_creator",
                return_value=(process_id, payload, descriptors),
            ),
            mock.patch.object(
                storage_module,
                "_validate_received_descriptors",
            ),
            mock.patch.object(
                storage_module,
                "_descriptor_identity",
                side_effect=OSError(errno.EBADF, "injected"),
            ),
        ):
            with self.assertRaises(OSError):
                create_storage_namespace(
                    work_bytes=WORK_BYTES,
                    work_inodes=WORK_INODES,
                    history_bytes=HISTORY_BYTES,
                    history_inodes=HISTORY_INODES,
                )
        for descriptor in descriptors:
            _assert_closed(self, descriptor)

    def test_creator_protocol_rejects_a_hidden_fork_sender(self) -> None:
        open_descriptors_before = frozenset(os.listdir("/proc/self/fd"))
        scratch_before = _scratch_names()
        nonce = bytes(range(32))
        parent_socket, child_socket = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET,
        )
        parent_socket.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_PASSCRED,
            1,
        )
        command = (
            os.fspath(PYTHON_BINARY),
            "-I",
            "-B",
            "-c",
            HIDDEN_FORK_CREATOR,
            str(child_socket.fileno()),
            storage_module._PROTOCOL_MAGIC.hex(),
            nonce.hex(),
            str(WORK_BYTES),
            str(WORK_INODES),
            str(HISTORY_BYTES),
            str(HISTORY_INODES),
        )
        try:
            with self.assertRaises(ModelSessionError) as caught:
                storage_module._run_creator(
                    command,
                    child_socket=child_socket,
                    parent_socket=parent_socket,
                )
        finally:
            parent_socket.close()
            try:
                child_socket.close()
            except OSError:
                pass
        self.assertEqual(caught.exception.code, "invalid_storage_protocol")
        self.assertEqual(_scratch_names(), scratch_before)
        self.assertEqual(
            frozenset(os.listdir("/proc/self/fd")),
            open_descriptors_before,
        )

class StorageNamespaceRealTest(unittest.TestCase):
    def create(self):
        return create_storage_namespace(
            work_bytes=WORK_BYTES,
            work_inodes=WORK_INODES,
            history_bytes=HISTORY_BYTES,
            history_inodes=HISTORY_INODES,
        )

    def test_creator_exits_scratch_disappears_and_fds_pin_exact_pools(
        self,
    ) -> None:
        before = _scratch_names()
        storage = self.create()
        descriptors = (
            storage.work_descriptor,
            storage.history_descriptor,
            storage.user_namespace_descriptor,
            storage.mount_namespace_descriptor,
        )
        try:
            self.assertEqual(_scratch_names(), before)
            work = os.fstatvfs(storage.work_descriptor)
            history = os.fstatvfs(storage.history_descriptor)
            self.assertEqual(work.f_blocks * work.f_frsize, WORK_BYTES)
            self.assertEqual(history.f_blocks * history.f_frsize, HISTORY_BYTES)
            self.assertEqual(work.f_files, WORK_INODES)
            self.assertEqual(history.f_files, HISTORY_INODES)
            self.assertEqual(
                work.f_flag & (os.ST_NOSUID | os.ST_NODEV),
                os.ST_NOSUID | os.ST_NODEV,
            )
            self.assertEqual(
                history.f_flag & (os.ST_NOSUID | os.ST_NODEV),
                os.ST_NOSUID | os.ST_NODEV,
            )
            work_metadata = os.fstat(storage.work_descriptor)
            history_metadata = os.fstat(storage.history_descriptor)
            self.assertNotEqual(work_metadata.st_dev, history_metadata.st_dev)
            self.assertEqual(stat.S_IMODE(work_metadata.st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(history_metadata.st_mode), 0o700)
            work_file = os.open(
                "after-creator-exit",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=storage.work_descriptor,
            )
            os.close(work_file)
            os.unlink("after-creator-exit", dir_fd=storage.work_descriptor)
        finally:
            storage.close()
        for descriptor in descriptors:
            _assert_closed(self, descriptor)
        self.assertEqual(_scratch_names(), before)

    def test_creator_prearm_parent_death_leaves_no_process_or_scratch(
        self,
    ) -> None:
        scratch_before = _scratch_names()
        nonce = bytes(range(32)).hex()
        root = f"/tmp/model-session-storage.{nonce}"
        creator_command = (
            os.fspath(SETPRIV_BINARY),
            "--pdeathsig",
            "KILL",
            os.fspath(UNSHARE_BINARY),
            "--user",
            "--map-root-user",
            "--mount",
            "--propagation",
            "private",
            os.fspath(PYTHON_BINARY),
            "-I",
            "-B",
            "-c",
            launch_module.snapshot_namespace_child_source(),
            storage_module._CREATOR_COMMAND,
            "3",
            "EXPECTED_PARENT_PID",
            f"{root}/work",
            f"{root}/history",
            str(WORK_BYTES),
            str(WORK_INODES),
            str(HISTORY_BYTES),
            str(HISTORY_INODES),
            nonce,
        )
        supervisor = subprocess.Popen(
            (
                sys.executable,
                "-I",
                "-B",
                "-c",
                PREARM_SUPERVISOR,
                *creator_command,
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertIsNotNone(supervisor.stdout)
        child_pid = int(supervisor.stdout.readline().strip())
        child_pidfd = os.pidfd_open(child_pid)
        try:
            self.assertEqual(supervisor.wait(), 0)
            os.kill(child_pid, signal.SIGCONT)
            _wait_for_pidfd_exit(child_pidfd)
            _remaining_stdout, stderr = supervisor.communicate()
            self.assertEqual(stderr, "")
        finally:
            os.close(child_pidfd)
            if supervisor.poll() is None:
                supervisor.kill()
                supervisor.wait()
        self.assertEqual(_scratch_names(), scratch_before)

    def test_both_pools_enforce_exact_byte_and_inode_enospc(self) -> None:
        with self.create() as storage:
            for descriptor, byte_limit, inode_limit in (
                (storage.work_descriptor, WORK_BYTES, WORK_INODES),
                (
                    storage.history_descriptor,
                    HISTORY_BYTES,
                    HISTORY_INODES,
                ),
            ):
                _exhaust_bytes(descriptor, byte_limit)
                _exhaust_inodes(descriptor, inode_limit)

    def test_close_never_closes_a_reused_descriptor_number(self) -> None:
        storage = self.create()
        descriptor = storage.work_descriptor
        os.close(descriptor)
        replacement_source = os.open("/dev/null", os.O_RDONLY)
        if replacement_source == descriptor:
            replacement = replacement_source
        else:
            os.dup2(replacement_source, descriptor, inheritable=False)
            os.close(replacement_source)
            replacement = descriptor
        with self.assertRaises(ModelSessionError) as caught:
            _unused = storage.work_descriptor
        self.assertEqual(caught.exception.code, "invalid_storage_namespace")
        storage.close()
        try:
            self.assertTrue(stat.S_ISCHR(os.fstat(replacement).st_mode))
        finally:
            os.close(replacement)

if __name__ == "__main__":
    unittest.main()
