from __future__ import annotations

import json
import threading
import urllib.error
import unittest
from unittest import mock

from model_lab.errors import HttpRequestError
from model_lab.http import JsonHttpTransport


class _SlowResponse:
    status = 200

    def __init__(self, current):
        self.current = current
        self.chunks = [b"{", b'"value"', b":", b"true", b"}", b""]
        self.reads = 0
        self.timeouts = []

    def __enter__(self):
        return self

    def __exit__(self, _kind, _error, _traceback):
        return False

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def read1(self, _size):
        self.reads += 1
        self.current[0] += 1.0
        return self.chunks.pop(0)


class _LateResponse:
    status = 200

    def __init__(self):
        self.closed = threading.Event()
        self.headers = {"Content-Length": "2"}
        self.chunks = [b"{}", b""]

    def __enter__(self):
        return self

    def __exit__(self, _kind, _error, _traceback):
        self.close()
        return False

    def close(self):
        self.closed.set()

    def read1(self, _size):
        return self.chunks.pop(0)


class HttpDeadlineTest(unittest.TestCase):
    def test_json_decode_cannot_publish_after_startup_deadline(self):
        current = [0.0]
        response = _LateResponse()

        def immediate_open(_request, *, timeout):
            self.assertEqual(timeout, 2.5)
            return response

        real_loads = json.loads

        def decode_after_deadline(value, *args, **kwargs):
            current[0] = 3.0
            return real_loads(value, *args, **kwargs)

        transport = JsonHttpTransport(
            opener=immediate_open,
            deadline=2.5,
            monotonic=lambda: current[0],
        )

        with (
            mock.patch(
                "model_lab.http.json.loads",
                side_effect=decode_after_deadline,
            ),
            self.assertRaises(HttpRequestError) as caught,
        ):
            transport.request_json(
                "GET",
                "https://huggingface.co/api/models/example/model",
            )

        self.assertEqual(caught.exception.code, "service_startup_timeout")

    def test_mutating_method_is_rejected_before_opener(self):
        calls = []

        def forbidden_open(_request, *, timeout):
            calls.append(timeout)
            raise AssertionError("mutating opener must not run")

        transport = JsonHttpTransport(opener=forbidden_open)

        with self.assertRaises(HttpRequestError) as caught:
            transport.request_json(
                "POST",
                "https://huggingface.co/api/models/example/model",
            )

        self.assertEqual(caught.exception.code, "unsupported_http_method")
        self.assertEqual(calls, [])

    def test_blocked_json_opener_returns_at_transport_deadline_and_closes_late(self):
        release = threading.Event()
        response = _LateResponse()

        def blocked_open(_request, *, timeout):
            self.assertEqual(timeout, 0.02)
            release.wait(timeout=0.2)
            return response

        transport = JsonHttpTransport(
            timeout_seconds=0.02,
            opener=blocked_open,
        )

        with self.assertRaises(HttpRequestError) as caught:
            transport.request_json(
                "GET",
                "https://huggingface.co/api/models/example/model",
            )

        self.assertEqual(caught.exception.code, "http_error")
        release.set()
        self.assertTrue(response.closed.wait(timeout=1.0))

    def test_blocked_head_opener_returns_at_transport_deadline_and_closes_late(self):
        release = threading.Event()
        response = _LateResponse()

        def blocked_open(_request, *, timeout):
            self.assertEqual(timeout, 0.02)
            release.wait(timeout=0.2)
            return response

        transport = JsonHttpTransport(
            timeout_seconds=0.02,
            opener=blocked_open,
        )

        with self.assertRaises(HttpRequestError) as caught:
            transport.content_length(
                "https://huggingface.co/example/model/resolve/revision/file",
            )

        self.assertEqual(caught.exception.code, "http_error")
        release.set()
        self.assertTrue(response.closed.wait(timeout=1.0))

    def test_head_http_error_closes_response_before_returning_failure(self):
        response = _LateResponse()

        def failing_open(request, *, timeout):
            self.assertEqual(timeout, 30.0)
            raise urllib.error.HTTPError(
                request.full_url,
                404,
                "fixture",
                {},
                response,
            )

        transport = JsonHttpTransport(opener=failing_open)

        with self.assertRaises(HttpRequestError) as caught:
            transport.content_length(
                "https://huggingface.co/example/model/resolve/revision/file",
            )

        self.assertEqual(caught.exception.status, 404)
        self.assertTrue(response.closed.is_set())

    def test_trickled_response_honors_default_transport_deadline(self):
        current = [0.0]
        response = _SlowResponse(current)

        def slow_open(_request, *, timeout):
            self.assertEqual(timeout, 2.5)
            return response

        transport = JsonHttpTransport(
            timeout_seconds=2.5,
            opener=slow_open,
            monotonic=lambda: current[0],
        )

        with self.assertRaises(HttpRequestError) as caught:
            transport.request_json(
                "GET",
                "https://huggingface.co/api/models/example/model",
            )

        self.assertEqual(caught.exception.code, "http_error")
        self.assertEqual(response.reads, 3)
        self.assertEqual(response.timeouts, [2.5, 1.5, 0.5])

    def test_trickled_allowlisted_error_cannot_reset_startup_deadline(self):
        current = [0.0]

        class SlowErrorBody:
            def __init__(self) -> None:
                self.chunks = [b"{", b'"error"', b":", b'"safe"', b"}", b""]
                self.closed = False
                self.reads = 0

            def close(self):
                self.closed = True

            def read1(self, _size):
                self.reads += 1
                current[0] += 1.0
                return self.chunks.pop(0)

            def settimeout(self, _timeout):
                return None

        body = SlowErrorBody()

        def slow_open(request, *, timeout):
            self.assertEqual(timeout, 2.5)
            raise urllib.error.HTTPError(
                request.full_url,
                503,
                "fixture",
                {},
                body,
            )

        transport = JsonHttpTransport(
            opener=slow_open,
            deadline=2.5,
            monotonic=lambda: current[0],
        )

        with self.assertRaises(HttpRequestError) as caught:
            transport.request_json(
                "GET",
                "https://huggingface.co/api/models/example/model",
                allowed_error_responses=frozenset({(503, "safe")}),
            )

        self.assertEqual(caught.exception.code, "service_startup_timeout")
        self.assertEqual(body.reads, 3)
        self.assertTrue(body.closed)

    def test_trickled_response_cannot_reset_service_startup_deadline(self):
        current = [0.0]
        response = _SlowResponse(current)

        def slow_open(_request, *, timeout):
            self.assertEqual(timeout, 2.5)
            return response

        transport = JsonHttpTransport(
            opener=slow_open,
            deadline=2.5,
            monotonic=lambda: current[0],
        )

        with self.assertRaises(HttpRequestError) as caught:
            transport.request_json(
                "GET",
                "https://huggingface.co/api/models/example/model",
            )

        self.assertEqual(caught.exception.code, "service_startup_timeout")
        self.assertEqual(response.reads, 3)
        self.assertEqual(response.timeouts, [2.5, 1.5, 0.5])


if __name__ == "__main__":
    unittest.main()
