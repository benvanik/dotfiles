"""Delegated cgroup-v2 ownership for one bounded model-session launch."""

from __future__ import annotations

import contextlib
import os
import pathlib
import select
import stat
from dataclasses import dataclass
from types import TracebackType
from typing import Self

from .errors import ModelSessionError


CGROUP_ROOT = pathlib.Path("/sys/fs/cgroup")
SYSTEMD_RUN = pathlib.Path("/usr/bin/systemd-run")
CONTROL_CGROUP_NAME = "control"
WORKLOAD_CGROUP_NAME = "workload"
MAX_CGROUP_TEXT_BYTES = 64 * 1024


def _fail(message: str, *, code: str = "cgroup_unavailable") -> None:
    raise ModelSessionError(message, code=code)


def delegated_scope_command(command: tuple[str, ...]) -> tuple[str, ...]:
    """Wrap a launcher command in a fresh user-owned delegated scope."""

    if not command or any(
        not isinstance(argument, str) or not argument or "\x00" in argument
        for argument in command
    ):
        _fail("delegated scope command contains an invalid argument")
    try:
        metadata = SYSTEMD_RUN.stat()
    except OSError as error:
        raise ModelSessionError(
            f"cannot inspect {SYSTEMD_RUN}: {error}",
            code="cgroup_unavailable",
        ) from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not metadata.st_mode & stat.S_IXUSR
    ):
        _fail(f"{SYSTEMD_RUN} is not a trusted root-owned executable")
    return (
        os.fspath(SYSTEMD_RUN),
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        "--same-dir",
        "--property=Delegate=yes",
        "--property=MemoryAccounting=yes",
        "--property=TasksAccounting=yes",
        "--",
        *command,
    )


def _read_bounded_file(path: pathlib.Path, *, label: str) -> str:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        raise ModelSessionError(
            f"cannot open {label} {path}: {error}",
            code="cgroup_unavailable",
        ) from error
    try:
        chunks: list[bytes] = []
        remaining = MAX_CGROUP_TEXT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 4096))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(content) > MAX_CGROUP_TEXT_BYTES:
        _fail(f"{label} exceeds its protocol bound")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ModelSessionError(
            f"{label} is not valid UTF-8",
            code="cgroup_unavailable",
        ) from error


def _current_cgroup_relative_path(
    proc_cgroup: pathlib.Path = pathlib.Path("/proc/self/cgroup"),
) -> pathlib.PurePosixPath:
    text = _read_bounded_file(proc_cgroup, label="process cgroup membership")
    lines = text.splitlines()
    if len(lines) != 1 or not lines[0].startswith("0::"):
        _fail("process is not in one unambiguous cgroup-v2 hierarchy")
    value = lines[0][3:]
    path = pathlib.PurePosixPath(value)
    if (
        not value
        or not path.is_absolute()
        or path.as_posix() != value
        or any(component in {"", ".", ".."} for component in path.parts[1:])
    ):
        _fail("process cgroup path is not canonical")
    return path


