"""Hardened bubblewrap boundary for one locked model-session run.

All host objects are opened with ``O_PATH|O_NOFOLLOW`` and passed to
bubblewrap by descriptor.  The returned plan owns those descriptors until the
caller either executes the plan or closes its context.
"""

from __future__ import annotations

import os
import pathlib
import re
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import TYPE_CHECKING, Self

from .attachment import (
    inference_workload_identity,
    load_inference_attachment,
)
from .errors import ModelSessionError
from .pi_runtime import (
    INFERENCE_RELAY_PATH,
    INFERENCE_RELAY_ROLE,
    PI_MODELS_PATH,
    PI_MODELS_ROLE,
    SESSION_POLICY_PATH,
    SESSION_POLICY_ROLE,
    fingerprint_pi_installation_for_root_descriptor,
)

if TYPE_CHECKING:
    from .runs import SessionRun


BWRAP_BINARY = pathlib.Path("/usr/bin/bwrap")
MINIMUM_BWRAP_VERSION = (0, 11, 0)
PRIVATE_TMP_BYTES = 1024 * 1024 * 1024
PRIVATE_HOME_BYTES = 256 * 1024 * 1024
PRIVATE_CONFIG_BYTES = 16 * 1024 * 1024
PRIVATE_SHM_BYTES = 256 * 1024 * 1024
INFERENCE_SOCKET_DESTINATION = "/run/model-session/inference.sock"

_VERSION_PATTERN = re.compile(
    r"^bubblewrap ([0-9]+)\.([0-9]+)\.([0-9]+)(?:[^0-9].*)?$"
)
_SESSION_ID_PATTERN = re.compile(
    r"^[0-9]{8}T[0-9]{12}Z-[0-9a-f]{16}$"
)
_REQUIRED_BWRAP_OPTIONS = frozenset(
    {
        "--assert-userns-disabled",
        "--bind-fd",
        "--chmod",
        "--clearenv",
        "--dev",
        "--die-with-parent",
        "--disable-userns",
        "--hostname",
        "--new-session",
        "--proc",
        "--remount-ro",
        "--ro-bind-fd",
        "--size",
        "--tmpfs",
        "--unshare-all",
        "--unshare-user",
    }
)

# The sandbox has no host credentials, external network, daemon sockets, or
# host process namespace.  Masking these entrypoints additionally prevents an
# agent from mistaking a host administration client for an available tool.
DENIED_COMMAND_DESTINATIONS = (
    "/usr/bin/aws",
    "/usr/bin/az",
    "/usr/bin/bwrap",
    "/usr/bin/buildah",
    "/usr/bin/chroot",
    "/usr/bin/consul",
    "/usr/bin/ctr",
    "/usr/bin/doas",
    "/usr/bin/docker",
    "/usr/bin/docker-compose",
    "/usr/bin/dockerd",
    "/usr/bin/gcloud",
    "/usr/bin/gsutil",
    "/usr/bin/helm",
    "/usr/bin/kubectl",
    "/usr/bin/login",
    "/usr/bin/machinectl",
    "/usr/bin/mount",
    "/usr/bin/nerdctl",
    "/usr/bin/nomad",
    "/usr/bin/nsenter",
    "/usr/bin/pkexec",
    "/usr/bin/podman",
    "/usr/bin/runpod",
    "/usr/bin/runpodctl",
    "/usr/bin/scp",
    "/usr/bin/setpriv",
    "/usr/bin/sftp",
    "/usr/bin/ssh",
    "/usr/bin/ssh-add",
    "/usr/bin/ssh-agent",
    "/usr/bin/ssh-copy-id",
    "/usr/bin/ssh-keygen",
    "/usr/bin/ssh-keyscan",
    "/usr/bin/su",
    "/usr/bin/sudo",
    "/usr/bin/systemctl",
    "/usr/bin/terraform",
    "/usr/bin/tofu",
    "/usr/bin/umount",
    "/usr/bin/unshare",
    "/usr/bin/vault",
    "/usr/lib/openssh/ssh-keysign",
)

