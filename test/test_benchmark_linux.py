from __future__ import annotations

import errno
import os
import pathlib
import socket
import stat
import unittest
from types import SimpleNamespace
from unittest import mock

from benchmark_lock.errors import BenchmarkLockError
from benchmark_lock.linux import (
    ADDR_NO_RANDOMIZE,
    PeerCredentials,
    attest_client_peer,
    disable_aslr_for_exec,
    open_peer_pidfd,
    peer_credentials,
    require_root_peer,
    validate_root_socket_path,
)


class BenchmarkLinuxTest(unittest.TestCase):
    def test_peer_credentials_and_pidfd_are_kernel_held(self) -> None:
        server, client = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET,
        )
        identity = None
        try:
            identity = attest_client_peer(server)
            self.assertEqual(
                identity.credentials,
                PeerCredentials(
                    pid=os.getpid(),
                    uid=os.getuid(),
                    gid=os.getgid(),
                ),
            )
            self.assertFalse(os.get_inheritable(identity.pid_descriptor))
            os.fstat(identity.pid_descriptor)
        finally:
            if identity is not None:
                identity.close()
            server.close()
            client.close()

    def test_unsupported_architecture_has_no_pid_open_fallback(self) -> None:
        server, client = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET,
        )
        try:
            with mock.patch(
                "benchmark_lock.linux.os.pidfd_open",
                create=True,
            ) as pidfd_open:
                with self.assertRaises(BenchmarkLockError) as caught:
                    open_peer_pidfd(server, architecture="sparc64")
            self.assertEqual(
                caught.exception.code,
                "benchmark_platform_unsupported",
            )
            pidfd_open.assert_not_called()
        finally:
            server.close()
            client.close()

    def test_root_peer_rejects_the_unprivileged_real_peer(self) -> None:
        if os.getuid() == 0:
            self.skipTest("test requires an unprivileged test process")
        server, client = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET,
        )
        try:
            self.assertEqual(peer_credentials(server).uid, os.getuid())
            with self.assertRaises(BenchmarkLockError) as caught:
                require_root_peer(server)
            self.assertEqual(
                caught.exception.code,
                "invalid_benchmark_channel",
            )
        finally:
            server.close()
            client.close()

    def test_root_socket_identity_is_exact(self) -> None:
        path = pathlib.Path("/run/benchmarkd/control.sock")
        parent = SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o755,
            st_uid=0,
            st_gid=0,
        )
        endpoint = SimpleNamespace(
            st_mode=stat.S_IFSOCK | 0o660,
            st_uid=0,
            st_gid=742,
        )

        def lstat(candidate):
            return parent if pathlib.Path(candidate) == path.parent else endpoint

        with mock.patch("benchmark_lock.linux.os.lstat", side_effect=lstat):
            validate_root_socket_path(path, expected_group_id=742)

        unsafe_parent = SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o775,
            st_uid=0,
            st_gid=0,
        )
        with mock.patch(
            "benchmark_lock.linux.os.lstat",
            side_effect=(unsafe_parent, endpoint),
        ):
            with self.assertRaises(BenchmarkLockError):
                validate_root_socket_path(path, expected_group_id=742)

        unsafe_endpoints = (
            SimpleNamespace(
                st_mode=stat.S_IFSOCK | 0o600,
                st_uid=0,
                st_gid=742,
            ),
            SimpleNamespace(
                st_mode=stat.S_IFSOCK | 0o660,
                st_uid=1000,
                st_gid=742,
            ),
            SimpleNamespace(
                st_mode=stat.S_IFSOCK | 0o660,
                st_uid=0,
                st_gid=743,
            ),
        )
        for unsafe_endpoint in unsafe_endpoints:
            with self.subTest(metadata=unsafe_endpoint):
                with mock.patch(
                    "benchmark_lock.linux.os.lstat",
                    side_effect=(parent, unsafe_endpoint),
                ):
                    with self.assertRaises(BenchmarkLockError):
                        validate_root_socket_path(
                            path,
                            expected_group_id=742,
                        )

    def test_personality_is_set_and_verified(self) -> None:
        calls: list[int] = []
        results = iter((0, 0, ADDR_NO_RANDOMIZE))

        def personality(value: int) -> int:
            calls.append(value)
            return next(results)

        with mock.patch(
            "benchmark_lock.linux._PERSONALITY",
            side_effect=personality,
        ):
            self.assertEqual(disable_aslr_for_exec(), 0)
        self.assertEqual(
            calls,
            [
                0xFFFFFFFF,
                ADDR_NO_RANDOMIZE,
                0xFFFFFFFF,
            ],
        )

    def test_existing_process_personality_is_preserved(self) -> None:
        existing = 0x0008 | ADDR_NO_RANDOMIZE
        with mock.patch(
            "benchmark_lock.linux._PERSONALITY",
            return_value=existing,
        ) as personality:
            self.assertEqual(disable_aslr_for_exec(), existing)
        personality.assert_called_once_with(0xFFFFFFFF)

    def test_personality_failure_is_never_a_silent_fallback(self) -> None:
        def denied(_value: int) -> int:
            import ctypes

            ctypes.set_errno(errno.EPERM)
            return -1

        with mock.patch(
            "benchmark_lock.linux._PERSONALITY",
            side_effect=denied,
        ):
            with self.assertRaises(BenchmarkLockError) as caught:
                disable_aslr_for_exec()
        self.assertEqual(
            caught.exception.code,
            "benchmark_aslr_control_failed",
        )
        self.assertIn("Operation not permitted", str(caught.exception))
