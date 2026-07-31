from __future__ import annotations

import socket
import unittest
from unittest import mock

from benchmark_lock.errors import BenchmarkLockError
from benchmark_lock.maintenance import hold_maintenance, probe_status
from benchmark_lock.protocol import (
    ErrorEvent,
    MaintenanceEvent,
    MaintenanceRequest,
    StatusEvent,
    StatusRequest,
)


class BenchmarkMaintenanceTest(unittest.TestCase):
    def test_status_probe_requires_one_complete_root_exchange(self) -> None:
        connection = mock.Mock(spec=socket.socket)
        with (
            mock.patch("benchmark_lock.maintenance.os.geteuid", return_value=0),
            mock.patch(
                "benchmark_lock.maintenance.connect_broker",
                return_value=connection,
            ),
            mock.patch("benchmark_lock.maintenance.send_request") as send,
            mock.patch(
                "benchmark_lock.maintenance.receive_event",
                return_value=StatusEvent(None, 0, "idle"),
            ),
        ):
            probe_status()

        send.assert_called_once_with(connection, StatusRequest())
        connection.close.assert_called_once_with()

    def test_status_probe_rejects_non_root_errors_and_wrong_events(self) -> None:
        with (
            mock.patch("benchmark_lock.maintenance.os.geteuid", return_value=1000),
            self.assertRaises(BenchmarkLockError) as caught,
        ):
            probe_status()
        self.assertEqual(caught.exception.code, "maintenance_not_authorized")

        for event, expected_code in (
            (ErrorEvent("broker_failed", "Broker failed."), "broker_failed"),
            (MaintenanceEvent(), "invalid_benchmark_protocol"),
        ):
            with self.subTest(event=event):
                connection = mock.Mock(spec=socket.socket)
                with (
                    mock.patch("benchmark_lock.maintenance.os.geteuid", return_value=0),
                    mock.patch(
                        "benchmark_lock.maintenance.connect_broker",
                        return_value=connection,
                    ),
                    mock.patch("benchmark_lock.maintenance.send_request"),
                    mock.patch(
                        "benchmark_lock.maintenance.receive_event",
                        return_value=event,
                    ),
                    self.assertRaises(BenchmarkLockError) as caught,
                ):
                    probe_status()
                self.assertEqual(caught.exception.code, expected_code)
                connection.close.assert_called_once_with()

    def test_root_channel_is_held_for_the_context_lifetime(self) -> None:
        connection = mock.Mock(spec=socket.socket)
        with (
            mock.patch("benchmark_lock.maintenance.os.geteuid", return_value=0),
            mock.patch(
                "benchmark_lock.maintenance.connect_broker",
                return_value=connection,
            ),
            mock.patch("benchmark_lock.maintenance.send_request") as send,
            mock.patch(
                "benchmark_lock.maintenance.receive_event",
                return_value=MaintenanceEvent(),
            ),
        ):
            with hold_maintenance():
                connection.close.assert_not_called()
            connection.close.assert_called_once_with()
        send.assert_called_once_with(connection, MaintenanceRequest())

    def test_non_root_and_broker_rejection_fail_loud(self) -> None:
        with (
            mock.patch("benchmark_lock.maintenance.os.geteuid", return_value=1000),
            self.assertRaises(BenchmarkLockError) as caught,
        ):
            with hold_maintenance():
                self.fail("non-root maintenance context was entered")
        self.assertEqual(caught.exception.code, "maintenance_not_authorized")

        connection = mock.Mock(spec=socket.socket)
        with (
            mock.patch("benchmark_lock.maintenance.os.geteuid", return_value=0),
            mock.patch(
                "benchmark_lock.maintenance.connect_broker",
                return_value=connection,
            ),
            mock.patch("benchmark_lock.maintenance.send_request"),
            mock.patch(
                "benchmark_lock.maintenance.receive_event",
                return_value=ErrorEvent("maintenance_busy", "Lease is active."),
            ),
            self.assertRaises(BenchmarkLockError) as caught,
        ):
            with hold_maintenance():
                self.fail("rejected maintenance context was entered")
        self.assertEqual(caught.exception.code, "maintenance_busy")
        connection.close.assert_called_once_with()
