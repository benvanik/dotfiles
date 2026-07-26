"""Validated SSH, loopback tunnel, and bidirectional copy boundaries."""

from __future__ import annotations

import datetime
import ipaddress
import os
import pathlib
import re
import shlex
import stat
import subprocess
from dataclasses import asdict, dataclass
from typing import Any, Callable

from .allocation import verify_allocated_pod
from .api import RunpodApi
from .errors import RunpodLocalError
from .instances import InstanceStore, lease_expiry_reasons
from .paths import ensure_private_directory
from .profile import (
    validate_ssh_identity_file,
    validate_ssh_key_pair,
    validate_ssh_public_key,
)
from .state import StateStore, validate_record_name


PROVIDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,191}$")
REMOTE_PATH_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9._+@%=-]+$")
REMOTE_COPY_ROOTS = ("/workspace", "/root/runpod-session")
SENSITIVE_ENVIRONMENT_PATTERN = re.compile(
    r"(?:^|_)(?:TOKEN|KEY|SECRET|PASSWORD|CREDENTIALS?)(?:_|$)",
    re.IGNORECASE,
)
RFC1918_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


@dataclass(frozen=True)
class SshEndpoint:
    instance_name: str
    operation_id: str
    pod_id: str
    host: str
    port: int
    user: str
    identity_file: pathlib.Path
    known_hosts_file: pathlib.Path
    host_key_alias: str

    def safe_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["identity_file"] = str(self.identity_file)
        result["known_hosts_file"] = str(self.known_hosts_file)
        return result


