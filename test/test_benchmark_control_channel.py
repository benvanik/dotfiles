from __future__ import annotations

import socket
import unittest
from types import SimpleNamespace
from unittest import mock

from benchmark_lock.control_channel import BENCHMARK_GROUP_NAME, connect_broker
from benchmark_lock.errors import BenchmarkLockError


class BenchmarkControlChannelTest(unittest.TestCase):
    def test_connect_uses_validated_root_seqpacket_endpoint(self) -> None:
        connection = mock.Mock(spec=socket.socket)
        group = SimpleNamespace(gr_gid=742)
        with (
            mock.patch(
                "benchmark_lock.control_channel.grp.getgrnam",
                return_value=group,
            ) as get_group,
            mock.patch(
                "benchmark_lock.control_channel.validate_root_socket_path"
            ) as validate,
            mock.patch(
                "benchmark_lock.control_channel.require_root_peer"
            ) as require_root,
            mock.patch(
                "benchmark_lock.control_channel.socket.socket",
                return_value=connection,
            ) as socket_factory,
        ):
            self.assertIs(connect_broker(), connection)

        self.assertEqual(BENCHMARK_GROUP_NAME, "benchmark")
        get_group.assert_called_once_with("benchmark")
        validate.assert_called_once()
        self.assertEqual(validate.call_args.kwargs["expected_group_id"], 742)
        socket_factory.assert_called_once_with(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET | getattr(socket, "SOCK_CLOEXEC", 0),
        )
        connection.set_inheritable.assert_called_once_with(False)
        connection.connect.assert_called_once_with("/run/benchmarkd/control.sock")
        require_root.assert_called_once_with(connection)
        connection.close.assert_not_called()

    def test_missing_group_fails_before_socket_creation(self) -> None:
        with (
            mock.patch(
                "benchmark_lock.control_channel.grp.getgrnam",
                side_effect=KeyError("benchmark"),
            ),
            mock.patch("benchmark_lock.control_channel.socket.socket") as create,
            self.assertRaises(BenchmarkLockError) as caught,
        ):
            connect_broker()

        self.assertEqual(caught.exception.code, "benchmark_broker_unavailable")
        create.assert_not_called()

    def test_connection_or_peer_failure_closes_the_channel(self) -> None:
        failures = (
            OSError("connect failed"),
            BenchmarkLockError("wrong peer", code="invalid_benchmark_channel"),
        )
        for failure in failures:
            with self.subTest(failure=failure):
                connection = mock.Mock(spec=socket.socket)
                group = SimpleNamespace(gr_gid=742)
                if isinstance(failure, OSError):
                    connection.connect.side_effect = failure
                    peer_effect = None
                else:
                    peer_effect = failure
                with (
                    mock.patch(
                        "benchmark_lock.control_channel.grp.getgrnam",
                        return_value=group,
                    ),
                    mock.patch(
                        "benchmark_lock.control_channel.validate_root_socket_path"
                    ),
                    mock.patch(
                        "benchmark_lock.control_channel.require_root_peer",
                        side_effect=peer_effect,
                    ),
                    mock.patch(
                        "benchmark_lock.control_channel.socket.socket",
                        return_value=connection,
                    ),
                    self.assertRaises(BenchmarkLockError) as caught,
                ):
                    connect_broker()

                self.assertIn(
                    caught.exception.code,
                    {"benchmark_broker_unavailable", "invalid_benchmark_channel"},
                )
                connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
