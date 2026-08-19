# shellcheck shell=bash
# Atomic managed-file copy, snapshot, and rollback publication under HOME.
# Sourced by bin/dotfiles after managed-link-publication.sh.

# Copy one absolute source path to one absolute destination path.
_copy_path() (
    local src="$1"
    local dst="$2"
    local expected_destination_generation="${3:-}"
    local source_generation=""
    local source_payload_generation=""
    local destination_generation=""
    local destination_payload_generation=""
    local staged_payload_generation=""
    local destination_parent=""
    local destination_name=""
    local backup_container=""
    local backup_path=""
    local backup_staging_path=""
    local staging_path=""
    # Invoked indirectly by the signal and EXIT traps below.
    # shellcheck disable=SC2317,SC2329
    cleanup_copy_staging() {
        local exit_status=$?
        local cleanup_failed=false
        trap - EXIT
        trap '' HUP INT TERM
        if [ -n "$backup_staging_path" ] &&
                [ -e "$backup_staging_path" ] &&
                ! unlink "$backup_staging_path"; then
            error "Could not clean managed backup staging: $backup_staging_path"
            cleanup_failed=true
        fi
        if [ -n "$staging_path" ] &&
                [ -e "$staging_path" ] &&
                ! unlink "$staging_path"; then
            error "Could not clean managed copy staging: $staging_path"
            cleanup_failed=true
        fi
        if [ -n "$destination_parent" ] &&
                [ -d "$destination_parent" ] &&
                ! _fsync_transaction_paths "$destination_parent"; then
            error "Could not persist managed copy staging cleanup"
            cleanup_failed=true
        fi
        if [ -d "$BACKUP_DIR" ] &&
                ! _fsync_transaction_paths "$BACKUP_DIR"; then
            error "Could not persist managed backup staging cleanup"
            cleanup_failed=true
        fi
        if [ "$cleanup_failed" = true ]; then
            exit_status=1
        fi
        exit "$exit_status"
    }
    trap cleanup_copy_staging EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    if [ ! -f "$src" ] || [ -L "$src" ]; then
        error "Managed copy source is not a regular file: $src"
        return 1
    fi
    if ! source_generation=$(_managed_path_generation exact "$src") ||
            ! source_payload_generation=$(
                _managed_path_generation payload "$src"
            ); then
        error "Could not capture managed copy source generation: $src"
        return 1
    fi
    if ! destination_parent=$(dirname "$dst"); then
        error "Could not resolve managed destination parent: $dst"
        return 1
    fi
    if ! destination_name=$(basename "$dst"); then
        error "Could not resolve managed destination name: $dst"
        return 1
    fi
    if ! _validate_home_destination "$dst"; then
        error "Managed copy destination escapes HOME: $dst"
        return 1
    fi
    if ! _mkdir_p_durable "$destination_parent"; then
        error "Could not create managed destination directory: $destination_parent"
        return 1
    fi
    if ! _validate_home_destination "$dst"; then
        error "Managed copy destination changed outside HOME: $dst"
        return 1
    fi

    if [ -e "$dst" ] && [ ! -L "$dst" ] && [ ! -f "$dst" ]; then
        error "Refusing to replace non-file managed destination: $dst"
        return 1
    fi
    if ! destination_generation=$(_managed_path_generation exact "$dst") ||
            ! destination_payload_generation=$(
                _managed_path_generation payload "$dst"
            ); then
        error "Could not capture managed destination generation: $dst"
        return 1
    fi
    if [ -n "$expected_destination_generation" ] &&
            [ "$destination_generation" != \
                "$expected_destination_generation" ]; then
        error "Managed destination changed after transaction snapshot: $dst"
        return 1
    fi

    # Preserve a differing regular file before publication, but leave the
    # current destination in service until its replacement is ready.
    if [ -f "$dst" ] && [ ! -L "$dst" ]; then
        if [ "$source_payload_generation" = \
                "$destination_payload_generation" ]; then
            if ! _require_managed_path_generation \
                    "$dst" "$destination_generation" ||
                    ! _require_managed_path_generation \
                    "$src" "$source_generation"; then
                return 1
            fi
            info "Already copied $dst"
            return
        fi
        if ! _mkdir_p_durable "$BACKUP_DIR"; then
            error "Could not create managed-file backup directory: $BACKUP_DIR"
            return 1
        fi
        if ! backup_staging_path=$(mktemp \
                "$BACKUP_DIR/.dotfiles-backup.XXXXXX"); then
            error "Could not stage managed-file backup in $BACKUP_DIR"
            return 1
        fi
        if ! cp -p "$dst" "$backup_staging_path"; then
            error "Could not copy managed-file backup for $dst"
            return 1
        fi
        if ! _require_managed_path_generation \
                "$dst" "$destination_generation" ||
                ! staged_payload_generation=$(
                    _managed_path_generation payload "$backup_staging_path"
                ) ||
                [ "$staged_payload_generation" != \
                    "$destination_payload_generation" ]; then
            error "Managed destination changed while its backup was staged: $dst"
            return 1
        fi
        if ! _fsync_transaction_paths "$backup_staging_path" "$BACKUP_DIR"; then
            error "Could not persist managed-file backup staging for $dst"
            return 1
        fi

        # Reserve a collision-free human-readable name, then publish with a
        # no-overwrite hard link. A concurrent process can make publication
        # fail, but it can never replace another backup.
        if ! backup_path=$(mktemp \
                "$BACKUP_DIR/$destination_name.XXXXXX"); then
            error "Could not reserve managed-file backup name for $dst"
            return 1
        fi
        if ! unlink "$backup_path"; then
            error "Could not release managed-file backup reservation: $backup_path"
            return 1
        fi
        if ! ln "$backup_staging_path" "$backup_path"; then
            error "Could not publish managed-file backup: $backup_path"
            return 1
        fi
        if ! _fsync_transaction_paths "$backup_path" "$BACKUP_DIR"; then
            error "Could not persist managed-file backup: $backup_path"
            return 1
        fi
        if ! unlink "$backup_staging_path"; then
            error "Could not release managed-file backup staging: $backup_staging_path"
            return 1
        fi
        if ! _fsync_transaction_paths "$BACKUP_DIR"; then
            error "Could not persist managed-file backup staging retirement"
            return 1
        fi
        backup_staging_path=""
        warn "Backed up $dst to $backup_path"
    elif [ -L "$dst" ]; then
        if ! _mkdir_p_durable "$BACKUP_DIR"; then
            error "Could not create managed-link backup directory: $BACKUP_DIR"
            return 1
        fi
        if ! backup_container=$(mktemp -d \
                "$BACKUP_DIR/.dotfiles-copy-backup.XXXXXX"); then
            error "Could not reserve managed-link backup for $dst"
            return 1
        fi
        backup_path="$backup_container/$destination_name"
        if ! cp -Pp "$dst" "$backup_path"; then
            error "Could not preserve managed-link destination: $dst"
            return 1
        fi
        if ! _require_managed_path_generation \
                "$dst" "$destination_generation" ||
                ! staged_payload_generation=$(
                    _managed_path_generation payload "$backup_path"
                ) ||
                [ "$staged_payload_generation" != \
                    "$destination_payload_generation" ]; then
            error "Managed symlink changed while its backup was staged: $dst"
            return 1
        fi
        if ! _fsync_transaction_paths "$backup_container" "$BACKUP_DIR"; then
            error "Could not persist managed-link backup: $backup_path"
            return 1
        fi
        warn "Backed up $dst to $backup_path"
    fi

    # Stage on the destination filesystem, then use Python's os.replace for
    # portable atomic replacement of regular files and symlinks alike.
    if ! staging_path=$(mktemp \
            "$destination_parent/.dotfiles-copy.XXXXXX"); then
        error "Could not stage managed destination: $dst"
        return 1
    fi
    if ! cp -p "$src" "$staging_path"; then
        if ! unlink "$staging_path"; then
            error "Could not clean failed managed staging file: $staging_path"
        fi
        return 1
    fi
    if ! _require_managed_path_generation "$src" "$source_generation" ||
            ! staged_payload_generation=$(
                _managed_path_generation payload "$staging_path"
            ) ||
            [ "$staged_payload_generation" != "$source_payload_generation" ]; then
        error "Managed copy source changed while publication was staged: $src"
        return 1
    fi
    if ! _fsync_transaction_paths "$staging_path"; then
        error "Could not persist managed destination staging: $staging_path"
        return 1
    fi
    # Participating dotfiles writers share the HOME lock. This final exact
    # recheck catches other changes visible before rename; portable os.replace
    # is not a filesystem compare-and-swap against a writer ignoring the lock.
    if ! _require_managed_path_generation \
            "$dst" "$destination_generation"; then
        return 1
    fi
    if ! python3 -c \
        'import os, sys; os.replace(sys.argv[1], sys.argv[2])' \
        "$staging_path" "$dst"; then
        if ! unlink "$staging_path"; then
            error "Could not clean failed managed staging file: $staging_path"
        fi
        return 1
    fi
    staging_path=""
    if ! _fsync_transaction_paths "$dst" "$destination_parent"; then
        error "Could not persist managed destination: $dst"
        return 1
    fi
    info "Copied $dst from $src"
)

