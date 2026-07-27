"""Private local Hugging Face credential paths and file loading."""

from __future__ import annotations

import contextlib
import os
import pathlib
import stat
from collections.abc import Iterator, Mapping
from typing import BinaryIO

from .errors import RunpodLocalError
from .paths import ensure_private_directory


MAX_HF_TOKEN_BYTES = 8192
HF_CLI_VERSION = "1.24.0"


def huggingface_token_path(
    *,
    environment: Mapping[str, str] | None = None,
    home: pathlib.Path | None = None,
) -> pathlib.Path:
    """Return the configured active-token path without reading it."""

    values = os.environ if environment is None else environment
    configured = values.get("HF_TOKEN_PATH")
    if configured:
        expanded = os.path.expandvars(os.path.expanduser(configured))
        path = pathlib.Path(expanded)
    else:
        xdg_config = values.get("XDG_CONFIG_HOME")
        base = (
            pathlib.Path(os.path.expandvars(os.path.expanduser(xdg_config)))
            if xdg_config
            else (home or pathlib.Path.home()) / ".config"
        )
        path = base / "huggingface" / "token"
    if not path.is_absolute():
        raise RunpodLocalError(
            "HF_TOKEN_PATH must be absolute or expand to an absolute path",
            code="unsafe_hf_token_path",
        )
    return path


def ensure_huggingface_secret_directory(path: pathlib.Path) -> pathlib.Path:
    """Create and validate the immediate private token directory."""

    return ensure_private_directory(path.parent)


def _validate_hf_token_bytes(raw: bytes) -> bytes:
    if len(raw) > MAX_HF_TOKEN_BYTES:
        raise RunpodLocalError(
            f"Hugging Face token exceeds {MAX_HF_TOKEN_BYTES} bytes",
            code="invalid_hf_token",
        )
    token = raw.removesuffix(b"\r\n").removesuffix(b"\n")
    if (
        not token
        or raw not in {token, token + b"\n", token + b"\r\n"}
        or any(byte <= 32 or byte == 127 for byte in token)
    ):
        raise RunpodLocalError(
            "Hugging Face token must be one non-empty token without whitespace",
            code="invalid_hf_token",
        )
    return token


def _open_huggingface_token(path: pathlib.Path) -> BinaryIO:
    token_file: BinaryIO | None = None
    try:
        before = path.lstat()
    except FileNotFoundError as error:
        raise RunpodLocalError(
            "no Hugging Face credential configured; run `hf auth login` "
            "from a trusted terminal",
            code="hf_credential_missing",
        ) from error
    except OSError as error:
        raise RunpodLocalError(
            f"cannot inspect Hugging Face credential file {path}: {error}",
            code="hf_credential_read_error",
        ) from error
    if path.is_symlink() or not stat.S_ISREG(before.st_mode):
        raise RunpodLocalError(
            f"Hugging Face credential path is not a regular file: {path}",
            code="unsafe_hf_credential_file",
        )
    if hasattr(os, "getuid") and before.st_uid != os.getuid():
        raise RunpodLocalError(
            f"Hugging Face credential file is not owned by the current user: "
            f"{path}",
            code="unsafe_hf_credential_file",
        )
    if before.st_mode & 0o077:
        raise RunpodLocalError(
            f"Hugging Face credential file permissions are broader than 0600: "
            f"{path}",
            code="unsafe_hf_credential_permissions",
        )
    if before.st_size <= 0 or before.st_size > MAX_HF_TOKEN_BYTES:
        raise RunpodLocalError(
            "Hugging Face credential file is empty or exceeds the size limit",
            code="invalid_hf_token",
        )

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RunpodLocalError(
            f"cannot open Hugging Face credential file {path}: {error}",
            code="hf_credential_read_error",
        ) from error
    try:
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        ):
            raise RunpodLocalError(
                f"Hugging Face credential changed while opening it: {path}",
                code="unsafe_hf_credential_file",
            )
        if hasattr(os, "getuid") and after.st_uid != os.getuid():
            raise RunpodLocalError(
                f"Hugging Face credential file is not owned by the current "
                f"user: {path}",
                code="unsafe_hf_credential_file",
            )
        if after.st_mode & 0o077:
            raise RunpodLocalError(
                "Hugging Face credential file permissions are broader than "
                f"0600: {path}",
                code="unsafe_hf_credential_permissions",
            )
        token_file = os.fdopen(descriptor, "rb")
        descriptor = -1
        raw = token_file.read(MAX_HF_TOKEN_BYTES + 1)
        _validate_hf_token_bytes(raw)
        token_file.seek(0)
        return token_file
    except BaseException:
        if token_file is not None:
            token_file.close()
        if descriptor >= 0:
            os.close(descriptor)
        raise


@contextlib.contextmanager
def open_huggingface_token_file(
    path: pathlib.Path | None = None,
) -> Iterator[BinaryIO]:
    """Yield a validated token file positioned at byte zero."""

    token_file = _open_huggingface_token(path or huggingface_token_path())
    try:
        yield token_file
    finally:
        token_file.close()


def load_huggingface_token(
    path: pathlib.Path | None = None,
    *,
    required: bool = False,
) -> str | None:
    """Load one token for an in-process authenticated Hub request."""

    selected = path or huggingface_token_path()
    try:
        with open_huggingface_token_file(selected) as token_file:
            token = _validate_hf_token_bytes(
                token_file.read(MAX_HF_TOKEN_BYTES + 1)
            )
    except RunpodLocalError as error:
        if not required and error.code == "hf_credential_missing":
            return None
        raise
    try:
        return token.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RunpodLocalError(
            "Hugging Face token is not valid UTF-8",
            code="invalid_hf_token",
        ) from error


def configured_huggingface_token() -> str | None:
    """Resolve the legacy environment override, then the private token file."""

    environment_token = os.environ.get("HF_TOKEN")
    if environment_token is not None:
        return _validate_hf_token_bytes(environment_token.encode("utf-8")).decode(
            "utf-8"
        )
    return load_huggingface_token()