_FIXED_ENVIRONMENT = (
    ("HOME", "/home/agent"),
    ("LANG", "C.UTF-8"),
    ("LC_ALL", "C.UTF-8"),
    ("LOGNAME", "agent"),
    (
        "MODEL_SESSION_BASE_URL",
        "http://127.0.0.1:41111/v1",
    ),
    ("MODEL_SESSION_INFERENCE_SOCKET", INFERENCE_SOCKET_DESTINATION),
    ("NO_PROXY", "127.0.0.1,localhost"),
    ("PATH", "/opt/pi/bin:/usr/bin:/bin"),
    ("PI_CODING_AGENT_DIR", "/config"),
    ("PI_CODING_AGENT_SESSION_DIR", "/sessions"),
    ("PI_OFFLINE", "1"),
    ("PI_SKIP_VERSION_CHECK", "1"),
    ("PI_TELEMETRY", "0"),
    ("PYTHONDONTWRITEBYTECODE", "1"),
    ("SHELL", "/bin/sh"),
    ("TERM", "xterm-256color"),
    ("TMPDIR", "/tmp"),
    ("USER", "agent"),
)


def _fail(message: str, *, code: str = "unsafe_sandbox") -> None:
    raise ModelSessionError(message, code=code)


def _normal_absolute_path(value: pathlib.Path | str, *, label: str) -> pathlib.Path:
    text = os.fspath(value)
    if (
        not isinstance(text, str)
        or not text
        or "\x00" in text
        or not pathlib.Path(text).is_absolute()
        or os.path.normpath(text) != text
    ):
        _fail(f"{label} must be an absolute normalized path")
    return pathlib.Path(text)


def _is_within(path: pathlib.Path, root: pathlib.Path) -> bool:
    return path == root or path.is_relative_to(root)


def _overlaps(first: pathlib.Path, second: pathlib.Path) -> bool:
    return _is_within(first, second) or _is_within(second, first)


