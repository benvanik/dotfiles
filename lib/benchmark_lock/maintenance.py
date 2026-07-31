"""Root administration fence for atomic benchmark service replacement."""

from __future__ import annotations

import contextlib
import os
import socket
from collections.abc import Iterator

from .client import connect_broker
from .errors import BenchmarkLockError
from .protocol import (
    ErrorEvent,
    MaintenanceEvent,
    MaintenanceRequest,
    receive_event,
    send_request,
)


@contextlib.contextmanager
def hold_maintenance() -> Iterator[None]:
    """Fence admissions until the caller's root-owned channel is closed."""

    if os.geteuid() != 0:
        raise BenchmarkLockError(
            "benchmark maintenance requires root",
            code="maintenance_not_authorized",
        )
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
