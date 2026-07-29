"""Private Unix-socket inference proxy with completed-request accounting."""

from __future__ import annotations

import collections
import dataclasses
import os
import pathlib
import signal
import socket
import stat
import threading
from collections.abc import Callable

from .errors import ModelLabError

MAX_HTTP_HEADER_BYTES = 256 * 1024
COPY_BYTES = 256 * 1024
MAX_OUTSTANDING_REQUESTS = 1024
MAX_RETAINED_PROXY_FAILURES = 64


@dataclasses.dataclass(frozen=True)
class _PendingRequest:
    method: bytes
    path: str | None


class _MessageBody:
    """Streaming HTTP/1.1 body framer with bounded retained metadata."""

    def __init__(self, completed: Callable[[], None]) -> None:
        self.completed = completed
        self.state = "header"
        self.buffer = bytearray()
        self.remaining = 0

    @property
    def retained_bytes(self) -> int:
        return len(self.buffer)

    def feed(self, data: bytes) -> None:
        position = 0
        while position < len(data):
            if self.state in {"header", "chunk-size", "trailers"}:
                self.buffer.append(data[position])
                position += 1
                if len(self.buffer) > MAX_HTTP_HEADER_BYTES:
                    raise ModelLabError(
                        "inference HTTP framing metadata exceeds its size bound",
                        code="invalid_inference_http",
                    )
                if self.state == "header" and self.buffer.endswith(b"\r\n\r\n"):
                    header = bytes(self.buffer)
                    self.buffer.clear()
                    self._start_message(header)
                elif (
                    self.state == "chunk-size"
                    and self.buffer.endswith(b"\r\n")
                ):
                    line = bytes(self.buffer[:-2])
                    self.buffer.clear()
                    self._start_chunk(line)
                elif self.state == "trailers" and (
                    self.buffer == b"\r\n"
                    or self.buffer.endswith(b"\r\n\r\n")
                ):
                    self.buffer.clear()
                    self._finish_message()
                continue
            if self.state == "fixed":
                consumed = min(self.remaining, len(data) - position)
                position += consumed
                self.remaining -= consumed
                if self.remaining == 0:
                    self._finish_message()
                continue
            if self.state == "chunk-data":
                consumed = min(self.remaining, len(data) - position)
                position += consumed
                self.remaining -= consumed
                if self.remaining == 0:
                    self.state = "chunk-terminator"
                continue
            if self.state == "chunk-terminator":
                self.buffer.append(data[position])
                position += 1
                if len(self.buffer) == 2:
                    if self.buffer != b"\r\n":
                        raise ModelLabError(
                            "HTTP chunk terminator is malformed",
                            code="invalid_inference_http",
                        )
                    self.buffer.clear()
                    self.state = "chunk-size"
                continue
            raise AssertionError(f"unknown HTTP framing state: {self.state}")

    def _start_message(self, header: bytes) -> None:
        raise NotImplementedError

    def _finish_message(self) -> None:
        self.state = "header"
        self.remaining = 0
        self.completed()

    def _fixed(self, length: int) -> None:
        if length == 0:
            self._finish_message()
            return
        self.remaining = length
        self.state = "fixed"

    def _chunked(self) -> None:
        self.state = "chunk-size"

    def _start_chunk(self, line: bytes) -> None:
        raw_size = line.partition(b";")[0]
        if not raw_size:
            raise ModelLabError(
                "HTTP chunk size is malformed",
                code="invalid_inference_http",
            )
        try:
            size = int(raw_size, 16)
        except ValueError as error:
            raise ModelLabError(
                "HTTP chunk size is malformed",
                code="invalid_inference_http",
            ) from error
        if size < 0:
            raise ModelLabError(
                "HTTP chunk size is negative",
                code="invalid_inference_http",
            )
        if size == 0:
            self.state = "trailers"
            return
        self.remaining = size
        self.state = "chunk-data"


