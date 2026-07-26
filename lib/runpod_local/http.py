"""Small JSON HTTP transport with credential-safe failures."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

from .errors import HttpRequestError


DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024


class CredentialSafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Drops authorization when a redirect crosses an origin boundary."""

    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> urllib.request.Request | None:
        redirected = super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )
        if redirected is None:
            return None
        old_origin = urllib.parse.urlsplit(request.full_url)[:2]
        new_origin = urllib.parse.urlsplit(new_url)[:2]
        if old_origin != new_origin:
            redirected.remove_header("Authorization")
            redirected.unredirected_hdrs.pop("Authorization", None)
            redirected.unredirected_hdrs.pop("authorization", None)
        return redirected


def _default_open(request: urllib.request.Request, *, timeout: float) -> Any:
    opener = urllib.request.build_opener(CredentialSafeRedirectHandler())
    return opener.open(request, timeout=timeout)


def public_url(url: str) -> str:
    """Returns a URL safe for diagnostics by discarding query and fragment data."""
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


class JsonHttpTransport:
    """Standard-library HTTP transport with injectable opener for tests."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        opener: Any = _default_open,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self._opener = opener

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        payload: Any | None = None,
        expected_statuses: tuple[int, ...] = (200,),
    ) -> Any:
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "dotfiles-runpod/0.1",
        }
        if headers:
            request_headers.update(headers)
        body = None
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url,
            data=body,
            headers=request_headers,
            method=method.upper(),
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                status = int(response.status)
                if status not in expected_statuses:
                    raise HttpRequestError(
                        f"{method.upper()} {public_url(url)} returned HTTP {status}",
                        status=status,
                    )
                raw = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as error:
            raise HttpRequestError(
                f"{method.upper()} {public_url(url)} returned HTTP {error.code}",
                status=error.code,
            ) from error
        except urllib.error.URLError as error:
            reason = getattr(error, "reason", None)
            reason_name = type(reason).__name__ if reason is not None else "network error"
            raise HttpRequestError(
                f"{method.upper()} {public_url(url)} failed: {reason_name}"
            ) from error
        except TimeoutError as error:
            raise HttpRequestError(
                f"{method.upper()} {public_url(url)} timed out"
            ) from error

        if len(raw) > self.max_response_bytes:
            raise HttpRequestError(
                f"{method.upper()} {public_url(url)} exceeded the "
                f"{self.max_response_bytes}-byte response limit",
                code="response_too_large",
            )
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HttpRequestError(
                f"{method.upper()} {public_url(url)} returned invalid JSON",
                code="invalid_json",
            ) from error

    def content_length(
        self, url: str, *, headers: Mapping[str, str] | None = None
    ) -> int:
        request_headers = {"User-Agent": "dotfiles-runpod/0.1"}
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(
            url, headers=request_headers, method="HEAD"
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                value = response.headers.get("Content-Length")
        except urllib.error.HTTPError as error:
            raise HttpRequestError(
                f"HEAD {public_url(url)} returned HTTP {error.code}",
                status=error.code,
            ) from error
        except urllib.error.URLError as error:
            reason = getattr(error, "reason", None)
            reason_name = type(reason).__name__ if reason is not None else "network error"
            raise HttpRequestError(
                f"HEAD {public_url(url)} failed: {reason_name}"
            ) from error
        if value is None:
            raise HttpRequestError(
                f"HEAD {public_url(url)} did not return Content-Length",
                code="missing_content_length",
            )
        try:
            length = int(value)
        except ValueError as error:
            raise HttpRequestError(
                f"HEAD {public_url(url)} returned an invalid Content-Length",
                code="invalid_content_length",
            ) from error
        if length < 0:
            raise HttpRequestError(
                f"HEAD {public_url(url)} returned a negative Content-Length",
                code="invalid_content_length",
            )
        return length