def _provider_id(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not PROVIDER_ID_PATTERN.fullmatch(value):
        raise RunpodLocalError(
            f"invalid Runpod {label}: {value!r}",
            code="invalid_provider_id",
        )
    return value


def _public_pod_ipv4(value: Any) -> str:
    if value in (None, ""):
        raise RunpodLocalError(
            "Pod does not have a public SSH address yet",
            code="pod_not_ready",
        )
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise RunpodLocalError(
            f"Pod public address is invalid: {value!r}",
            code="invalid_ssh_endpoint",
        ) from error
    if not isinstance(address, ipaddress.IPv4Address):
        raise RunpodLocalError(
            "only Runpod's IPv4 SSH endpoint is supported",
            code="invalid_ssh_endpoint",
        )
    if (
        address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or any(address in network for network in RFC1918_NETWORKS)
    ):
        raise RunpodLocalError(
            f"Pod public address is not an admissible Runpod endpoint: {address}",
            code="invalid_ssh_endpoint",
        )
    return str(address)


def _port(value: Any, *, label: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= 65535
    ):
        raise RunpodLocalError(
            f"{label} must be an integer from 1 through 65535",
            code="invalid_port",
        )
    return value


def _known_hosts_path(state: StateStore, pod_id: str) -> pathlib.Path:
    return state.root / "ssh" / "known-hosts" / pod_id


def ensure_known_hosts_file(path: pathlib.Path) -> pathlib.Path:
    ensure_private_directory(path.parent)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise RunpodLocalError(
            f"cannot open dedicated known-hosts file {path}: {error}",
            code="unsafe_known_hosts",
        ) from error
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RunpodLocalError(
                f"known-hosts path is not a regular file: {path}",
                code="unsafe_known_hosts",
            )
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise RunpodLocalError(
                f"known-hosts file is not owned by the current user: {path}",
                code="unsafe_known_hosts",
            )
    finally:
        os.close(descriptor)
    return path


def validate_known_hosts_file(path: pathlib.Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise RunpodLocalError(
            f"known-hosts path is not a regular file: {path}",
            code="unsafe_known_hosts",
        )
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise RunpodLocalError(
            f"known-hosts file is not owned by the current user: {path}",
            code="unsafe_known_hosts",
        )
    if metadata.st_mode & 0o077:
        raise RunpodLocalError(
            f"known-hosts permissions are broader than 0600: {path}",
            code="unsafe_known_hosts",
        )


def resolve_endpoint(
    name: str,
    *,
    instances: InstanceStore,
    api: RunpodApi,
    state: StateStore,
) -> SshEndpoint:
    validate_record_name(name)
    record = instances.load(name)
    if record is None:
        raise AssertionError("required instance unexpectedly absent")
    if record["phase"] != "active":
        raise RunpodLocalError(
            f"instance {name} is {record['phase']}, not active",
            code="instance_not_active",
        )
    expiry_reasons = lease_expiry_reasons(
        record, now=datetime.datetime.now(datetime.timezone.utc)
    )
    if expiry_reasons:
        raise RunpodLocalError(
            "instance lease has expired: " + ", ".join(expiry_reasons),
            code="lease_expired",
        )
    pod_id = _provider_id(record.get("pod_id"), label="Pod ID")
    pod = api.get_pod(pod_id)
    return endpoint_from_record_pod(record, pod=pod, state=state)


def endpoint_from_record_pod(
    record: dict[str, Any],
    *,
    pod: dict[str, Any],
    state: StateStore,
) -> SshEndpoint:
    name = record["name"]
    pod_id = _provider_id(record.get("pod_id"), label="Pod ID")
    if pod.get("id") != pod_id or pod.get("name") != record["remote_name"]:
        raise RunpodLocalError(
            "live Pod identity does not match the active receipt",
            code="pod_identity_conflict",
        )
    violations, pending = verify_allocated_pod(record, pod)
    if violations:
        raise RunpodLocalError(
            "live Pod violates its active allocation receipt: "
            + "; ".join(violations),
            code="allocation_policy_mismatch",
        )
    if pending:
        raise RunpodLocalError(
            "live Pod allocation is not fully populated yet: "
            + ", ".join(pending),
            code="pod_not_ready",
        )
    if pod.get("desired_status") != "RUNNING":
        raise RunpodLocalError(
            "Pod is not in RUNNING desired state",
            code="pod_not_ready",
        )
    connection = record.get("connection")
    if not isinstance(connection, dict):
        raise RunpodLocalError(
            "instance receipt has no connection policy",
            code="invalid_instance_record",
        )
    if connection.get("user") != "root":
        raise RunpodLocalError(
            "only the root Runpod SSH user is supported",
            code="invalid_ssh_endpoint",
        )
    if connection.get("internal_ssh_port") != 22:
        raise RunpodLocalError(
            "instance receipt does not use internal SSH port 22",
            code="invalid_ssh_endpoint",
        )
    mappings = pod.get("port_mappings")
    if not isinstance(mappings, dict) or "22" not in mappings:
        raise RunpodLocalError(
            "Pod SSH port mapping is not ready yet",
            code="pod_not_ready",
        )
    host = _public_pod_ipv4(pod.get("public_ip"))
    port = _port(mappings["22"], label="mapped SSH port")
    identity = validate_ssh_identity_file(connection.get("identity_file"))
    payload = record.get("pod_payload")
    environment = payload.get("env") if isinstance(payload, dict) else None
    injected_public_key = (
        environment.get("SSH_PUBLIC_KEY")
        if isinstance(environment, dict)
        else None
    )
    if not isinstance(injected_public_key, str):
        raise RunpodLocalError(
            "instance receipt has no injected SSH public key",
            code="invalid_instance_record",
        )
    injected_public_key = validate_ssh_public_key(injected_public_key)
    validate_ssh_key_pair(str(identity), injected_public_key)
    known_hosts = _known_hosts_path(state, pod_id)
    validate_known_hosts_file(known_hosts)
    return SshEndpoint(
        instance_name=name,
        operation_id=record["operation_id"],
        pod_id=pod_id,
        host=host,
        port=port,
        user="root",
        identity_file=identity,
        known_hosts_file=known_hosts,
        host_key_alias=f"runpod-{pod_id}",
    )


def _ssh_options(endpoint: SshEndpoint) -> list[str]:
    values = [
        ("BatchMode", "yes"),
        ("IdentitiesOnly", "yes"),
        ("IdentityAgent", "none"),
        ("PasswordAuthentication", "no"),
        ("KbdInteractiveAuthentication", "no"),
        ("UserKnownHostsFile", str(endpoint.known_hosts_file)),
        ("GlobalKnownHostsFile", "/dev/null"),
        ("HostKeyAlias", endpoint.host_key_alias),
        ("StrictHostKeyChecking", "accept-new"),
        ("CheckHostIP", "no"),
        ("UpdateHostKeys", "no"),
        ("ForwardAgent", "no"),
        ("ForwardX11", "no"),
        ("PermitLocalCommand", "no"),
        ("ProxyCommand", "none"),
        ("ProxyJump", "none"),
        ("ConnectTimeout", "15"),
        ("ConnectionAttempts", "1"),
        ("ServerAliveInterval", "30"),
        ("ServerAliveCountMax", "3"),
    ]
    result = ["-F", "/dev/null"]
    for name, value in values:
        result.extend(["-o", f"{name}={value}"])
    return result


def _validate_remote_arguments(arguments: list[str]) -> None:
    for argument in arguments:
        if "\x00" in argument or any(ord(character) < 32 for character in argument):
            raise RunpodLocalError(
                "remote command arguments cannot contain control characters",
                code="invalid_remote_command",
            )


def build_ssh_argv(
    endpoint: SshEndpoint,
    remote_argv: list[str] | None = None,
) -> list[str]:
    argv = ["ssh", *_ssh_options(endpoint)]
    argv.extend(
        [
            "-i",
            str(endpoint.identity_file),
            "-p",
            str(endpoint.port),
            f"{endpoint.user}@{endpoint.host}",
        ]
    )
    if remote_argv:
        _validate_remote_arguments(remote_argv)
        argv.append("exec " + shlex.join(remote_argv))
    return argv


def build_tunnel_argv(
    endpoint: SshEndpoint,
    *,
    local_port: int,
    remote_port: int,
) -> list[str]:
    local_port = _port(local_port, label="local tunnel port")
    remote_port = _port(remote_port, label="remote tunnel port")
    return [
        "ssh",
        *_ssh_options(endpoint),
        "-o",
        "ExitOnForwardFailure=yes",
        "-N",
        "-T",
        "-L",
        f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
        "-i",
        str(endpoint.identity_file),
        "-p",
        str(endpoint.port),
        f"{endpoint.user}@{endpoint.host}",
    ]


def validate_remote_copy_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or any(ord(character) < 32 for character in value)
    ):
        raise RunpodLocalError(
            "remote copy path must use an allowed absolute root",
            code="invalid_remote_path",
        )
    root = next(
        (
            candidate
            for candidate in REMOTE_COPY_ROOTS
            if value == candidate or value.startswith(f"{candidate}/")
        ),
        None,
    )
    if root is None:
        raise RunpodLocalError(
            "remote copy paths must be beneath /workspace or "
            "/root/runpod-session",
            code="invalid_remote_path",
        )
    suffix = value[len(root) :]
    parts = suffix[1:].split("/") if suffix else []
    if any(
        part in ("", ".", "..") or not REMOTE_PATH_SEGMENT_PATTERN.fullmatch(part)
        for part in parts
    ):
        raise RunpodLocalError(
            "remote copy path is not canonical or contains unsafe characters",
            code="invalid_remote_path",
        )
    return value


def _local_push_source(value: str, *, recursive: bool) -> pathlib.Path:
    try:
        path = pathlib.Path(value).expanduser().resolve(strict=True)
    except OSError as error:
        raise RunpodLocalError(
            f"local copy source does not exist or cannot be resolved: {value}",
            code="invalid_local_path",
        ) from error
    if not path.is_file() and not path.is_dir():
        raise RunpodLocalError(
            f"local copy source is not a regular file or directory: {path}",
            code="invalid_local_path",
        )
    if path.is_dir() and not recursive:
        raise RunpodLocalError(
            "copying a local directory requires --recursive",
            code="recursive_copy_required",
        )
    return path


def _local_pull_destination(value: str) -> pathlib.Path:
    path = pathlib.Path(value).expanduser().absolute()
    parent = path if path.exists() and path.is_dir() else path.parent
    if not parent.exists() or not parent.is_dir():
        raise RunpodLocalError(
            f"local copy destination parent does not exist: {parent}",
            code="invalid_local_path",
        )
    return path


def build_copy_argv(
    endpoint: SshEndpoint,
    *,
    direction: str,
    source: str,
    destination: str,
    recursive: bool,
) -> list[str]:
    if direction == "push":
        local_operand = str(_local_push_source(source, recursive=recursive))
        remote_path = validate_remote_copy_path(destination)
        source_operand = local_operand
        destination_operand = (
            f"{endpoint.user}@{endpoint.host}:{remote_path}"
        )
    elif direction == "pull":
        remote_path = validate_remote_copy_path(source)
        source_operand = f"{endpoint.user}@{endpoint.host}:{remote_path}"
        destination_operand = str(_local_pull_destination(destination))
    else:
        raise RunpodLocalError(
            f"invalid copy direction: {direction!r}",
            code="invalid_copy_direction",
        )
    argv = ["scp", *_ssh_options(endpoint)]
    argv.extend(
        [
            "-i",
            str(endpoint.identity_file),
            "-P",
            str(endpoint.port),
        ]
    )
    if recursive:
        argv.append("-r")
    argv.extend(["--", source_operand, destination_operand])
    return argv


def sanitized_subprocess_environment(
    environment: dict[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {
        name: value
        for name, value in source.items()
        if name != "SSH_AUTH_SOCK"
        and not SENSITIVE_ENVIRONMENT_PATTERN.search(name)
    }


def run_with_activity(
    argv: list[str],
    *,
    instances: InstanceStore,
    name: str,
    expected_operation_id: str,
    expected_pod_id: str,
    source: str,
    maintain_activity: bool = True,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    clock: Callable[[], datetime.datetime] | None = None,
) -> int:
    now = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
    record = instances.check_active_lease(
        name,
        now=now(),
        expected_operation_id=expected_operation_id,
        expected_pod_id=expected_pod_id,
    )
    try:
        process = popen_factory(
            argv,
            env=sanitized_subprocess_environment(),
            shell=False,
        )
    except OSError as error:
        raise RunpodLocalError(
            f"cannot start remote client: {error}",
            code="remote_client_start_failed",
        ) from error
    if maintain_activity:
        try:
            record = instances.touch(
                name,
                now=now(),
                source=source,
                expected_operation_id=expected_operation_id,
                expected_pod_id=expected_pod_id,
            )
        except RunpodLocalError:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
    idle_timeout = record["lease"]["idle_timeout_seconds"]
    heartbeat_seconds = (
        30
        if idle_timeout is None
        else min(30, max(1, idle_timeout // 3))
    )
    while True:
        try:
            return_code = process.wait(timeout=heartbeat_seconds)
            break
        except subprocess.TimeoutExpired:
            try:
                if maintain_activity:
                    instances.touch(
                        name,
                        now=now(),
                        source=source,
                        expected_operation_id=expected_operation_id,
                        expected_pod_id=expected_pod_id,
                        record_event=False,
                    )
                else:
                    instances.check_active_lease(
                        name,
                        now=now(),
                        expected_operation_id=expected_operation_id,
                        expected_pod_id=expected_pod_id,
                    )
            except RunpodLocalError:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
    if return_code == 0 and maintain_activity:
        instances.touch(
            name,
            now=now(),
            source=source,
            expected_operation_id=expected_operation_id,
            expected_pod_id=expected_pod_id,
        )
    return int(return_code)
