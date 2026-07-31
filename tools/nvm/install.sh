#!/bin/bash
# Install a pinned NVM runtime without allowing the upstream installer to edit
# shell profiles or force-checkout an arbitrary NVM_DIR.
# Usage: nvm/install.sh [--force]
set -euo pipefail

TOOL_NAME="nvm"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../install-utils.sh
source "$SCRIPT_DIR/../install-utils.sh"

# Updating NVM is a reviewed source change. The commit and all three runtime
# hashes move together; raw GitHub bytes are never executed before verification.
NVM_VERSION="0.40.6"
NVM_COMMIT="b6cf55f6adf3b953d0e5e00a4049444e300e3af8"
NVM_SH_SHA256="baad94563757afa5147950e3dfa272f06b6307e1a7d92c553f8438959b5165fe"
NVM_EXEC_SHA256="f3b7c71ac96ca4f2f75871af20070c3063d1e3fcdc44019af0635c95112e9e76"
NVM_COMPLETION_SHA256="b7eb3bf03d59b61e451957b020640aa55fe8bf47fb39d85d244e259f445d2fbe"
NVM_SOURCE_ROOT="https://raw.githubusercontent.com/nvm-sh/nvm/$NVM_COMMIT"
INSTALLATION_RECORD=".dotfiles-nvm-installation"
STAGING_RECORD=".dotfiles-nvm-staging"
NVM_RELEASES_NAME=".dotfiles-releases"
NVM_CURRENT_NAME=".dotfiles-current"
NVM_DIR_PATH=""
FORCE=false
NVM_STAGING_DIR=""
NVM_RELEASES_ROOT=""
RUNTIME_RECOVERY_DIR=""
NVM_LEGACY_MIGRATION=false
MIGRATE=false
NVM_INSTALL_LOCK=""
NVM_INSTALL_LOCK_TOKEN=""
NVM_INSTALL_LOCK_OWNED=false

show_help() {
    cat << EOF
Usage: nvm/install.sh [options]

Install the pinned NVM $NVM_VERSION runtime to ~/.nvm without editing shell
profiles. Existing Node versions, aliases, and caches remain in place.

Options:
  -f, --force  Replace the managed runtime release after complete checksum and
               functional validation; legacy runtime files must be unmodified
  -m, --migrate
               Install when absent, migrate an unmodified canonical checkout,
               or upgrade an older managed release; current installs are no-op
  -h, --help   Show this help

The repository shell configuration already loads ~/.nvm/nvm.sh.
EOF
}

parse_arguments() {
    while [ $# -gt 0 ]; do
        case "$1" in
            -h|--help)
                show_help
                exit 0
                ;;
            -f|--force)
                if [ "$MIGRATE" = "true" ]; then
                    error "--force and --migrate are mutually exclusive"
                    exit 1
                fi
                FORCE=true
                shift
                ;;
            -m|--migrate)
                if [ "$FORCE" = "true" ]; then
                    error "--force and --migrate are mutually exclusive"
                    exit 1
                fi
                MIGRATE=true
                shift
                ;;
            --)
                shift
                if [ $# -ne 0 ]; then
                    error "nvm/install.sh accepts no positional arguments"
                    exit 1
                fi
                ;;
            -*)
                error "Unknown option: $1"
                exit 1
                ;;
            *)
                error "Unknown argument: $1"
                exit 1
                ;;
        esac
    done
}

validate_release_identity() {
    validate_version_component "$NVM_VERSION" "NVM version" || return 1
    if [[ ! "$NVM_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        error "Invalid pinned NVM version: $NVM_VERSION"
        return 1
    fi
    if [[ ! "$NVM_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
        error "Invalid pinned NVM commit: $NVM_COMMIT"
        return 1
    fi
    local digest
    for digest in \
            "$NVM_SH_SHA256" \
            "$NVM_EXEC_SHA256" \
            "$NVM_COMPLETION_SHA256"; do
        if [[ ! "$digest" =~ ^[0-9a-f]{64}$ ]]; then
            error "Invalid pinned NVM runtime digest"
            return 1
        fi
    done
}

read_record() {
    local release_root="$1"
    local record="$release_root/$INSTALLATION_RECORD"
    local key
    local value
    local line_count=0

    RECORD_SCHEMA=""
    RECORD_TOOL=""
    RECORD_VERSION=""
    RECORD_COMMIT=""
    RECORD_NVM_SH_SHA256=""
    RECORD_NVM_EXEC_SHA256=""
    RECORD_COMPLETION_SHA256=""

    [ -f "$record" ] && [ ! -L "$record" ] || return 1
    while IFS='=' read -r key value || [ -n "$key" ]; do
        line_count=$((line_count + 1))
        case "$key" in
            schema) [ -z "$RECORD_SCHEMA" ] || return 1; RECORD_SCHEMA="$value" ;;
            tool) [ -z "$RECORD_TOOL" ] || return 1; RECORD_TOOL="$value" ;;
            version) [ -z "$RECORD_VERSION" ] || return 1; RECORD_VERSION="$value" ;;
            commit) [ -z "$RECORD_COMMIT" ] || return 1; RECORD_COMMIT="$value" ;;
            nvm_sh_sha256)
                [ -z "$RECORD_NVM_SH_SHA256" ] || return 1
                RECORD_NVM_SH_SHA256="$value"
                ;;
            nvm_exec_sha256)
                [ -z "$RECORD_NVM_EXEC_SHA256" ] || return 1
                RECORD_NVM_EXEC_SHA256="$value"
                ;;
            completion_sha256)
                [ -z "$RECORD_COMPLETION_SHA256" ] || return 1
                RECORD_COMPLETION_SHA256="$value"
                ;;
            *) return 1 ;;
        esac
    done < "$record"

    [ "$line_count" -eq 7 ] &&
        [ "$RECORD_SCHEMA" = "1" ] &&
        [ "$RECORD_TOOL" = "nvm" ] &&
        [ "$RECORD_VERSION" = "$NVM_VERSION" ] &&
        [ "$RECORD_COMMIT" = "$NVM_COMMIT" ] &&
        [ "$RECORD_NVM_SH_SHA256" = "$NVM_SH_SHA256" ] &&
        [ "$RECORD_NVM_EXEC_SHA256" = "$NVM_EXEC_SHA256" ] &&
        [ "$RECORD_COMPLETION_SHA256" = "$NVM_COMPLETION_SHA256" ]
}

write_record() {
    local release_root="$1"

    {
        printf 'schema=1\n'
        printf 'tool=nvm\n'
        printf 'version=%s\n' "$NVM_VERSION"
        printf 'commit=%s\n' "$NVM_COMMIT"
        printf 'nvm_sh_sha256=%s\n' "$NVM_SH_SHA256"
        printf 'nvm_exec_sha256=%s\n' "$NVM_EXEC_SHA256"
        printf 'completion_sha256=%s\n' "$NVM_COMPLETION_SHA256"
    } > "$release_root/$INSTALLATION_RECORD"
}

