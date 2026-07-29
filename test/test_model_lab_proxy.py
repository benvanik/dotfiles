from __future__ import annotations

import pathlib
import socket
import tempfile
import threading
import unittest

from model_lab.proxy import (
    MAX_HTTP_HEADER_BYTES,
    MAX_OUTSTANDING_REQUESTS,
    MeteredUnixProxy,
    _RequestTracker,
    _ResponseTracker,
)


class Upstream:
    def __init__(self, path: pathlib.Path, response: bytes) -> None:
        self.path = path
        self.response = response
        self.received = bytearray()
        self.ready = threading.Event()
        self.finished = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(1)
        self.thread = threading.Thread(target=self.run)

    def start(self) -> None:
        self.thread.start()
        self.ready.wait()

    def run(self) -> None:
        self.ready.set()
        connection, _ = self.listener.accept()
        try:
            while b"\r\n\r\n" not in self.received:
                self.received.extend(connection.recv(4096))
            connection.sendall(self.response)
        finally:
            connection.close()
            self.listener.close()
            self.finished.set()

    def close(self) -> None:
        self.finished.wait()
        self.thread.join()


class IdleUpstream:
    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self.ready = threading.Event()
        self.accepted = threading.Event()
        self.finished = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(1)
        self.thread = threading.Thread(target=self.run)

    def start(self) -> None:
        self.thread.start()
        self.ready.wait()

    def run(self) -> None:
        self.ready.set()
        connection, _ = self.listener.accept()
        self.accepted.set()
        try:
            while connection.recv(4096):
                pass
        finally:
            connection.close()
            self.listener.close()
            self.finished.set()


