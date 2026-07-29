"""Fail-closed reads and canonical identities for authored documents."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import stat
from typing import Any

from .errors import ModelLabError

MAX_AUTHORED_DOCUMENT_BYTES = 256 * 1024


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("ascii")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def read_owned_regular_file(
    path: os.PathLike[str] | str,
    *,
    label: str,
    maximum_bytes: int = MAX_AUTHORED_DOCUMENT_BYTES,
) -> bytes:
    """Reads one stable, owned, non-writable regular file via its descriptor."""
    try:
        source_path = pathlib.Path(path)
    except TypeError as error:
        raise ModelLabError(
            f"{label} path is invalid",
            code="unsafe_authored_document",
        ) from error
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source_path, flags)
    except OSError as error:
        raise ModelLabError(
            f"cannot safely open {label} {source_path}: {error}",
            code="unsafe_authored_document",
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size < 1
            or opened.st_size > maximum_bytes
            or (hasattr(os, "getuid") and opened.st_uid != os.getuid())
            or opened.st_mode & 0o022
        ):
            raise ModelLabError(
                f"{label} has an unsafe identity: {source_path}",
                code="unsafe_authored_document",
            )
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        final = os.fstat(descriptor)
        if (
            len(payload) > maximum_bytes
            or final.st_size != opened.st_size
            or final.st_mtime_ns != opened.st_mtime_ns
            or final.st_ctime_ns != opened.st_ctime_ns
        ):
            raise ModelLabError(
                f"{label} changed while reading: {source_path}",
                code="unsafe_authored_document",
            )
        return payload
    finally:
        os.close(descriptor)