def _open_absolute_no_symlinks(
    path: pathlib.Path,
    *,
    label: str,
    final_must_be_directory: bool,
) -> int:
    """Open an absolute path one component at a time without following links."""

    if not hasattr(os, "O_PATH") or not hasattr(os, "O_NOFOLLOW"):
        _fail(
            "model-session sandbox requires Linux O_PATH and O_NOFOLLOW",
            code="sandbox_platform_unsupported",
        )
    path = _normal_absolute_path(path, label=label)
    parts = path.parts
    if len(parts) < 2 or parts[0] != "/":
        _fail(f"{label} must name a concrete object below /")

    common_flags = os.O_PATH | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    current = os.open(
        "/",
        common_flags | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        for index, component in enumerate(parts[1:]):
            final = index == len(parts) - 2
            flags = common_flags
            if not final or final_must_be_directory:
                flags |= getattr(os, "O_DIRECTORY", 0)
            try:
                child = os.open(component, flags, dir_fd=current)
            except OSError as error:
                raise ModelSessionError(
                    f"cannot open {label} {path} without following links: {error}",
                    code="unsafe_sandbox_source",
                ) from error
            os.close(current)
            current = child
            metadata = os.fstat(current)
            if stat.S_ISLNK(metadata.st_mode):
                _fail(
                    f"{label} contains a symbolic-link component: {path}",
                    code="unsafe_sandbox_source",
                )
            if not final and not stat.S_ISDIR(metadata.st_mode):
                _fail(
                    f"{label} contains a non-directory component: {path}",
                    code="unsafe_sandbox_source",
                )
        return current
    except BaseException:
        os.close(current)
        raise


def _validate_descriptor(
    descriptor: int,
    *,
    path: pathlib.Path,
    label: str,
    expected_type: str,
    allowed_owners: frozenset[int],
    exact_mode: int | None = None,
    reject_group_or_world_write: bool = True,
) -> None:
    metadata = os.fstat(descriptor)
    predicates = {
        "directory": stat.S_ISDIR,
        "regular file": stat.S_ISREG,
        "Unix socket": stat.S_ISSOCK,
        "character device": stat.S_ISCHR,
    }
    predicate = predicates[expected_type]
    if not predicate(metadata.st_mode):
        _fail(
            f"{label} is not a {expected_type}: {path}",
            code="unsafe_sandbox_source",
        )
    if metadata.st_uid not in allowed_owners:
        _fail(
            f"{label} has an unexpected owner: {path}",
            code="unsafe_sandbox_source",
        )
    mode = stat.S_IMODE(metadata.st_mode)
    if exact_mode is not None and mode != exact_mode:
        _fail(
            f"{label} permissions must be exactly {exact_mode:04o}: {path}",
            code="unsafe_sandbox_permissions",
        )
    if reject_group_or_world_write and mode & (stat.S_IWGRP | stat.S_IWOTH):
        _fail(
            f"{label} is group- or world-writable: {path}",
            code="unsafe_sandbox_permissions",
        )


def _open_validated(
    path: pathlib.Path,
    *,
    label: str,
    expected_type: str,
    allowed_owners: frozenset[int],
    exact_mode: int | None = None,
    reject_group_or_world_write: bool = True,
) -> int:
    descriptor = _open_absolute_no_symlinks(
        path,
        label=label,
        final_must_be_directory=expected_type == "directory",
    )
    try:
        _validate_descriptor(
            descriptor,
            path=path,
            label=label,
            expected_type=expected_type,
            allowed_owners=allowed_owners,
            exact_mode=exact_mode,
            reject_group_or_world_write=reject_group_or_world_write,
        )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _probe_bwrap(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [os.fspath(BWRAP_BINARY), *arguments],
            check=False,
            capture_output=True,
            text=True,
            env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )
    except OSError as error:
        raise ModelSessionError(
            f"cannot execute {BWRAP_BINARY}: {error}",
            code="bwrap_unavailable",
        ) from error


def validate_bwrap() -> tuple[int, int, int]:
    """Validate the exact system bubblewrap binary and required feature set."""

    descriptor = _open_absolute_no_symlinks(
        BWRAP_BINARY,
        label="bubblewrap binary",
        final_must_be_directory=False,
    )
    try:
        _validate_descriptor(
            descriptor,
            path=BWRAP_BINARY,
            label="bubblewrap binary",
            expected_type="regular file",
            allowed_owners=frozenset({0}),
        )
        metadata = os.fstat(descriptor)
        if not metadata.st_mode & stat.S_IXUSR:
            _fail(
                f"bubblewrap binary is not executable: {BWRAP_BINARY}",
                code="bwrap_unavailable",
            )
    finally:
        os.close(descriptor)

    version_result = _probe_bwrap(("--version",))
    if version_result.returncode != 0:
        _fail(
            f"{BWRAP_BINARY} --version failed: {version_result.stderr.strip()}",
            code="bwrap_unavailable",
        )
    match = _VERSION_PATTERN.fullmatch(version_result.stdout.strip())
    if match is None:
        _fail(
            f"cannot parse bubblewrap version: {version_result.stdout.strip()!r}",
            code="bwrap_version_unsupported",
        )
    version = tuple(int(part) for part in match.groups())
    if version < MINIMUM_BWRAP_VERSION:
        minimum = ".".join(str(part) for part in MINIMUM_BWRAP_VERSION)
        actual = ".".join(str(part) for part in version)
        _fail(
            f"bubblewrap {actual} is too old; model-session requires {minimum}+",
            code="bwrap_version_unsupported",
        )

    help_result = _probe_bwrap(("--help",))
    if help_result.returncode != 0:
        _fail(
            f"{BWRAP_BINARY} --help failed: {help_result.stderr.strip()}",
            code="bwrap_unavailable",
        )
    help_text = help_result.stdout + help_result.stderr
    missing = sorted(
        option for option in _REQUIRED_BWRAP_OPTIONS if option not in help_text
    )
    if missing:
        _fail(
            "bubblewrap lacks required sandbox capabilities: "
            + ", ".join(missing),
            code="bwrap_capability_unsupported",
        )
    return version


@dataclass
class SandboxPlan:
    """An argv plus the descriptors it exclusively owns for one launch."""

    argv: tuple[str, ...]
    pass_fds: tuple[int, ...]
    _owned_descriptors: list[int] = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        descriptors = self._owned_descriptors
        self._owned_descriptors = []
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass

    def __enter__(self) -> Self:
        if self._closed:
            _fail("cannot enter a closed sandbox plan")
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


def _validate_command(command: Sequence[str]) -> tuple[str, ...]:
    if isinstance(command, (str, bytes)) or not command:
        _fail("sandbox command must be a nonempty argument sequence")
    arguments = tuple(command)
    if any(
        not isinstance(argument, str) or "\x00" in argument
        for argument in arguments
    ):
        _fail("sandbox command arguments must be NUL-free strings")
    executable = pathlib.PurePosixPath(arguments[0])
    if (
        not executable.is_absolute()
        or executable.as_posix() != arguments[0]
        or any(part in {"", ".", ".."} for part in executable.parts)
    ):
        _fail(
            "sandbox command executable must be an absolute normalized "
            "sandbox path"
        )
    allowed = (
        arguments[0] == "/usr/bin/python3"
        or arguments[0].startswith("/opt/pi/")
    )
    if not allowed:
        _fail(
            "sandbox entrypoint must be locked under /opt/pi or be the "
            "system /usr/bin/python3 relay runtime"
        )
    return arguments


def _validate_source_relationships(run: SessionRun) -> None:
    if (
        not isinstance(run.session_id, str)
        or not _SESSION_ID_PATTERN.fullmatch(run.session_id)
    ):
        _fail("sandbox run has an invalid internally generated session ID")
    profile_id = run.profile.profile_id
    if (
        not isinstance(profile_id, str)
        or not re.fullmatch(r"^[a-z][a-z0-9-]{0,62}$", profile_id)
    ):
        _fail("sandbox run has an invalid profile ID")
    state_root = _normal_absolute_path(
        run.profile.state_root,
        label="state root",
    )
    session_root = _normal_absolute_path(run.root, label="session root")
    if session_root != (
        state_root / "sessions" / profile_id / run.session_id
    ):
        _fail("session root is not at its canonical state path")
    expected_session_paths = {
        "snapshot root": session_root / "snapshot",
        "session workspace": session_root / "workspace",
        "Pi sessions": session_root / "pi" / "sessions",
    }
    actual_session_paths = {
        "snapshot root": run.snapshot_root,
        "session workspace": run.workspace,
        "Pi sessions": run.pi_sessions,
    }
    for label, expected in expected_session_paths.items():
        actual = _normal_absolute_path(actual_session_paths[label], label=label)
        if actual != expected:
            _fail(f"{label} is not at its canonical session path")

    required_resources = (
        (PI_MODELS_ROLE, PI_MODELS_PATH),
        (INFERENCE_RELAY_ROLE, INFERENCE_RELAY_PATH),
        (SESSION_POLICY_ROLE, SESSION_POLICY_PATH),
    )
    for role, relative_path in required_resources:
        resource = run.resource_for_role(role)
        expected_path = run.snapshot_root.joinpath(*relative_path.parts)
        if (
            resource is None
            or resource.relative_path != relative_path
            or _normal_absolute_path(
                resource.path,
                label=f"locked {role} resource",
            )
            != expected_path
        ):
            _fail(
                f"locked {role} resource is not at its canonical snapshot path"
            )

    project_root = _normal_absolute_path(
        run.profile.project_root,
        label="project root",
    )
    expected_report = project_root / "reports" / run.session_id
    expected_memory = project_root / "memory" / run.session_id
    if _normal_absolute_path(
        run.report_directory,
        label="session report directory",
    ) != expected_report:
        _fail("session report directory is not at its canonical project path")
    if _normal_absolute_path(
        run.memory_directory,
        label="session memory directory",
    ) != expected_memory:
        _fail("session memory directory is not at its canonical project path")

    pi_root = _normal_absolute_path(
        run.profile.pi.installation_root,
        label="Pi installation root",
    )
    infrastructure_root = pathlib.Path(__file__).resolve().parents[2]
    broad_roots = {
        pathlib.Path("/"),
        pathlib.Path("/home"),
        pathlib.Path("/media"),
        pathlib.Path("/mnt"),
        pathlib.Path("/opt"),
        pathlib.Path("/srv"),
        pathlib.Path("/tmp"),
        pathlib.Path("/usr"),
        pathlib.Path("/var"),
    }
    if any(source in broad_roots for source in (state_root, project_root, pi_root)):
        _fail("sandbox source is a dangerously broad host root")
    if _overlaps(pi_root, infrastructure_root):
        _fail("Pi installation root is not a dedicated external tree")

    sources = (
        state_root,
        project_root,
        pi_root,
    )
    home = pathlib.Path.home()
    credential_roots = (
        home / ".aws",
        home / ".config",
        home / ".gnupg",
        home / ".ssh",
    )
    system_roots = (
        pathlib.Path("/boot"),
        pathlib.Path("/dev"),
        pathlib.Path("/etc"),
        pathlib.Path("/proc"),
        pathlib.Path("/root"),
        pathlib.Path("/sys"),
        pathlib.Path("/usr"),
    )
    for source in sources:
        for protected in (*credential_roots, *system_roots):
            if _overlaps(source, protected):
                _fail(f"sandbox source overlaps protected host state: {source}")
    for index, first in enumerate(sources):
        for second in sources[index + 1 :]:
            if _overlaps(first, second):
                _fail(f"sandbox source roots overlap: {first} and {second}")
    if any(
        _overlaps(source, infrastructure_root)
        for source in sources
    ):
        _fail("sandbox source overlaps the dotfiles infrastructure repository")


def _existing_mask_destinations() -> tuple[str, ...]:
    destinations = []
    for destination in DENIED_COMMAND_DESTINATIONS:
        try:
            metadata = pathlib.Path(destination).lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ModelSessionError(
                f"cannot inspect denied command {destination}: {error}",
                code="unsafe_sandbox_source",
            ) from error
        if stat.S_ISREG(metadata.st_mode):
            destinations.append(destination)
    return tuple(destinations)


def build_sandbox_plan(
    run: SessionRun,
    *,
    command: Sequence[str],
    attachment_runtime_root: os.PathLike[str] | str | None = None,
) -> SandboxPlan:
    """Build one descriptor-backed invocation from a fresh live attachment."""

    validate_bwrap()
    arguments = _validate_command(command)
    _validate_source_relationships(run)
    current_user = os.getuid()
    user_only = frozenset({current_user})
    root_or_user = frozenset({0, current_user})
    descriptors: list[int] = []

    def open_source(
        path: pathlib.Path | str,
        *,
        label: str,
        expected_type: str = "directory",
        allowed_owners: frozenset[int] = user_only,
        exact_mode: int | None = None,
        reject_group_or_world_write: bool = True,
    ) -> int:
        descriptor = _open_validated(
            _normal_absolute_path(path, label=label),
            label=label,
            expected_type=expected_type,
            allowed_owners=allowed_owners,
            exact_mode=exact_mode,
            reject_group_or_world_write=reject_group_or_world_write,
        )
        descriptors.append(descriptor)
        return descriptor

    try:
        usr_descriptor = open_source(
            pathlib.Path("/usr"),
            label="system /usr",
            allowed_owners=frozenset({0}),
        )
        workspace_descriptor = open_source(
            run.workspace,
            label="session workspace",
            exact_mode=0o700,
        )
        profile_descriptor = open_source(
            run.snapshot_root / "profile",
            label="locked profile resources",
            exact_mode=0o700,
        )
        runtime_descriptor = open_source(
            run.snapshot_root / "runtime",
            label="locked model-session runtime",
            exact_mode=0o700,
        )
        pi_installation_descriptor = open_source(
            run.profile.pi.installation_root,
            label="Pi installation root",
            allowed_owners=root_or_user,
        )
        current_pi_installation = (
            fingerprint_pi_installation_for_root_descriptor(
                run.profile,
                pi_installation_descriptor,
            )
        )
        if current_pi_installation != run.pi_installation:
            _fail(
                "Pi installation changed after the run was loaded",
                code="pi_installation_changed",
            )
        models = run.resource_for_role(PI_MODELS_ROLE)
        if models is None:
            _fail("locked Pi models resource is missing")
        pi_models_descriptor = open_source(
            models.path,
            label="locked Pi models configuration",
            expected_type="regular file",
            exact_mode=0o600,
        )
        pi_sessions_descriptor = open_source(
            run.pi_sessions,
            label="Pi sessions",
            exact_mode=0o700,
        )
        project_descriptor = open_source(
            run.profile.project_root,
            label="project root",
        )
        report_descriptor = open_source(
            run.report_directory,
            label="session report directory",
            exact_mode=0o700,
        )
        memory_descriptor = open_source(
            run.memory_directory,
            label="session memory directory",
            exact_mode=0o700,
        )
        attachment = load_inference_attachment(
            run.profile,
            runtime_root=attachment_runtime_root,
        )
        if (
            attachment.profile_id != run.profile.profile_id
            or attachment.project_id != run.profile.project_id
            or attachment.workload_sha256
            != inference_workload_identity(run.profile)
        ):
            _fail(
                "loaded inference attachment does not match the locked "
                "session workload",
                code="inference_attachment_mismatch",
            )
        socket_path = _normal_absolute_path(
            attachment.socket_path,
            label="inference socket",
        )
        if any(
            _is_within(
                socket_path,
                _normal_absolute_path(root, label="sandbox source root"),
            )
            for root in (
                run.workspace,
                run.snapshot_root / "profile",
                run.snapshot_root / "pi",
                run.snapshot_root / "runtime",
                run.pi_sessions,
                run.profile.pi.installation_root,
                run.profile.project_root,
            )
        ):
            _fail("inference socket must not be reachable through another mount")
        socket_descriptor = open_source(
            socket_path,
            label="inference socket",
            expected_type="Unix socket",
            exact_mode=0o600,
        )
        socket_metadata = os.fstat(socket_descriptor)
        if (
            socket_metadata.st_dev != attachment.socket_device
            or socket_metadata.st_ino != attachment.socket_inode
        ):
            _fail(
                "attached inference socket was replaced before sandbox launch",
                code="inference_attachment_unavailable",
            )
        command_mask_source = open_source(
            pathlib.Path("/dev/null"),
            label="command mask source",
            expected_type="character device",
            allowed_owners=frozenset({0}),
            reject_group_or_world_write=False,
        )
        command_masks = []
        for destination in _existing_mask_destinations():
            descriptor = os.dup(command_mask_source)
            descriptors.append(descriptor)
            command_masks.append((descriptor, destination))
        os.close(command_mask_source)
        descriptors.remove(command_mask_source)

        workspace_authority = pathlib.Path(run.workspace) / ".pi"
        authority_descriptor = open_source(
            workspace_authority,
            label="workspace authority mask target",
            exact_mode=0o700,
        )
        os.close(authority_descriptor)
        descriptors.remove(authority_descriptor)

        argv = [
            os.fspath(BWRAP_BINARY),
            "--unshare-all",
            "--unshare-user",
            "--disable-userns",
            "--assert-userns-disabled",
            "--new-session",
            "--die-with-parent",
            "--hostname",
            "model-session",
            "--clearenv",
            "--ro-bind-fd",
            str(usr_descriptor),
            "/usr",
            "--symlink",
            "usr/bin",
            "/bin",
            "--symlink",
            "usr/sbin",
            "/sbin",
            "--symlink",
            "usr/lib",
            "/lib",
            "--symlink",
            "usr/lib64",
            "/lib64",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            # /usr/local is host-local mutable policy and may contain
            # credential or administration clients.  System runtimes do not
            # need it for this boundary.
            "--tmpfs",
            "/usr/local",
            "--chmod",
            "0555",
            "/usr/local",
            "--remount-ro",
            "/usr/local",
        ]
        for descriptor, destination in command_masks:
            argv.extend(
                (
                    "--ro-bind-fd",
                    str(descriptor),
                    destination,
                )
            )
        argv.extend(
            (
                "--size",
                str(PRIVATE_TMP_BYTES),
                "--tmpfs",
                "/tmp",
                "--chmod",
                "1777",
                "/tmp",
                "--dir",
                "/home",
                "--size",
                str(PRIVATE_HOME_BYTES),
                "--tmpfs",
                "/home/agent",
                "--chmod",
                "0700",
                "/home/agent",
                "--bind-fd",
                str(workspace_descriptor),
                "/workspace",
                # This mount must remain after /workspace: project-local Pi
                # authority is never allowed to survive into a resumed agent.
                "--tmpfs",
                "/workspace/.pi",
                "--chmod",
                "0555",
                "/workspace/.pi",
                "--remount-ro",
                "/workspace/.pi",
                "--ro-bind-fd",
                str(profile_descriptor),
                "/profile",
                "--ro-bind-fd",
                str(pi_installation_descriptor),
                "/opt/pi",
                "--size",
                str(PRIVATE_CONFIG_BYTES),
                "--tmpfs",
                "/config",
                "--chmod",
                "0700",
                "/config",
                "--ro-bind-fd",
                str(pi_models_descriptor),
                "/config/models.json",
                "--ro-bind-fd",
                str(runtime_descriptor),
                "/runtime",
                "--bind-fd",
                str(pi_sessions_descriptor),
                "/sessions",
                "--ro-bind-fd",
                str(project_descriptor),
                "/project",
                "--bind-fd",
                str(report_descriptor),
                f"/project/reports/{run.session_id}",
                "--bind-fd",
                str(memory_descriptor),
                f"/project/memory/{run.session_id}",
                "--dir",
                "/run",
                "--dir",
                "/run/model-session",
                "--ro-bind-fd",
                str(socket_descriptor),
                INFERENCE_SOCKET_DESTINATION,
                "--size",
                str(PRIVATE_SHM_BYTES),
                "--tmpfs",
                "/dev/shm",
                "--chmod",
                "1777",
                "/dev/shm",
                "--remount-ro",
                "/dev",
                "--remount-ro",
                "/",
            )
        )
        for name, value in _FIXED_ENVIRONMENT:
            argv.extend(("--setenv", name, value))
        argv.extend(("--chdir", "/workspace", "--", *arguments))
        return SandboxPlan(
            argv=tuple(argv),
            pass_fds=tuple(descriptors),
            _owned_descriptors=descriptors,
        )
    except BaseException:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