# Copy a managed file for tools that do not reliably follow symlinks.
_copy() {
    _copy_path "$DOTFILES/$1" "$HOME/$2"
}

# Validate one absolute copy destination without changing its current payload.
# The write probe is signal-safe and lives on the destination filesystem.
_preflight_copy_path() (
    local src="$1"
    local dst="$2"
    local destination_parent=""
    local preflight_path=""
    # Invoked indirectly by the signal and EXIT traps below.
    # shellcheck disable=SC2317,SC2329
    cleanup_copy_preflight() {
        local exit_status=$?
        local cleanup_failed=false
        local cleanup_parent=false
        trap - EXIT
        trap '' HUP INT TERM
        if [ -n "$preflight_path" ]; then
            cleanup_parent=true
            if { [ -e "$preflight_path" ] || [ -L "$preflight_path" ]; } &&
                    ! unlink "$preflight_path"; then
                error "Could not clean managed preflight staging: $preflight_path"
                cleanup_failed=true
            fi
        fi
        if [ "$cleanup_parent" = true ] &&
                [ -n "$destination_parent" ] &&
                [ -d "$destination_parent" ] &&
                ! _fsync_transaction_paths "$destination_parent"; then
            error "Could not persist managed preflight cleanup: $destination_parent"
            cleanup_failed=true
        fi
        if [ "$cleanup_failed" = true ]; then
            exit_status=1
        fi
        exit "$exit_status"
    }
    trap cleanup_copy_preflight EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    if [ ! -f "$src" ] || [ -L "$src" ]; then
        error "Managed copy source is not a regular file: $src"
        return 1
    fi

    if ! destination_parent=$(dirname "$dst"); then
        error "Could not resolve managed destination parent: $dst"
        return 1
    fi
    if ! _validate_home_destination "$dst"; then
        error "Managed copy destination escapes HOME: $dst"
        return 1
    fi
    if ! _mkdir_p_durable "$destination_parent"; then
        error "Could not create managed destination directory: $destination_parent"
        return 1
    fi
    if ! _validate_home_destination "$dst"; then
        error "Managed copy destination changed outside HOME: $dst"
        return 1
    fi
    if [ -e "$dst" ] && [ ! -L "$dst" ] && [ ! -f "$dst" ]; then
        error "Refusing non-file managed destination: $dst"
        return 1
    fi
    if ! preflight_path=$(mktemp \
            "$destination_parent/.dotfiles-preflight.XXXXXX"); then
        error "Managed destination is not writable: $dst"
        return 1
    fi
    if ! cp -p "$src" "$preflight_path"; then
        error "Could not stage managed contract for $dst"
        return 1
    fi
    if ! unlink "$preflight_path"; then
        error "Could not clean managed preflight file: $preflight_path"
        return 1
    fi
    if ! _fsync_transaction_paths "$destination_parent"; then
        error "Could not persist managed preflight cleanup: $destination_parent"
        return 1
    fi
    preflight_path=""
)

