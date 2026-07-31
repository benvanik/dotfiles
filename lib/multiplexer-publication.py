#!/usr/bin/env python3
"""Atomically activate one managed tmux/Byobu generation.

The installed commands and Byobu resource directories are stable projections
through one `current` selector. Upgrades therefore change the entire stack at
one rename commit point. The first migration of legacy in-place files is
explicit, journaled, and recoverable after signals or process death.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import sys
import uuid
from pathlib import Path
from typing import Any


FORMAT_VERSION = 1
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
RESOURCE_PATHS = (
    Path("etc/byobu"),
    Path("lib/byobu"),
    Path("share/byobu"),
    Path("share/doc/byobu"),
)


class PublicationError(RuntimeError):
    """A fail-closed publication error."""


class PublicationInterrupted(PublicationError):
    """A signal interrupted publication."""


def inject_test_fault(name: str) -> None:
    fault = os.environ.get("DOTFILES_MULTIPLEXER_TEST_FAULT", "")
    if fault == name:
        raise PublicationError(f"injected test fault: {name}")
    if fault == f"hard-crash-{name}":
        os._exit(97)


def path_present(path: Path) -> bool:
    return os.path.lexists(path)


def reject_final_symlink(path: Path, label: str) -> None:
    if path.is_symlink() or (path_present(path) and not path.is_dir()):
        raise PublicationError(f"{label} is not an ordinary directory: {path}")


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    if path.is_symlink() or (path_present(path) and not path.is_file()):
        raise PublicationError(f"refusing non-regular state file: {path}")
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


def load_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise PublicationError(f"{label} is not an ordinary file: {path}")
    try:
        with path.open("r", encoding="utf-8") as source:
            value = json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationError(f"could not read {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PublicationError(f"{label} is not a JSON object: {path}")
    return value


def safe_relative_path(value: str, label: str) -> Path:
    path = Path(value)
    if (
        not value
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise PublicationError(f"unsafe {label}: {value!r}")
    return path


def exact_symlink(path: Path, target: str) -> bool:
    return path.is_symlink() and os.readlink(path) == target


def atomic_symlink(target: str, destination: Path) -> None:
    temporary = destination.parent / (f".{destination.name}.{uuid.uuid4().hex}.link")
    os.symlink(target, temporary)
    try:
        os.replace(temporary, destination)
        fsync_directory(destination.parent)
    except BaseException:
        if path_present(temporary):
            os.unlink(temporary)
        raise


def create_symlink(target: str, destination: Path) -> None:
    os.symlink(target, destination)
    fsync_directory(destination.parent)


def ensure_parent_plan(prefix: Path, destinations: list[Path]) -> list[str]:
    missing: set[Path] = set()
    for destination in destinations:
        parent = destination.parent
        while parent != prefix:
            try:
                parent.relative_to(prefix)
            except ValueError as exc:
                raise PublicationError(
                    f"projection escapes installation prefix: {destination}"
                ) from exc
            if path_present(parent):
                if parent.is_symlink() or not parent.is_dir():
                    raise PublicationError(
                        f"projection parent is not an ordinary directory: {parent}"
                    )
            else:
                missing.add(parent)
            parent = parent.parent
    return [
        str(path.relative_to(prefix))
        for path in sorted(missing, key=lambda item: len(item.parts))
    ]


def create_planned_parents(prefix: Path, relative_paths: list[str]) -> None:
    for value in relative_paths:
        relative_path = safe_relative_path(value, "created parent")
        path = prefix / relative_path
        if path_present(path):
            if path.is_symlink() or not path.is_dir():
                raise PublicationError(
                    f"projection parent is not an ordinary directory: {path}"
                )
            continue
        path.mkdir()
        fsync_directory(path.parent)


def remove_empty_planned_parents(prefix: Path, relative_paths: list[str]) -> None:
    for value in reversed(relative_paths):
        path = prefix / safe_relative_path(value, "created parent")
        if path.is_dir() and not path.is_symlink():
            try:
                path.rmdir()
            except OSError:
                continue
            fsync_directory(path.parent)


def validate_generation_member(generation: Path, relative_path: Path) -> None:
    path = generation / relative_path
    if not path_present(path):
        raise PublicationError(f"generation member is missing: {path}")
    resolved_generation = generation.resolve()
    resolved_path = path.resolve()
    try:
        contained = os.path.commonpath((resolved_generation, resolved_path)) == str(
            resolved_generation
        )
    except ValueError:
        contained = False
    if not contained:
        raise PublicationError(f"generation member escapes its root: {path}")


def desired_projection_paths(generation: Path) -> list[Path]:
    bin_directory = generation / "bin"
    if not bin_directory.is_dir() or bin_directory.is_symlink():
        raise PublicationError(
            f"generation bin path is not an ordinary directory: {bin_directory}"
        )
    relative_paths: list[Path] = []
    for entry in sorted(os.scandir(bin_directory), key=lambda item: item.name):
        if not SAFE_COMPONENT.fullmatch(entry.name):
            raise PublicationError(f"unsafe generated command name: {entry.name!r}")
        relative_path = Path("bin") / entry.name
        validate_generation_member(generation, relative_path)
        resolved = (generation / relative_path).resolve()
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise PublicationError(
                f"generated command is not an executable file: {relative_path}"
            )
        relative_paths.append(relative_path)
    if (
        Path("bin/tmux") not in relative_paths
        or Path("bin/byobu") not in relative_paths
    ):
        raise PublicationError("generation does not contain both tmux and byobu")
    for relative_path in RESOURCE_PATHS:
        path = generation / relative_path
        if path_present(path):
            validate_generation_member(generation, relative_path)
            if not path.resolve().is_dir():
                raise PublicationError(
                    f"generated resource is not a directory: {relative_path}"
                )
            relative_paths.append(relative_path)
    return relative_paths


def validate_manifest(
    manifest: dict[str, Any],
    prefix: Path,
    state_root: Path,
) -> dict[str, str]:
    if manifest.get("format") != FORMAT_VERSION:
        raise PublicationError("unsupported multiplexer projection manifest")
    if manifest.get("install_prefix") != str(prefix):
        raise PublicationError("projection manifest belongs to another prefix")
    if manifest.get("state_root") != str(state_root):
        raise PublicationError("projection manifest belongs to another state root")
    generation = manifest.get("generation")
    if not isinstance(generation, str) or not SAFE_COMPONENT.fullmatch(generation):
        raise PublicationError("projection manifest has an unsafe generation")
    projections = manifest.get("projections")
    if not isinstance(projections, list):
        raise PublicationError("projection manifest has no projection list")
    result: dict[str, str] = {}
    for projection in projections:
        if not isinstance(projection, dict):
            raise PublicationError("projection manifest entry is not an object")
        relative_value = projection.get("path")
        target = projection.get("target")
        if not isinstance(relative_value, str) or not isinstance(target, str):
            raise PublicationError("projection manifest entry is incomplete")
        relative_path = safe_relative_path(relative_value, "manifest path")
        if relative_value in result:
            raise PublicationError(
                f"duplicate projection manifest path: {relative_value}"
            )
        expected_target = str(state_root / "current" / relative_path)
        if target != expected_target:
            raise PublicationError(
                f"projection manifest has an unexpected target: {relative_value}"
            )
        result[relative_value] = target
    return result


def remove_backup_shell(backup_root: Path, state_root: Path) -> None:
    if not backup_root.exists() or backup_root.is_symlink():
        return
    try:
        backup_root.relative_to(state_root / "backups")
    except ValueError as exc:
        raise PublicationError(
            f"backup root escapes managed state: {backup_root}"
        ) from exc
    for directory, child_directories, files in os.walk(
        backup_root, topdown=False, followlinks=False
    ):
        if files:
            return
        for child in child_directories:
            child_path = Path(directory) / child
            if child_path.is_symlink():
                return
            try:
                child_path.rmdir()
            except OSError:
                return
        try:
            Path(directory).rmdir()
        except OSError:
            return


class Publisher:
    def __init__(
        self,
        state_root: Path,
        prefix: Path,
        generation_name: str,
        force: bool,
    ) -> None:
        self.state_root = state_root
        self.prefix = prefix
        self.generation_name = generation_name
        self.force = force
        self.generation_root = state_root / "generations"
        self.generation = self.generation_root / generation_name
        self.selector = state_root / "current"
        self.manifest_path = state_root / "projections.json"
        self.journal_path = state_root / "activation-transaction.json"

    def validate_roots(self) -> None:
        reject_final_symlink(self.state_root, "multiplexer state root")
        reject_final_symlink(self.prefix, "multiplexer installation prefix")
        if not self.state_root.is_dir() or not self.prefix.is_dir():
            raise PublicationError("multiplexer state root and prefix must exist")
        if not self.generation.is_dir() or self.generation.is_symlink():
            raise PublicationError(
                f"managed generation is not an ordinary directory: {self.generation}"
            )
        if self.generation.parent != self.generation_root:
            raise PublicationError("managed generation is outside its generation root")
        if path_present(self.selector) and not self.selector.is_symlink():
            raise PublicationError(
                f"multiplexer selector is not a symlink: {self.selector}"
            )
        backups_root = self.state_root / "backups"
        reject_final_symlink(backups_root, "multiplexer backup root")
        backups_root.mkdir(exist_ok=True)
        reject_final_symlink(backups_root, "multiplexer backup root")

    def load_manifest(self) -> tuple[dict[str, Any] | None, dict[str, str]]:
        if not path_present(self.manifest_path):
            return None, {}
        manifest = load_json(self.manifest_path, "projection manifest")
        return manifest, validate_manifest(manifest, self.prefix, self.state_root)

    def validate_recorded_selector(self, manifest: dict[str, Any] | None) -> None:
        if manifest is None:
            if path_present(self.selector):
                raise PublicationError(
                    "multiplexer selector exists without an ownership manifest"
                )
            return
        expected = f"generations/{manifest['generation']}"
        if not exact_symlink(self.selector, expected):
            raise PublicationError(
                "multiplexer selector does not match its ownership manifest"
            )

    def rollback(self, journal: dict[str, Any]) -> None:
        journal_prefix = Path(journal.get("install_prefix", ""))
        journal_state_root = Path(journal.get("state_root", ""))
        if journal_prefix != self.prefix or journal_state_root != self.state_root:
            raise PublicationError("activation journal belongs to another installation")
        entries = journal.get("entries")
        if not isinstance(entries, list):
            raise PublicationError("activation journal has no entry list")
        for entry in reversed(entries):
            if not isinstance(entry, dict):
                raise PublicationError("activation journal entry is not an object")
            relative_value = entry.get("path")
            target = entry.get("target")
            prior = entry.get("prior")
            if not isinstance(relative_value, str) or not isinstance(target, str):
                raise PublicationError("activation journal entry is incomplete")
            destination = self.prefix / safe_relative_path(
                relative_value, "journal path"
            )
            if prior == "absent":
                if exact_symlink(destination, target):
                    os.unlink(destination)
                    fsync_directory(destination.parent)
                elif path_present(destination):
                    raise PublicationError(
                        f"cannot roll back changed projection: {destination}"
                    )
            elif prior == "projection":
                if not exact_symlink(destination, target):
                    raise PublicationError(
                        f"managed projection changed during rollback: {destination}"
                    )
            elif prior == "collision":
                backup_value = entry.get("backup")
                if not isinstance(backup_value, str):
                    raise PublicationError("collision journal entry has no backup")
                backup = self.state_root / safe_relative_path(
                    backup_value, "backup path"
                )
                if exact_symlink(destination, target):
                    os.unlink(destination)
                    fsync_directory(destination.parent)
                if path_present(backup):
                    if path_present(destination):
                        raise PublicationError(
                            f"cannot restore backup over changed path: {destination}"
                        )
                    os.rename(backup, destination)
                    fsync_directory(destination.parent)
                    fsync_directory(backup.parent)
                elif not path_present(destination):
                    raise PublicationError(f"migration backup is missing: {backup}")
            else:
                raise PublicationError(f"unknown activation prior state: {prior!r}")
        remove_empty_planned_parents(self.prefix, journal.get("created_parents", []))
        backup_root_value = journal.get("backup_root")
        if isinstance(backup_root_value, str):
            remove_backup_shell(
                self.state_root / safe_relative_path(backup_root_value, "backup root"),
                self.state_root,
            )
        os.unlink(self.journal_path)
        fsync_directory(self.state_root)

    def finish_committed(self, journal: dict[str, Any]) -> None:
        entries = journal.get("entries")
        obsolete = journal.get("obsolete")
        if not isinstance(entries, list) or not isinstance(obsolete, list):
            raise PublicationError("activation journal is incomplete")
        for entry in entries:
            relative_value = entry.get("path")
            target = entry.get("target")
            if not isinstance(relative_value, str) or not isinstance(target, str):
                raise PublicationError("activation journal entry is incomplete")
            destination = self.prefix / safe_relative_path(
                relative_value, "journal path"
            )
            if not exact_symlink(destination, target):
                raise PublicationError(
                    f"committed projection is missing or changed: {destination}"
                )
        for entry in obsolete:
            relative_value = entry.get("path")
            target = entry.get("target")
            if not isinstance(relative_value, str) or not isinstance(target, str):
                raise PublicationError("obsolete projection entry is incomplete")
            destination = self.prefix / safe_relative_path(
                relative_value, "obsolete path"
            )
            if exact_symlink(destination, target):
                os.unlink(destination)
                fsync_directory(destination.parent)
            elif path_present(destination):
                raise PublicationError(
                    f"obsolete projection changed during activation: {destination}"
                )
        manifest = {
            "format": FORMAT_VERSION,
            "generation": journal["generation"],
            "install_prefix": str(self.prefix),
            "projections": [
                {"path": entry["path"], "target": entry["target"]} for entry in entries
            ],
            "state_root": str(self.state_root),
        }
        atomic_json(self.manifest_path, manifest)
        os.unlink(self.journal_path)
        fsync_directory(self.state_root)

    def recover(self) -> None:
        if not path_present(self.journal_path):
            return
        journal = load_json(self.journal_path, "activation journal")
        if journal.get("format") != FORMAT_VERSION:
            raise PublicationError("unsupported activation journal")
        expected_selector = f"generations/{journal.get('generation', '')}"
        if exact_symlink(self.selector, expected_selector):
            self.finish_committed(journal)
        else:
            self.rollback(journal)

    def activate(self) -> Path | None:
        self.validate_roots()
        self.recover()
        manifest, old_projections = self.load_manifest()
        self.validate_recorded_selector(manifest)

        desired_relative_paths = desired_projection_paths(self.generation)
        desired: dict[str, str] = {
            str(relative_path): str(self.state_root / "current" / relative_path)
            for relative_path in desired_relative_paths
        }
        destinations = [self.prefix / Path(value) for value in desired]
        created_parents = ensure_parent_plan(self.prefix, destinations)

        entries: list[dict[str, str]] = []
        has_collision = False
        transaction_id = uuid.uuid4().hex
        backup_root_relative = Path("backups") / transaction_id
        for relative_value, target in desired.items():
            destination = self.prefix / Path(relative_value)
            if exact_symlink(destination, target):
                prior = "projection"
                entry = {
                    "path": relative_value,
                    "prior": prior,
                    "target": target,
                }
            elif relative_value in old_projections and exact_symlink(
                destination, old_projections[relative_value]
            ):
                # Targets are stable through `current`, but retain the explicit
                # old-manifest branch so a future projection schema can evolve.
                prior = "projection"
                entry = {
                    "path": relative_value,
                    "prior": prior,
                    "target": target,
                }
            elif path_present(destination):
                if not self.force:
                    raise PublicationError(
                        "legacy or foreign multiplexer path requires --force "
                        f"for a preserved migration: {destination}"
                    )
                has_collision = True
                backup_relative = backup_root_relative / relative_value
                entry = {
                    "backup": str(backup_relative),
                    "path": relative_value,
                    "prior": "collision",
                    "target": target,
                }
            else:
                entry = {
                    "path": relative_value,
                    "prior": "absent",
                    "target": target,
                }
            entries.append(entry)

        obsolete = [
            {"path": relative_value, "target": target}
            for relative_value, target in old_projections.items()
            if relative_value not in desired
        ]
        for entry in obsolete:
            destination = self.prefix / Path(entry["path"])
            if not exact_symlink(destination, entry["target"]):
                raise PublicationError(
                    f"recorded projection changed before update: {destination}"
                )

        expected_selector = f"generations/{self.generation_name}"
        if (
            manifest is not None
            and manifest.get("generation") == self.generation_name
            and exact_symlink(self.selector, expected_selector)
            and not obsolete
            and all(
                exact_symlink(self.prefix / Path(path), target)
                for path, target in desired.items()
            )
        ):
            return None

        if has_collision and self.state_root.stat().st_dev != self.prefix.stat().st_dev:
            raise PublicationError(
                "legacy migration requires state root and prefix on one filesystem"
            )

        journal = {
            "backup_root": str(backup_root_relative),
            "created_parents": created_parents,
            "entries": entries,
            "format": FORMAT_VERSION,
            "generation": self.generation_name,
            "install_prefix": str(self.prefix),
            "obsolete": obsolete,
            "state_root": str(self.state_root),
        }
        atomic_json(self.journal_path, journal)
        try:
            create_planned_parents(self.prefix, created_parents)
            backup_root = self.state_root / backup_root_relative
            if has_collision:
                backup_root.mkdir(parents=True)
                fsync_directory(backup_root.parent)
            for entry in entries:
                destination = self.prefix / Path(entry["path"])
                target = entry["target"]
                if entry["prior"] == "projection":
                    if not exact_symlink(destination, target):
                        raise PublicationError(
                            f"managed projection changed during update: {destination}"
                        )
                    continue
                if entry["prior"] == "collision":
                    backup = self.state_root / Path(entry["backup"])
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(destination, backup)
                    fsync_directory(destination.parent)
                    fsync_directory(backup.parent)
                    inject_test_fault("after-collision-move")
                create_symlink(target, destination)

            atomic_symlink(expected_selector, self.selector)
            inject_test_fault("after-selector")
            self.finish_committed(journal)
            return backup_root if has_collision else None
        except BaseException:
            if path_present(self.journal_path):
                self.recover()
            raise


def install_signal_handlers() -> None:
    def interrupted(signum: int, _frame: object) -> None:
        raise PublicationInterrupted(f"interrupted by signal {signum}")

    for signal_number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(signal_number, interrupted)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--install-prefix", required=True, type=Path)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    if not SAFE_COMPONENT.fullmatch(arguments.generation):
        raise PublicationError(
            f"unsafe multiplexer generation: {arguments.generation!r}"
        )
    state_root = arguments.state_root.absolute()
    prefix = arguments.install_prefix.absolute()
    install_signal_handlers()

    lock_path = state_root / ".activation.lock"
    if lock_path.is_symlink() or (path_present(lock_path) and not lock_path.is_file()):
        raise PublicationError(f"activation lock is not an ordinary file: {lock_path}")
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PublicationError(
                f"another multiplexer activation owns: {lock_path}"
            ) from exc
        publisher = Publisher(
            state_root,
            prefix,
            arguments.generation,
            arguments.force,
        )
        backup_root = publisher.activate()
    finally:
        os.close(descriptor)
    if backup_root is not None:
        print(f"preserved legacy multiplexer paths at {backup_root}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PublicationError as exc:
        print(f"multiplexer publication: {exc}", file=sys.stderr)
        raise SystemExit(1)
