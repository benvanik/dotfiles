"""Strict duration and UTC timestamp helpers."""

from __future__ import annotations

import datetime
import re

from .errors import RunpodLocalError


DURATION_PART_PATTERN = re.compile(r"([1-9][0-9]*)([smhd])")
DURATION_UNITS = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
}
MAX_DURATION_SECONDS = 30 * 24 * 60 * 60
UTC_TIMESTAMP_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)


def parse_duration(value: str) -> int:
    position = 0
    seconds = 0
    for match in DURATION_PART_PATTERN.finditer(value):
        if match.start() != position:
            break
        seconds += int(match.group(1)) * DURATION_UNITS[match.group(2)]
        position = match.end()
    if position != len(value) or seconds <= 0:
        raise RunpodLocalError(
            f"invalid duration {value!r}; use forms such as 30m, 4h, or 1h30m",
            code="invalid_duration",
        )
    if seconds > MAX_DURATION_SECONDS:
        raise RunpodLocalError(
            "duration exceeds the 30-day safety limit",
            code="duration_too_long",
        )
    return seconds


def utc_timestamp(
    value: datetime.datetime | None = None,
) -> str:
    instant = value or datetime.datetime.now(datetime.timezone.utc)
    if instant.tzinfo is None:
        raise RunpodLocalError(
            "UTC timestamp input must be timezone-aware",
            code="invalid_timestamp",
        )
    return (
        instant.astimezone(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_utc_timestamp(value: str) -> datetime.datetime:
    if not isinstance(value, str) or not UTC_TIMESTAMP_PATTERN.fullmatch(value):
        raise RunpodLocalError(
            f"invalid UTC timestamp: {value!r}",
            code="invalid_timestamp",
        )
    try:
        instant = datetime.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise RunpodLocalError(
            f"invalid UTC timestamp: {value!r}",
            code="invalid_timestamp",
        ) from error
    return instant.astimezone(datetime.timezone.utc)