class _RequestTracker(_MessageBody):
    def __init__(self) -> None:
        self.requests: collections.deque[_PendingRequest] = collections.deque()
        self._current: _PendingRequest | None = None
        super().__init__(self._complete_request)

    @property
    def paths(self) -> collections.deque[str | None]:
        """Compatibility view used only by older focused tests."""
        return collections.deque(request.path for request in self.requests)

    def _start_message(self, header: bytes) -> None:
        lines = header.split(b"\r\n")
        try:
            method, raw_path, version = lines[0].split(b" ", 2)
            path = raw_path.decode("ascii").partition("?")[0]
        except (ValueError, UnicodeDecodeError) as error:
            raise ModelLabError(
                "inference request line is malformed",
                code="invalid_inference_http",
            ) from error
        if version not in {b"HTTP/1.0", b"HTTP/1.1"}:
            raise ModelLabError(
                "inference request HTTP version is unsupported",
                code="invalid_inference_http",
            )
        content_length = _content_length(lines[1:])
        transfer_encoding = _transfer_encoding(lines[1:])
        if transfer_encoding is not None and content_length is not None:
            raise ModelLabError(
                "inference request has ambiguous body framing",
                code="invalid_inference_http",
            )
        self._current = _PendingRequest(
            method=method,
            path=path if path.startswith("/v1/") else None,
        )
        if transfer_encoding == "chunked":
            self._chunked()
        elif content_length is not None:
            self._fixed(content_length)
        elif method in {b"GET", b"HEAD", b"DELETE", b"OPTIONS"}:
            self._finish_message()
        else:
            raise ModelLabError(
                "inference request body has no HTTP framing",
                code="invalid_inference_http",
            )

    def _complete_request(self) -> None:
        current = self._current
        if current is None:
            raise AssertionError("completed request has no parsed identity")
        if len(self.requests) >= MAX_OUTSTANDING_REQUESTS:
            raise ModelLabError(
                "inference connection exceeds the outstanding-request bound",
                code="too_many_outstanding_inference_requests",
            )
        self.requests.append(current)
        self._current = None


def _content_length(lines: list[bytes]) -> int | None:
    values = []
    for line in lines:
        name, separator, value = line.partition(b":")
        if separator and name.strip().lower() == b"content-length":
            try:
                parsed = int(value.strip())
            except ValueError as error:
                raise ModelLabError(
                    "HTTP content-length is malformed",
                    code="invalid_inference_http",
                ) from error
            if parsed < 0:
                raise ModelLabError(
                    "HTTP content-length is negative",
                    code="invalid_inference_http",
                )
            values.append(parsed)
    if not values:
        return None
    if len(set(values)) != 1:
        raise ModelLabError(
            "HTTP content-length is ambiguous",
            code="invalid_inference_http",
        )
    return values[0]


def _transfer_encoding(lines: list[bytes]) -> str | None:
    values = []
    for line in lines:
        name, separator, value = line.partition(b":")
        if separator and name.strip().lower() == b"transfer-encoding":
            values.extend(
                item.strip().lower() for item in value.split(b",") if item.strip()
            )
    if not values:
        return None
    if values != [b"chunked"]:
        raise ModelLabError(
            "unsupported HTTP transfer encoding",
            code="invalid_inference_http",
        )
    return "chunked"


class _ResponseTracker(_MessageBody):
    def __init__(
        self,
        requests: _RequestTracker,
        completed: Callable[[], None],
    ) -> None:
        self.requests = requests
        self._status: int | None = None
        super().__init__(completed)

    def _start_message(self, header: bytes) -> None:
        lines = header.split(b"\r\n")
        try:
            version, raw_status, _ = lines[0].split(b" ", 2)
            status = int(raw_status)
        except (ValueError, TypeError) as error:
            raise ModelLabError(
                "inference response status line is malformed",
                code="invalid_inference_http",
            ) from error
        if version not in {b"HTTP/1.0", b"HTTP/1.1"} or not 100 <= status <= 999:
            raise ModelLabError(
                "inference response status line is malformed",
                code="invalid_inference_http",
            )
        transfer_encoding = _transfer_encoding(lines[1:])
        content_length = _content_length(lines[1:])
        if transfer_encoding is not None and content_length is not None:
            raise ModelLabError(
                "inference response has ambiguous body framing",
                code="invalid_inference_http",
            )
        self._status = status
        if 100 <= status < 200:
            self.state = "header"
            return
        request = (
            self.requests.requests[0]
            if self.requests.requests
            else None
        )
        if status in {204, 304} or (
            request is not None and request.method == b"HEAD"
        ):
            self._finish_message()
        elif transfer_encoding == "chunked":
            self._chunked()
        elif content_length is not None:
            self._fixed(content_length)
        else:
            raise ModelLabError(
                "inference response body has no HTTP framing",
                code="invalid_inference_http",
            )

    def _finish_message(self) -> None:
        request = (
            self.requests.requests.popleft()
            if self.requests.requests
            else None
        )
        self.state = "header"
        self.remaining = 0
        self._status = None
        if request is not None and request.path is not None:
            self.completed()


