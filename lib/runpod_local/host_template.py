"""Reviewed generic SSH overlay for immutable upstream container images."""

from __future__ import annotations

import hashlib
import os
import pathlib
import stat
from typing import Any

from .errors import RunpodLocalError
from .paths import dotfiles_root
from .template import build_private_template_contract

SSH_BOOTSTRAP_RELATIVE_PATH = pathlib.PurePosixPath("runpod/bootstrap/ssh/bootstrap.sh")
SSH_BOOTSTRAP_SHA256 = (
    "53debc1afa74b41fcc03855eb8047abf66daf2015e4bc29b73df2a3523b763ee"
)
MAX_SSH_BOOTSTRAP_BYTES = 16 * 1024


def _ssh_bootstrap_text() -> str:
    path = dotfiles_root().joinpath(*SSH_BOOTSTRAP_RELATIVE_PATH.parts)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RunpodLocalError(
            f"cannot open generic SSH bootstrap {path}: {error}",
            code="unsafe_host_template",
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size < 1
            or metadata.st_size > MAX_SSH_BOOTSTRAP_BYTES
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            or metadata.st_mode & 0o002
        ):
            raise RunpodLocalError(
                f"generic SSH bootstrap has an unsafe identity: {path}",
                code="unsafe_host_template",
            )
        payload = os.read(descriptor, MAX_SSH_BOOTSTRAP_BYTES + 1)
        if os.read(descriptor, 1) or len(payload) != metadata.st_size:
            raise RunpodLocalError(
                f"generic SSH bootstrap changed while reading: {path}",
                code="unsafe_host_template",
            )
    finally:
        os.close(descriptor)
    if hashlib.sha256(payload).hexdigest() != SSH_BOOTSTRAP_SHA256:
        raise RunpodLocalError(
            f"generic SSH bootstrap identity drifted: {path}",
            code="unsafe_host_template",
        )
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RunpodLocalError(
            f"generic SSH bootstrap is not UTF-8: {path}",
            code="unsafe_host_template",
        ) from error


def build_generic_host_template(
    *,
    name: str,
    image: str,
    container_disk_gb: int,
    template_id: str | None = None,
) -> dict[str, Any]:
    """Build one model-agnostic private SSH template overlay."""

    return build_private_template_contract(
        name=name,
        image=image,
        docker_entrypoint=["/bin/bash", "-c"],
        docker_start_cmd=[_ssh_bootstrap_text()],
        container_disk_gb=container_disk_gb,
        volume_in_gb=0,
        volume_mount_path="/workspace",
        template_id=template_id,
    )
