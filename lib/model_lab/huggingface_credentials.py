"""Private local Hugging Face credential paths and file loading."""

from __future__ import annotations

import base64
import contextlib
import os
import pathlib
import posixpath
import stat
from collections.abc import Iterator, Mapping
from typing import BinaryIO

from .errors import ModelLabError
from .paths import ensure_private_directory


MAX_HF_TOKEN_BYTES = 8192
HF_CLI_VERSION = "1.24.0"
REMOTE_HF_SESSION_ROOT = "/root/runpod-session"
REMOTE_HF_TOKEN_PATH = (
    f"{REMOTE_HF_SESSION_ROOT}/secrets/huggingface/token"
)
REMOTE_HF_CREDENTIAL_ABSENT = 3
REMOTE_HF_CREDENTIAL_UNSAFE = 4
REMOTE_HF_CREDENTIAL_INVALID = 5
REMOTE_HF_CREDENTIAL_FAILURE = 6

REMOTE_HF_CREDENTIAL_PROGRAM = r"""
import os
import re
import secrets
import stat
import sys

MAX_TOKEN_BYTES = 8192
TEMPORARY_TOKEN_NAME = re.compile(
    r"\.token\.[1-9][0-9]{0,19}\.[0-9a-f]{24}",
    re.ASCII,
)
EXIT_ABSENT = 3
EXIT_UNSAFE = 4
EXIT_INVALID = 5
EXIT_FAILURE = 6


class CredentialFailure(Exception):
    def __init__(self, message, exit_code):
        super().__init__(message)
        self.exit_code = exit_code


def stop(message, exit_code=EXIT_UNSAFE):
    raise CredentialFailure(message, exit_code)


def validate_directory(path, create):
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        if not create:
            return False
        os.mkdir(path, mode=0o700)
        metadata = os.lstat(path)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        stop("remote Hugging Face credential directory is unsafe")
    return True


def open_private_directory(path):
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        os.close(descriptor)
        stop("remote Hugging Face credential directory changed")
    return descriptor


def token_metadata(directory_descriptor):
    try:
        return os.stat(
            "token",
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None


def validate_token_metadata(metadata, allow_empty=False):
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or metadata.st_size > MAX_TOKEN_BYTES
        or (not allow_empty and metadata.st_size == 0)
    ):
        stop("remote Hugging Face credential file is unsafe")


def is_temporary_token_name(name):
    return TEMPORARY_TOKEN_NAME.fullmatch(name) is not None


def temporary_token_names(directory_descriptor):
    names = []
    for name in os.listdir(directory_descriptor):
        if not is_temporary_token_name(name):
            continue
        metadata = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        validate_token_metadata(metadata, allow_empty=True)
        names.append(name)
    return sorted(names)


def remove_tokens(directory_descriptor, names):
    if not names:
        return
    for name in names:
        os.unlink(name, dir_fd=directory_descriptor)
    os.fsync(directory_descriptor)


def read_token():
    raw = sys.stdin.buffer.read(MAX_TOKEN_BYTES + 1)
    if len(raw) > MAX_TOKEN_BYTES:
        stop(
            "Hugging Face credential input exceeds the size limit",
            EXIT_INVALID,
        )
    token = raw.removesuffix(b"\r\n").removesuffix(b"\n")
    if (
        not token
        or raw not in {token, token + b"\n", token + b"\r\n"}
        or any(byte <= 32 or byte == 127 for byte in token)
    ):
        stop(
            "Hugging Face credential input is not one token",
            EXIT_INVALID,
        )
    return token


def install_token(directory_descriptor, token):
    existing = token_metadata(directory_descriptor)
    if existing is not None:
        validate_token_metadata(existing)
    remove_tokens(
        directory_descriptor,
        temporary_token_names(directory_descriptor),
    )
    temporary_name = (
        f".token.{os.getpid()}.{secrets.token_hex(12)}"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(
        temporary_name,
        flags,
        0o600,
        dir_fd=directory_descriptor,
    )
    installed = False
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(token):
            offset += os.write(descriptor, token[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            "token",
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        os.fsync(directory_descriptor)
        installed = True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not installed:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in {
        "push",
        "status",
        "clear",
    }:
        stop("invalid remote Hugging Face credential action", EXIT_INVALID)
    action = sys.argv[1]
    session_root = sys.argv[2]
    if (
        not session_root.startswith("/")
        or session_root != os.path.normpath(session_root)
        or any(ord(character) < 32 for character in session_root)
    ):
        stop("invalid remote Hugging Face session root", EXIT_INVALID)

    os.umask(0o077)
    secret_root = os.path.join(session_root, "secrets")
    huggingface_root = os.path.join(secret_root, "huggingface")
    create = action == "push"
    for directory in (session_root, secret_root, huggingface_root):
        if not validate_directory(directory, create):
            raise SystemExit(EXIT_ABSENT)

    directory_descriptor = open_private_directory(huggingface_root)
    try:
        if action == "push":
            install_token(directory_descriptor, read_token())
            return

        metadata = token_metadata(directory_descriptor)
        temporary_names = temporary_token_names(directory_descriptor)
        if metadata is not None:
            validate_token_metadata(metadata)
        if action == "status":
            if temporary_names:
                stop(
                    "remote Hugging Face credential has an incomplete install"
                )
            if metadata is None:
                raise SystemExit(EXIT_ABSENT)
            return
        names = temporary_names
        if metadata is not None:
            names.append("token")
        if not names:
            raise SystemExit(EXIT_ABSENT)
        remove_tokens(directory_descriptor, names)
    finally:
        os.close(directory_descriptor)


try:
    main()
except CredentialFailure as error:
    print(str(error), file=sys.stderr)
    raise SystemExit(error.exit_code) from None
except OSError:
    print(
        "remote Hugging Face credential filesystem operation failed",
        file=sys.stderr,
    )
    raise SystemExit(EXIT_FAILURE) from None
"""
REMOTE_HF_CREDENTIAL_PROGRAM_BASE64 = base64.b64encode(
    REMOTE_HF_CREDENTIAL_PROGRAM.encode("utf-8")
).decode("ascii")
REMOTE_HF_CREDENTIAL_LOADER = (
    "import base64,sys;"
    "exec(compile(base64.b64decode(sys.argv.pop(1)),"
    "'<runpod-hf-credential>','exec'))"
)
REMOTE_HF_ISOLATED_PYTHON_PREFIX = (
    "/usr/bin/env",
    "-i",
    "HOME=/root",
    "PATH=/usr/bin:/bin",
    "/usr/bin/python3.12",
    "-I",
    "-S",
)
REMOTE_HF_PROBE_PROGRAM = (
    "import os,sys;"
    "ok=(sys.version_info[:2]==(3,12)"
    " and sys.executable=='/usr/bin/python3.12'"
    " and sys.flags.isolated"
    " and sys.flags.no_site"
    " and sys.flags.ignore_environment"
    " and sys.flags.safe_path"
    " and all(os.path.isabs(path) and not path.startswith('/workspace')"
    " for path in sys.path));"
    "raise SystemExit(0 if ok else 1)"
)


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
        raise ModelLabError(
            "HF_TOKEN_PATH must be absolute or expand to an absolute path",
            code="unsafe_hf_token_path",
        )
    return path