class MeteredUnixProxyTest(unittest.TestCase):
    def test_counts_complete_v1_content_length_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            root.chmod(0o700)
            upstream = Upstream(
                root / "upstream.sock",
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}",
            )
            upstream.start()
            completions = []
            proxy = MeteredUnixProxy(
                listen_path=root / "public.sock",
                upstream_path=upstream.path,
                completed=lambda: completions.append("completed"),
            )
            proxy.bind()
            serving = threading.Thread(target=proxy.serve)
            serving.start()
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(root / "public.sock"))
            client.sendall(
                b"POST /v1/chat/completions HTTP/1.1\r\n"
                b"Host: local\r\nContent-Length: 2\r\n\r\n{}"
            )
            response = bytearray()
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
            client.close()
            upstream.close()
            proxy.close()
            serving.join()

            self.assertEqual(
                response,
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}",
            )
            self.assertEqual(completions, ["completed"])

    def test_counts_complete_chunked_stream_but_not_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            root.chmod(0o700)
            upstream = Upstream(
                root / "upstream.sock",
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                b"4\r\ndata\r\n0\r\n\r\n",
            )
            upstream.start()
            completions = []
            proxy = MeteredUnixProxy(
                listen_path=root / "public.sock",
                upstream_path=upstream.path,
                completed=lambda: completions.append("completed"),
            )
            proxy.bind()
            serving = threading.Thread(target=proxy.serve)
            serving.start()
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(root / "public.sock"))
            client.sendall(b"GET /health HTTP/1.1\r\nHost: local\r\n\r\n")
            while client.recv(4096):
                pass
            client.close()
            upstream.close()
            proxy.close()
            serving.join()

            self.assertEqual(completions, [])

    def test_close_shuts_down_an_idle_keep_alive_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            root.chmod(0o700)
            upstream = IdleUpstream(root / "upstream.sock")
            upstream.start()
            proxy = MeteredUnixProxy(
                listen_path=root / "public.sock",
                upstream_path=upstream.path,
                completed=lambda: None,
            )
            proxy.bind()
            serving = threading.Thread(target=proxy.serve)
            serving.start()
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(root / "public.sock"))
            client.sendall(b"GET /health HTTP/1.1\r\nHost: local\r\n\r\n")
            self.assertTrue(upstream.accepted.wait(2))

            proxy.close()
            serving.join(2)
            upstream.thread.join(2)
            client.close()

            self.assertFalse(serving.is_alive())
            self.assertFalse(upstream.thread.is_alive())
            self.assertTrue(upstream.finished.is_set())

    def test_large_fixed_bodies_retain_only_bounded_framing_state(self) -> None:
        requests = _RequestTracker()
        body_bytes = 8 * 1024 * 1024
        requests.feed(
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: local\r\n"
            + f"Content-Length: {body_bytes}\r\n\r\n".encode("ascii")
        )
        payload = b"x" * (1024 * 1024)
        for _ in range(8):
            requests.feed(payload)
            self.assertLessEqual(
                requests.retained_bytes,
                MAX_HTTP_HEADER_BYTES,
            )

        completions = []
        responses = _ResponseTracker(
            requests,
            lambda: completions.append("completed"),
        )
        responses.feed(
            b"HTTP/1.1 200 OK\r\n"
            + f"Content-Length: {body_bytes}\r\n\r\n".encode("ascii")
        )
        for _ in range(8):
            responses.feed(payload)
            self.assertLessEqual(
                responses.retained_bytes,
                MAX_HTTP_HEADER_BYTES,
            )
        self.assertEqual(completions, ["completed"])

    def test_large_multichunk_response_is_streamed_and_counted_once(self) -> None:
        requests = _RequestTracker()
        requests.feed(
            b"POST /v1/responses HTTP/1.1\r\n"
            b"Host: local\r\nContent-Length: 0\r\n\r\n"
        )
        completions = []
        responses = _ResponseTracker(
            requests,
            lambda: completions.append("completed"),
        )
        responses.feed(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        )
        payload = b"z" * (1024 * 1024)
        for _ in range(6):
            responses.feed(b"100000\r\n")
            responses.feed(payload)
            responses.feed(b"\r\n")
            self.assertLessEqual(
                responses.retained_bytes,
                MAX_HTTP_HEADER_BYTES,
            )
            self.assertEqual(completions, [])
        responses.feed(b"0\r\n\r\n")
        self.assertEqual(completions, ["completed"])

    def test_pipeline_bound_closes_both_relays_and_records_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            root.chmod(0o700)
            upstream = IdleUpstream(root / "upstream.sock")
            upstream.start()
            proxy = MeteredUnixProxy(
                listen_path=root / "public.sock",
                upstream_path=upstream.path,
                completed=lambda: None,
            )
            proxy.bind()
            serving = threading.Thread(target=proxy.serve)
            serving.start()
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(2)
            client.connect(str(root / "public.sock"))
            request = b"GET /v1/models HTTP/1.1\r\nHost: local\r\n\r\n"
            client.sendall(request * (MAX_OUTSTANDING_REQUESTS + 1))
            try:
                self.assertEqual(client.recv(1), b"")
            except ConnectionResetError:
                pass
            self.assertTrue(upstream.finished.wait(2))

            summary = proxy.failure_summary()
            proxy.close()
            serving.join(2)
            upstream.thread.join(2)
            client.close()

            self.assertFalse(serving.is_alive())
            self.assertEqual(summary["failure_count"], 1)
            self.assertEqual(
                summary["recent"][-1]["code"],
                "too_many_outstanding_inference_requests",
            )

    def test_malformed_request_unblocks_response_relay_without_thread_error(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            root.chmod(0o700)
            upstream = IdleUpstream(root / "upstream.sock")
            upstream.start()
            proxy = MeteredUnixProxy(
                listen_path=root / "public.sock",
                upstream_path=upstream.path,
                completed=lambda: None,
            )
            proxy.bind()
            serving = threading.Thread(target=proxy.serve)
            serving.start()
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(2)
            client.connect(str(root / "public.sock"))
            client.sendall(
                b"POST /v1/chat/completions HTTP/1.1\r\n"
                b"Host: local\r\n\r\n"
            )
            try:
                self.assertEqual(client.recv(1), b"")
            except ConnectionResetError:
                pass
            self.assertTrue(upstream.finished.wait(2))

            summary = proxy.failure_summary()
            proxy.close()
            serving.join(2)
            upstream.thread.join(2)
            client.close()

            self.assertFalse(serving.is_alive())
            self.assertEqual(summary["failure_count"], 1)
            self.assertEqual(
                summary["recent"][-1]["code"],
                "invalid_inference_http",
            )


if __name__ == "__main__":
    unittest.main()
