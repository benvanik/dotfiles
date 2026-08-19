# shellcheck shell=bash
# Create-once and optimistic replacement of machine-local HOME files.
# Sourced by bin/dotfiles after agent-contract-publication.sh.

# Publish a new regular file without ever replacing an occupied destination.
# The renderer receives the staging pathname followed by its own arguments.
_create_local_file_locked() (
    local destination="$1"
    local mode="$2"
    local renderer="$3"
    local destination_parent=""
    local staging_path=""
    shift 3

    # Invoked indirectly by the EXIT trap below.
    # shellcheck disable=SC2317,SC2329
    cleanup_local_file_staging() {
        local exit_status=$?
        trap - EXIT
        trap '' HUP INT TERM
        if [ -n "$staging_path" ] &&
                { [ -e "$staging_path" ] || [ -L "$staging_path" ]; } &&
                ! unlink "$staging_path"; then
            error "Could not clean local-file staging: $staging_path"
            exit_status=1
        fi
        if [ -n "$destination_parent" ] &&
                [ -d "$destination_parent" ] &&
                ! _fsync_transaction_paths "$destination_parent"; then
            error "Could not persist local-file staging cleanup"
            exit_status=1
        fi
        exit "$exit_status"
    }
    trap cleanup_local_file_staging EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    if [ -L "$destination" ]; then
        error "Refusing symlinked local configuration: $destination"
        return 1
    fi
    if [ -e "$destination" ]; then
        error "Refusing occupied local configuration: $destination"
        return 1
    fi
    if ! destination_parent=$(dirname "$destination"); then
        error "Could not resolve local configuration parent: $destination"
        return 1
    fi
    if ! _mkdir_p_durable "$destination_parent"; then
        error "Could not create local configuration parent: $destination_parent"
        return 1
    fi
    if ! _validate_home_destination "$destination"; then
        error "Local configuration destination escapes HOME: $destination"
        return 1
    fi
    if ! staging_path=$(mktemp \
            "$destination_parent/.dotfiles-local.XXXXXX"); then
        error "Could not stage local configuration: $destination"
        return 1
    fi
    if ! "$renderer" "$staging_path" "$@"; then
        error "Could not render local configuration: $destination"
        return 1
    fi
    if ! chmod "$mode" "$staging_path"; then
        error "Could not set local configuration mode: $destination"
        return 1
    fi
    if ! _fsync_transaction_paths "$staging_path"; then
        error "Could not persist staged local configuration: $destination"
        return 1
    fi

    # A same-filesystem hard link provides create-if-absent publication on
    # Bash 3.2 hosts without an overwrite race.
    if ! ln "$staging_path" "$destination"; then
        error "Local configuration became occupied: $destination"
        return 1
    fi
    if ! _fsync_transaction_paths "$destination" "$destination_parent"; then
        error "Could not persist local configuration: $destination"
        return 1
    fi
    if ! unlink "$staging_path"; then
        error "Could not retire local-file staging: $staging_path"
        return 1
    fi
    if ! _fsync_transaction_paths "$destination_parent"; then
        error "Could not persist local-file staging retirement: $destination"
        return 1
    fi
    staging_path=""
)

_local_file_generation() {
    python3 -c '
import os
import stat
import sys

path_stat = os.lstat(sys.argv[1])
if not stat.S_ISREG(path_stat.st_mode):
    sys.exit(1)
print(
    path_stat.st_dev,
    path_stat.st_ino,
    path_stat.st_mtime_ns,
    path_stat.st_size,
)
' "$1"
}

# Replace one existing regular local file atomically. The source generation is
# checked again immediately before publication so an editor racing the prompt
# is reported instead of overwritten.
_replace_local_file_locked() (
    local destination="$1"
    local renderer="$2"
    local destination_parent=""
    local original_generation=""
    local current_generation=""
    local staging_path=""
    shift 2

    # Invoked indirectly by the EXIT trap below.
    # shellcheck disable=SC2317,SC2329
    cleanup_local_file_replacement() {
        local exit_status=$?
        local cleanup_parent=false
        trap - EXIT
        trap '' HUP INT TERM
        if [ -n "$staging_path" ]; then
            cleanup_parent=true
            if { [ -e "$staging_path" ] || [ -L "$staging_path" ]; } &&
                    ! unlink "$staging_path"; then
                error "Could not clean local-file replacement: $staging_path"
                exit_status=1
            fi
        fi
        if [ "$cleanup_parent" = true ] &&
                [ -n "$destination_parent" ] &&
                [ -d "$destination_parent" ] &&
                ! _fsync_transaction_paths "$destination_parent"; then
            error "Could not persist local-file replacement cleanup: $destination_parent"
            exit_status=1
        fi
        exit "$exit_status"
    }
    trap cleanup_local_file_replacement EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    if [ -L "$destination" ] || [ ! -f "$destination" ]; then
        error "Local configuration is not a real regular file: $destination"
        return 1
    fi
    if ! _validate_home_destination "$destination"; then
        error "Local configuration destination escapes HOME: $destination"
        return 1
    fi
    if ! destination_parent=$(dirname "$destination"); then
        error "Could not resolve local configuration parent: $destination"
        return 1
    fi
    if ! original_generation=$(_local_file_generation "$destination"); then
        error "Could not inspect local configuration: $destination"
        return 1
    fi
    if ! staging_path=$(mktemp \
            "$destination_parent/.dotfiles-local.XXXXXX"); then
        error "Could not stage local configuration replacement: $destination"
        return 1
    fi
    if ! cp -p "$destination" "$staging_path"; then
        error "Could not snapshot local configuration: $destination"
        return 1
    fi
    if ! "$renderer" "$staging_path" "$@"; then
        error "Could not render local configuration replacement: $destination"
        return 1
    fi
    if ! _fsync_transaction_paths "$staging_path"; then
        error "Could not persist local configuration replacement: $destination"
        return 1
    fi
    if ! current_generation=$(_local_file_generation "$destination") ||
            [ "$current_generation" != "$original_generation" ]; then
        error "Local configuration changed while it was being edited: $destination"
        return 1
    fi
    if ! python3 -c \
            'import os, sys; os.replace(sys.argv[1], sys.argv[2])' \
            "$staging_path" "$destination"; then
        error "Could not publish local configuration replacement: $destination"
        return 1
    fi
    staging_path=""
    if ! _fsync_transaction_paths "$destination" "$destination_parent"; then
        error "Could not persist local configuration replacement: $destination"
        return 1
    fi
)
