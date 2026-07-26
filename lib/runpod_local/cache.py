"""Private atomic JSON cache used for immutable model metadata."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import tempfile
import time
from typing import Any

from .errors import RunpodLocalError
from .paths import ensure_private_directory


class JsonCache:
    def __init__(self, root: pathlib.Path, *, now: Any = time.time) -> None:
        self.root = root
        self._now = now

    def _path(self, key: str) -> pathlib.Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def get(
        self,
        key: str,
        *,
        maximum_age_seconds: float | None,
    ) -> Any | None:
        path = self._path(key)
        try:
            with path.open("r", encoding="utf-8") as cache_file:
                record = json.load(cache_file)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as error:
            raise RunpodLocalError(
                f"cannot read model metadata cache entry {path}: {error}",
                code="cache_read_error",
            ) from error
        if not isinstance(record, dict) or record.get("key") != key:
            raise RunpodLocalError(
                f"model metadata cache entry {path} has the wrong identity",
                code="cache_identity_error",
            )
        stored_at = record.get("stored_at")
        if not isinstance(stored_at, (int, float)):
            raise RunpodLocalError(
                f"model metadata cache entry {path} has no valid timestamp",
                code="cache_format_error",
            )
        if (
            maximum_age_seconds is not None
            and self._now() - float(stored_at) > maximum_age_seconds
        ):
            return None
        return record.get("value")

    def put(self, key: str, value: Any) -> None:
        ensure_private_directory(self.root)
        path = self._path(key)
        record = {
            "schema_version": 1,
            "key": key,
            "stored_at": self._now(),
            "value": value,
        }
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.root, prefix=f".{path.name}.", suffix=".tmp"
        )
        temporary_path = pathlib.Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
                json.dump(record, temporary_file, sort_keys=True, separators=(",", ":"))
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, path)
            path.chmod(0o600)
            directory_descriptor = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except Exception:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            raise
