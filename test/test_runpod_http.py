from __future__ import annotations

import http.client
import http.server
import io
import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
import unittest
from unittest import mock

from runpod_local.errors import HttpRequestError
from runpod_local.http import CredentialSafeRedirectHandler, JsonHttpTransport


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


class HttpSecurityTest(unittest.TestCase):
    def test_absolute_call_deadline_is_not_reanchored_after_open(self):
        current = [10.0]
        response = _SlowResponse(current)

        def delayed_open(_request, *, timeout):
            self.assertEqual(timeout, 4.0)
            current[0] = 13.0
            return response

        transport = JsonHttpTransport(opener=delayed_open)

        with self.assertRaises(HttpRequestError) as caught:
            transport.request_json(
                "GET",
                "https://api.example.invalid/v1/pods",
                deadline=14.0,
                monotonic=lambda: current[0],
            )

        self.assertEqual(caught.exception.code, "remote_client_timeout")
        self.assertEqual(response.reads, 1)
        self.assertEqual(response.timeouts, [1.0])

    def test_json_decode_cannot_publish_after_call_deadline(self):
        current = [0.0]
        response = _LateResponse()

        def immediate_open(_request, *, timeout):
            self.assertEqual(timeout, 2.5)
            return response

        real_loads = json.loads

        def decode_after_deadline(value, *args, **kwargs):
            current[0] = 3.0
            return real_loads(value, *args, **kwargs)

        transport = JsonHttpTransport(opener=immediate_open)

        with (
            mock.patch(
                "runpod_local.http.json.loads",
                side_effect=decode_after_deadline,
            ),
            self.assertRaises(HttpRequestError) as caught,
        ):
            transport.request_json(
                "GET",
                "https://api.example.invalid/v1/pods",
                deadline=2.5,
                monotonic=lambda: current[0],
            )

        self.assertEqual(caught.exception.code, "remote_client_timeout")

    def test_worker_result_decode_cannot_publish_after_call_deadline(self):
        current = [0.0]

        class CompletedMutationProcess:
            def __init__(self, command, **_kwargs):
                self.args = command
                self.returncode = 0

            def communicate(self, input=None, timeout=None):
                del input, timeout
                return (
                    b'{"body_base64":"e30=","headers":[],"kind":"response",'
                    b'"schema":"runpod.http-worker-result.v1","status":201}',
                    b"",
                )

            def poll(self):
                return self.returncode

        def process_factory(command, **kwargs):
            return CompletedMutationProcess(command, **kwargs)

        real_loads = json.loads

        def decode_after_deadline(value, *args, **kwargs):
            current[0] = 3.0
            return real_loads(value, *args, **kwargs)

        transport = JsonHttpTransport(process_factory=process_factory)

        with (
            mock.patch(
                "runpod_local.http.json.loads",
                side_effect=decode_after_deadline,
            ),
            self.assertRaises(HttpRequestError) as caught,
        ):
            transport.request_json(
                "POST",
                "https://api.example.invalid/v1/pods",
                payload={"name": "fixture"},
                expected_statuses=(201,),
                deadline=2.5,
                monotonic=lambda: current[0],
            )

        self.assertEqual(caught.exception.code, "remote_client_timeout")

    def test_isolated_mutation_worker_preserves_request_and_response(self):
        observed = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                observed["authorization"] = self.headers["Authorization"]
                observed["body"] = self.rfile.read(length)
                response = b'{"id":"pod123"}'
                self.send_response(201)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, _format, *_arguments):
                return None

        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            Handler,
        )
        server_thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        server_thread.start()

        def stop_server():
            server.shutdown()
            server.server_close()
            server_thread.join()

        self.addCleanup(stop_server)
        transport = JsonHttpTransport()
        value = transport.request_json(
            "POST",
            f"http://127.0.0.1:{server.server_port}/pods",
            headers={"Authorization": "Bearer fixture-secret"},
            payload={"name": "fixture"},
            expected_statuses=(201,),
        )

        self.assertEqual(value, {"id": "pod123"})
        self.assertEqual(
            observed,
            {
                "authorization": "Bearer fixture-secret",
                "body": b'{"name":"fixture"}',
            },
        )

    def test_mutation_worker_is_stopped_and_reaped_before_timeout_returns(self):
        class BlockingMutationProcess:
            def __init__(self, command, **_kwargs):
                self.args = command
                self.communicate_calls = 0
                self.killed = False
                self.late_provider_side_effect = False
                self.returncode = None
                self.terminated = False

            def communicate(self, input=None, timeout=None):
                del input
                self.communicate_calls += 1
                if not self.terminated and not self.killed:
                    raise subprocess.TimeoutExpired(self.args, timeout)
                self.returncode = -15 if self.terminated else -9
                return b"", b""

            def kill(self):
                self.killed = True

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True

            def attempt_late_provider_side_effect(self):
                if self.returncode is None:
                    self.late_provider_side_effect = True

        processes = []

        def process_factory(command, **kwargs):
            process = BlockingMutationProcess(command, **kwargs)
            processes.append(process)
            return process

        transport = JsonHttpTransport(process_factory=process_factory)

        with self.assertRaises(HttpRequestError) as caught:
            transport.request_json(
                "POST",
                "https://api.example.invalid/graphql",
                payload={"query": "mutation { create }"},
                timeout_seconds=0.02,
            )

        self.assertEqual(caught.exception.code, "remote_client_timeout")
        self.assertEqual(len(processes), 1)
        process = processes[0]
        self.assertTrue(process.terminated)
        self.assertEqual(process.returncode, -15)
        self.assertEqual(process.communicate_calls, 2)
        process.attempt_late_provider_side_effect()
        self.assertFalse(process.late_provider_side_effect)

    def test_delayed_mutation_worker_rejects_before_provider_request(self):
        attempted = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                attempted.set()
                self.send_response(201)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, _format, *_arguments):
                return None

        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            Handler,
        )
        server_thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        server_thread.start()

        def stop_server():
            server.shutdown()
            server.server_close()
            server_thread.join()

        self.addCleanup(stop_server)

        class DelayedMutationProcess:
            def __init__(self, command, **_kwargs):
                self.args = command
                self.returncode = None
                self.worker_output = None

            def communicate(self, input=None, timeout=None):
                del timeout
                document = json.loads(input)
                deadline = document["deadline_monotonic"]
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    threading.Event().wait(remaining)
                completed = subprocess.run(
                    self.args,
                    input=input,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.returncode = completed.returncode
                self.worker_output = completed.stdout
                return completed.stdout, completed.stderr

            def poll(self):
                return self.returncode

        processes = []

        def process_factory(command, **kwargs):
            process = DelayedMutationProcess(command, **kwargs)
            processes.append(process)
            return process

        transport = JsonHttpTransport(process_factory=process_factory)
        with self.assertRaises(HttpRequestError) as caught:
            transport.request_json(
                "POST",
                f"http://127.0.0.1:{server.server_port}/pods",
                payload={"name": "fixture"},
                expected_statuses=(201,),
                timeout_seconds=0.05,
            )

        self.assertEqual(caught.exception.code, "remote_client_timeout")
        self.assertFalse(attempted.is_set())
        self.assertEqual(len(processes), 1)
        process = processes[0]
        self.assertEqual(process.returncode, 0)
        self.assertEqual(
            json.loads(process.worker_output),
            {
                "schema": "runpod.http-worker-result.v1",
                "kind": "timeout",
            },
        )

    def test_mutation_worker_protocol_keeps_credentials_out_of_process_metadata(
        self,
    ):
        secret = "fixture-secret"
        inherited_secret = "inherited-fixture-secret"

        class CompletedMutationProcess:
            def __init__(self, command, **kwargs):
                self.args = command
                self.input_document = None
                self.launch_keywords = kwargs
                self.returncode = 0

            def communicate(self, input=None, timeout=None):
                del timeout
                self.input_document = json.loads(input)
                result = {
                    "schema": "runpod.http-worker-result.v1",
                    "kind": "response",
                    "status": 201,
                    "headers": [["Content-Type", "application/json"]],
                    "body_base64": "eyJpZCI6InBvZDEyMyJ9",
                }
                return json.dumps(result).encode("ascii"), b""

            def poll(self):
                return self.returncode

        processes = []

        def process_factory(command, **kwargs):
            process = CompletedMutationProcess(command, **kwargs)
            processes.append(process)
            return process

        transport = JsonHttpTransport(process_factory=process_factory)
        with mock.patch.dict(
            "os.environ",
            {"UNRELATED_API_SECRET": inherited_secret},
        ):
            value = transport.request_json(
                "POST",
                "https://api.example.invalid/v1/pods",
                headers={"Authorization": f"Bearer {secret}"},
                payload={"name": "fixture"},
                expected_statuses=(201,),
            )

        self.assertEqual(value, {"id": "pod123"})
        process = processes[0]
        self.assertNotIn(secret, "\0".join(process.args))
        self.assertNotIn(inherited_secret, "\0".join(process.args))
        self.assertEqual(process.launch_keywords["env"], {})
        self.assertEqual(
            process.input_document["schema"],
            "runpod.http-worker-request.v2",
        )
        self.assertGreater(
            process.input_document["deadline_monotonic"],
            time.monotonic(),
        )
        self.assertIn(
            ["Authorization", f"Bearer {secret}"],
            process.input_document["headers"],
        )

    def test_blocked_json_opener_returns_at_call_deadline_and_closes_late(self):
        release = threading.Event()
        response = _LateResponse()

        def blocked_open(_request, *, timeout):
            self.assertEqual(timeout, 0.02)
            release.wait(timeout=0.2)
            return response

        transport = JsonHttpTransport(opener=blocked_open)

        with self.assertRaises(HttpRequestError) as caught:
            transport.request_json(
                "GET",
                "https://api.example.invalid/v1/pods",
                timeout_seconds=0.02,
            )

        self.assertEqual(caught.exception.code, "remote_client_timeout")
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
                "https://api.example.invalid/v1/checkpoint",
            )

        self.assertEqual(caught.exception.code, "http_error")
        release.set()
        self.assertTrue(response.closed.wait(timeout=1.0))

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
                "https://api.example.invalid/v1/pods",
            )

        self.assertEqual(caught.exception.code, "http_error")
        self.assertEqual(response.reads, 3)
        self.assertEqual(response.timeouts, [2.5, 1.5, 0.5])

    def test_trickled_allowlisted_error_cannot_reset_call_deadline(self):
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
            monotonic=lambda: current[0],
        )

        with self.assertRaises(HttpRequestError) as caught:
            transport.request_json(
                "GET",
                "https://api.example.invalid/v1/pods",
                allowed_error_responses=frozenset({(503, "safe")}),
                timeout_seconds=2.5,
            )

        self.assertEqual(caught.exception.code, "remote_client_timeout")
        self.assertEqual(body.reads, 3)
        self.assertTrue(body.closed)

    def test_trickled_response_cannot_reset_call_deadline(self):
        current = [0.0]
        response = _SlowResponse(current)

        def slow_open(_request, *, timeout):
            self.assertEqual(timeout, 2.5)
            return response

        transport = JsonHttpTransport(
            opener=slow_open,
            monotonic=lambda: current[0],
        )

        with self.assertRaises(HttpRequestError) as caught:
            transport.request_json(
                "GET",
                "https://api.example.invalid/v1/pods",
                timeout_seconds=2.5,
            )

        self.assertEqual(caught.exception.code, "remote_client_timeout")
        self.assertEqual(response.reads, 3)
        self.assertEqual(response.timeouts, [2.5, 1.5, 0.5])

    def test_request_timeout_is_bounded_by_call_and_transport_limits(self):
        observed_timeouts = []

        def timing_open(_request, *, timeout):
            observed_timeouts.append(timeout)
            raise TimeoutError("fixture timeout")

        transport = JsonHttpTransport(
            timeout_seconds=30.0,
            opener=timing_open,
        )
        error_codes = []
        for requested in (4.5, 90.0):
            with self.assertRaises(HttpRequestError) as caught:
                transport.request_json(
                    "GET",
                    "https://api.example.invalid/v1/pods",
                    timeout_seconds=requested,
                )
            error_codes.append(caught.exception.code)

        self.assertEqual(observed_timeouts, [4.5, 30.0])
        self.assertEqual(
            error_codes,
            ["remote_client_timeout", "http_error"],
        )

    def test_wrapped_socket_timeout_at_call_deadline_is_typed(self):
        def timing_open(_request, *, timeout):
            self.assertEqual(timeout, 4.5)
            raise urllib.error.URLError(
                TimeoutError("controlled wrapped timeout")
            )

        transport = JsonHttpTransport(
            timeout_seconds=30.0,
            opener=timing_open,
        )

        with self.assertRaises(HttpRequestError) as caught:
            transport.request_json(
                "GET",
                "https://api.example.invalid/v1/pods",
                timeout_seconds=4.5,
            )

        self.assertEqual(caught.exception.code, "remote_client_timeout")

    def test_nonpositive_call_timeout_starts_no_http_request(self):
        open_calls = []

        def forbidden_open(_request, *, timeout):
            open_calls.append(timeout)
            raise AssertionError("HTTP request must not start")

        transport = JsonHttpTransport(opener=forbidden_open)
        with self.assertRaises(HttpRequestError) as caught:
            transport.request_json(
                "GET",
                "https://api.example.invalid/v1/pods",
                timeout_seconds=0.0,
            )

        self.assertEqual(caught.exception.code, "remote_client_timeout")
        self.assertEqual(open_calls, [])

    def test_cross_origin_redirect_drops_authorization(self):
        request = urllib.request.Request(
            "https://huggingface.co/example/model",
            headers={"Authorization": "Bearer fixture-secret"},
        )
        redirected = CredentialSafeRedirectHandler().redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://cdn.example.invalid/signed-object",
        )
        self.assertIsNotNone(redirected)
        self.assertIsNone(redirected.get_header("Authorization"))

    def test_same_origin_redirect_preserves_authorization(self):
        request = urllib.request.Request(
            "https://huggingface.co/example/model",
            headers={"Authorization": "Bearer fixture-secret"},
        )
        redirected = CredentialSafeRedirectHandler().redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://huggingface.co/example/other",
        )
        self.assertIsNotNone(redirected)
        self.assertEqual(
            redirected.get_header("Authorization"), "Bearer fixture-secret"
        )

    def test_http_error_never_reports_query_credentials(self):
        def failing_open(request, *, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", {}, None
            )

        transport = JsonHttpTransport(opener=failing_open)
        with self.assertRaises(HttpRequestError) as caught:
            transport.request_json(
                "GET", "https://api.example.invalid/graphql?api_key=fixture-secret"
            )
        self.assertNotIn("fixture-secret", str(caught.exception))
        self.assertNotIn("api_key", str(caught.exception))

    def test_http_error_reports_only_an_exact_allowlisted_message(self):
        safe_message = (
            "create pod: There are no instances currently available"
        )

        def failing_open(request, *, timeout):
            raise urllib.error.HTTPError(
                request.full_url,
                500,
                "Internal Server Error",
                {},
                io.BytesIO(
                    ('{"error":"' + safe_message + '"}').encode("utf-8")
                ),
            )

        transport = JsonHttpTransport(opener=failing_open)
        with self.assertRaises(HttpRequestError) as caught:
            transport.request_json(
                "POST",
                "https://rest.example.invalid/v1/pods",
                allowed_error_responses=frozenset({(500, safe_message)}),
            )

        self.assertEqual(caught.exception.provider_error, safe_message)
        self.assertIn(safe_message, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_http_error_accepts_matching_provider_status_document(self):
        safe_message = (
            "create pod: There are no instances currently available"
        )

        def failing_open(request, *, timeout):
            raise urllib.error.HTTPError(
                request.full_url,
                500,
                "Internal Server Error",
                {},
                io.BytesIO(
                    (
                        '{ "status" : 500, "error" : "'
                        + safe_message
                        + '" }'
                    ).encode("utf-8")
                ),
            )

        transport = JsonHttpTransport(opener=failing_open)
        with self.assertRaises(HttpRequestError) as caught:
            transport.request_json(
                "POST",
                "https://rest.example.invalid/v1/pods",
                allowed_error_responses=frozenset({(500, safe_message)}),
            )

        self.assertEqual(caught.exception.provider_error, safe_message)
        self.assertIn(safe_message, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_http_error_discards_unapproved_provider_content(self):
        secret = "provider echoed fixture-secret"

        def failing_open(request, *, timeout):
            raise urllib.error.HTTPError(
                request.full_url,
                500,
                "Internal Server Error",
                {},
                io.BytesIO(
                    ('{"error":"' + secret + '"}').encode("utf-8")
                ),
            )

        transport = JsonHttpTransport(opener=failing_open)
        with self.assertRaises(HttpRequestError) as caught:
            transport.request_json(
                "POST",
                "https://rest.example.invalid/v1/pods",
                allowed_error_responses=frozenset({(500, "safe fixture")}),
            )

        self.assertIsNone(caught.exception.provider_error)
        self.assertNotIn(secret, str(caught.exception))

    def test_http_error_rejects_near_match_provider_responses(self):
        safe_message = (
            "create pod: There are no instances currently available"
        )
        cases = {
            "wrong_status": (
                503,
                ('{"error":"' + safe_message + '"}').encode("utf-8"),
            ),
            "extra_field": (
                500,
                (
                    '{"error":"'
                    + safe_message
                    + '","request":"fixture-secret"}'
                ).encode("utf-8"),
            ),
            "message_field": (
                500,
                ('{"message":"' + safe_message + '"}').encode("utf-8"),
            ),
            "mismatched_body_status": (
                500,
                (
                    '{"error":"' + safe_message + '","status":503}'
                ).encode("utf-8"),
            ),
            "mismatched_http_status": (
                503,
                (
                    '{"error":"' + safe_message + '","status":500}'
                ).encode("utf-8"),
            ),
            "string_body_status": (
                500,
                (
                    '{"error":"' + safe_message + '","status":"500"}'
                ).encode("utf-8"),
            ),
            "float_body_status": (
                500,
                (
                    '{"error":"' + safe_message + '","status":500.0}'
                ).encode("utf-8"),
            ),
            "boolean_body_status": (
                500,
                (
                    '{"error":"' + safe_message + '","status":true}'
                ).encode("utf-8"),
            ),
            "null_body_status": (
                500,
                (
                    '{"error":"' + safe_message + '","status":null}'
                ).encode("utf-8"),
            ),
            "duplicate_error": (
                500,
                (
                    '{"error":"fixture-secret","error":"'
                    + safe_message
                    + '","status":500}'
                ).encode("utf-8"),
            ),
            "duplicate_status": (
                500,
                (
                    '{"error":"'
                    + safe_message
                    + '","status":503,"status":500}'
                ).encode("utf-8"),
            ),
            "malformed": (
                500,
                ('{"error":"' + safe_message).encode("utf-8"),
            ),
            "non_object": (
                500,
                ('["' + safe_message + '"]').encode("utf-8"),
            ),
            "oversized": (
                500,
                ('{"error":"' + safe_message + '"}').encode("utf-8")
                + b" " * (64 * 1024),
            ),
        }

        for name, (status, response_body) in cases.items():
            with self.subTest(name=name):
                def failing_open(request, *, timeout):
                    raise urllib.error.HTTPError(
                        request.full_url,
                        status,
                        "fixture provider error",
                        {},
                        io.BytesIO(response_body),
                    )

                transport = JsonHttpTransport(opener=failing_open)
                with self.assertRaises(HttpRequestError) as caught:
                    transport.request_json(
                        "POST",
                        "https://rest.example.invalid/v1/pods",
                        allowed_error_responses=frozenset(
                            {(500, safe_message)}
                        ),
                    )

                self.assertIsNone(caught.exception.provider_error)
                self.assertNotIn("fixture-secret", str(caught.exception))

    def test_http_error_read_failure_remains_sanitized_and_ambiguous(self):
        safe_message = (
            "create pod: There are no instances currently available"
        )

        class IncompleteBody:
            def read(self, _size):
                raise http.client.IncompleteRead(b"fixture-secret")

            def close(self):
                pass

        def failing_open(request, *, timeout):
            raise urllib.error.HTTPError(
                request.full_url,
                500,
                "fixture provider error",
                {},
                IncompleteBody(),
            )

        transport = JsonHttpTransport(opener=failing_open)
        with self.assertRaises(HttpRequestError) as caught:
            transport.request_json(
                "POST",
                "https://rest.example.invalid/v1/pods",
                allowed_error_responses=frozenset({(500, safe_message)}),
            )

        self.assertIsNone(caught.exception.provider_error)
        self.assertNotIn("fixture-secret", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_http_error_close_failure_cannot_authorize_transition(self):
        safe_message = (
            "create pod: There are no instances currently available"
        )
        response_body = ('{"error":"' + safe_message + '"}').encode("utf-8")

        class BrokenCloseBody:
            def read(self, _size):
                return response_body

            def close(self):
                raise RuntimeError("fixture-secret")

        def failing_open(request, *, timeout):
            raise urllib.error.HTTPError(
                request.full_url,
                500,
                "fixture provider error",
                {},
                BrokenCloseBody(),
            )

        transport = JsonHttpTransport(opener=failing_open)
        with self.assertRaises(HttpRequestError) as caught:
            transport.request_json(
                "POST",
                "https://rest.example.invalid/v1/pods",
                allowed_error_responses=frozenset({(500, safe_message)}),
            )

        self.assertIsNone(caught.exception.provider_error)
        self.assertNotIn("fixture-secret", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)


if __name__ == "__main__":
    unittest.main()
