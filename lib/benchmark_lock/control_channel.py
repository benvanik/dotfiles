"""Authenticated transport shared by benchmark users and administration.

Its group, endpoint, socket type, and root-peer attestation are the stable v1
cutover boundary between a live source checkout and an installed broker.
"""

from __future__ import annotations

import grp
import os
import socket

from .errors import BenchmarkLockError
from .linux import CONTROL_SOCKET_PATH, require_root_peer, validate_root_socket_path


BENCHMARK_GROUP_NAME = "benchmark"


def _benchmark_group_id() -> int:
    try:
        return grp.getgrnam(BENCHMARK_GROUP_NAME).gr_gid
    except KeyError as error:
        raise BenchmarkLockError(
            f"system group {BENCHMARK_GROUP_NAME!r} is not installed",
            code="benchmark_broker_unavailable",
        ) from error


def connect_broker() -> socket.socket:
    """Connect one CLOEXEC sequenced-packet channel to the root broker."""

    group_id = _benchmark_group_id()
    validate_root_socket_path(
        CONTROL_SOCKET_PATH,
        expected_group_id=group_id,
    )
    socket_type = socket.SOCK_SEQPACKET | getattr(
        socket,
        "SOCK_CLOEXEC",
        0,
    )
    try:
        connection = socket.socket(socket.AF_UNIX, socket_type)
    except OSError as error:
        raise BenchmarkLockError(
            f"cannot create benchmark broker channel: {error}",
            code="benchmark_broker_unavailable",
        ) from error
    try:
        connection.set_inheritable(False)
        connection.connect(os.fspath(CONTROL_SOCKET_PATH))
        require_root_peer(connection)
        return connection
    except BenchmarkLockError:
        connection.close()
        raise
    except OSError as error:
        connection.close()
        raise BenchmarkLockError(
            f"cannot connect to benchmark broker: {error}",
            code="benchmark_broker_unavailable",
        ) from error
