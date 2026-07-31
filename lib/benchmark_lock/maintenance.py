"""Root administration fence for atomic benchmark service replacement."""

from __future__ import annotations

import contextlib
import os
import socket
from collections.abc import Iterator

from .control_channel import connect_broker
from .errors import BenchmarkLockError
from .protocol import (
    ErrorEvent,
    MaintenanceEvent,
    MaintenanceRequest,
    StatusEvent,
    StatusRequest,
    receive_event,
    send_request,
)


def _require_root() -> None:
    if os.geteuid() != 0:
        raise BenchmarkLockError(
            "benchmark maintenance requires root",
            code="maintenance_not_authorized",
        )


def probe_status() -> None:
    """Require one complete status exchange with the active root broker."""

    _require_root()
    connection: socket.socket | None = None
    try:
        connection = connect_broker()
        send_request(connection, StatusRequest())
        event = receive_event(connection)
        if isinstance(event, ErrorEvent):
            raise BenchmarkLockError(event.message, code=event.code)
        if not isinstance(event, StatusEvent):
            raise BenchmarkLockError(
                "broker returned a non-status event",
                code="invalid_benchmark_protocol",
            )
    finally:
        if connection is not None:
            connection.close()


@contextlib.contextmanager
def hold_maintenance() -> Iterator[None]:
    """Fence admissions until the caller's root-owned channel is closed."""

    _require_root()
    connection: socket.socket | None = None
    try:
        connection = connect_broker()
        send_request(connection, MaintenanceRequest())
        event = receive_event(connection)
        if isinstance(event, ErrorEvent):
            raise BenchmarkLockError(event.message, code=event.code)
        if not isinstance(event, MaintenanceEvent):
            raise BenchmarkLockError(
                "broker returned a non-maintenance event",
                code="invalid_benchmark_protocol",
            )
        yield
    finally:
        if connection is not None:
            connection.close()