class MeteredUnixProxy:
    """Relays private HTTP and records only fully framed `/v1/` responses."""

    def __init__(
        self,
        *,
        listen_path: pathlib.Path,
        upstream_path: pathlib.Path,
        completed: Callable[[], None],
    ) -> None:
        self.listen_path = listen_path
        self.upstream_path = upstream_path
        self.completed = completed
        self._listener: socket.socket | None = None
        self._stopped = threading.Event()
        self._workers: set[threading.Thread] = set()
        self._connections: set[tuple[socket.socket, socket.socket]] = set()
        self._workers_lock = threading.Lock()
        self._failures: collections.deque[tuple[str, str]] = collections.deque(
            maxlen=MAX_RETAINED_PROXY_FAILURES
        )
        self._failure_count = 0

    def failure_summary(self) -> dict[str, object]:
        """Return bounded connection failures for supervisor diagnostics."""

        with self._workers_lock:
            return {
                "failure_count": self._failure_count,
                "recent": [
                    {"code": code, "message": message}
                    for code, message in self._failures
                ],
            }

    def _record_failure(self, error: BaseException) -> None:
        code = getattr(error, "code", "inference_proxy_connection_failed")
        with self._workers_lock:
            self._failure_count += 1
            self._failures.append((str(code), str(error)))

    def bind(self) -> None:
        parent = self.listen_path.parent
        metadata = parent.stat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or parent.is_symlink()
            or metadata.st_mode & 0o077
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            raise ModelLabError(
                f"proxy socket parent is not private: {parent}",
                code="unsafe_proxy_socket_parent",
            )
        if self.listen_path.exists() or self.listen_path.is_symlink():
            raise ModelLabError(
                f"proxy socket path already exists: {self.listen_path}",
                code="proxy_socket_in_use",
            )
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(os.fspath(self.listen_path))
            os.chmod(self.listen_path, 0o600)
            listener.listen(128)
        except BaseException:
            listener.close()
            raise
        self._listener = listener

    def serve(self) -> None:
        if self._listener is None:
            self.bind()
        listener = self._listener
        if listener is None:
            raise AssertionError("bound proxy listener is absent")
        while not self._stopped.is_set():
            try:
                downstream, _ = listener.accept()
            except OSError:
                if self._stopped.is_set():
                    break
                raise
            if self._stopped.is_set():
                downstream.close()
                break
            worker = threading.Thread(
                target=self._serve_connection,
                args=(downstream,),
                daemon=True,
            )
            with self._workers_lock:
                self._workers.add(worker)
            worker.start()

    def close(self) -> None:
        self._stopped.set()
        listener = self._listener
        if listener is not None:
            wake = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                wake.connect(os.fspath(self.listen_path))
            except OSError:
                pass
            finally:
                wake.close()
            listener.close()
        with self._workers_lock:
            workers = tuple(self._workers)
            connections = tuple(self._connections)
        for pair in connections:
            for connection in pair:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        for worker in workers:
            worker.join()

    def _serve_connection(self, downstream: socket.socket) -> None:
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        pair = (downstream, upstream)
        requests = _RequestTracker()
        responses = _ResponseTracker(requests, self.completed)
        relay_threads: list[threading.Thread] = []
        relay_errors: list[BaseException] = []
        relay_lock = threading.Lock()

        def relay(
            source: socket.socket,
            destination: socket.socket,
            observe: Callable[[bytes], None],
        ) -> None:
            try:
                self._relay(source, destination, observe)
            except BaseException as error:
                with relay_lock:
                    first_failure = not relay_errors
                    relay_errors.append(error)
                if first_failure:
                    # Publish the framing/relay failure before closing either
                    # socket. A downstream observer that sees EOF may then
                    # synchronously inspect the reason without racing the
                    # connection-owner thread's final joins.
                    self._record_failure(error)
            finally:
                # Either direction reaching EOF or rejecting framing ends the
                # HTTP connection. Shutting down both descriptors is the
                # synchronization contract that releases the peer relay.
                for connection in pair:
                    try:
                        connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        try:
            with self._workers_lock:
                self._connections.add(pair)
            if self._stopped.is_set():
                return
            upstream.connect(os.fspath(self.upstream_path))
            if self._stopped.is_set():
                return
            request_worker = threading.Thread(
                target=relay,
                args=(downstream, upstream, requests.feed),
                daemon=True,
            )
            response_worker = threading.Thread(
                target=relay,
                args=(upstream, downstream, responses.feed),
                daemon=True,
            )
            relay_threads.extend((request_worker, response_worker))
            request_worker.start()
            response_worker.start()
            for relay_thread in relay_threads:
                relay_thread.join()
        except BaseException as error:
            self._record_failure(error)
        finally:
            for connection in (downstream, upstream):
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
            for relay_thread in relay_threads:
                if relay_thread is not threading.current_thread():
                    relay_thread.join()
            with self._workers_lock:
                self._connections.discard(pair)
                self._workers.discard(threading.current_thread())

    @staticmethod
    def _relay(
        source: socket.socket,
        destination: socket.socket,
        observe: Callable[[bytes], None],
    ) -> None:
        while True:
            payload = source.recv(COPY_BYTES)
            if not payload:
                return
            observe(payload)
            destination.sendall(payload)


def install_signal_shutdown(proxy: MeteredUnixProxy) -> None:
    def close_proxy(_signum: int, _frame: object) -> None:
        proxy.close()

    signal.signal(signal.SIGTERM, close_proxy)
    signal.signal(signal.SIGINT, close_proxy)
