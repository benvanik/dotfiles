from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
