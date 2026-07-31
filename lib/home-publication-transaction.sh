# shellcheck shell=bash
# Durable primitives and lock orchestration for managed HOME publication.
# Sourced by bin/dotfiles after its diagnostics are defined.

# Backup directory for existing paths (XDG-compliant location).
BACKUP_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/dotfiles/backups"
# shellcheck disable=SC2034  # Consumed by the managed link/copy modules.
BACKUP_DIR="$BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)"

# Rename without mv's cross-filesystem copy fallback. A failed EXDEV leaves the
# source path untouched, which is required for transaction rollback.
_atomic_rename_path() {
    python3 -c \
        'import os, sys; os.rename(sys.argv[1], sys.argv[2])' \
        "$1" "$2"
}

# Attempt a non-blocking advisory lock on file descriptor 9. The descriptor is
# opened by the calling subshell and retains the lock after this helper exits.
_try_home_transaction_lock() {
    python3 -c '
import errno
import fcntl
import sys

try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError as error:
    if error.errno in (errno.EACCES, errno.EAGAIN):
        sys.exit(75)
    raise
'
}

_acquire_home_transaction_lock() {
    local lock_status=0

    while :; do
        if _try_home_transaction_lock; then
            return 0
        else
            lock_status=$?
        fi
        if [ "$lock_status" -ne 75 ]; then
            error "Could not acquire dotfiles HOME transaction lock"
            return 1
        fi
        if ! sleep 0.05; then
            error "Interrupted while waiting for dotfiles HOME transaction lock"
            return 1
        fi
    done
}

# Flush regular payloads and their directory entries before a journal permits
# live-path mutation.
_fsync_transaction_paths() {
    python3 -c '
import os
import stat
import sys

for path in sys.argv[1:]:
    path_stat = os.stat(path, follow_symlinks=False)
    if stat.S_ISLNK(path_stat.st_mode):
        continue
    descriptor = os.open(
        path,
        os.O_RDONLY | (getattr(os, "O_DIRECTORY", 0)
                       if stat.S_ISDIR(path_stat.st_mode) else 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
' "$@"
}

# Describe either the exact identity of one managed file path or the semantic
# payload that a transaction owns. Exact generations detect an editor before
# publication; payload generations let crash recovery distinguish the
# transaction's before/after states without depending on inode allocation.
_managed_path_generation() {
    python3 - "$1" "$2" << 'PY'
import hashlib
import os
import stat
import sys

generation_kind, path = sys.argv[1:]
if generation_kind not in ("exact", "payload"):
    raise SystemExit(f"invalid managed generation kind: {generation_kind}")

try:
    path_stat = os.lstat(path)
except FileNotFoundError:
    print("absent")
    raise SystemExit(0)

if stat.S_ISLNK(path_stat.st_mode):
    target = os.fsencode(os.readlink(path)).hex()
    final_stat = os.lstat(path)
    before = (
        path_stat.st_dev,
        path_stat.st_ino,
        path_stat.st_mtime_ns,
        path_stat.st_ctime_ns,
        stat.S_IMODE(path_stat.st_mode),
    )
    after = (
        final_stat.st_dev,
        final_stat.st_ino,
        final_stat.st_mtime_ns,
        final_stat.st_ctime_ns,
        stat.S_IMODE(final_stat.st_mode),
    )
    if before != after:
        raise SystemExit(f"managed symlink changed while reading: {path}")
    if generation_kind == "exact":
        print("symlink:" + ":".join(str(value) for value in after) + ":" + target)
    else:
        print("symlink:" + target)
    raise SystemExit(0)

if not stat.S_ISREG(path_stat.st_mode):
    print(
        "other:"
        + str(stat.S_IFMT(path_stat.st_mode))
        + ":"
        + str(path_stat.st_dev)
        + ":"
        + str(path_stat.st_ino)
    )
    raise SystemExit(0)

flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
flags |= getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(path, flags)
try:
    opened_before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened_before.st_mode)
        or (opened_before.st_dev, opened_before.st_ino)
        != (path_stat.st_dev, path_stat.st_ino)
    ):
        raise SystemExit(f"managed file changed before reading: {path}")
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    opened_after = os.fstat(descriptor)
finally:
    os.close(descriptor)

identity_before = (
    opened_before.st_dev,
    opened_before.st_ino,
    opened_before.st_size,
    opened_before.st_mtime_ns,
    opened_before.st_ctime_ns,
    stat.S_IMODE(opened_before.st_mode),
)
identity_after = (
    opened_after.st_dev,
    opened_after.st_ino,
    opened_after.st_size,
    opened_after.st_mtime_ns,
    opened_after.st_ctime_ns,
    stat.S_IMODE(opened_after.st_mode),
)
if identity_before != identity_after:
    raise SystemExit(f"managed file changed while reading: {path}")

if generation_kind == "exact":
    print(
        "file:"
        + ":".join(str(value) for value in identity_after)
        + ":"
        + digest.hexdigest()
    )
else:
    print(
        "file:"
        + str(stat.S_IMODE(opened_after.st_mode))
        + ":"
        + digest.hexdigest()
    )
PY
}

