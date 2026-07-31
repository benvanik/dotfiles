#!/usr/bin/env python3
"""Crash-recoverable publication of one managed child directory."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import stat
import sys
import uuid
from pathlib import Path
from typing import Any


FORMAT_VERSION = 1


class PublicationError(RuntimeError):
    """A fail-closed publication error."""


class PublicationInterrupted(PublicationError):
    """A signal interrupted publication."""


def path_present(path: Path) -> bool:
    return os.path.lexists(path)


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    except BaseException:
        if path_present(temporary):
            os.unlink(temporary)
        raise


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise PublicationError(f"publication journal is not an ordinary file: {path}")
    try:
        with path.open("r", encoding="utf-8") as source:
            value = json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationError(f"could not read publication journal: {path}") from exc
    if not isinstance(value, dict):
        raise PublicationError(f"publication journal is not an object: {path}")
    return value


def ordinary_directory(path: Path, label: str) -> None:
    if not path.is_dir() or path.is_symlink():
        raise PublicationError(f"{label} is not an ordinary directory: {path}")


def direct_child(parent: Path, path: Path, label: str) -> None:
    if path.parent != parent:
        raise PublicationError(f"{label} is not a direct child of {parent}: {path}")


def physical_child(parent: Path, path: Path, label: str) -> None:
    if path == parent:
        raise PublicationError(f"{label} is the managed root itself: {path}")
    try:
        contained = os.path.commonpath((parent.resolve(), path.resolve())) == str(
            parent.resolve()
        )
    except ValueError:
        contained = False
    if not contained:
        raise PublicationError(f"{label} resolves outside {parent}: {path}")


def validate_same_filesystem(parent: Path, path: Path, label: str) -> None:
    if os.path.ismount(path):
        raise PublicationError(f"{label} is a mounted directory: {path}")
    if parent.stat().st_dev != path.stat().st_dev:
        raise PublicationError(f"{label} is on another filesystem: {path}")


def preflight_tree_removal(parent: Path, path: Path) -> None:
    direct_child(parent, path, "transaction cleanup root")
    if path.is_symlink():
        return
    ordinary_directory(path, "transaction cleanup root")
    validate_same_filesystem(parent, path, "transaction cleanup root")
    parent_device = parent.stat().st_dev
    for directory, child_directories, _files in os.walk(
        path, topdown=True, followlinks=False
    ):
        retained_directories: list[str] = []
        for child_name in child_directories:
            child = Path(directory) / child_name
            metadata = os.lstat(child)
            if stat.S_ISLNK(metadata.st_mode):
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                raise PublicationError(
                    f"unexpected transaction object during cleanup: {child}"
                )
            if os.path.ismount(child) or metadata.st_dev != parent_device:
                raise PublicationError(
                    f"transaction cleanup would cross a filesystem: {child}"
                )
            retained_directories.append(child_name)
        child_directories[:] = retained_directories


def remove_transaction_tree(parent: Path, path: Path) -> None:
    if not path_present(path):
        return
    if path.is_symlink():
        os.unlink(path)
        fsync_directory(parent)
        return
    preflight_tree_removal(parent, path)
    for directory, child_directories, files in os.walk(
        path, topdown=False, followlinks=False
    ):
        for name in files:
            os.unlink(Path(directory) / name)
        for name in child_directories:
            child = Path(directory) / name
            if child.is_symlink():
                os.unlink(child)
        Path(directory).rmdir()
    fsync_directory(parent)


def inject_test_fault(name: str) -> None:
    fault = os.environ.get("DOTFILES_PUBLISHER_TEST_FAULT", "")
    if fault == name:
        raise PublicationError(f"injected publication fault: {name}")
    if fault == f"hard-crash-{name}":
        os._exit(96)
    if fault == f"process-crash-{name}":
        os.kill(os.getppid(), signal.SIGKILL)
        os._exit(95)


class Publisher:
    def __init__(self, parent: Path, child: str) -> None:
        self.parent = parent
        self.child = child
        self.install = parent / child
        self.lock_root = parent / f".publish-{child}.lock"
        self.journal_path = self.lock_root
        self.guard_path = parent / f".publish-{child}.guard"

    def validate_parent(self) -> None:
        ordinary_directory(self.parent, "managed publication root")

    def validate_install_if_present(self) -> None:
        if not path_present(self.install):
            return
        ordinary_directory(self.install, "existing installation")
        validate_same_filesystem(self.parent, self.install, "existing installation")

    def validate_payload(self, payload: Path) -> None:
        ordinary_directory(payload, "staged payload")
        physical_child(self.parent, payload, "staged payload")
        validate_same_filesystem(self.parent, payload, "staged payload")

    def validate_staging_root(self, staging_root: Path, payload: Path) -> None:
        direct_child(self.parent, staging_root, "staging root")
        expected_prefix = f".dotfiles-stage-{self.child}."
        suffix = staging_root.name.removeprefix(expected_prefix)
        if not (
            staging_root != self.install
            and staging_root.name.startswith(expected_prefix)
            and len(suffix) == 32
            and all(character in "0123456789abcdef" for character in suffix)
        ):
            raise PublicationError(
                f"staged payload is outside its child-bound transaction root: {payload}"
            )
        try:
            payload.relative_to(staging_root)
        except ValueError as exc:
            raise PublicationError(
                f"publication payload is outside its staging root: {payload}"
            ) from exc
        if ".." in payload.parts:
            raise PublicationError(
                f"publication payload contains parent traversal: {payload}"
            )
        resolved_staging_root = staging_root.resolve()
        resolved_payload = payload.resolve()
        try:
            physically_contained = os.path.commonpath(
                (resolved_staging_root, resolved_payload)
            ) == str(resolved_staging_root)
        except ValueError:
            physically_contained = False
        if not physically_contained:
            raise PublicationError(
                f"publication payload resolves outside its staging root: {payload}"
            )
        if path_present(staging_root):
            ordinary_directory(staging_root, "staging root")
            validate_same_filesystem(self.parent, staging_root, "staging root")

    def validate_installer_guard(self, descriptor: int, guard_path: Path) -> None:
        guard_path = guard_path.absolute()
        if guard_path != self.guard_path:
            raise PublicationError(
                f"installer guard belongs to another managed child: {guard_path}"
            )
        try:
            descriptor_metadata = os.fstat(descriptor)
            guard_metadata = os.lstat(guard_path)
        except OSError as exc:
            raise PublicationError(
                f"could not inspect installer guard: {guard_path}"
            ) from exc
        if (
            guard_path.is_symlink()
            or not stat.S_ISREG(guard_metadata.st_mode)
            or (guard_metadata.st_dev, guard_metadata.st_ino)
            != (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
        ):
            raise PublicationError(f"installer guard identity changed: {guard_path}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PublicationError(
                f"caller does not own installer guard: {guard_path}"
            ) from exc
        descriptor_metadata = os.fstat(descriptor)
        guard_metadata = os.lstat(guard_path)
        if (
            guard_path.is_symlink()
            or not stat.S_ISREG(guard_metadata.st_mode)
            or (guard_metadata.st_dev, guard_metadata.st_ino)
            != (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
        ):
            raise PublicationError(f"installer guard identity changed: {guard_path}")

    def validate_replacement(self, replacement: Path) -> None:
        direct_child(self.parent, replacement, "replacement root")
        expected_prefix = f".replace-{self.child}."
        suffix = replacement.name.removeprefix(expected_prefix)
        if not (
            replacement.name.startswith(expected_prefix)
            and len(suffix) == 32
            and all(character in "0123456789abcdef" for character in suffix)
        ):
            raise PublicationError(
                f"publication replacement has an unexpected name: {replacement}"
            )

    def cleanup_orphan_staging(self) -> None:
        expected_prefix = f".dotfiles-stage-{self.child}."
        for staging_root in sorted(self.parent.glob(f"{expected_prefix}*")):
            suffix = staging_root.name.removeprefix(expected_prefix)
            if not (
                len(suffix) == 32
                and all(character in "0123456789abcdef" for character in suffix)
            ):
                raise PublicationError(
                    f"unrecognized child staging requires inspection: {staging_root}"
                )
            self.validate_staging_root(staging_root, staging_root)
            remove_transaction_tree(self.parent, staging_root)

    def journal(self) -> dict[str, Any]:
        journal = load_json(self.journal_path)
        expected_keys = {
            "child",
            "format",
            "had_previous",
            "install",
            "parent",
            "payload",
            "replacement",
            "staging_root",
        }
        if set(journal) != expected_keys or journal.get("format") != FORMAT_VERSION:
            raise PublicationError(
                f"publication journal has an unsupported schema: {self.journal_path}"
            )
        if journal.get("child") != self.child:
            raise PublicationError("publication journal child identity changed")
        if journal.get("parent") != str(self.parent):
            raise PublicationError("publication journal root identity changed")
        if journal.get("install") != str(self.install):
            raise PublicationError("publication journal destination identity changed")
        if not isinstance(journal.get("had_previous"), bool):
            raise PublicationError("publication journal has invalid prior state")

        payload_value = journal.get("payload")
        replacement_value = journal.get("replacement")
        staging_root_value = journal.get("staging_root")
        if (
            not isinstance(payload_value, str)
            or not isinstance(replacement_value, str)
            or not isinstance(staging_root_value, str)
        ):
            raise PublicationError("publication journal paths are incomplete")
        payload = Path(payload_value)
        replacement = Path(replacement_value)
        staging_root = Path(staging_root_value)
        if (
            not payload.is_absolute()
            or not replacement.is_absolute()
            or not staging_root.is_absolute()
        ):
            raise PublicationError("publication journal paths are not absolute")
        physical_child(self.parent, payload, "staged payload")
        self.validate_replacement(replacement)
        self.validate_staging_root(staging_root, payload)
        if path_present(payload):
            self.validate_payload(payload)
        if path_present(replacement):
            ordinary_directory(replacement, "replacement root")
            validate_same_filesystem(self.parent, replacement, "replacement root")
        return journal

    def remove_transaction_state(
        self, replacement: Path, *, inject_cleanup_fault: bool = False
    ) -> None:
        if path_present(replacement):
            remove_transaction_tree(self.parent, replacement)
        if inject_cleanup_fault:
            inject_test_fault("after-replacement-cleanup")
        if path_present(self.lock_root):
            if not self.lock_root.is_file() or self.lock_root.is_symlink():
                raise PublicationError(
                    f"publication journal changed during cleanup: {self.lock_root}"
                )
            os.unlink(self.lock_root)
            fsync_directory(self.parent)

    def recover(self, *, cleanup_abandoned_staging: bool = False) -> None:
        if not path_present(self.lock_root):
            return
        journal = self.journal()
        payload = Path(journal["payload"])
        replacement = Path(journal["replacement"])
        staging_root = Path(journal["staging_root"])
        previous = replacement / "previous"
        had_previous = journal["had_previous"]

        install_present = path_present(self.install)
        payload_present = path_present(payload)
        previous_present = path_present(previous)
        if install_present:
            self.validate_install_if_present()
        if previous_present:
            ordinary_directory(previous, "previous installation")
            physical_child(self.parent, previous, "previous installation")
            if previous.stat().st_dev != self.parent.stat().st_dev:
                raise PublicationError(
                    f"previous installation is on another filesystem: {previous}"
                )

        if had_previous:
            if install_present and payload_present and not previous_present:
                # The process stopped before the old-generation rename.
                pass
            elif not install_present and payload_present and previous_present:
                # The old generation moved, but the payload did not. Restore.
                os.rename(previous, self.install)
                fsync_directory(self.parent)
                fsync_directory(replacement)
            elif install_present and not payload_present and previous_present:
                # Payload rename is the commit point. Retain it and discard old.
                pass
            elif install_present and not payload_present and not previous_present:
                # The payload committed and old-generation cleanup completed,
                # but the durable journal itself was not yet unlinked.
                pass
            else:
                raise PublicationError(
                    "ambiguous interrupted publication; preserve and inspect "
                    f"{self.lock_root} and {replacement}"
                )
        else:
            if not install_present and payload_present and not previous_present:
                # The process stopped before publication.
                pass
            elif install_present and not payload_present and not previous_present:
                # The payload rename committed.
                pass
            else:
                raise PublicationError(
                    "ambiguous interrupted first publication; preserve and inspect "
                    f"{self.lock_root} and {replacement}"
                )
        self.remove_transaction_state(replacement)
        if cleanup_abandoned_staging and path_present(staging_root):
            inject_test_fault("after-recovery-transaction-cleanup")
            remove_transaction_tree(self.parent, staging_root)

    def reject_orphan_replacements(self) -> None:
        matches = sorted(self.parent.glob(f".replace-{self.child}.*"))
        if matches:
            joined = ", ".join(str(path) for path in matches)
            raise PublicationError(
                f"orphaned replacement state requires inspection: {joined}"
            )

    def publish(self, payload: Path) -> None:
        self.validate_parent()
        self.recover(cleanup_abandoned_staging=True)
        self.reject_orphan_replacements()
        self.validate_payload(payload)
        self.validate_install_if_present()
        had_previous = path_present(self.install)
        relative_payload = payload.relative_to(self.parent)
        staging_root = self.parent / relative_payload.parts[0]
        self.validate_staging_root(staging_root, payload)

        replacement = self.parent / (f".replace-{self.child}.{uuid.uuid4().hex}")
        if path_present(self.lock_root):
            raise PublicationError(
                f"publication transaction unexpectedly exists: {self.lock_root}"
            )
        journal = {
            "child": self.child,
            "format": FORMAT_VERSION,
            "had_previous": had_previous,
            "install": str(self.install),
            "parent": str(self.parent),
            "payload": str(payload),
            "replacement": str(replacement),
            "staging_root": str(staging_root),
        }

        try:
            atomic_json(self.journal_path, journal)
            inject_test_fault("after-journal")
            replacement.mkdir()
            fsync_directory(self.parent)
            inject_test_fault("after-replacement-create")
            if had_previous:
                os.rename(self.install, replacement / "previous")
                fsync_directory(self.parent)
                fsync_directory(replacement)
                inject_test_fault("after-previous-rename")
            os.rename(payload, self.install)
            fsync_directory(self.parent)
            inject_test_fault("after-payload-rename")
            self.remove_transaction_state(replacement, inject_cleanup_fault=True)
        except BaseException:
            if path_present(self.lock_root):
                self.recover(cleanup_abandoned_staging=False)
            raise


def install_signal_handlers() -> None:
    def interrupted(signum: int, _frame: object) -> None:
        raise PublicationInterrupted(f"interrupted by signal {signum}")

    for signal_number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(signal_number, interrupted)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent", required=True, type=Path)
    parser.add_argument("--child", required=True)
    parser.add_argument("--payload", type=Path)
    parser.add_argument("--recover-only", action="store_true")
    parser.add_argument("--cleanup-orphan-staging", action="store_true")
    parser.add_argument("--installer-guard-fd", type=int)
    parser.add_argument("--installer-guard-path", type=Path)
    arguments = parser.parse_args()
    if arguments.recover_only == (arguments.payload is not None):
        parser.error("select exactly one of --payload or --recover-only")
    guard_arguments_present = (
        arguments.installer_guard_fd is not None
        and arguments.installer_guard_path is not None
    )
    if arguments.cleanup_orphan_staging and not guard_arguments_present:
        parser.error("--cleanup-orphan-staging requires an installer guard FD and path")
    if (arguments.installer_guard_fd is None) != (
        arguments.installer_guard_path is None
    ):
        parser.error("installer guard FD and path must be provided together")
    if arguments.cleanup_orphan_staging and not arguments.recover_only:
        parser.error("orphan staging cleanup is valid only with --recover-only")
    if (
        arguments.installer_guard_fd is not None
        and arguments.installer_guard_fd not in (8, 9)
    ):
        parser.error("installer guard FD must be 8 or 9")
    return arguments


def main() -> int:
    arguments = parse_arguments()
    if (
        len(arguments.child) > 128
        or re.fullmatch(r"\.?[0-9A-Za-z][0-9A-Za-z._+-]*", arguments.child) is None
    ):
        raise PublicationError(f"unsafe managed publication child: {arguments.child!r}")
    parent = arguments.parent.absolute()
    install_signal_handlers()
    Publisher(parent, arguments.child).validate_parent()

    publisher = Publisher(parent, arguments.child)
    close_guard_descriptor = False
    if arguments.installer_guard_fd is not None:
        assert arguments.installer_guard_path is not None
        descriptor = arguments.installer_guard_fd
        publisher.validate_installer_guard(
            descriptor,
            arguments.installer_guard_path,
        )
    else:
        guard_path = publisher.guard_path
        if guard_path.is_symlink() or (
            path_present(guard_path) and not guard_path.is_file()
        ):
            raise PublicationError(
                f"publication guard is not an ordinary file: {guard_path}"
            )
        descriptor = os.open(
            guard_path,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        close_guard_descriptor = True
        publisher.validate_installer_guard(descriptor, guard_path)
    try:
        if arguments.recover_only:
            publisher.validate_install_if_present()
            publisher.recover(cleanup_abandoned_staging=True)
            publisher.reject_orphan_replacements()
            if arguments.cleanup_orphan_staging:
                assert arguments.installer_guard_fd is not None
                assert arguments.installer_guard_path is not None
                publisher.validate_installer_guard(
                    arguments.installer_guard_fd,
                    arguments.installer_guard_path,
                )
                publisher.cleanup_orphan_staging()
        else:
            assert arguments.payload is not None
            publisher.publish(arguments.payload.absolute())
    finally:
        if close_guard_descriptor:
            os.close(descriptor)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PublicationError as exc:
        print(f"managed directory publication: {exc}", file=sys.stderr)
        raise SystemExit(1)
