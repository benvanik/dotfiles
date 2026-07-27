from __future__ import annotations

import http.client
import io
import urllib.error
import urllib.request
import unittest

from runpod_local.errors import HttpRequestError
from runpod_local.http import CredentialSafeRedirectHandler, JsonHttpTransport


class HttpSecurityTest(unittest.TestCase):
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
