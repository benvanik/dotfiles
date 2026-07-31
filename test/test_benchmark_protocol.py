from __future__ import annotations

import json
import os
import socket
import unittest

from benchmark_lock.errors import BenchmarkLockError
from benchmark_lock.protocol import (
    EVENT_SCHEMA,
    MAX_PACKET_BYTES,
    REQUEST_SCHEMA,
    AcquireRequest,
    ActiveLease,
    ErrorEvent,
    GrantedEvent,
    MaintenanceEvent,
    MaintenanceRequest,
    QueuedEvent,
    StatusEvent,
    StatusRequest,
    WaitingEvent,
    canonical_json_bytes,
    enable_sender_credentials,
    encode_event,
    encode_request,
    parse_event,
    parse_request,
    receive_event,
    receive_request,
    send_event,
    send_request,
)


LEASE_ID = "a" * 32


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("ascii")


class BenchmarkProtocolTest(unittest.TestCase):
    def test_packet_root_must_be_an_object(self) -> None:
        with self.assertRaises(BenchmarkLockError) as caught:
            canonical_json_bytes(  # type: ignore[arg-type]
                ["not", "an", "object"]
            )
        self.assertEqual(
            caught.exception.code,
            "invalid_benchmark_protocol",
        )

    def test_request_round_trip_has_exact_fields(self) -> None:
        acquire = AcquireRequest(
            label="gfx1100 kernel 7",
            inherited_lease_id="b" * 32,
        )
        self.assertEqual(parse_request(encode_request(acquire)), acquire)
        self.assertEqual(
            encode_request(acquire),
            _canonical(
                {
                    "inherited_lease_id": "b" * 32,
                    "label": "gfx1100 kernel 7",
                    "operation": "acquire",
                    "schema": REQUEST_SCHEMA,
                }
            ),
        )
        self.assertEqual(
            parse_request(encode_request(StatusRequest())),
            StatusRequest(),
        )
        self.assertEqual(
            parse_request(encode_request(MaintenanceRequest())),
            MaintenanceRequest(),
        )

    def test_event_round_trip_has_exact_fields(self) -> None:
        active = ActiveLease(
            lease_id="b" * 32,
            pid=1234,
            uid=1000,
            label="active benchmark",
            elapsed_seconds=67,
        )
        events = (
            QueuedEvent(LEASE_ID, 2, active),
            WaitingEvent(LEASE_ID, 1, None),
            GrantedEvent(LEASE_ID, "performance"),
            ErrorEvent("policy_failed", "The fixed policy failed."),
            StatusEvent(active, 3, "held"),
            MaintenanceEvent(),
        )
        for event in events:
            with self.subTest(event=event):
                self.assertEqual(parse_event(encode_event(event)), event)

    def test_noncanonical_or_unbounded_json_is_rejected(self) -> None:
        canonical = encode_request(StatusRequest())
        specimens = (
            json.dumps({"operation": "status", "schema": REQUEST_SCHEMA}).encode(
                "ascii"
            )
            + b"\n",
            canonical[:-1],
            canonical + b"x",
            b'{"operation":"status","operation":"acquire",'
            b'"schema":"benchmarkd.request.v1"}\n',
            b"\xff\n",
            b"[" + b"[" * 1000 + b"]" * 1000 + b"]\n",
            b"x" * (MAX_PACKET_BYTES + 1),
        )
        for payload in specimens:
            with self.subTest(size=len(payload)):
                with self.assertRaises(BenchmarkLockError) as caught:
                    parse_request(payload)
                self.assertEqual(
                    caught.exception.code,
                    "invalid_benchmark_protocol",
                )

    def test_request_schema_fields_and_values_are_exact(self) -> None:
        base = {
            "inherited_lease_id": None,
            "label": "kernel",
            "operation": "acquire",
            "schema": REQUEST_SCHEMA,
        }
        specimens = (
            {**base, "extra": True},
            {key: value for key, value in base.items() if key != "label"},
            {**base, "schema": "benchmarkd.request.v2"},
            {**base, "operation": "release"},
            {**base, "label": "line\nbreak"},
            {**base, "label": "x" * 129},
            {**base, "inherited_lease_id": "not-a-lease"},
            {
                "operation": "status",
                "schema": REQUEST_SCHEMA,
                "label": "unsupported",
            },
        )
        for value in specimens:
            with self.subTest(value=value):
                with self.assertRaises(BenchmarkLockError):
                    parse_request(_canonical(value))

    def test_event_schema_fields_and_values_are_exact(self) -> None:
        base = {
            "aslr": "process",
            "lease_id": LEASE_ID,
            "policy": "performance",
            "schema": EVENT_SCHEMA,
            "type": "granted",
        }
        specimens = (
            {**base, "extra": 1},
            {**base, "aslr": "global"},
            {**base, "lease_id": "short"},
            {**base, "policy": "has spaces"},
            {**base, "type": "unknown"},
            {
                "active": None,
                "policy_state": "mystery",
                "queue_depth": 0,
                "schema": EVENT_SCHEMA,
                "type": "status",
            },
            {
                "active": None,
                "lease_id": LEASE_ID,
                "position": 0,
                "schema": EVENT_SCHEMA,
                "type": "queued",
            },
        )
        for value in specimens:
            with self.subTest(value=value):
                with self.assertRaises(BenchmarkLockError):
                    parse_event(_canonical(value))

    def test_seqpacket_request_authenticates_the_actual_writer(self) -> None:
        server, client = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET,
        )
        try:
            enable_sender_credentials(server)
            send_request(client, AcquireRequest("kernel"))
            credentials = (os.getpid(), os.getuid(), os.getgid())
            self.assertEqual(
                receive_request(
                    server,
                    expected_credentials=credentials,
                ),
                AcquireRequest("kernel"),
            )
        finally:
            server.close()
            client.close()

    def test_seqpacket_request_rejects_credential_mismatch(self) -> None:
        server, client = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET,
        )
        try:
            enable_sender_credentials(server)
            send_request(client, StatusRequest())
            with self.assertRaises(BenchmarkLockError) as caught:
                receive_request(
                    server,
                    expected_credentials=(
                        os.getpid(),
                        os.getuid() + 1,
                        os.getgid(),
                    ),
                )
            self.assertEqual(
                caught.exception.code,
                "invalid_benchmark_channel",
            )
        finally:
            server.close()
            client.close()

    def test_seqpacket_event_round_trip_preserves_packet_boundary(self) -> None:
        server, client = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET,
        )
        try:
            event = GrantedEvent(LEASE_ID, "performance")
            send_event(server, event)
            self.assertEqual(receive_event(client), event)
        finally:
            server.close()
            client.close()

    def test_stream_channel_is_rejected(self) -> None:
        server, client = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_STREAM,
        )
        try:
            with self.assertRaises(BenchmarkLockError) as caught:
                send_request(client, StatusRequest())
            self.assertEqual(
                caught.exception.code,
                "invalid_benchmark_channel",
            )
        finally:
            server.close()
            client.close()

    def test_oversized_seqpacket_is_rejected_without_prefix_parsing(
        self,
    ) -> None:
        server, client = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_SEQPACKET,
        )
        try:
            oversized = b"x" * (MAX_PACKET_BYTES + 100)
            client.send(oversized)
            with self.assertRaises(BenchmarkLockError) as caught:
                receive_event(server)
            self.assertEqual(
                caught.exception.code,
                "invalid_benchmark_protocol",
            )
        finally:
            server.close()
            client.close()