nvm_release_root_valid() {
    local release_root="$1"
    local release_parent

    [ -d "$release_root" ] && [ ! -L "$release_root" ] || return 1
    release_parent=$(dirname "$release_root")
    python3 - "$release_parent" "$release_root" << 'PY'
import os
import sys

parent, root = sys.argv[1:]
if os.path.ismount(root):
    raise SystemExit(1)
if os.stat(parent).st_dev != os.stat(root).st_dev:
    raise SystemExit(1)
if os.path.dirname(os.path.realpath(root)) != os.path.realpath(parent):
    raise SystemExit(1)
PY
}

nvm_release_valid() {
    local release_root="$1"

    nvm_release_root_valid "$release_root" || return 1
    read_record "$release_root" || return 1
    [ -f "$release_root/nvm.sh" ] && [ ! -L "$release_root/nvm.sh" ] ||
        return 1
    [ -f "$release_root/nvm-exec" ] &&
        [ ! -L "$release_root/nvm-exec" ] &&
        [ -x "$release_root/nvm-exec" ] || return 1
    [ -f "$release_root/bash_completion" ] &&
        [ ! -L "$release_root/bash_completion" ] || return 1
    verify_sha256 "$release_root/nvm.sh" "$NVM_SH_SHA256" &&
        verify_sha256 "$release_root/nvm-exec" "$NVM_EXEC_SHA256" &&
        verify_sha256 "$release_root/bash_completion" "$NVM_COMPLETION_SHA256"
}

nvm_installation_valid() {
    local releases_root="$NVM_DIR_PATH/$NVM_RELEASES_NAME"
    local release_root="$NVM_DIR_PATH/$NVM_RELEASES_NAME/$NVM_VERSION"
    local name
    local reported_version

    [ -d "$releases_root" ] && [ ! -L "$releases_root" ] || return 1
    [ -L "$NVM_DIR_PATH/$NVM_CURRENT_NAME" ] || return 1
    [[ "$(readlink "$NVM_DIR_PATH/$NVM_CURRENT_NAME")" = "$NVM_RELEASES_NAME/$NVM_VERSION" ]] ||
        return 1
    for name in nvm.sh nvm-exec bash_completion; do
        [ -L "$NVM_DIR_PATH/$name" ] || return 1
        [[ "$(readlink "$NVM_DIR_PATH/$name")" = "$NVM_CURRENT_NAME/$name" ]] ||
            return 1
    done
    nvm_release_valid "$release_root" || return 1
    reported_version=$(
        (
            # shellcheck disable=SC2030,SC2031  # Subshell-local validation.
            export NVM_DIR="$NVM_DIR_PATH"
            # shellcheck source=/dev/null
            . "$NVM_DIR_PATH/nvm.sh"
            nvm --version
        )
    ) || return 1
    [ "$reported_version" = "$NVM_VERSION" ]
}

legacy_nvm_checkout_valid() {
    local origin
    local checkout_status

    [ -d "$NVM_DIR_PATH/.git" ] && [ ! -L "$NVM_DIR_PATH/.git" ] ||
        return 1
    [ -f "$NVM_DIR_PATH/nvm.sh" ] && [ ! -L "$NVM_DIR_PATH/nvm.sh" ] ||
        return 1
    [ -f "$NVM_DIR_PATH/nvm-exec" ] &&
        [ ! -L "$NVM_DIR_PATH/nvm-exec" ] || return 1
    [ -f "$NVM_DIR_PATH/bash_completion" ] &&
        [ ! -L "$NVM_DIR_PATH/bash_completion" ] || return 1
    origin=$(git -C "$NVM_DIR_PATH" remote get-url --all origin 2>/dev/null) ||
        return 1
    case "$origin" in
        https://github.com/nvm-sh/nvm.git|git@github.com:nvm-sh/nvm.git) ;;
        *) return 1 ;;
    esac
    checkout_status=$(
        git -c core.fsmonitor=false -c core.hooksPath=/dev/null \
            -C "$NVM_DIR_PATH" status --porcelain=v1 \
            --untracked-files=no --ignore-submodules=none
    ) || return 1
    [ -z "$checkout_status" ]
}

managed_release_valid() {
    local release_root="$1"
    local expected_version="$2"
    local recorded_version

    nvm_release_root_valid "$release_root" || return 1
    recorded_version=$(
        python3 - "$release_root" "$INSTALLATION_RECORD" << 'PY'
import hashlib
import os
import re
import stat
import sys

root, record_name = sys.argv[1:]
record_path = os.path.join(root, record_name)
if os.path.islink(record_path) or not os.path.isfile(record_path):
    raise SystemExit(1)
with open(record_path, encoding="ascii") as record:
    lines = record.read().splitlines()
if len(lines) != 7:
    raise SystemExit(1)
fields = {}
for line in lines:
    key, separator, value = line.partition("=")
    if not separator or key in fields:
        raise SystemExit(1)
    fields[key] = value
expected_keys = {
    "schema",
    "tool",
    "version",
    "commit",
    "nvm_sh_sha256",
    "nvm_exec_sha256",
    "completion_sha256",
}
if set(fields) != expected_keys or fields["schema"] != "1" or fields["tool"] != "nvm":
    raise SystemExit(1)
if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", fields["version"]):
    raise SystemExit(1)
if not re.fullmatch(r"[0-9a-f]{40}", fields["commit"]):
    raise SystemExit(1)
for name, digest_key, executable in (
    ("nvm.sh", "nvm_sh_sha256", False),
    ("nvm-exec", "nvm_exec_sha256", True),
    ("bash_completion", "completion_sha256", False),
):
    path = os.path.join(root, name)
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        raise SystemExit(1)
    if not stat.S_ISREG(mode) or (executable and not os.access(path, os.X_OK)):
        raise SystemExit(1)
    expected_digest = fields[digest_key]
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise SystemExit(1)
    digest = hashlib.sha256()
    with open(path, "rb") as runtime_file:
        for chunk in iter(lambda: runtime_file.read(1024 * 1024), b""):
            digest.update(chunk)
    actual_digest = digest.hexdigest()
    if actual_digest != expected_digest:
        raise SystemExit(1)
print(fields["version"])
PY
    ) || return 1
    [ "$recorded_version" = "$expected_version" ]
}