_require_managed_path_generation() {
    local path="$1"
    local expected_generation="$2"
    local actual_generation=""

    if ! actual_generation=$(_managed_path_generation exact "$path"); then
        error "Could not inspect managed path generation: $path"
        return 1
    fi
    if [ "$actual_generation" != "$expected_generation" ]; then
        error "Managed path changed during publication: $path"
        return 1
    fi
}

# Create an absolute directory tree and make each newly created name durable in
# its parent before returning. Transaction roots and backup roots must survive
# the same power loss as the live-path mutation they authorize.
_mkdir_p_durable() {
    python3 -c '
import os
import sys

path = os.path.abspath(sys.argv[1])
missing = []
cursor = path
while not os.path.lexists(cursor):
    missing.append(cursor)
    parent = os.path.dirname(cursor)
    if parent == cursor:
        break
    cursor = parent

if not os.path.isdir(cursor):
    raise SystemExit(f"directory ancestor is not a directory: {cursor}")

def fsync_directory(directory):
    descriptor = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

for directory in reversed(missing):
    parent = os.path.dirname(directory)
    try:
        os.mkdir(directory)
    except FileExistsError:
        if not os.path.isdir(directory):
            raise
    fsync_directory(directory)
    fsync_directory(parent)

if not os.path.isdir(path):
    raise SystemExit(f"path is not a directory: {path}")
fsync_directory(path)
fsync_directory(os.path.dirname(path))
' "$1"
}

# A recovered link journal may only name a destination below the HOME whose
# lock protects it.
_validate_home_destination() {
    python3 -c '
import os
import sys

home_path = os.path.abspath(sys.argv[1])
home = os.path.realpath(home_path)
destination = os.path.abspath(sys.argv[2])
destination_parent = os.path.realpath(os.path.dirname(destination))
if (destination == home_path or
        os.path.commonpath((home, destination_parent)) != home):
    sys.exit(1)
' "$HOME" "$1"
}

_validate_link_staging_path() {
    python3 -c '
import os
import re
import sys

destination_parent = os.path.dirname(os.path.abspath(sys.argv[1]))
staging_path = os.path.abspath(sys.argv[2])
if (os.path.dirname(staging_path) != destination_parent or
        re.fullmatch(
            r"\.dotfiles-link\.[0-9a-f]{32}",
            os.path.basename(staging_path),
        ) is None):
    sys.exit(1)
' "$1" "$2"
}

_validate_link_backup_path() {
    python3 -c '
import os
import pathlib
import re
import sys

destination_name = os.path.basename(sys.argv[1])
backup_path = pathlib.Path(os.path.abspath(sys.argv[2]))
backup_root = pathlib.Path(os.path.realpath(sys.argv[3]))
backup_parent = pathlib.Path(os.path.realpath(backup_path.parent))
try:
    relative_parent = backup_parent.relative_to(backup_root)
except ValueError:
    sys.exit(1)
if os.path.commonpath((backup_root, backup_parent)) != str(backup_root):
    sys.exit(1)
parts = (*relative_parent.parts, backup_path.name)
if (backup_path.name != destination_name or
        re.fullmatch(
            r"\.dotfiles-link-backup\.[0-9a-f]{32}",
            backup_path.parent.name,
        ) is None or
        len(parts) != 3):
    sys.exit(1)
' "$1" "$2" "$BACKUP_ROOT"
}

# Run one local HOME mutation under the same lock used by managed links and
# agent-contract publication. Recovery always precedes new work so a killed
# earlier installer cannot leave later create-once decisions observing an
# unresolved destination.
_with_home_transaction_lock() (
    local state_root="$HOME/.local/state/dotfiles"
    local lock_path="$state_root/home-transaction.lock"
    local lock_open=false

    # Invoked indirectly by the EXIT trap below.
    # shellcheck disable=SC2317
    close_home_transaction_lock() {
        local exit_status=$?
        trap - EXIT
        trap '' HUP INT TERM
        if [ "$lock_open" = true ] && ! exec 9>&-; then
            error "Could not close dotfiles HOME transaction lock"
            exit_status=1
        fi
        exit "$exit_status"
    }
    trap close_home_transaction_lock EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    if [ -L "$state_root" ] ||
            { [ -e "$state_root" ] && [ ! -d "$state_root" ]; }; then
        error "Dotfiles state root is not a real directory: $state_root"
        return 1
    fi
    if ! _mkdir_p_durable "$state_root"; then
        error "Could not create dotfiles state root: $state_root"
        return 1
    fi
    if [ -L "$lock_path" ] ||
            { [ -e "$lock_path" ] && [ ! -f "$lock_path" ]; }; then
        error "Dotfiles HOME lock is not a regular file: $lock_path"
        return 1
    fi
    if ! exec 9>>"$lock_path"; then
        error "Could not open dotfiles HOME transaction lock: $lock_path"
        return 1
    fi
    lock_open=true
    if ! _acquire_home_transaction_lock; then
        return 1
    fi
    if ! _recover_link_transactions "$state_root"; then
        return 1
    fi
    if ! _recover_agent_contract_transactions \
            "$state_root" \
            "$HOME/.claude/CLAUDE.md" \
            "$HOME/.codex/AGENTS.md"; then
        return 1
    fi

    "$@"
)
