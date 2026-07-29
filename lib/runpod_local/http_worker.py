"""Isolated urllib worker for killable RunPod provider mutations."""

from __future__ import annotations

import base64
import http.client
import json
import math
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


REQUEST_SCHEMA = "runpod.http-worker-request.v2"
RESULT_SCHEMA = "runpod.http-worker-result.v1"
MAX_REQUEST_DOCUMENT_BYTES = 16 * 1024 * 1024


class CredentialSafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Drop authorization when a redirect crosses an origin boundary."""

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
            request,
            file_pointer,
            code,
            message,
            headers,
            new_url,
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


def _request_document() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_REQUEST_DOCUMENT_BYTES + 1)
    if not raw or len(raw) > MAX_REQUEST_DOCUMENT_BYTES:
        raise ValueError("invalid request document size")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("schema") != REQUEST_SCHEMA:
        raise ValueError("invalid request document")
    method = value.get("method")
    url = value.get("url")
    headers = value.get("headers")
    body_base64 = value.get("body_base64")
    timeout = value.get("timeout")
    deadline_monotonic = value.get("deadline_monotonic")
    success_limit = value.get("success_limit")
    error_limit = value.get("error_limit")
    if (
        set(value)
        != {
            "schema",
            "method",
            "url",
            "headers",
            "body_base64",
            "timeout",
            "deadline_monotonic",
            "success_limit",
            "error_limit",
        }
        or not isinstance(method, str)
        or not method
        or not isinstance(url, str)
        or not url
        or not isinstance(headers, list)
        or not all(
            isinstance(item, list)
            and len(item) == 2
            and all(isinstance(part, str) for part in item)
            for item in headers
        )
        or not isinstance(body_base64, str)
        or isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
        or isinstance(deadline_monotonic, bool)
        or not isinstance(deadline_monotonic, (int, float))
        or not math.isfinite(deadline_monotonic)
        or deadline_monotonic <= 0
        or isinstance(success_limit, bool)
        or not isinstance(success_limit, int)
        or success_limit < 0
        or isinstance(error_limit, bool)
        or not isinstance(error_limit, int)
        or error_limit < 0
    ):
        raise ValueError("invalid request fields")
    try:
        body = base64.b64decode(body_base64, validate=True)
    except ValueError as error:
        raise ValueError("invalid request body") from error
    return {
        "method": method,
        "url": url,
        "headers": headers,
        "body": body or None,
        "timeout": float(timeout),
        "deadline_monotonic": float(deadline_monotonic),
        "success_limit": success_limit,
        "error_limit": error_limit,
    }


def _headers(response: Any) -> list[list[str]]:
    content_length = response.headers.get("Content-Length")
    if content_length is None:
        return []
    return [["Content-Length", str(content_length)]]


def _read(response: Any, limit: int) -> bytes:
    return response.read(limit + 1)


def _timeout_result() -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA,
        "kind": "timeout",
    }


def _perform(request_document: dict[str, Any]) -> dict[str, Any]:
    remaining = request_document["deadline_monotonic"] - time.monotonic()
    if remaining <= 0:
        return _timeout_result()
    request = urllib.request.Request(
        request_document["url"],
        data=request_document["body"],
        headers=dict(request_document["headers"]),
        method=request_document["method"],
    )
    opener = urllib.request.build_opener(CredentialSafeRedirectHandler())
    try:
        with opener.open(
            request,
            timeout=min(request_document["timeout"], remaining),
        ) as response:
            return {
                "schema": RESULT_SCHEMA,
                "kind": "response",
                "status": int(response.status),
                "headers": _headers(response),
                "body_base64": base64.b64encode(
                    _read(response, request_document["success_limit"])
                ).decode("ascii"),
            }
    except urllib.error.HTTPError as error:
        try:
            body = _read(error, request_document["error_limit"])
            headers = _headers(error)
        except (OSError, ValueError, http.client.HTTPException):
            body = b""
            headers = []
        finally:
            try:
                error.close()
            except Exception:
                body = b""
                headers = []
        return {
            "schema": RESULT_SCHEMA,
            "kind": "http_error",
            "status": int(error.code),
            "headers": headers,
            "body_base64": base64.b64encode(body).decode("ascii"),
        }
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", None)
        return {
            "schema": RESULT_SCHEMA,
            "kind": "url_error",
            "timeout": isinstance(reason, TimeoutError),
            "reason_type": (
                type(reason).__name__
                if reason is not None
                else "network error"
            ),
        }
    except TimeoutError:
        return _timeout_result()
    except BaseException as error:
        return {
            "schema": RESULT_SCHEMA,
            "kind": "worker_error",
            "error_type": type(error).__name__,
        }


def main() -> int:
    try:
        request_document = _request_document()
        remaining = (
            request_document["deadline_monotonic"] - time.monotonic()
        )
        if remaining <= 0:
            result = _timeout_result()
        else:
            previous_handler = signal.getsignal(signal.SIGALRM)

            def expire_request(
                _signal_number: int,
                _frame: Any,
            ) -> None:
                raise TimeoutError(
                    "isolated HTTP mutation exceeded its absolute deadline"
                )

            signal.signal(signal.SIGALRM, expire_request)
            signal.setitimer(signal.ITIMER_REAL, remaining)
            try:
                try:
                    result = _perform(request_document)
                except TimeoutError:
                    result = _timeout_result()
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous_handler)
        payload = json.dumps(
            result,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
        return 0
    except BaseException:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
