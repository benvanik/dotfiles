"""Explicit model-lab ownership of ephemeral Hugging Face host credentials."""

from __future__ import annotations

import pathlib
import subprocess
from collections.abc import Callable
from typing import Any

from runpod_local.api import RunpodApi
from runpod_local.auth import CredentialStore
from runpod_local.errors import RunpodLocalError
from runpod_local.instances import InstanceStore
from runpod_local.paths import (
    credentials_file,
    state_root as default_runpod_state_root,
)
from runpod_local.remote import (
    build_ssh_argv,
    ensure_known_hosts_file,
    resolve_endpoint,
    run_with_activity,
)
from runpod_local.state import StateStore
from runpod_local.template import validate_image_digest

from .errors import ModelLabError
from .huggingface_credentials import (
    REMOTE_HF_CREDENTIAL_ABSENT,
    REMOTE_HF_CREDENTIAL_UNSAFE,
    REMOTE_HF_TOKEN_PATH,
    build_remote_hf_credential_argv,
    build_remote_hf_probe_argv,
    huggingface_token_path,
    open_huggingface_token_file,
)


def _translate(error: RunpodLocalError) -> ModelLabError:
    return ModelLabError(str(error), code=error.code)


def _require_return_code(action: str, return_code: int) -> None:
    if return_code == 0:
        return
    if return_code == REMOTE_HF_CREDENTIAL_UNSAFE:
        raise ModelLabError(
            "remote Hugging Face credential path or permissions are unsafe",
            code="unsafe_remote_hf_credential",
        )
    raise ModelLabError(
        f"remote Hugging Face credential {action} failed with exit status "
        f"{return_code}",
        code="remote_hf_credential_failed",
    )


def manage_huggingface_credential(
    action: str,
    host_name: str,
    *,
    token_file: pathlib.Path | None = None,
    runpod_state_root: pathlib.Path | None = None,
    credentials_path: pathlib.Path | None = None,
    api_factory: Callable[[Any], Any] = RunpodApi,
    popen_factory: Callable[..., Any] = subprocess.Popen,
) -> dict[str, Any]:
    """Push, inspect, or clear a token without placing its bytes in argv."""

    if action not in {"push", "status", "clear"}:
        raise ModelLabError(
            "hf-auth action must be push, status, or clear",
            code="invalid_hf_auth_action",
        )
    state = StateStore(
        runpod_state_root
        if runpod_state_root is not None
        else default_runpod_state_root()
    )
    instances = InstanceStore(state)
    try:
        credential = CredentialStore(
            credentials_path or credentials_file()
        ).load(required=True)
        if credential is None:
            raise AssertionError("required RunPod credential unexpectedly absent")
        endpoint = resolve_endpoint(
            host_name,
            instances=instances,
            api=api_factory(credential),
            state=state,
        )
        ensure_known_hosts_file(endpoint.known_hosts_file)
    except RunpodLocalError as error:
        raise _translate(error) from error

    def execute(
        arguments: list[str],
        *,
        source: str,
        stdin: Any = None,
    ) -> int:
        try:
            return run_with_activity(
                build_ssh_argv(endpoint, arguments),
                instances=instances,
                name=host_name,
                expected_operation_id=endpoint.operation_id,
                expected_pod_id=endpoint.pod_id,
                source=source,
                stdin=stdin,
                popen_factory=popen_factory,
            )
        except RunpodLocalError as error:
            raise _translate(error) from error

    if action == "push":
        try:
            record = instances.load(host_name)
            if record is None:
                raise AssertionError("required host receipt unexpectedly absent")
            expected = record.get("expected")
            image = expected.get("image") if isinstance(expected, dict) else None
            validate_image_digest(image)
        except RunpodLocalError as error:
            raise ModelLabError(
                "Hugging Face credential push requires a host receipt with "
                "one digest-pinned image",
                code="hf_auth_unpinned_image",
            ) from error
        selected_token = token_file or huggingface_token_path()
        with open_huggingface_token_file(selected_token) as token:
            probe_return_code = execute(
                build_remote_hf_probe_argv(),
                source="model-lab-hf-auth-probe",
            )
            if probe_return_code != 0:
                raise ModelLabError(
                    "remote Hugging Face credential host probe failed with "
                    f"exit status {probe_return_code}",
                    code="remote_hf_credential_probe_failed",
                )
            token.seek(0)
            return_code = execute(
                build_remote_hf_credential_argv("push"),
                source="model-lab-hf-auth-push",
                stdin=token,
            )
        _require_return_code(action, return_code)
        configured = True
        changed = True
    else:
        return_code = execute(
            build_remote_hf_credential_argv(action),
            source=f"model-lab-hf-auth-{action}",
        )
        if return_code == REMOTE_HF_CREDENTIAL_ABSENT:
            configured = False
            changed = False
        else:
            _require_return_code(action, return_code)
            configured = action == "status"
            changed = action == "clear"
    return {
        "schema_version": "model-lab.hf-auth.v1",
        "action": action,
        "host_name": host_name,
        "configured": configured,
        "changed": changed,
        "remote_token_path": REMOTE_HF_TOKEN_PATH,
        "storage": "ephemeral-container",
    }
