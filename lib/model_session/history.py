"""Descriptor-retained resume history for isolated Pi sessions."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import resource
import unicodedata
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

from .errors import ModelSessionError
from .lease import (
    RunInspection,
    RunLease,
    inspect_run_from_state,
    open_pi_session_at,
)
from .runs import SessionRun, list_run_ids_from_state


MAX_SESSION_LINE_BYTES = 16 * 1024 * 1024
MAX_SESSION_FILE_BYTES = 256 * 1024 * 1024
MAX_TITLE_CHARACTERS = 96
HISTORY_DESCRIPTOR_HEADROOM = 64

_PI_TIMESTAMP_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$"
)
_PI_HEADER_KEYS = {"type", "version", "id", "timestamp", "cwd"}


@dataclass(frozen=True)
class SessionHistory:
    """Non-authoritative display data for one catalog-owned run."""

    session_id: str
    created_at: str
    project_id: str | None
    title: str
    updated_at: str
    pi_session_name: str | None
    prompt_fingerprint: str | None
    active: bool
    history_error: str | None


@dataclass
class HistoryCatalog:
    """Picker results plus exact root/receipt descriptors for every entry."""

    entries: tuple[SessionHistory, ...]
    _inspections: dict[str, RunInspection]
    _inspection_errors: dict[str, str]
    _closed: bool = False

    def acquire(self, session_id: str) -> RunLease:
        """Transfer one exact listed run into a full process-lifetime lease."""

        self._require_open()
        inspection = self._inspections.get(session_id)
        if inspection is None:
            error_code = self._inspection_errors.get(session_id)
            if error_code is not None:
                _fail(
                    "selected session has invalid structural state: "
                    f"{session_id}",
                    code=error_code,
                )
            _fail(
                f"session is not present in this history catalog: {session_id}",
                code="unknown_session",
            )
        lease = inspection.acquire()
        del self._inspections[session_id]
        try:
            _inspect_lease_history(lease)
            return lease
        except BaseException:
            lease.close()
            raise

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        for inspection in self._inspections.values():
            inspection.close()
        self._inspections.clear()

    def _require_open(self) -> None:
        if self._closed:
            _fail(
                "cannot use a closed history catalog",
                code="session_history_closed",
            )

    def __enter__(self) -> Self:
        self._require_open()
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()


def _fail(message: str, *, code: str = "invalid_session_history") -> None:
    raise ModelSessionError(message, code=code)


def prompt_fingerprint(run: SessionRun) -> str:
    """Return a compact hash over all prompt-bearing locked resources."""

    components = []
    for role in ("agents", "system_prompt", "append_system_prompt"):
        resource = run.resource_for_role(role)
        if resource is not None:
            components.append(f"{role}:{resource.sha256}")
    return hashlib.sha256("\n".join(components).encode("ascii")).hexdigest()[:12]


def _session_id_timestamp(session_id: str) -> str:
    timestamp = session_id.split("-", 1)[0]
    try:
        parsed = datetime.datetime.strptime(
            timestamp,
            "%Y%m%dT%H%M%S%fZ",
        ).replace(tzinfo=datetime.timezone.utc)
    except ValueError as error:
        raise ModelSessionError(
            f"validated session ID has an invalid timestamp: {session_id}",
            code="invalid_session_state",
        ) from error
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _check_history_descriptor_budget(run_count: int) -> None:
    soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft_limit == resource.RLIM_INFINITY:
        return
    try:
        open_count = len(os.listdir("/proc/self/fd"))
    except OSError:
        open_count = HISTORY_DESCRIPTOR_HEADROOM
    required = open_count + run_count * 2 + HISTORY_DESCRIPTOR_HEADROOM
    if required > soft_limit:
        _fail(
            "resume history cannot retain exact authority for all "
            f"{run_count} runs within the {soft_limit}-descriptor process "
            "limit",
            code="session_history_too_large",
        )


def _sanitized_title(value: str) -> str:
    without_controls = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in value
    )
    visible = " ".join(without_controls.split())
    if len(visible) <= MAX_TITLE_CHARACTERS:
        return visible
    return visible[: MAX_TITLE_CHARACTERS - 1].rstrip() + "…"


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return " ".join(parts)


def _strict_json_object(raw_line: bytes, line_number: int) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite number {value}")

    def unique_object(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    if not raw_line.strip():
        _fail(
            f"Pi session line {line_number} is blank",
            code="invalid_pi_session",
        )
    try:
        text = raw_line.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as error:
        raise ModelSessionError(
            f"Pi session line {line_number} is invalid JSON",
            code="invalid_pi_session",
        ) from error
    if not isinstance(value, dict):
        _fail(
            f"Pi session line {line_number} is not an object",
            code="invalid_pi_session",
        )
    return value


def _pi_timestamp(value: Any) -> str:
    if not isinstance(value, str) or _PI_TIMESTAMP_PATTERN.fullmatch(value) is None:
        _fail(
            "Pi session header timestamp is not an exact UTC millisecond "
            "timestamp",
            code="foreign_pi_session",
        )
    try:
        datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as error:
        raise ModelSessionError(
            "Pi session header timestamp is not a real calendar time",
            code="foreign_pi_session",
        ) from error
    return value


def _safe_name(name: str) -> str:
    """Render an untrusted filesystem name as bounded printable ASCII."""

    rendered = ascii(name)
    if len(rendered) <= 320:
        return rendered
    return rendered[:316] + "...'"


def _initial_file_metadata(
    descriptor: int,
    *,
    name: str,
) -> os.stat_result:
    initial = os.fstat(descriptor)
    if initial.st_size > MAX_SESSION_FILE_BYTES:
        _fail(
            "Pi session file exceeds "
            f"{MAX_SESSION_FILE_BYTES} bytes: {_safe_name(name)}",
            code="invalid_pi_session",
        )
    return initial


def _validate_file_after_read(
    descriptor: int,
    *,
    initial: os.stat_result,
    name: str,
) -> None:
    current = os.fstat(descriptor)
    immutable_metadata = (
        "st_dev",
        "st_ino",
        "st_uid",
        "st_mode",
        "st_nlink",
    )
    if any(
        getattr(initial, field) != getattr(current, field)
        for field in immutable_metadata
    ):
        _fail(
            "Pi session file metadata changed while being inspected: "
            f"{_safe_name(name)}",
            code="invalid_pi_session",
        )
    if current.st_size < initial.st_size:
        _fail(
            "Pi session file shrank while being inspected: "
            f"{_safe_name(name)}",
            code="invalid_pi_session",
        )


def _pi_session_metadata(
    descriptor: int,
    *,
    name: str,
    session_id: str,
) -> tuple[str, str]:
    metadata = _initial_file_metadata(descriptor, name=name)
    remaining = metadata.st_size
    line_buffer = bytearray()
    line_number = 0
    header_seen = False
    first_user_title = ""
    session_info_seen = False
    explicit_title = ""

    def accept_line(raw_line: bytes) -> None:
        nonlocal explicit_title
        nonlocal first_user_title
        nonlocal header_seen
        nonlocal line_number
        nonlocal session_info_seen

        line_number += 1
        entry = _strict_json_object(raw_line, line_number)
        if not header_seen:
            if set(entry) != _PI_HEADER_KEYS:
                _fail(
                    "Pi session header fields do not match a wrapper-owned "
                    "session",
                    code="foreign_pi_session",
                )
            timestamp = _pi_timestamp(entry["timestamp"])
            if (
                entry["type"] != "session"
                or entry["version"] != 3
                or isinstance(entry["version"], bool)
                or entry["id"] != session_id
                or entry["cwd"] != "/workspace"
            ):
                _fail(
                    f"Pi session header is not bound to outer run {session_id}",
                    code="foreign_pi_session",
                )
            expected_name = (
                timestamp.replace(":", "-").replace(".", "-")
                + f"_{session_id}.jsonl"
            )
            if name != expected_name:
                _fail(
                    "Pi session filename is not derived from its header: "
                    f"{_safe_name(name)}",
                    code="foreign_pi_session",
                )
            header_seen = True
            return
        if entry.get("type") == "session":
            _fail(
                "Pi session contains more than one session header",
                code="foreign_pi_session",
            )
        if entry.get("type") == "session_info":
            name_value = entry.get("name")
            if name_value is not None and not isinstance(name_value, str):
                _fail(
                    "Pi session_info name is not a string or null",
                    code="invalid_pi_session",
                )
            session_info_seen = True
            explicit_title = (
                "" if name_value is None else _sanitized_title(name_value)
            )
            return
        if first_user_title or entry.get("type") != "message":
            return
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("role") != "user":
            return
        first_user_title = _sanitized_title(
            _text_content(message.get("content"))
        )

    while remaining:
        chunk = os.read(descriptor, min(remaining, 64 * 1024))
        if not chunk:
            _fail(
                "Pi session file shrank while being inspected: "
                f"{_safe_name(name)}",
                code="invalid_pi_session",
            )
        remaining -= len(chunk)
        offset = 0
        while True:
            newline = chunk.find(b"\n", offset)
            if newline < 0:
                tail = chunk[offset:]
                if len(line_buffer) + len(tail) > MAX_SESSION_LINE_BYTES:
                    _fail(
                        "Pi session partial line exceeds "
                        f"{MAX_SESSION_LINE_BYTES} bytes",
                        code="invalid_pi_session",
                    )
                line_buffer.extend(tail)
                break
            segment = chunk[offset:newline]
            if len(line_buffer) + len(segment) > MAX_SESSION_LINE_BYTES:
                _fail(
                    f"Pi session line {line_number + 1} exceeds "
                    f"{MAX_SESSION_LINE_BYTES} bytes",
                    code="invalid_pi_session",
                )
            line_buffer.extend(segment)
            accept_line(bytes(line_buffer))
            line_buffer.clear()
            offset = newline + 1

    _validate_file_after_read(
        descriptor,
        initial=metadata,
        name=name,
    )
    if not header_seen:
        _fail(
            "Pi session has no newline-terminated header",
            code="invalid_pi_session",
        )
    selected_title = (
        explicit_title if session_info_seen and explicit_title else first_user_title
    )
    updated_at = (
        datetime.datetime.fromtimestamp(
            metadata.st_mtime,
            tz=datetime.timezone.utc,
        )
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    return selected_title or "(empty session)", updated_at


def _inspect_mutable_history(
    inspection: RunInspection,
) -> tuple[str, str, str | None]:
    sessions_descriptor = inspection.open_pi_sessions()
    try:
        names: list[str] = []
        try:
            with os.scandir(sessions_descriptor) as entries:
                for entry in entries:
                    names.append(entry.name)
                    if len(names) == 2:
                        break
        except OSError as error:
            raise ModelSessionError(
                "cannot enumerate retained Pi session state: "
                f"{error}",
                code="unsafe_session_state",
            ) from error
        if not names:
            return "(empty session)", inspection.run.created_at, None
        if len(names) == 2:
            _fail(
                f"outer run {inspection.run.session_id} contains more than "
                "one Pi session entry; exact resume is ambiguous",
                code="ambiguous_pi_session",
            )
        name = names[0]
        descriptor = open_pi_session_at(sessions_descriptor, name)
        try:
            title, updated_at = _pi_session_metadata(
                descriptor,
                name=name,
                session_id=inspection.run.session_id,
            )
        finally:
            os.close(descriptor)
        return title, updated_at, name
    finally:
        os.close(sessions_descriptor)


def _inspect_lease_history(lease: RunLease) -> tuple[str, str, str | None]:
    """Revalidate the current exact Pi target under the acquired launch lock."""

    names = lease.list_pi_session_names()
    if not names:
        return "(empty session)", lease.run.created_at, None
    name = names[0]
    descriptor = lease.open_pi_session(name)
    try:
        title, updated_at = _pi_session_metadata(
            descriptor,
            name=name,
            session_id=lease.run.session_id,
        )
    finally:
        os.close(descriptor)
    return title, updated_at, name


def acquire_history_run_from_state(
    state_root: os.PathLike[str] | str,
    profile_id: str,
    session_id: str,
) -> RunLease:
    """Acquire and revalidate one exact resume target without scanning siblings."""

    inspection = inspect_run_from_state(state_root, profile_id, session_id)
    try:
        lease = inspection.acquire()
    finally:
        inspection.close()
    try:
        _inspect_lease_history(lease)
        return lease
    except BaseException:
        lease.close()
        raise


def enumerate_history(
    state_root: os.PathLike[str] | str,
    profile_id: str,
) -> HistoryCatalog:
    """Retain exact run roots and build safe display metadata for a picker."""

    session_ids = list_run_ids_from_state(state_root, profile_id)
    _check_history_descriptor_budget(len(session_ids))
    inspections: dict[str, RunInspection] = {}
    inspection_errors: dict[str, str] = {}
    histories: list[SessionHistory] = []
    try:
        for session_id in session_ids:
            try:
                inspection = inspect_run_from_state(
                    state_root,
                    profile_id,
                    session_id,
                )
            except ModelSessionError as error:
                inspection_errors[session_id] = error.code
                created_at = _session_id_timestamp(session_id)
                histories.append(
                    SessionHistory(
                        session_id=session_id,
                        created_at=created_at,
                        project_id=None,
                        title="(invalid session state)",
                        updated_at=created_at,
                        pi_session_name=None,
                        prompt_fingerprint=None,
                        active=False,
                        history_error=error.code,
                    )
                )
                continue
            inspections[session_id] = inspection
            if inspection.try_lock():
                try:
                    try:
                        title, updated_at, pi_session_name = (
                            _inspect_mutable_history(inspection)
                        )
                        history_error = None
                    except ModelSessionError as error:
                        title = "(invalid session)"
                        updated_at = inspection.run.created_at
                        pi_session_name = None
                        history_error = error.code
                finally:
                    inspection.unlock()
                active = False
            else:
                title = "(active session)"
                updated_at = inspection.run.created_at
                pi_session_name = None
                active = True
                history_error = None
            histories.append(
                SessionHistory(
                    session_id=session_id,
                    created_at=inspection.run.created_at,
                    project_id=inspection.run.profile.project_id,
                    title=title,
                    updated_at=updated_at,
                    pi_session_name=pi_session_name,
                    prompt_fingerprint=prompt_fingerprint(inspection.run),
                    active=active,
                    history_error=history_error,
                )
            )
    except BaseException:
        for inspection in inspections.values():
            inspection.close()
        raise
    entries = tuple(
        sorted(
            histories,
            key=lambda history: (
                history.active,
                history.updated_at,
                history.created_at,
                history.session_id,
            ),
            reverse=True,
        )
    )
    return HistoryCatalog(
        entries=entries,
        _inspections=inspections,
        _inspection_errors=inspection_errors,
    )