# Validate one managed-copy destination.
_preflight_copy_destination() {
    _preflight_copy_path "$DOTFILES/$1" "$HOME/$2"
}

# Preserve a file, symlink, or absence for transaction rollback.
_snapshot_copy_destination() {
    local dst="$1"
    local snapshot_path="$2"
    local expected_generation="$3"
    local expected_payload_generation="$4"
    local snapshot_payload_generation=""

    if [ -L "$dst" ]; then
        if ! cp -Pp "$dst" "$snapshot_path"; then
            error "Could not snapshot managed symlink destination: $dst"
            return 1
        fi
    elif [ -f "$dst" ]; then
        if ! cp -p "$dst" "$snapshot_path"; then
            error "Could not snapshot managed file destination: $dst"
            return 1
        fi
    elif [ -e "$dst" ]; then
        error "Refusing non-file managed destination: $dst"
        return 1
    fi
    if ! _require_managed_path_generation \
            "$dst" "$expected_generation" ||
            ! snapshot_payload_generation=$(
                _managed_path_generation payload "$snapshot_path"
            ) ||
            [ "$snapshot_payload_generation" != \
                "$expected_payload_generation" ]; then
        error "Managed destination changed while its transaction snapshot was staged: $dst"
        return 1
    fi
}

# Atomically restore a destination snapshot. A missing snapshot represents a
# destination that did not exist when the transaction began.
_restore_copy_destination() (
    local snapshot_path="$1"
    local dst="$2"
    local managed_source="$3"
    local before_generation=""
    local current_generation=""
    local managed_generation=""
    local destination_parent=""
    local staging_container=""
    local replacement_path=""

    # Invoked indirectly by the signal and EXIT traps below.
    # shellcheck disable=SC2317,SC2329
    cleanup_restore_staging() {
        local exit_status=$?
        local cleanup_failed=false
        trap - EXIT
        trap '' HUP INT TERM

        if [ -n "$replacement_path" ] &&
                { [ -e "$replacement_path" ] || [ -L "$replacement_path" ]; } &&
                ! unlink "$replacement_path"; then
            error "Could not clean managed rollback staging: $replacement_path"
            cleanup_failed=true
        fi
        if [ -n "$staging_container" ] && [ -d "$staging_container" ] &&
                ! rmdir "$staging_container"; then
            error "Could not clean managed rollback directory: $staging_container"
            cleanup_failed=true
        fi
        if [ -n "$destination_parent" ] &&
                [ -d "$destination_parent" ] &&
                ! _fsync_transaction_paths "$destination_parent"; then
            error "Could not persist managed rollback staging cleanup"
            cleanup_failed=true
        fi
        if [ "$cleanup_failed" = true ]; then
            exit_status=1
        fi
        exit "$exit_status"
    }
    trap cleanup_restore_staging EXIT
    # Rollback is entered only after the owning transaction has committed to
    # failure. Complete it even if the original signal is delivered to the
    # whole process group or another signal arrives during restoration.
    trap '' HUP INT TERM

    if ! destination_parent=$(dirname "$dst"); then
        error "Could not resolve managed rollback parent: $dst"
        return 1
    fi
    if ! _validate_home_destination "$dst"; then
        error "Managed rollback destination escapes HOME: $dst"
        return 1
    fi
    if ! before_generation=$(
            _managed_path_generation payload "$snapshot_path"
        ) ||
            ! managed_generation=$(
                _managed_path_generation payload "$managed_source"
            ) ||
            ! current_generation=$(
                _managed_path_generation payload "$dst"
            ); then
        error "Could not classify managed destination rollback: $dst"
        return 1
    fi
    if [ "$current_generation" = "$before_generation" ]; then
        return 0
    fi
    if [ "$current_generation" != "$managed_generation" ]; then
        warn "Preserving externally changed managed destination: $dst"
        return 0
    fi
    if [ ! -e "$snapshot_path" ] && [ ! -L "$snapshot_path" ]; then
        if [ -L "$dst" ] || [ -f "$dst" ]; then
            if ! unlink "$dst"; then
                error "Could not restore absent managed destination: $dst"
                return 1
            fi
            if ! _fsync_transaction_paths "$destination_parent"; then
                error "Could not persist absent managed destination: $dst"
                return 1
            fi
        elif [ -e "$dst" ]; then
            error "Refusing to remove non-file during managed rollback: $dst"
            return 1
        fi
        return 0
    fi

    if ! staging_container=$(mktemp -d \
            "$destination_parent/.dotfiles-restore.XXXXXX"); then
        error "Could not create managed rollback directory: $destination_parent"
        return 1
    fi
    replacement_path="$staging_container/replacement"
    if ! cp -Pp "$snapshot_path" "$replacement_path"; then
        error "Could not stage managed rollback for $dst"
        return 1
    fi
    if ! _fsync_transaction_paths \
            "$replacement_path" "$staging_container"; then
        error "Could not persist managed rollback staging for $dst"
        return 1
    fi
    if ! python3 -c \
        'import os, sys; os.replace(sys.argv[1], sys.argv[2])' \
        "$replacement_path" "$dst"; then
        error "Could not restore managed destination: $dst"
        return 1
    fi
    replacement_path=""
    if ! _fsync_transaction_paths \
            "$dst" "$staging_container" "$destination_parent"; then
        error "Could not persist managed destination rollback: $dst"
        return 1
    fi
)