managed_nvm_root_recoverable() {
    local releases_root="$NVM_DIR_PATH/$NVM_RELEASES_NAME"
    local current_path="$NVM_DIR_PATH/$NVM_CURRENT_NAME"
    local current_target
    local current_version
    local release_root
    local name

    [ -d "$releases_root" ] && [ ! -L "$releases_root" ] || return 1
    [ -L "$current_path" ] || return 1
    current_target=$(readlink "$current_path")
    case "$current_target" in
        "$NVM_RELEASES_NAME"/*)
            current_version="${current_target#"$NVM_RELEASES_NAME"/}"
            validate_version_component \
                "$current_version" "managed NVM version" || return 1
            ;;
        *) return 1 ;;
    esac
    release_root="$NVM_DIR_PATH/$current_target"
    [ -d "$release_root" ] && [ ! -L "$release_root" ] || return 1
    for name in nvm.sh nvm-exec bash_completion; do
        [ -L "$NVM_DIR_PATH/$name" ] || return 1
        [[ "$(readlink "$NVM_DIR_PATH/$name")" = "$NVM_CURRENT_NAME/$name" ]] ||
            return 1
    done
    managed_release_valid "$release_root" "$current_version"
}

nvm_root_empty() {
    local root_entry
    local release_entry
    local publication_guard=".publish-$NVM_VERSION.guard"

    [ -d "$NVM_DIR_PATH" ] || return 1
    root_entry=$(find "$NVM_DIR_PATH" -mindepth 1 -maxdepth 1 \
        ! -name '.dotfiles-install.lock' \
        ! -name "$NVM_RELEASES_NAME" -print -quit) || return 1
    [ -z "$root_entry" ] || return 1
    if [ -e "$NVM_RELEASES_ROOT" ] || [ -L "$NVM_RELEASES_ROOT" ]; then
        [ -d "$NVM_RELEASES_ROOT" ] && [ ! -L "$NVM_RELEASES_ROOT" ] ||
            return 1
        release_entry=$(find "$NVM_RELEASES_ROOT" -mindepth 1 -maxdepth 1 \
            ! -name "$publication_guard" \
            -print -quit) || return 1
        [ -z "$release_entry" ]
    fi
}

orphaned_current_release_valid() {
    local root_entry
    local release_entry
    local release_root="$NVM_RELEASES_ROOT/$NVM_VERSION"
    local publication_guard=".publish-$NVM_VERSION.guard"

    root_entry=$(find "$NVM_DIR_PATH" -mindepth 1 -maxdepth 1 \
        ! -name '.dotfiles-install.lock' \
        ! -name "$NVM_RELEASES_NAME" -print -quit) || return 1
    [ -z "$root_entry" ] || return 1
    release_entry=$(find "$NVM_RELEASES_ROOT" -mindepth 1 -maxdepth 1 \
        ! -name "$NVM_VERSION" \
        ! -name "$publication_guard" -print -quit) || return 1
    [ -z "$release_entry" ] || return 1
    nvm_release_valid "$release_root"
}

read_install_lock_owner() {
    local lock_root="$1"
    local owner_path="$lock_root/owner"
    local key
    local value
    local line_count=0

    LOCK_OWNER_SCHEMA=""
    LOCK_OWNER_PID=""
    LOCK_OWNER_TOKEN=""
    [ -d "$lock_root" ] && [ ! -L "$lock_root" ] || return 1
    [ -f "$owner_path" ] && [ ! -L "$owner_path" ] || return 1
    while IFS='=' read -r key value || [ -n "$key" ]; do
        line_count=$((line_count + 1))
        case "$key" in
            schema)
                [ -z "$LOCK_OWNER_SCHEMA" ] || return 1
                LOCK_OWNER_SCHEMA="$value"
                ;;
            pid)
                [ -z "$LOCK_OWNER_PID" ] || return 1
                LOCK_OWNER_PID="$value"
                ;;
            token)
                [ -z "$LOCK_OWNER_TOKEN" ] || return 1
                LOCK_OWNER_TOKEN="$value"
                ;;
            *) return 1 ;;
        esac
    done < "$owner_path"
    [ "$line_count" -eq 3 ] &&
        [ "$LOCK_OWNER_SCHEMA" = "1" ] &&
        [[ "$LOCK_OWNER_PID" =~ ^[1-9][0-9]*$ ]] &&
        [[ "$LOCK_OWNER_TOKEN" =~ ^[1-9][0-9]*-[0-9]+-[0-9]+$ ]]
}

install_lock_owner_active() {
    local owner_pid="$1"
    local observed_pid

    if kill -0 "$owner_pid" 2>/dev/null; then
        return 0
    fi
    observed_pid=$(ps -p "$owner_pid" -o pid= 2>/dev/null) || return 1
    [ -n "$observed_pid" ]
}

release_install_lock() {
    local lock_entry
    local recorded_token=""

    [ "$NVM_INSTALL_LOCK_OWNED" = "true" ] || return 0
    if [ "$NVM_INSTALL_LOCK" != "$NVM_DIR_PATH/.dotfiles-install.lock" ] ||
            [ ! -d "$NVM_INSTALL_LOCK" ] ||
            [ -L "$NVM_INSTALL_LOCK" ]; then
        error "Refusing to remove a changed NVM install lock"
        error "Lock requires inspection: $NVM_INSTALL_LOCK"
        return 1
    fi
    if read_install_lock_owner "$NVM_INSTALL_LOCK"; then
        recorded_token="$LOCK_OWNER_TOKEN"
        if [ "$recorded_token" != "$NVM_INSTALL_LOCK_TOKEN" ]; then
            error "Refusing to remove an NVM install lock with different ownership"
            error "Lock requires inspection: $NVM_INSTALL_LOCK"
            return 1
        fi
    else
        lock_entry=$(find "$NVM_INSTALL_LOCK" -mindepth 1 -maxdepth 1 \
            -print -quit) || return 1
        if [ -n "$lock_entry" ]; then
            error "Refusing to remove an incomplete NVM install lock"
            error "Lock requires inspection: $NVM_INSTALL_LOCK"
            return 1
        fi
    fi
    if ! remove_managed_tree "$NVM_DIR_PATH" "$NVM_INSTALL_LOCK"; then
        error "NVM install lock requires inspection: $NVM_INSTALL_LOCK"
        return 1
    fi
    NVM_INSTALL_LOCK_OWNED=false
    NVM_INSTALL_LOCK=""
    NVM_INSTALL_LOCK_TOKEN=""
}

reclaim_stale_install_lock() {
    local captured_pid
    local captured_token
    local reclaim_directory
    local expected_entries

    if ! read_install_lock_owner "$NVM_INSTALL_LOCK"; then
        error "Interrupted NVM install lock has invalid ownership metadata"
        error "Inspect that exact lock before removing it: $NVM_INSTALL_LOCK"
        return 1
    fi
    captured_pid="$LOCK_OWNER_PID"
    captured_token="$LOCK_OWNER_TOKEN"
    if install_lock_owner_active "$captured_pid"; then
        error "Another NVM install owns: $NVM_INSTALL_LOCK"
        return 1
    fi

    reclaim_directory="$NVM_INSTALL_LOCK/.reclaim-$NVM_INSTALL_LOCK_TOKEN"
    if ! mkdir "$reclaim_directory" 2>/dev/null; then
        error "Another process is inspecting the interrupted NVM install lock"
        error "Lock requires inspection: $NVM_INSTALL_LOCK"
        return 1
    fi
    if ! read_install_lock_owner "$NVM_INSTALL_LOCK" ||
            [ "$LOCK_OWNER_PID" != "$captured_pid" ] ||
            [ "$LOCK_OWNER_TOKEN" != "$captured_token" ] ||
            install_lock_owner_active "$captured_pid"; then
        remove_managed_tree "$NVM_INSTALL_LOCK" "$reclaim_directory" || true
        error "NVM install-lock ownership changed during stale recovery"
        error "Lock requires inspection: $NVM_INSTALL_LOCK"
        return 1
    fi
    expected_entries=$(find "$NVM_INSTALL_LOCK" -mindepth 1 -maxdepth 1 \
        ! -name owner ! -name "$(basename "$reclaim_directory")" \
        -print -quit) || return 1
    if [ -n "$expected_entries" ]; then
        remove_managed_tree "$NVM_INSTALL_LOCK" "$reclaim_directory" || true
        error "Interrupted NVM install lock contains unexpected state"
        error "Lock requires inspection: $NVM_INSTALL_LOCK"
        return 1
    fi
    warn "Reclaiming interrupted NVM install lock from PID $captured_pid"
    if ! remove_managed_tree "$NVM_DIR_PATH" "$NVM_INSTALL_LOCK"; then
        error "Interrupted NVM install lock requires inspection"
        error "Lock remains at: $NVM_INSTALL_LOCK"
        return 1
    fi
}

create_install_lock() {
    NVM_INSTALL_LOCK_TOKEN="${BASHPID:-$$}-$RANDOM-$RANDOM"
    NVM_INSTALL_LOCK_OWNED=true
    if ! mkdir "$NVM_INSTALL_LOCK" 2>/dev/null; then
        NVM_INSTALL_LOCK_OWNED=false
        return 1
    fi
    runtime_fault_point after-lock-directory || return 1
    if ! python3 - "$NVM_INSTALL_LOCK/owner" \
            "${BASHPID:-$$}" "$NVM_INSTALL_LOCK_TOKEN" << 'PY'
import os
import sys

owner_path, owner_pid, owner_token = sys.argv[1:]
descriptor = os.open(
    owner_path,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
    0o600,
)
with os.fdopen(descriptor, "w", encoding="ascii") as owner:
    owner.write("schema=1\n")
    owner.write(f"pid={owner_pid}\n")
    owner.write(f"token={owner_token}\n")
PY
    then
        error "Failed to publish NVM install-lock ownership"
        return 1
    fi
    runtime_fault_point after-lock-owner || return 1
    if ! read_install_lock_owner "$NVM_INSTALL_LOCK" ||
            [ "$LOCK_OWNER_PID" != "${BASHPID:-$$}" ] ||
            [ "$LOCK_OWNER_TOKEN" != "$NVM_INSTALL_LOCK_TOKEN" ] ||
            ! python3 - "$NVM_DIR_PATH" "$NVM_INSTALL_LOCK" << 'PY'
import os
import sys

nvm_root, lock_root = sys.argv[1:]
with open(os.path.join(lock_root, "owner"), "rb") as owner:
    os.fsync(owner.fileno())
for directory in (lock_root, nvm_root):
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
PY
    then
        error "Failed to make NVM install-lock ownership durable"
        return 1
    fi
    runtime_fault_point after-lock-durable || return 1
}

acquire_install_lock() {
    local attempt=1

    NVM_INSTALL_LOCK="$NVM_DIR_PATH/.dotfiles-install.lock"
    while [ "$attempt" -le 2 ]; do
        if create_install_lock; then
            return 0
        fi
        if [ "$NVM_INSTALL_LOCK_OWNED" = "true" ]; then
            if ! release_install_lock; then
                error "Owned lock remains at: $NVM_INSTALL_LOCK"
            fi
            return 1
        fi
        if [ "$attempt" -eq 2 ] ||
                ! reclaim_stale_install_lock; then
            error "Another or interrupted NVM install owns: $NVM_INSTALL_LOCK"
            error "Inspect that exact lock before removing it"
            return 1
        fi
        attempt=$((attempt + 1))
    done
}

replace_symlink_atomically() (
    local destination="$1"
    local target="$2"
    local temporary_link=""

    # shellcheck disable=SC2317  # Invoked by the EXIT trap below.
    cleanup_temporary_link() {
        if [ -n "$temporary_link" ] &&
                { [ -e "$temporary_link" ] || [ -L "$temporary_link" ]; }; then
            unlink "$temporary_link"
        fi
    }
    trap cleanup_temporary_link EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    temporary_link=$(mktemp "$NVM_DIR_PATH/.runtime-link.XXXXXX")
    unlink "$temporary_link"
    ln -s "$target" "$temporary_link"
    if ! python3 - "$temporary_link" "$destination" << 'PY'
import os
import sys

os.replace(sys.argv[1], sys.argv[2])
directory_fd = os.open(os.path.dirname(sys.argv[2]), os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
    then
        return 1
    fi
    temporary_link=""
)

replace_current_link() {
    local target="$1"
    local current_path="$NVM_DIR_PATH/$NVM_CURRENT_NAME"

    if { [ -e "$current_path" ] || [ -L "$current_path" ]; } &&
            [ ! -L "$current_path" ]; then
        error "Refusing to replace non-symlink NVM release pointer: $current_path"
        return 1
    fi
    replace_symlink_atomically "$current_path" "$target"
}

unlink_runtime_path() {
    local path="$1"

    case "$path" in
        "$NVM_DIR_PATH/$NVM_CURRENT_NAME"|\
        "$NVM_DIR_PATH/nvm.sh"|\
        "$NVM_DIR_PATH/nvm-exec"|\
        "$NVM_DIR_PATH/bash_completion") ;;
        *)
            error "Refusing unexpected NVM unlink path: $path"
            return 1
            ;;
    esac
    python3 - "$path" "$NVM_DIR_PATH" << 'PY'
import os
import sys

path, parent = sys.argv[1:]
os.unlink(path)
directory_fd = os.open(parent, os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
}

copy_runtime_path() {
    local source_path="$1"
    local destination_path="$2"

    python3 - "$source_path" "$destination_path" << 'PY'
import os
import shutil
import stat
import sys

source, destination = sys.argv[1:]
mode = os.lstat(source).st_mode
if stat.S_ISLNK(mode):
    os.symlink(os.readlink(source), destination)
elif stat.S_ISREG(mode):
    shutil.copy2(source, destination, follow_symlinks=False)
    with open(destination, "rb") as copied_file:
        os.fsync(copied_file.fileno())
else:
    raise SystemExit(f"unsupported NVM runtime path type: {source}")
PY
}

restore_runtime_path() (
    local source_path="$1"
    local destination_path="$2"
    local temporary_path=""

    # shellcheck disable=SC2317  # Invoked by the EXIT trap below.
    cleanup_temporary_path() {
        if [ -n "$temporary_path" ] &&
                { [ -e "$temporary_path" ] || [ -L "$temporary_path" ]; }; then
            unlink "$temporary_path"
        fi
    }
    trap cleanup_temporary_path EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    temporary_path=$(mktemp "$NVM_DIR_PATH/.runtime-restore.XXXXXX")
    unlink "$temporary_path"
    copy_runtime_path "$source_path" "$temporary_path"
    python3 - "$temporary_path" "$destination_path" << 'PY'
import os
import sys

temporary, destination = sys.argv[1:]
os.replace(temporary, destination)
directory_fd = os.open(os.path.dirname(destination), os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
    temporary_path=""
)

sync_recovery_metadata() {
    local recovery_dir="$1"

    python3 - "$NVM_DIR_PATH" "$recovery_dir" << 'PY'
import os
import stat
import sys

parent, root = sys.argv[1:]
for name in ("state", "prior-current", "captured", "absent"):
    path = os.path.join(root, name)
    with open(path, "rb") as file:
        os.fsync(file.fileno())
for name in ("nvm.sh", "nvm-exec", "bash_completion"):
    path = os.path.join(root, name)
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        continue
    if stat.S_ISREG(mode):
        with open(path, "rb") as file:
            os.fsync(file.fileno())
directory_fd = os.open(root, os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
parent_fd = os.open(parent, os.O_RDONLY)
try:
    os.fsync(parent_fd)
finally:
    os.close(parent_fd)
PY
}

recovery_state() {
    local recovery_dir="$1"

    [ -f "$recovery_dir/state" ] && [ ! -L "$recovery_dir/state" ] ||
        return 1
    python3 - "$recovery_dir/state" << 'PY'
import sys

with open(sys.argv[1], encoding="ascii") as state_file:
    lines = state_file.read().splitlines()
if len(lines) != 1 or lines[0] not in {"preparing", "rollback", "committed"}:
    raise SystemExit("invalid NVM recovery state")
print(lines[0])
PY
}

set_recovery_state() (
    local recovery_dir="$1"
    local state="$2"
    local temporary_state=""

    case "$state" in
        preparing|rollback|committed) ;;
        *)
            error "Invalid NVM recovery state transition: $state"
            return 1
            ;;
    esac
    # shellcheck disable=SC2317  # Invoked by the EXIT trap below.
    cleanup_temporary_state() {
        if [ -n "$temporary_state" ] &&
                { [ -e "$temporary_state" ] ||
                    [ -L "$temporary_state" ]; }; then
            unlink "$temporary_state"
        fi
    }
    trap cleanup_temporary_state EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    temporary_state=$(mktemp "$recovery_dir/.state.XXXXXX")
    printf '%s\n' "$state" > "$temporary_state"
    python3 - "$temporary_state" "$recovery_dir/state" \
        "$recovery_dir" "$NVM_DIR_PATH" << 'PY'
import os
import sys

temporary, destination, recovery_root, nvm_root = sys.argv[1:]
with open(temporary, "rb") as state_file:
    os.fsync(state_file.fileno())
os.replace(temporary, destination)
for directory in (recovery_root, nvm_root):
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
PY
    temporary_state=""
)

runtime_fault_point() {
    local point="$1"

    case "${DOTFILES_INSTALLER_TEST_FAULT:-}" in
        "$point")
            error "Injected NVM runtime publication failure at $point"
            return 1
            ;;
        "term-$point")
            kill -TERM "${BASHPID:-$$}"
            return 1
            ;;
        "kill-$point")
            kill -KILL "${BASHPID:-$$}"
            return 1
            ;;
    esac
}

recovery_metadata_valid() {
    local recovery_dir="$1"
    local name
    local captured_count
    local absent_count

    [ "$(recovery_state "$recovery_dir")" = "rollback" ] || return 1
    for name in prior-current captured absent; do
        [ -f "$recovery_dir/$name" ] &&
            [ ! -L "$recovery_dir/$name" ] || return 1
    done
    if grep -Eqv '^(nvm\.sh|nvm-exec|bash_completion)$' \
            "$recovery_dir/captured" ||
            grep -Eqv '^(nvm\.sh|nvm-exec|bash_completion)$' \
                "$recovery_dir/absent"; then
        return 1
    fi
    for name in nvm.sh nvm-exec bash_completion; do
        captured_count=$(grep -Fxc "$name" "$recovery_dir/captured" || true)
        absent_count=$(grep -Fxc "$name" "$recovery_dir/absent" || true)
        [ $((captured_count + absent_count)) -eq 1 ] || return 1
        if [ "$captured_count" -eq 1 ]; then
            [ -L "$recovery_dir/$name" ] ||
                { [ -f "$recovery_dir/$name" ] &&
                    [ ! -L "$recovery_dir/$name" ]; } || return 1
        elif [ -e "$recovery_dir/$name" ] ||
                [ -L "$recovery_dir/$name" ]; then
            return 1
        fi
    done
    python3 - "$recovery_dir/prior-current" << 'PY'
import sys

with open(sys.argv[1], encoding="utf-8") as metadata:
    lines = metadata.read().splitlines()
if len(lines) != 1:
    raise SystemExit("invalid prior-current metadata")
if not (lines[0] == "absent=" or lines[0].startswith("present=")):
    raise SystemExit("invalid prior-current metadata")
PY
}

runtime_path_matches_recovery() {
    local recovery_path="$1"
    local destination="$2"

    python3 - "$recovery_path" "$destination" << 'PY'
import filecmp
import os
import stat
import sys

recovery, destination = sys.argv[1:]
try:
    recovery_mode = os.lstat(recovery).st_mode
    destination_mode = os.lstat(destination).st_mode
except FileNotFoundError:
    raise SystemExit(1)
if stat.S_ISLNK(recovery_mode):
    matches = (
        stat.S_ISLNK(destination_mode)
        and os.readlink(recovery) == os.readlink(destination)
    )
elif stat.S_ISREG(recovery_mode):
    matches = (
        stat.S_ISREG(destination_mode)
        and stat.S_IMODE(recovery_mode) == stat.S_IMODE(destination_mode)
        and filecmp.cmp(recovery, destination, shallow=False)
    )
else:
    matches = False
raise SystemExit(0 if matches else 1)
PY
}

rollback_runtime_transaction() {
    local recovery_dir="$1"
    local name
    local destination
    local prior_state
    local prior_target
    local restore_failed=false

    case "$recovery_dir" in
        "$NVM_DIR_PATH"/.dotfiles-runtime-recovery.*) ;;
        *)
            error "Refusing unexpected NVM recovery path: $recovery_dir"
            return 1
            ;;
    esac
    if [ ! -d "$recovery_dir" ] || [ -L "$recovery_dir" ]; then
        error "NVM runtime recovery is not an ordinary directory: $recovery_dir"
        return 1
    fi
    recovery_metadata_valid "$recovery_dir" || {
        error "NVM recovery metadata is invalid: $recovery_dir"
        return 1
    }

    IFS='=' read -r prior_state prior_target < "$recovery_dir/prior-current" ||
        restore_failed=true
    case "$prior_state" in
        present)
            case "$prior_target" in
                "$NVM_RELEASES_NAME"/*)
                    validate_version_component \
                        "${prior_target#"$NVM_RELEASES_NAME"/}" \
                        "prior NVM release" || restore_failed=true
                    if [ ! -d "$NVM_DIR_PATH/$prior_target" ] ||
                            [ -L "$NVM_DIR_PATH/$prior_target" ]; then
                        error "Prior NVM release is missing: $prior_target"
                        restore_failed=true
                    fi
                    ;;
                *)
                    error "Invalid prior NVM release pointer: $prior_target"
                    restore_failed=true
                    ;;
            esac
            if [ "$restore_failed" != "true" ]; then
                destination="$NVM_DIR_PATH/$NVM_CURRENT_NAME"
                if [ -L "$destination" ] &&
                        [[ "$(readlink "$destination")" = "$prior_target" ]]; then
                    :
                elif [ ! -e "$destination" ] && [ ! -L "$destination" ]; then
                    replace_current_link "$prior_target" ||
                        restore_failed=true
                elif [ -L "$destination" ] &&
                        [[ "$(readlink "$destination")" = "$NVM_RELEASES_NAME/$NVM_VERSION" ]]; then
                    replace_current_link "$prior_target" ||
                        restore_failed=true
                else
                    error "Refusing to overwrite a changed NVM release pointer"
                    restore_failed=true
                fi
            fi
            ;;
        absent)
            destination="$NVM_DIR_PATH/$NVM_CURRENT_NAME"
            if [ -L "$destination" ] &&
                    [[ "$(readlink "$destination")" = "$NVM_RELEASES_NAME/$NVM_VERSION" ]]; then
                unlink_runtime_path "$destination" || restore_failed=true
            elif [ ! -e "$destination" ] && [ ! -L "$destination" ]; then
                :
            elif [ -e "$destination" ] || [ -L "$destination" ]; then
                error "Refusing to remove concurrently changed NVM release pointer"
                restore_failed=true
            fi
            ;;
        *)
            error "Invalid prior NVM recovery state: $prior_state"
            restore_failed=true
            ;;
    esac

    for name in nvm.sh nvm-exec bash_completion; do
        destination="$NVM_DIR_PATH/$name"
        if grep -Fxq "$name" "$recovery_dir/captured"; then
            if runtime_path_matches_recovery \
                    "$recovery_dir/$name" "$destination"; then
                continue
            fi
            if [ -L "$destination" ] &&
                    [[ "$(readlink "$destination")" = "$NVM_CURRENT_NAME/$name" ]]; then
                restore_runtime_path \
                    "$recovery_dir/$name" "$destination" ||
                    restore_failed=true
            else
                error "Refusing to overwrite a changed NVM runtime path: $destination"
                restore_failed=true
            fi
        elif [ -L "$destination" ] &&
                [[ "$(readlink "$destination")" = "$NVM_CURRENT_NAME/$name" ]]; then
            unlink_runtime_path "$destination" || restore_failed=true
        elif [ -e "$destination" ] || [ -L "$destination" ]; then
            error "Refusing to remove concurrently changed NVM path: $destination"
            restore_failed=true
        fi
    done

    if [ "$restore_failed" = "true" ]; then
        error "NVM runtime rollback requires manual recovery"
        error "Recovery payload preserved at: $recovery_dir"
        return 1
    fi
    remove_managed_tree "$NVM_DIR_PATH" "$recovery_dir"
    RUNTIME_RECOVERY_DIR=""
    info "Restored the prior NVM runtime after interrupted publication"
}

recover_abandoned_runtime_transactions() {
    local preparation_dir
    local recovery_dir
    local recovery_count=0
    local state

    for preparation_dir in \
            "$NVM_DIR_PATH"/.dotfiles-runtime-preparation.*; do
        [ -e "$preparation_dir" ] || [ -L "$preparation_dir" ] || continue
        if [ ! -d "$preparation_dir" ] || [ -L "$preparation_dir" ]; then
            error "Invalid NVM runtime preparation path: $preparation_dir"
            return 1
        fi
        state=$(recovery_state "$preparation_dir") || {
            error "Incomplete NVM runtime preparation requires inspection"
            error "Preserved at: $preparation_dir"
            return 1
        }
        case "$state" in
            preparing|rollback)
                warn "Removing interrupted pre-publication state: $preparation_dir"
                remove_managed_tree "$NVM_DIR_PATH" "$preparation_dir" ||
                    return 1
                ;;
            *)
                error "Unexpected NVM preparation state '$state': $preparation_dir"
                return 1
                ;;
        esac
    done

    for recovery_dir in "$NVM_DIR_PATH"/.dotfiles-runtime-recovery.*; do
        [ -e "$recovery_dir" ] || [ -L "$recovery_dir" ] || continue
        recovery_count=$((recovery_count + 1))
        RUNTIME_RECOVERY_DIR="$recovery_dir"
    done
    if [ "$recovery_count" -gt 1 ]; then
        error "Multiple NVM recovery transactions require inspection"
        error "Recovery root: $NVM_DIR_PATH"
        return 1
    fi
    [ "$recovery_count" -eq 1 ] || return 0

    state=$(recovery_state "$RUNTIME_RECOVERY_DIR") || {
        error "Invalid NVM recovery transaction: $RUNTIME_RECOVERY_DIR"
        return 1
    }
    case "$state" in
        rollback)
            warn "Recovering interrupted NVM runtime publication: $RUNTIME_RECOVERY_DIR"
            rollback_runtime_transaction "$RUNTIME_RECOVERY_DIR" || return 1
            ;;
        committed)
            warn "Cleaning committed NVM recovery state: $RUNTIME_RECOVERY_DIR"
            remove_managed_tree "$NVM_DIR_PATH" "$RUNTIME_RECOVERY_DIR" ||
                return 1
            RUNTIME_RECOVERY_DIR=""
            ;;
        *)
            error "NVM recovery transaction was never made durable"
            error "Preserved at: $RUNTIME_RECOVERY_DIR"
            return 1
            ;;
    esac
}

write_staging_record() {
    local staging_root="$1"
    local record_path="$staging_root/$STAGING_RECORD"

    {
        printf 'schema=1\n'
        printf 'tool=nvm\n'
        printf 'version=%s\n' "$NVM_VERSION"
        printf 'lock_token=%s\n' "$NVM_INSTALL_LOCK_TOKEN"
        printf 'state=staging\n'
    } > "$record_path"
    python3 - "$record_path" "$staging_root" "$NVM_RELEASES_ROOT" << 'PY'
import os
import sys

record, staging_root, releases_root = sys.argv[1:]
with open(record, "rb") as record_file:
    os.fsync(record_file.fileno())
for directory in (staging_root, releases_root):
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
PY
}

staging_record_valid() {
    local staging_root="$1"
    local staging_name
    local record_path="$staging_root/$STAGING_RECORD"
    local key
    local value
    local line_count=0

    STAGING_SCHEMA=""
    STAGING_TOOL=""
    STAGING_VERSION=""
    STAGING_LOCK_TOKEN=""
    STAGING_STATE=""
    [ -d "$staging_root" ] && [ ! -L "$staging_root" ] || return 1
    [ -f "$record_path" ] && [ ! -L "$record_path" ] || return 1
    while IFS='=' read -r key value || [ -n "$key" ]; do
        line_count=$((line_count + 1))
        case "$key" in
            schema)
                [ -z "$STAGING_SCHEMA" ] || return 1
                STAGING_SCHEMA="$value"
                ;;
            tool)
                [ -z "$STAGING_TOOL" ] || return 1
                STAGING_TOOL="$value"
                ;;
            version)
                [ -z "$STAGING_VERSION" ] || return 1
                STAGING_VERSION="$value"
                ;;
            lock_token)
                [ -z "$STAGING_LOCK_TOKEN" ] || return 1
                STAGING_LOCK_TOKEN="$value"
                ;;
            state)
                [ -z "$STAGING_STATE" ] || return 1
                STAGING_STATE="$value"
                ;;
            *) return 1 ;;
        esac
    done < "$record_path"
    [ "$line_count" -eq 5 ] &&
        [ "$STAGING_SCHEMA" = "1" ] &&
        [ "$STAGING_TOOL" = "nvm" ] &&
        [[ "$STAGING_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] &&
        [[ "$STAGING_LOCK_TOKEN" =~ ^[1-9][0-9]*-[0-9]+-[0-9]+$ ]] &&
        [ "$STAGING_STATE" = "staging" ] || return 1

    staging_name=$(basename "$staging_root")
    case "$staging_name" in
        ".dotfiles-stage-$STAGING_VERSION."*) ;;
        *) return 1 ;;
    esac
    staging_name="${staging_name#".dotfiles-stage-$STAGING_VERSION."}"
    [[ "$staging_name" =~ ^[0-9a-f]{32}$ ]]
}

staging_payload_shape_valid() {
    local staging_root="$1"
    local unexpected_entry
    local payload_root="$staging_root/payload"

    unexpected_entry=$(find "$staging_root" -mindepth 1 -maxdepth 1 \
        ! -name "$STAGING_RECORD" ! -name payload -print -quit) || return 1
    [ -z "$unexpected_entry" ] || return 1
    if [ -e "$payload_root" ] || [ -L "$payload_root" ]; then
        [ -d "$payload_root" ] && [ ! -L "$payload_root" ] || return 1
        unexpected_entry=$(find "$payload_root" -mindepth 1 -maxdepth 1 \
            ! -name nvm.sh \
            ! -name nvm-exec \
            ! -name bash_completion \
            ! -name "$INSTALLATION_RECORD" -print -quit) || return 1
        [ -z "$unexpected_entry" ] || return 1
        for unexpected_entry in "$payload_root"/* "$payload_root"/.*; do
            [ -e "$unexpected_entry" ] || [ -L "$unexpected_entry" ] ||
                continue
            case "$(basename "$unexpected_entry")" in
                .|..|nvm.sh|nvm-exec|bash_completion|"$INSTALLATION_RECORD")
                    ;;
                *) return 1 ;;
            esac
            [ -f "$unexpected_entry" ] && [ ! -L "$unexpected_entry" ] ||
                return 1
        done
    fi
}

recover_abandoned_staging_directories() {
    local publication_artifact
    local staging_root

    publication_artifact=$(find "$NVM_RELEASES_ROOT" -mindepth 1 -maxdepth 1 \
        \( -name '.publish-*.lock' -o -name '.replace-*' \) \
        -print -quit) || return 1
    if [ -n "$publication_artifact" ]; then
        error "Shared NVM release publication requires transaction recovery"
        error "Preserved at: $publication_artifact"
        return 1
    fi
    for staging_root in "$NVM_RELEASES_ROOT"/.dotfiles-stage-*; do
        [ -e "$staging_root" ] || [ -L "$staging_root" ] || continue
        if ! staging_record_valid "$staging_root" ||
                ! staging_payload_shape_valid "$staging_root"; then
            error "NVM release staging has no valid ownership closure"
            error "Preserved at: $staging_root"
            return 1
        fi
        warn "Removing interrupted NVM release staging: $staging_root"
        remove_managed_tree "$NVM_RELEASES_ROOT" "$staging_root" ||
            return 1
    done
}

publish_runtime_links() {
    local current_path="$NVM_DIR_PATH/$NVM_CURRENT_NAME"
    local prior_current=""
    local preparation_dir
    local name
    local destination

    if [ -L "$current_path" ]; then
        prior_current=$(readlink "$current_path")
        case "$prior_current" in
            "$NVM_RELEASES_NAME"/*)
                validate_version_component \
                    "${prior_current#"$NVM_RELEASES_NAME"/}" \
                    "current NVM release" || return 1
                ;;
            *)
                error "Refusing unexpected NVM release pointer: $prior_current"
                return 1
                ;;
        esac
    elif [ -e "$current_path" ]; then
        error "Refusing non-symlink NVM release pointer: $current_path"
        return 1
    fi

    for name in nvm.sh nvm-exec bash_completion; do
        destination="$NVM_DIR_PATH/$name"
        if [ -L "$destination" ]; then
            if [[ "$(readlink "$destination")" != "$NVM_CURRENT_NAME/$name" ]]; then
                error "Refusing unexpected NVM runtime link: $destination"
                return 1
            fi
        elif [ -e "$destination" ]; then
            if [ "$NVM_LEGACY_MIGRATION" != "true" ] ||
                    [ ! -f "$destination" ]; then
                error "Refusing unmanaged NVM runtime path: $destination"
                return 1
            fi
        fi
    done

    preparation_dir=$(
        mktemp -d "$NVM_DIR_PATH/.dotfiles-runtime-preparation.XXXXXX"
    )
    RUNTIME_RECOVERY_DIR="$preparation_dir"
    printf 'preparing\n' > "$preparation_dir/state"
    if [ -n "$prior_current" ]; then
        printf 'present=%s\n' "$prior_current" \
            > "$preparation_dir/prior-current"
    else
        printf 'absent=\n' > "$preparation_dir/prior-current"
    fi
    : > "$preparation_dir/captured"
    : > "$preparation_dir/absent"

    for name in nvm.sh nvm-exec bash_completion; do
        destination="$NVM_DIR_PATH/$name"
        if [ -e "$destination" ] || [ -L "$destination" ]; then
            copy_runtime_path "$destination" "$preparation_dir/$name"
            printf '%s\n' "$name" >> "$preparation_dir/captured"
        else
            printf '%s\n' "$name" >> "$preparation_dir/absent"
        fi
    done
    sync_recovery_metadata "$preparation_dir"
    set_recovery_state "$preparation_dir" rollback
    RUNTIME_RECOVERY_DIR="${preparation_dir/.dotfiles-runtime-preparation./.dotfiles-runtime-recovery.}"
    python3 - "$preparation_dir" "$RUNTIME_RECOVERY_DIR" \
        "$NVM_DIR_PATH" << 'PY'
import os
import sys

preparation, recovery, nvm_root = sys.argv[1:]
os.replace(preparation, recovery)
directory_fd = os.open(nvm_root, os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
    runtime_fault_point after-recovery || return 1

    replace_current_link "$NVM_RELEASES_NAME/$NVM_VERSION"
    runtime_fault_point after-current || return 1
    for name in nvm.sh nvm-exec bash_completion; do
        destination="$NVM_DIR_PATH/$name"
        if [ -L "$destination" ] &&
                [[ "$(readlink "$destination")" = "$NVM_CURRENT_NAME/$name" ]]; then
            continue
        fi
        replace_symlink_atomically \
            "$destination" "$NVM_CURRENT_NAME/$name"
        runtime_fault_point "after-$name" || return 1
    done

    if ! nvm_installation_valid; then
        error "Published NVM runtime failed functional validation"
        return 1
    fi
    runtime_fault_point after-validation || return 1

    set_recovery_state "$RUNTIME_RECOVERY_DIR" committed
    runtime_fault_point after-committed || return 1
    if ! remove_managed_tree "$NVM_DIR_PATH" "$RUNTIME_RECOVERY_DIR"; then
        error "NVM is installed, but committed recovery cleanup failed"
        error "Committed recovery state remains at: $RUNTIME_RECOVERY_DIR"
        return 1
    fi
    RUNTIME_RECOVERY_DIR=""
}

# shellcheck disable=SC2317  # Invoked by the EXIT trap installed in main.
cleanup_install() {
    local final_status=$?
    local state=""

    trap - EXIT HUP INT TERM
    if [ -n "$RUNTIME_RECOVERY_DIR" ] &&
            { [ -e "$RUNTIME_RECOVERY_DIR" ] ||
                [ -L "$RUNTIME_RECOVERY_DIR" ]; }; then
        state=$(recovery_state "$RUNTIME_RECOVERY_DIR" || true)
        case "$RUNTIME_RECOVERY_DIR:$state" in
            "$NVM_DIR_PATH"/.dotfiles-runtime-preparation.*:preparing|\
            "$NVM_DIR_PATH"/.dotfiles-runtime-preparation.*:rollback)
                if ! remove_managed_tree \
                        "$NVM_DIR_PATH" "$RUNTIME_RECOVERY_DIR"; then
                    error "NVM preparation cleanup requires inspection"
                    error "Preserved at: $RUNTIME_RECOVERY_DIR"
                    final_status=1
                fi
                ;;
            "$NVM_DIR_PATH"/.dotfiles-runtime-recovery.*:rollback)
                if ! rollback_runtime_transaction \
                        "$RUNTIME_RECOVERY_DIR"; then
                    final_status=1
                fi
                ;;
            "$NVM_DIR_PATH"/.dotfiles-runtime-recovery.*:committed)
                if ! remove_managed_tree \
                        "$NVM_DIR_PATH" "$RUNTIME_RECOVERY_DIR"; then
                    error "Committed NVM recovery state remains at: $RUNTIME_RECOVERY_DIR"
                    final_status=1
                fi
                ;;
            *)
                error "NVM transaction state requires manual inspection"
                error "Preserved at: $RUNTIME_RECOVERY_DIR"
                final_status=1
                ;;
        esac
    fi
    if [ -n "$NVM_STAGING_DIR" ] &&
            { [ -e "$NVM_STAGING_DIR" ] ||
                [ -L "$NVM_STAGING_DIR" ]; }; then
        if ! remove_managed_tree \
                "$NVM_RELEASES_ROOT" "$NVM_STAGING_DIR"; then
            error "NVM staging directory requires inspection: $NVM_STAGING_DIR"
            final_status=1
        fi
    fi
    if ! release_install_lock; then
        final_status=1
    fi
    exit "$final_status"
}

main() {
    # shellcheck disable=SC2031  # The validator's NVM_DIR is subshell-local.
    local requested_nvm_dir="${NVM_DIR:-}"
    local payload_dir
    local legacy_checkout=false
    local reuse_existing_release=false

    parse_arguments "$@"
    validate_release_identity
    NVM_DIR_PATH="$HOME/.nvm"
    if [ -n "$requested_nvm_dir" ] &&
            [ "$requested_nvm_dir" != "$NVM_DIR_PATH" ]; then
        error "This repository manages NVM only at $NVM_DIR_PATH"
        error "Refusing ambient NVM_DIR: $requested_nvm_dir"
        exit 1
    fi
    if [ -L "$NVM_DIR_PATH" ] ||
            { [ -e "$NVM_DIR_PATH" ] && [ ! -d "$NVM_DIR_PATH" ]; }; then
        error "Managed NVM root is not an ordinary directory: $NVM_DIR_PATH"
        exit 1
    fi
    mkdir -p "$NVM_DIR_PATH"
    trap cleanup_install EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    acquire_install_lock || exit 1

    NVM_RELEASES_ROOT="$NVM_DIR_PATH/$NVM_RELEASES_NAME"
    if [ -L "$NVM_RELEASES_ROOT" ] ||
            { [ -e "$NVM_RELEASES_ROOT" ] &&
                [ ! -d "$NVM_RELEASES_ROOT" ]; }; then
        error "NVM releases root is not an ordinary directory: $NVM_RELEASES_ROOT"
        exit 1
    fi
    mkdir -p "$NVM_RELEASES_ROOT"
    recover_staged_directory_publication \
        "$NVM_RELEASES_ROOT" "$NVM_VERSION" || exit 1
    recover_abandoned_runtime_transactions || exit 1
    recover_abandoned_staging_directories || exit 1

    if nvm_installation_valid; then
        if [ "$FORCE" != "true" ]; then
            warn "NVM $NVM_VERSION is already installed and verified"
            exit 0
        fi
    elif [ "$FORCE" != "true" ] && orphaned_current_release_valid; then
        warn "Resuming runtime publication for verified NVM $NVM_VERSION"
        reuse_existing_release=true
    elif ! nvm_root_empty; then
        if [ "$FORCE" != "true" ] && [ "$MIGRATE" != "true" ]; then
            error "Unmanaged or incomplete NVM installation: $NVM_DIR_PATH"
            error "Use --migrate for a canonical checkout or managed release"
            exit 1
        fi
        if legacy_nvm_checkout_valid; then
            legacy_checkout=true
        elif managed_nvm_root_recoverable; then
            :
        elif [ "$MIGRATE" = "true" ]; then
            error "Refusing to migrate an unidentified NVM directory"
            exit 1
        else
            error "Refusing to force-replace an unidentified NVM directory"
            exit 1
        fi
    fi
    NVM_LEGACY_MIGRATION="$legacy_checkout"

    if [ "$reuse_existing_release" != "true" ]; then
        NVM_STAGING_DIR=$(
            create_managed_staging_directory \
                "$NVM_RELEASES_ROOT" "$NVM_VERSION"
        )
        write_staging_record "$NVM_STAGING_DIR"
        payload_dir="$NVM_STAGING_DIR/payload"
        mkdir "$payload_dir"

        download "$NVM_SOURCE_ROOT/nvm.sh" "$payload_dir/nvm.sh"
        download "$NVM_SOURCE_ROOT/nvm-exec" "$payload_dir/nvm-exec"
        download "$NVM_SOURCE_ROOT/bash_completion" "$payload_dir/bash_completion"
        chmod 0755 "$payload_dir/nvm-exec"
        write_record "$payload_dir"
        nvm_release_valid "$payload_dir" || {
            error "NVM runtime checksum validation failed"
            exit 1
        }

        publish_staged_directory \
            "$NVM_RELEASES_ROOT" "$NVM_VERSION" "$payload_dir"
        runtime_fault_point after-release-publication || return 1
    fi
    publish_runtime_links
    info "NVM $NVM_VERSION installed successfully"
    info "Restart your shell or source $NVM_DIR_PATH/nvm.sh"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