def ensure_huggingface_secret_directory(path: pathlib.Path) -> pathlib.Path:
    """Create and validate the immediate private token directory."""

    return ensure_private_directory(path.parent)


def _validate_hf_token_bytes(raw: bytes) -> bytes:
    if len(raw) > MAX_HF_TOKEN_BYTES:
        raise ModelLabError(
            f"Hugging Face token exceeds {MAX_HF_TOKEN_BYTES} bytes",
            code="invalid_hf_token",
        )
    token = raw.removesuffix(b"\r\n").removesuffix(b"\n")
    if (
        not token
        or raw not in {token, token + b"\n", token + b"\r\n"}
        or any(byte <= 32 or byte == 127 for byte in token)
    ):
        raise ModelLabError(
            "Hugging Face token must be one non-empty token without whitespace",
            code="invalid_hf_token",
        )
    return token


def _open_huggingface_token(path: pathlib.Path) -> BinaryIO:
    token_file: BinaryIO | None = None
    try:
        before = path.lstat()
    except FileNotFoundError as error:
        raise ModelLabError(
            "no Hugging Face credential configured; run `hf auth login` "
            "from a trusted terminal",
            code="hf_credential_missing",
        ) from error
    except OSError as error:
        raise ModelLabError(
            f"cannot inspect Hugging Face credential file {path}: {error}",
            code="hf_credential_read_error",
        ) from error
    if path.is_symlink() or not stat.S_ISREG(before.st_mode):
        raise ModelLabError(
            f"Hugging Face credential path is not a regular file: {path}",
            code="unsafe_hf_credential_file",
        )
    if hasattr(os, "getuid") and before.st_uid != os.getuid():
        raise ModelLabError(
            f"Hugging Face credential file is not owned by the current user: "
            f"{path}",
            code="unsafe_hf_credential_file",
        )
    if before.st_mode & 0o077:
        raise ModelLabError(
            f"Hugging Face credential file permissions are broader than 0600: "
            f"{path}",
            code="unsafe_hf_credential_permissions",
        )
    if before.st_size <= 0 or before.st_size > MAX_HF_TOKEN_BYTES:
        raise ModelLabError(
            "Hugging Face credential file is empty or exceeds the size limit",
            code="invalid_hf_token",
        )

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ModelLabError(
            f"cannot open Hugging Face credential file {path}: {error}",
            code="hf_credential_read_error",
        ) from error
    try:
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        ):
            raise ModelLabError(
                f"Hugging Face credential changed while opening it: {path}",
                code="unsafe_hf_credential_file",
            )
        if hasattr(os, "getuid") and after.st_uid != os.getuid():
            raise ModelLabError(
                f"Hugging Face credential file is not owned by the current "
                f"user: {path}",
                code="unsafe_hf_credential_file",
            )
        if after.st_mode & 0o077:
            raise ModelLabError(
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
    except ModelLabError as error:
        if not required and error.code == "hf_credential_missing":
            return None
        raise
    try:
        return token.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ModelLabError(
            "Hugging Face token is not valid UTF-8",
            code="invalid_hf_token",
        ) from error


def configured_huggingface_token() -> str | None:
    """Load only the active private token file."""

    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        if os.environ.get(name):
            raise ModelLabError(
                f"refusing inherited {name}; use `hf auth login` so the "
                "credential remains in the private token file",
                code="unsafe_hf_environment_token",
            )
    return load_huggingface_token()


def build_remote_hf_credential_argv(
    action: str,
    *,
    session_root: str = REMOTE_HF_SESSION_ROOT,
) -> list[str]:
    """Build one fixed, non-secret remote credential operation."""

    if action not in {"push", "status", "clear"}:
        raise ModelLabError(
            f"unsupported Hugging Face credential action: {action!r}",
            code="invalid_hf_auth_action",
        )
    if (
        not isinstance(session_root, str)
        or not session_root.startswith("/")
        or session_root != posixpath.normpath(session_root)
        or any(ord(character) < 32 for character in session_root)
    ):
        raise ModelLabError(
            "remote Hugging Face session root must be an absolute canonical "
            "POSIX path",
            code="invalid_remote_hf_session_root",
        )
    return [
        *REMOTE_HF_ISOLATED_PYTHON_PREFIX,
        "-c",
        REMOTE_HF_CREDENTIAL_LOADER,
        REMOTE_HF_CREDENTIAL_PROGRAM_BASE64,
        action,
        session_root,
    ]


def build_remote_hf_probe_argv() -> list[str]:
    """Prove the exact isolated remote interpreter before streaming a token."""

    return [
        *REMOTE_HF_ISOLATED_PYTHON_PREFIX,
        "-c",
        REMOTE_HF_PROBE_PROGRAM,
    ]