def _directory_flags() -> int:
    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required):
        _fail(
            "delegated cgroups require O_DIRECTORY and O_NOFOLLOW",
            code="cgroup_platform_unsupported",
        )
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_cgroup_directory(
    relative: pathlib.PurePosixPath,
    *,
    root: pathlib.Path = CGROUP_ROOT,
) -> int:
    try:
        descriptor = os.open(root, _directory_flags())
    except OSError as error:
        raise ModelSessionError(
            f"cannot open cgroup-v2 root {root}: {error}",
            code="cgroup_unavailable",
        ) from error
    try:
        for component in relative.parts[1:]:
            child = os.open(component, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_control_file(
    directory_descriptor: int,
    name: str,
    flags: int,
) -> int:
    if "/" in name or name in {"", ".", ".."}:
        _fail("cgroup control-file name is invalid")
    try:
        descriptor = os.open(
            name,
            flags
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_descriptor,
        )
    except OSError as error:
        raise ModelSessionError(
            f"cannot open delegated cgroup control {name}: {error}",
            code="cgroup_unavailable",
        ) from error
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        _fail(f"delegated cgroup control is not a regular kernel file: {name}")
    return descriptor


def _write_all(descriptor: int, content: bytes, *, label: str) -> None:
    view = memoryview(content)
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError:
        pass
    while view:
        try:
            written = os.write(descriptor, view)
        except OSError as error:
            raise ModelSessionError(
                f"cannot write {label}: {error}",
                code="cgroup_configuration_failed",
            ) from error
        if written <= 0:
            _fail(
                f"short write while configuring {label}",
                code="cgroup_configuration_failed",
            )
        view = view[written:]


def _write_control(
    directory_descriptor: int,
    name: str,
    value: str,
) -> None:
    descriptor = _open_control_file(
        directory_descriptor,
        name,
        os.O_WRONLY,
    )
    try:
        _write_all(
            descriptor,
            value.encode("ascii"),
            label=f"cgroup {name}",
        )
    finally:
        os.close(descriptor)


def _read_control(descriptor: int, *, label: str) -> str:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        content = os.read(descriptor, MAX_CGROUP_TEXT_BYTES + 1)
    except OSError as error:
        raise ModelSessionError(
            f"cannot read {label}: {error}",
            code="cgroup_state_failed",
        ) from error
    if len(content) > MAX_CGROUP_TEXT_BYTES:
        _fail(f"{label} exceeds its protocol bound", code="cgroup_state_failed")
    try:
        return content.decode("ascii")
    except UnicodeDecodeError as error:
        raise ModelSessionError(
            f"{label} is not ASCII",
            code="cgroup_state_failed",
        ) from error


def _keyed_integers(text: str, *, label: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 2 or fields[0] in values:
            _fail(f"{label} has an invalid kernel format", code="cgroup_state_failed")
        try:
            number = int(fields[1])
        except ValueError as error:
            raise ModelSessionError(
                f"{label} has a non-integer value",
                code="cgroup_state_failed",
            ) from error
        if number < 0:
            _fail(f"{label} has a negative value", code="cgroup_state_failed")
        values[fields[0]] = number
    return values


def _create_child(
    parent_descriptor: int,
    name: str,
) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
    except OSError as error:
        raise ModelSessionError(
            f"cannot create delegated {name} cgroup: {error}",
            code="cgroup_configuration_failed",
        ) from error
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    except BaseException:
        with contextlib.suppress(OSError):
            os.rmdir(name, dir_fd=parent_descriptor)
        raise
    metadata = os.fstat(descriptor)
    if metadata.st_uid != os.getuid():
        os.close(descriptor)
        _fail(
            f"delegated {name} cgroup is not owned by the current user",
            code="cgroup_configuration_failed",
        )
    return descriptor


@dataclass
class SessionCgroup:
    """Owned aggregate and workload cgroup controls for one launcher process."""

    relative_path: pathlib.PurePosixPath
    _scope_descriptor: int
    _control_descriptor: int
    _workload_descriptor: int
    _workload_procs_descriptor: int
    _workload_kill_descriptor: int
    _workload_events_descriptor: int
    _memory_events_descriptor: int
    _initial_memory_events: dict[str, int]
    _closed: bool = False

    @classmethod
    def create(
        cls,
        *,
        memory_bytes: int,
        max_processes: int,
    ) -> Self:
        if (
            isinstance(memory_bytes, bool)
            or not isinstance(memory_bytes, int)
            or memory_bytes <= 0
            or isinstance(max_processes, bool)
            or not isinstance(max_processes, int)
            or max_processes <= 0
        ):
            _fail(
                "cgroup memory and process limits must be positive integers",
                code="cgroup_configuration_failed",
            )
        relative = _current_cgroup_relative_path()
        scope_descriptor = _open_cgroup_directory(relative)
        opened: list[int] = [scope_descriptor]
        try:
            metadata = os.fstat(scope_descriptor)
            if metadata.st_uid != os.getuid():
                _fail("current cgroup is not delegated to this user")
            controllers_descriptor = _open_control_file(
                scope_descriptor,
                "cgroup.controllers",
                os.O_RDONLY,
            )
            opened.append(controllers_descriptor)
            controllers = set(
                _read_control(
                    controllers_descriptor,
                    label="cgroup.controllers",
                ).split()
            )
            if not {"memory", "pids"}.issubset(controllers):
                _fail(
                    "delegated cgroup lacks memory or pids control",
                    code="cgroup_controller_unavailable",
                )
            os.close(controllers_descriptor)
            opened.remove(controllers_descriptor)

            _write_control(scope_descriptor, "memory.max", f"{memory_bytes}\n")
            _write_control(scope_descriptor, "memory.swap.max", "0\n")
            _write_control(scope_descriptor, "memory.oom.group", "1\n")
            _write_control(scope_descriptor, "pids.max", f"{max_processes}\n")

            memory_events_descriptor = _open_control_file(
                scope_descriptor,
                "memory.events",
                os.O_RDONLY,
            )
            opened.append(memory_events_descriptor)
            initial_memory_events = _keyed_integers(
                _read_control(
                    memory_events_descriptor,
                    label="memory.events",
                ),
                label="memory.events",
            )

            control_descriptor = _create_child(
                scope_descriptor,
                CONTROL_CGROUP_NAME,
            )
            opened.append(control_descriptor)
            workload_descriptor = _create_child(
                scope_descriptor,
                WORKLOAD_CGROUP_NAME,
            )
            opened.append(workload_descriptor)
            _write_control(control_descriptor, "cgroup.procs", "0\n")
            _write_control(
                scope_descriptor,
                "cgroup.subtree_control",
                "+memory +pids\n",
            )

            workload_procs_descriptor = _open_control_file(
                workload_descriptor,
                "cgroup.procs",
                os.O_WRONLY,
            )
            opened.append(workload_procs_descriptor)
            workload_kill_descriptor = _open_control_file(
                workload_descriptor,
                "cgroup.kill",
                os.O_WRONLY,
            )
            opened.append(workload_kill_descriptor)
            workload_events_descriptor = _open_control_file(
                workload_descriptor,
                "cgroup.events",
                os.O_RDONLY | getattr(os, "O_NONBLOCK", 0),
            )
            opened.append(workload_events_descriptor)
            return cls(
                relative_path=relative,
                _scope_descriptor=scope_descriptor,
                _control_descriptor=control_descriptor,
                _workload_descriptor=workload_descriptor,
                _workload_procs_descriptor=workload_procs_descriptor,
                _workload_kill_descriptor=workload_kill_descriptor,
                _workload_events_descriptor=workload_events_descriptor,
                _memory_events_descriptor=memory_events_descriptor,
                _initial_memory_events=initial_memory_events,
            )
        except BaseException:
            for descriptor in reversed(opened):
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            raise

    def duplicate_workload_procs(self) -> int:
        self._require_open()
        duplicate = os.dup(self._workload_procs_descriptor)
        os.set_inheritable(duplicate, False)
        return duplicate

    def workload_populated(self) -> bool:
        self._require_open()
        values = _keyed_integers(
            _read_control(
                self._workload_events_descriptor,
                label="workload cgroup.events",
            ),
            label="workload cgroup.events",
        )
        populated = values.get("populated")
        if populated not in {0, 1}:
            _fail(
                "workload cgroup.events omitted populated state",
                code="cgroup_state_failed",
            )
        return bool(populated)

    def kill_and_wait_empty(self) -> None:
        """Kill the workload leaf and wait on its explicit populated event."""

        self._require_open()
        if not self.workload_populated():
            return
        _write_all(
            self._workload_kill_descriptor,
            b"1\n",
            label="workload cgroup.kill",
        )
        poller = select.poll()
        poller.register(
            self._workload_events_descriptor,
            select.POLLPRI | select.POLLERR,
        )
        while self.workload_populated():
            try:
                poller.poll()
            except InterruptedError:
                continue

    def memory_event_delta(self) -> dict[str, int]:
        self._require_open()
        current = _keyed_integers(
            _read_control(
                self._memory_events_descriptor,
                label="memory.events",
            ),
            label="memory.events",
        )
        keys = set(self._initial_memory_events) | set(current)
        return {
            key: current.get(key, 0) - self._initial_memory_events.get(key, 0)
            for key in sorted(keys)
        }

    def _require_open(self) -> None:
        if self._closed:
            _fail("cannot use a closed session cgroup", code="cgroup_state_failed")

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        if self.workload_populated():
            _fail(
                "cannot release cgroup authority while workload is populated",
                code="cgroup_workload_live",
            )
        self._closed = True
        for descriptor in (
            self._memory_events_descriptor,
            self._workload_events_descriptor,
            self._workload_kill_descriptor,
            self._workload_procs_descriptor,
            self._workload_descriptor,
            self._control_descriptor,
            self._scope_descriptor,
        ):
            with contextlib.suppress(OSError):
                os.close(descriptor)

    def __enter__(self) -> Self:
        self._require_open()
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if self.workload_populated():
            self.kill_and_wait_empty()
        self.close()

    def __del__(self) -> None:
        if not getattr(self, "_closed", True):
            # Destructors cannot safely publish state; retain no live workload.
            with contextlib.suppress(BaseException):
                if self.workload_populated():
                    self.kill_and_wait_empty()
                if not self.workload_populated():
                    self.close()
