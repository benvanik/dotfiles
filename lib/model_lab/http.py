"""Small JSON HTTP transport with credential-safe failures."""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from .errors import HttpRequestError


DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_ERROR_RESPONSE_BYTES = 64 * 1024
RESPONSE_READ_CHUNK_BYTES = 64 * 1024


class _DuplicateJsonKeyError(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKeyError(key)
        value[key] = item
    return value


def _allowlisted_error_message(
    error: urllib.error.HTTPError,
    allowed_responses: frozenset[tuple[int, str]],
    *,
    read_body: Callable[[Any], bytes] | None = None,
) -> str | None:
    """Return only an exact caller-approved provider error message."""

    if not allowed_responses or error.fp is None:
        return None
    try:
        raw = (
            error.read(DEFAULT_MAX_ERROR_RESPONSE_BYTES + 1)
            if read_body is None
            else read_body(error.fp)
        )
    except (OSError, ValueError, http.client.HTTPException):
        return None
    if len(raw) > DEFAULT_MAX_ERROR_RESPONSE_BYTES:
        return None
    try:
        value = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKeyError,
        RecursionError,
    ):
        return None
    if not isinstance(value, dict):
        return None
    message = value.get("error")
    if not isinstance(message, str):
        return None
    if (error.code, message) not in allowed_responses:
        return None
    if set(value) == {"error"}:
        return message
    if (
        set(value) == {"error", "status"}
        and type(value["status"]) is int
        and value["status"] == error.code
    ):
        return message
    return None


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
    """Return a URL safe for diagnostics without query or fragment data."""

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
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self._opener = opener
        self.deadline = deadline
        self.monotonic = monotonic

    @staticmethod
    def _close_late_open_outcome(
        outcome: tuple[str, Any],
    ) -> None:
        kind, value = outcome
        if kind == "response":
            response = value
        elif isinstance(value, urllib.error.HTTPError):
            response = value
        else:
            return
        try:
            response.close()
        except Exception:
            # A failed best-effort close in an abandoned daemon worker cannot
            # replace the already-returned absolute-deadline failure.
            pass

    def _open_response(
        self,
        request: urllib.request.Request,
        *,
        timeout: float,
        deadline: float,
        deadline_error_code: str,
    ) -> Any:
        """Acquire one response without surrendering the absolute deadline."""

        condition = threading.Condition()
        state: dict[str, Any] = {
            "accepted": False,
            "abandoned": False,
            "outcome": None,
        }

        def open_in_worker() -> None:
            try:
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    raise HttpRequestError(
                        "HTTP request exceeded its absolute deadline",
                        code=deadline_error_code,
                    )
                outcome: tuple[str, Any] = (
                    "response",
                    self._opener(
                        request,
                        timeout=timeout,
                    ),
                )
            except BaseException as error:
                outcome = ("error", error)
            with condition:
                if not state["abandoned"]:
                    state["outcome"] = outcome
                    condition.notify_all()
                    while not state["accepted"] and not state["abandoned"]:
                        condition.wait()
                abandoned = state["abandoned"]
            if abandoned:
                self._close_late_open_outcome(outcome)

        worker = threading.Thread(
            target=open_in_worker,
            name="model-lab-http-open",
            daemon=True,
        )
        worker.start()
        accepted = False
        try:
            with condition:
                while state["outcome"] is None:
                    remaining = deadline - self.monotonic()
                    if remaining <= 0:
                        state["abandoned"] = True
                        condition.notify_all()
                        raise HttpRequestError(
                            "HTTP request exceeded its absolute deadline",
                            code=deadline_error_code,
                        )
                    condition.wait(timeout=remaining)
                if self.monotonic() >= deadline:
                    state["abandoned"] = True
                    condition.notify_all()
                    raise HttpRequestError(
                        "HTTP request exceeded its absolute deadline",
                        code=deadline_error_code,
                    )
                outcome = state["outcome"]
                state["accepted"] = True
                accepted = True
                condition.notify_all()
        finally:
            if not accepted:
                with condition:
                    state["abandoned"] = True
                    condition.notify_all()
        kind, value = outcome
        if kind == "error":
            raise value
        return value

    def _request_budget(self) -> tuple[float, float, str]:
        started = self.monotonic()
        if self.deadline is not None and self.deadline <= started:
            raise HttpRequestError(
                "HTTP request cannot start after the service startup deadline",
                code="service_startup_timeout",
            )
        transport_deadline = started + self.timeout_seconds
        if self.deadline is not None and self.deadline <= transport_deadline:
            return (
                self.deadline,
                self.deadline - started,
                "service_startup_timeout",
            )
        return transport_deadline, self.timeout_seconds, "http_error"

    def _remaining_response_time(
        self,
        deadline: float,
        *,
        deadline_error_code: str,
    ) -> float:
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise HttpRequestError(
                "HTTP response exceeded its absolute deadline",
                code=deadline_error_code,
            )
        return remaining

    @staticmethod
    def _set_response_timeout(response: Any, timeout: float) -> None:
        """Apply the shrinking deadline to the live urllib response socket."""

        set_timeout = getattr(response, "settimeout", None)
        if callable(set_timeout):
            set_timeout(timeout)
            return
        file_pointer = getattr(response, "fp", None)
        raw = getattr(file_pointer, "raw", None)
        response_socket = getattr(raw, "_sock", None)
        set_timeout = getattr(response_socket, "settimeout", None)
        if callable(set_timeout):
            set_timeout(timeout)

    def _read_response(
        self,
        response: Any,
        *,
        deadline: float,
        deadline_error_code: str,
        max_response_bytes: int | None = None,
    ) -> bytes:
        """Read one bounded body without letting progress reset its deadline."""

        response_limit = (
            self.max_response_bytes
            if max_response_bytes is None
            else max_response_bytes
        )
        body = bytearray()
        read = getattr(response, "read1", None)
        if not callable(read):
            read = response.read
        while len(body) <= response_limit:
            remaining = self._remaining_response_time(
                deadline,
                deadline_error_code=deadline_error_code,
            )
            self._set_response_timeout(response, remaining)
            try:
                chunk = read(
                    min(
                        RESPONSE_READ_CHUNK_BYTES,
                        response_limit + 1 - len(body),
                    )
                )
            except TimeoutError as error:
                raise HttpRequestError(
                    "HTTP response exceeded its absolute deadline",
                    code=deadline_error_code,
                ) from error
            self._remaining_response_time(
                deadline,
                deadline_error_code=deadline_error_code,
            )
            if not chunk:
                break
            body.extend(chunk)
        return bytes(body)

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        payload: Any | None = None,
        expected_statuses: tuple[int, ...] = (200,),
        allowed_error_responses: frozenset[tuple[int, str]] = frozenset(),
    ) -> Any:
        if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            raise HttpRequestError(
                "model metadata transport supports read-only HTTP methods",
                code="unsupported_http_method",
            )
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "dotfiles-model-lab/0.1",
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
        http_failure: HttpRequestError | None = None
        response_deadline, request_timeout, deadline_error_code = (
            self._request_budget()
        )
        try:
            response = self._open_response(
                request,
                timeout=request_timeout,
                deadline=response_deadline,
                deadline_error_code=deadline_error_code,
            )
            with response:
                status = int(response.status)
                if status not in expected_statuses:
                    raise HttpRequestError(
                        f"{method.upper()} {public_url(url)} returned HTTP {status}",
                        status=status,
                    )
                raw = self._read_response(
                    response,
                    deadline=response_deadline,
                    deadline_error_code=deadline_error_code,
                )
        except urllib.error.HTTPError as error:
            error_body_failure: HttpRequestError | None = None
            try:
                provider_error = _allowlisted_error_message(
                    error,
                    allowed_error_responses,
                    read_body=lambda response: self._read_response(
                        response,
                        deadline=response_deadline,
                        deadline_error_code=deadline_error_code,
                        max_response_bytes=DEFAULT_MAX_ERROR_RESPONSE_BYTES,
                    ),
                )
                self._remaining_response_time(
                    response_deadline,
                    deadline_error_code=deadline_error_code,
                )
            except HttpRequestError as failure:
                provider_error = None
                error_body_failure = failure
            status = error.code
            try:
                error.close()
            except Exception:
                provider_error = None
            if error_body_failure is not None:
                http_failure = error_body_failure
            else:
                detail = (
                    f": {provider_error}"
                    if provider_error is not None
                    else ""
                )
                http_failure = HttpRequestError(
                    f"{method.upper()} {public_url(url)} returned HTTP "
                    f"{status}{detail}",
                    status=status,
                    provider_error=provider_error,
                )
        except urllib.error.URLError as error:
            reason = getattr(error, "reason", None)
            reason_name = type(reason).__name__ if reason is not None else "network error"
            raise HttpRequestError(
                f"{method.upper()} {public_url(url)} failed: {reason_name}",
                code=(
                    deadline_error_code
                    if isinstance(reason, TimeoutError)
                    else "http_error"
                ),
            ) from error
        except TimeoutError as error:
            raise HttpRequestError(
                f"{method.upper()} {public_url(url)} timed out",
                code=deadline_error_code,
            ) from error

        if http_failure is not None:
            raise http_failure
        if len(raw) > self.max_response_bytes:
            raise HttpRequestError(
                f"{method.upper()} {public_url(url)} exceeded the "
                f"{self.max_response_bytes}-byte response limit",
                code="response_too_large",
            )
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            self._remaining_response_time(
                response_deadline,
                deadline_error_code=deadline_error_code,
            )
            raise HttpRequestError(
                f"{method.upper()} {public_url(url)} returned invalid JSON",
                code="invalid_json",
            ) from error
        self._remaining_response_time(
            response_deadline,
            deadline_error_code=deadline_error_code,
        )
        return value

    def content_length(
        self, url: str, *, headers: Mapping[str, str] | None = None
    ) -> int:
        request_headers = {"User-Agent": "dotfiles-model-lab/0.1"}
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(
            url, headers=request_headers, method="HEAD"
        )
        response_deadline, request_timeout, deadline_error_code = (
            self._request_budget()
        )
        try:
            response = self._open_response(
                request,
                timeout=request_timeout,
                deadline=response_deadline,
                deadline_error_code=deadline_error_code,
            )
            with response:
                value = response.headers.get("Content-Length")
            self._remaining_response_time(
                response_deadline,
                deadline_error_code=deadline_error_code,
            )
        except urllib.error.HTTPError as error:
            try:
                failure = HttpRequestError(
                    f"HEAD {public_url(url)} returned HTTP {error.code}",
                    status=error.code,
                )
            finally:
                try:
                    error.close()
                except Exception:
                    pass
            raise failure from error
        except urllib.error.URLError as error:
            reason = getattr(error, "reason", None)
            reason_name = type(reason).__name__ if reason is not None else "network error"
            raise HttpRequestError(
                f"HEAD {public_url(url)} failed: {reason_name}",
                code=(
                    deadline_error_code
                    if isinstance(reason, TimeoutError)
                    else "http_error"
                ),
            ) from error
        except TimeoutError as error:
            raise HttpRequestError(
                f"HEAD {public_url(url)} timed out",
                code=deadline_error_code,
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
