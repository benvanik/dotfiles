"""Small JSON HTTP transport with credential-safe failures."""

from __future__ import annotations

import base64
import http.client
import io
import json
import pathlib
import subprocess
import sys
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
HTTP_WORKER_REQUEST_SCHEMA = "runpod.http-worker-request.v2"
HTTP_WORKER_RESULT_SCHEMA = "runpod.http-worker-result.v1"
HTTP_WORKER_TERMINATE_SECONDS = 0.25
HTTP_WORKER_MAX_RESULT_OVERHEAD_BYTES = 64 * 1024


class _DuplicateJsonKeyError(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKeyError(key)
        value[key] = item
    return value


class _BufferedHttpResponse:
    """In-process response facade over one reaped mutation worker result."""

    def __init__(
        self,
        *,
        status: int,
        headers: dict[str, str],
        body: bytes,
    ) -> None:
        self.status = status
        self.headers = headers
        self._body = io.BytesIO(body)

    def __enter__(self) -> _BufferedHttpResponse:
        return self

    def __exit__(
        self,
        _kind: Any,
        _error: Any,
        _traceback: Any,
    ) -> bool:
        self.close()
        return False

    def close(self) -> None:
        self._body.close()

    def read1(self, size: int) -> bytes:
        return self._body.read1(size)


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
        monotonic: Callable[[], float] = time.monotonic,
        process_factory: Any = subprocess.Popen,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self._opener = opener
        self.monotonic = monotonic
        self._process_factory = process_factory

    @staticmethod
    def _stop_and_reap_worker(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=HTTP_WORKER_TERMINATE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()

    def _mutation_worker_result(
        self,
        request: urllib.request.Request,
        *,
        timeout: float,
        deadline: float,
        deadline_error_code: str,
        monotonic: Callable[[], float],
    ) -> dict[str, Any]:
        worker_clock_started = time.monotonic()
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise HttpRequestError(
                "HTTP request exceeded its absolute deadline",
                code=deadline_error_code,
            )
        # CLOCK_MONOTONIC is shared by processes on one Linux boot, but callers
        # may inject another clock for deterministic tests. Translate only the
        # remaining caller budget into the worker's real monotonic domain. Read
        # the worker clock first so a scheduling gap can only shorten this
        # deadline, never refresh it.
        worker_deadline = worker_clock_started + remaining
        request_body = request.data
        if request_body is None:
            request_body = b""
        if not isinstance(request_body, bytes):
            raise HttpRequestError(
                "HTTP mutation body is not bytes",
                code="http_error",
            )
        document = {
            "schema": HTTP_WORKER_REQUEST_SCHEMA,
            "method": request.get_method(),
            "url": request.full_url,
            "headers": [
                [name, value]
                for name, value in request.header_items()
            ],
            "body_base64": base64.b64encode(request_body).decode("ascii"),
            "timeout": timeout,
            "deadline_monotonic": worker_deadline,
            "success_limit": self.max_response_bytes,
            "error_limit": DEFAULT_MAX_ERROR_RESPONSE_BYTES,
        }
        payload = json.dumps(
            document,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        worker_path = pathlib.Path(__file__).with_name("http_worker.py")
        try:
            process = self._process_factory(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    str(worker_path),
                ],
                env={},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError as error:
            raise HttpRequestError(
                "cannot start isolated HTTP mutation worker",
                code="http_error",
            ) from error
        try:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, 0)
            output, _ = process.communicate(
                input=payload,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired as error:
            self._stop_and_reap_worker(process)
            raise HttpRequestError(
                "HTTP mutation exceeded its absolute deadline",
                code=deadline_error_code,
            ) from error
        except BaseException:
            self._stop_and_reap_worker(process)
            raise
        if monotonic() >= deadline:
            raise HttpRequestError(
                "HTTP mutation exceeded its absolute deadline",
                code=deadline_error_code,
            )
        if process.returncode != 0:
            raise HttpRequestError(
                "isolated HTTP mutation worker failed",
                code="http_error",
            )
        maximum_body_bytes = (
            max(
                self.max_response_bytes,
                DEFAULT_MAX_ERROR_RESPONSE_BYTES,
            )
            + 1
        )
        maximum_encoded_body_bytes = 4 * (
            (maximum_body_bytes + 2) // 3
        )
        if len(output) > (
            maximum_encoded_body_bytes
            + HTTP_WORKER_MAX_RESULT_OVERHEAD_BYTES
        ):
            raise HttpRequestError(
                "isolated HTTP mutation worker result exceeded its bound",
                code="response_too_large",
            )
        try:
            result = json.loads(
                output,
                object_pairs_hook=_unique_json_object,
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            _DuplicateJsonKeyError,
            RecursionError,
        ) as error:
            raise HttpRequestError(
                "isolated HTTP mutation worker returned an invalid result",
                code="http_error",
            ) from error
        if (
            not isinstance(result, dict)
            or result.get("schema") != HTTP_WORKER_RESULT_SCHEMA
        ):
            raise HttpRequestError(
                "isolated HTTP mutation worker returned an invalid result",
                code="http_error",
            )
        self._remaining_response_time(
            deadline,
            deadline_error_code=deadline_error_code,
            monotonic=monotonic,
        )
        return result

    @staticmethod
    def _worker_headers(value: Any) -> dict[str, str]:
        if (
            not isinstance(value, list)
            or not all(
                isinstance(item, list)
                and len(item) == 2
                and all(isinstance(part, str) for part in item)
                for item in value
            )
        ):
            raise HttpRequestError(
                "isolated HTTP mutation worker returned invalid headers",
                code="http_error",
            )
        return {name: item for name, item in value}

    @staticmethod
    def _worker_body(value: Any, *, maximum_bytes: int) -> bytes:
        if not isinstance(value, str):
            raise HttpRequestError(
                "isolated HTTP mutation worker returned an invalid body",
                code="http_error",
            )
        try:
            body = base64.b64decode(value, validate=True)
        except ValueError as error:
            raise HttpRequestError(
                "isolated HTTP mutation worker returned an invalid body",
                code="http_error",
            ) from error
        if len(body) > maximum_bytes:
            raise HttpRequestError(
                "isolated HTTP mutation worker returned an oversized body",
                code="response_too_large",
            )
        return body

    def _open_mutation_response(
        self,
        request: urllib.request.Request,
        *,
        timeout: float,
        deadline: float,
        deadline_error_code: str,
        monotonic: Callable[[], float],
    ) -> Any:
        result = self._mutation_worker_result(
            request,
            timeout=timeout,
            deadline=deadline,
            deadline_error_code=deadline_error_code,
            monotonic=monotonic,
        )
        kind = result.get("kind")
        if kind in {"response", "http_error"}:
            status = result.get("status")
            if (
                isinstance(status, bool)
                or not isinstance(status, int)
                or status < 100
                or status > 599
            ):
                raise HttpRequestError(
                    "isolated HTTP mutation worker returned an invalid status",
                    code="http_error",
                )
            headers = self._worker_headers(result.get("headers"))
            body = self._worker_body(
                result.get("body_base64"),
                maximum_bytes=(
                    DEFAULT_MAX_ERROR_RESPONSE_BYTES + 1
                    if kind == "http_error"
                    else self.max_response_bytes + 1
                ),
            )
            self._remaining_response_time(
                deadline,
                deadline_error_code=deadline_error_code,
                monotonic=monotonic,
            )
            if kind == "http_error":
                raise urllib.error.HTTPError(
                    request.full_url,
                    status,
                    "provider response",
                    headers,
                    io.BytesIO(body),
                )
            return _BufferedHttpResponse(
                status=status,
                headers=headers,
                body=body,
            )
        if kind == "timeout":
            raise TimeoutError("isolated HTTP mutation worker timed out")
        if kind == "url_error":
            timeout_result = result.get("timeout")
            reason_type = result.get("reason_type")
            if (
                not isinstance(timeout_result, bool)
                or not isinstance(reason_type, str)
            ):
                raise HttpRequestError(
                    "isolated HTTP mutation worker returned an invalid error",
                    code="http_error",
                )
            reason: BaseException
            if timeout_result:
                reason = TimeoutError("isolated HTTP worker timeout")
            else:
                reason = OSError(reason_type)
            raise urllib.error.URLError(reason)
        if kind == "worker_error" and isinstance(
            result.get("error_type"),
            str,
        ):
            raise HttpRequestError(
                "isolated HTTP mutation worker failed: "
                f"{result['error_type']}",
                code="http_error",
            )
        raise HttpRequestError(
            "isolated HTTP mutation worker returned an invalid result",
            code="http_error",
        )

    def _open_custom_mutation_response(
        self,
        request: urllib.request.Request,
        *,
        timeout: float,
        deadline: float,
        deadline_error_code: str,
        monotonic: Callable[[], float],
    ) -> Any:
        """Run a synchronous injected test opener without background mutation."""

        if monotonic() >= deadline:
            raise HttpRequestError(
                "HTTP request exceeded its absolute deadline",
                code=deadline_error_code,
            )
        response = self._opener(request, timeout=timeout)
        if monotonic() >= deadline:
            try:
                response.close()
            finally:
                raise HttpRequestError(
                    "HTTP request exceeded its absolute deadline",
                    code=deadline_error_code,
                )
        return response

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
        monotonic: Callable[[], float],
    ) -> Any:
        """Acquire one response without surrendering the absolute deadline."""

        if request.get_method() not in {"GET", "HEAD", "OPTIONS"}:
            if self._opener is _default_open:
                return self._open_mutation_response(
                    request,
                    timeout=timeout,
                    deadline=deadline,
                    deadline_error_code=deadline_error_code,
                    monotonic=monotonic,
                )
            return self._open_custom_mutation_response(
                request,
                timeout=timeout,
                deadline=deadline,
                deadline_error_code=deadline_error_code,
                monotonic=monotonic,
            )
        condition = threading.Condition()
        state: dict[str, Any] = {
            "accepted": False,
            "abandoned": False,
            "outcome": None,
        }

        def open_in_worker() -> None:
            try:
                remaining = deadline - monotonic()
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
            name="runpod-http-open",
            daemon=True,
        )
        worker.start()
        accepted = False
        try:
            with condition:
                while state["outcome"] is None:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        state["abandoned"] = True
                        condition.notify_all()
                        raise HttpRequestError(
                            "HTTP request exceeded its absolute deadline",
                            code=deadline_error_code,
                        )
                    condition.wait(timeout=remaining)
                if monotonic() >= deadline:
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

    def _remaining_response_time(
        self,
        deadline: float,
        *,
        deadline_error_code: str,
        monotonic: Callable[[], float],
    ) -> float:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise HttpRequestError(
                "HTTP response exceeded its absolute deadline",
                code=deadline_error_code,
            )
        return remaining

    def _read_response(
        self,
        response: Any,
        *,
        deadline: float,
        deadline_error_code: str,
        max_response_bytes: int | None = None,
        monotonic: Callable[[], float],
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
                monotonic=monotonic,
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
                monotonic=monotonic,
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
        timeout_seconds: float | None = None,
        deadline: float | None = None,
        monotonic: Callable[[], float] | None = None,
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
        http_failure: HttpRequestError | None = None
        clock = self.monotonic if monotonic is None else monotonic
        effective_timeout = (
            self.timeout_seconds
            if timeout_seconds is None
            else min(self.timeout_seconds, timeout_seconds)
        )
        deadline_bounded = (
            timeout_seconds is not None
            and timeout_seconds <= self.timeout_seconds
        )
        if effective_timeout <= 0:
            raise HttpRequestError(
                "HTTP request cannot start after its deadline",
                code="remote_client_timeout",
            )
        started = clock()
        response_deadline = started + effective_timeout
        if deadline is not None:
            absolute_remaining = deadline - started
            if absolute_remaining <= 0:
                raise HttpRequestError(
                    "HTTP request cannot start after its deadline",
                    code="remote_client_timeout",
                )
            if deadline <= response_deadline:
                response_deadline = deadline
                effective_timeout = absolute_remaining
                deadline_bounded = True
        deadline_error_code = (
            "remote_client_timeout" if deadline_bounded else "http_error"
        )
        try:
            response = self._open_response(
                request,
                timeout=effective_timeout,
                deadline=response_deadline,
                deadline_error_code=deadline_error_code,
                monotonic=clock,
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
                    monotonic=clock,
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
                        monotonic=clock,
                    ),
                )
                self._remaining_response_time(
                    response_deadline,
                    deadline_error_code=deadline_error_code,
                    monotonic=clock,
                )
            except HttpRequestError as failure:
                provider_error = None
                error_body_failure = failure
            status = error.code
            try:
                error.close()
            except Exception:
                # A broken response stream must not replace the sanitized
                # failure or authorize a definitive lifecycle transition.
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
                    "remote_client_timeout"
                    if deadline_bounded
                    and isinstance(reason, TimeoutError)
                    else "http_error"
                ),
            ) from error
        except TimeoutError as error:
            raise HttpRequestError(
                f"{method.upper()} {public_url(url)} timed out",
                code=(
                    "remote_client_timeout"
                    if deadline_bounded
                    else "http_error"
                ),
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
                monotonic=clock,
            )
            raise HttpRequestError(
                f"{method.upper()} {public_url(url)} returned invalid JSON",
                code="invalid_json",
            ) from error
        self._remaining_response_time(
            response_deadline,
            deadline_error_code=deadline_error_code,
            monotonic=clock,
        )
        return value

    def content_length(
        self, url: str, *, headers: Mapping[str, str] | None = None
    ) -> int:
        request_headers = {"User-Agent": "dotfiles-runpod/0.1"}
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(
            url, headers=request_headers, method="HEAD"
        )
        response_deadline = self.monotonic() + self.timeout_seconds
        try:
            response = self._open_response(
                request,
                timeout=self.timeout_seconds,
                deadline=response_deadline,
                deadline_error_code="http_error",
                monotonic=self.monotonic,
            )
            with response:
                value = response.headers.get("Content-Length")
            self._remaining_response_time(
                response_deadline,
                deadline_error_code="http_error",
                monotonic=self.monotonic,
            )
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
