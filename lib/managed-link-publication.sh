# shellcheck shell=bash
# Crash-recoverable publication of managed symlinks under HOME.
# Sourced by bin/dotfiles after home-publication-transaction.sh.

_new_link_transaction_token() {
    python3 -c 'import secrets; print(secrets.token_hex(16))'
}

_cleanup_recovered_link_staging() {
    local destination="$1"
    local source="$2"
    local staging_container="$3"
    local replacement_path="$staging_container/replacement"
    local replacement_target=""

    if ! _validate_link_staging_path "$destination" "$staging_container"; then
        error "Link journal names invalid staging: $staging_container"
        return 1
    fi
    if [ -L "$replacement_path" ]; then
        if ! replacement_target=$(readlink "$replacement_path"); then
            error "Could not read staged link during recovery: $replacement_path"
            return 1
        fi
        if [ "$replacement_target" != "$source" ]; then
            error "Refusing unexpected staged link during recovery: $replacement_path"
            return 1
        fi
        if ! unlink "$replacement_path"; then
            error "Could not clean staged link during recovery: $replacement_path"
            return 1
        fi
    elif [ -e "$replacement_path" ]; then
        error "Refusing unexpected staged path during recovery: $replacement_path"
        return 1
    fi

    if [ -d "$staging_container" ]; then
        if ! rmdir "$staging_container"; then
            error "Could not clean link staging during recovery: $staging_container"
            return 1
        fi
    elif [ -e "$staging_container" ] || [ -L "$staging_container" ]; then
        error "Refusing invalid link staging during recovery: $staging_container"
        return 1
    fi
    if ! _fsync_transaction_paths "$(dirname "$staging_container")"; then
        error "Could not persist recovered link staging cleanup"
        return 1
    fi
}

_cleanup_link_journal() {
    local journal_dir="$1"
    local journal_path=""
    local journal_parent=""

    if ! journal_parent=$(dirname "$journal_dir"); then
        error "Could not resolve link journal parent: $journal_dir"
        return 1
    fi

    # Clear readiness first. If cleanup is interrupted, the next invocation
    # knows that no live-path replay remains.
    journal_path="$journal_dir/ready"
    if [ -e "$journal_path" ] || [ -L "$journal_path" ]; then
        if [ ! -f "$journal_path" ] || [ -L "$journal_path" ]; then
            error "Invalid link journal readiness marker: $journal_path"
            return 1
        fi
        if ! unlink "$journal_path"; then
            error "Could not clear link journal readiness: $journal_path"
            return 1
        fi
        if ! _fsync_transaction_paths "$journal_dir" "$journal_parent"; then
            error "Could not persist retired link journal readiness"
            return 1
        fi
    fi

    for journal_path in \
            "$journal_dir/backup" \
            "$journal_dir/staging" \
            "$journal_dir/source" \
            "$journal_dir/destination"; do
        if [ -L "$journal_path" ] || [ -f "$journal_path" ]; then
            if ! unlink "$journal_path"; then
                error "Could not clean link journal metadata: $journal_path"
                return 1
            fi
        elif [ -e "$journal_path" ]; then
            error "Invalid link journal metadata: $journal_path"
            return 1
        fi
    done
    if ! _fsync_transaction_paths "$journal_dir"; then
        error "Could not persist cleaned link journal metadata"
        return 1
    fi
    if ! rmdir "$journal_dir"; then
        error "Could not clean link journal directory: $journal_dir"
        return 1
    fi
    if ! _fsync_transaction_paths "$journal_parent"; then
        error "Could not persist link journal retirement"
        return 1
    fi
}

_recover_one_link_transaction() {
    local journal_dir="$1"
    local ready_path="$journal_dir/ready"
    local destination_metadata="$journal_dir/destination"
    local source_metadata="$journal_dir/source"
    local staging_metadata="$journal_dir/staging"
    local backup_metadata="$journal_dir/backup"
    local destination=""
    local source=""
    local staging_container=""
    local backup_path=""
    local backup_container=""
    local destination_target=""
    local required_metadata=""
    local ready=false
    local committed=false

    if [ -e "$ready_path" ] || [ -L "$ready_path" ]; then
        if [ ! -f "$ready_path" ] || [ -L "$ready_path" ]; then
            error "Invalid link transaction readiness marker: $ready_path"
            return 1
        fi
        ready=true
    fi

    for required_metadata in \
            "$destination_metadata" \
            "$source_metadata" \
            "$staging_metadata"; do
        if [ ! -L "$required_metadata" ]; then
            if [ "$ready" = true ]; then
                error "Ready link transaction lacks metadata: $required_metadata"
                return 1
            fi
            # An incomplete pre-mutation journal has nothing to replay.
            return 0
        fi
    done
    if ! destination=$(readlink "$destination_metadata") ||
            ! source=$(readlink "$source_metadata") ||
            ! staging_container=$(readlink "$staging_metadata"); then
        error "Could not read link transaction metadata: $journal_dir"
        return 1
    fi
    if ! _validate_home_destination "$destination"; then
        error "Link journal destination escapes HOME: $destination"
        return 1
    fi
    if ! _validate_link_staging_path "$destination" "$staging_container"; then
        error "Link journal staging is outside its destination parent"
        return 1
    fi

    if [ -L "$backup_metadata" ]; then
        if ! backup_path=$(readlink "$backup_metadata"); then
            error "Could not read link backup metadata: $backup_metadata"
            return 1
        fi
        if ! _validate_link_backup_path "$destination" "$backup_path"; then
            error "Link journal backup path is invalid: $backup_path"
            return 1
        fi
        if ! backup_container=$(dirname "$backup_path"); then
            error "Could not resolve link backup container: $backup_path"
            return 1
        fi
    elif [ -e "$backup_metadata" ]; then
        error "Invalid link backup metadata: $backup_metadata"
        return 1
    fi

    if [ -L "$destination" ]; then
        if ! destination_target=$(readlink "$destination"); then
            error "Could not inspect recovered link destination: $destination"
            return 1
        fi
        if [ "$destination_target" = "$source" ]; then
            committed=true
        fi
    fi

    if [ "$ready" = true ]; then
        if [ "$committed" != true ] &&
                [ -n "$backup_path" ] &&
                { [ -e "$backup_path" ] || [ -L "$backup_path" ]; }; then
            if [ -e "$destination" ] || [ -L "$destination" ]; then
                error "Refusing occupied link destination during recovery: $destination"
                return 1
            fi
            if ! _atomic_rename_path "$backup_path" "$destination"; then
                error "Could not restore displaced link destination: $destination"
                return 1
            fi
            if ! _fsync_transaction_paths \
                    "$backup_container" "$(dirname "$destination")"; then
                error "Could not persist restored link destination: $destination"
                return 1
            fi
        elif [ "$committed" != true ] &&
                [ -n "$backup_path" ] &&
                [ ! -e "$destination" ] &&
                [ ! -L "$destination" ]; then
            error "Link recovery lost both destination and backup: $destination"
            return 1
        elif [ "$committed" != true ] &&
                [ -z "$backup_path" ] &&
                { [ -e "$destination" ] || [ -L "$destination" ]; }; then
            error "Refusing unexpected destination for absent-link rollback: $destination"
            return 1
        fi
    elif [ -n "$backup_path" ] &&
            { [ -e "$backup_path" ] || [ -L "$backup_path" ]; } &&
            [ "$committed" != true ]; then
        error "Unready link journal has unresolved displaced state: $journal_dir"
        return 1
    fi

    if ! _cleanup_recovered_link_staging \
            "$destination" "$source" "$staging_container"; then
        return 1
    fi
    if [ "$committed" != true ] &&
            [ -n "$backup_container" ] &&
            [ -d "$backup_container" ] &&
            [ ! -e "$backup_path" ] &&
            [ ! -L "$backup_path" ] &&
            ! rmdir "$backup_container"; then
        error "Could not clean recovered link backup container: $backup_container"
        return 1
    fi
    if [ "$committed" != true ] &&
            [ -n "$backup_container" ] &&
            [ ! -d "$backup_container" ] &&
            [ -d "$BACKUP_DIR" ] &&
            ! _fsync_transaction_paths "$BACKUP_DIR"; then
        error "Could not persist recovered link backup cleanup"
        return 1
    fi
    _cleanup_link_journal "$journal_dir"
}

_recover_link_transactions() {
    local state_root="$1"
    local journal_dir=""

    for journal_dir in "$state_root"/link-transaction.*; do
        [ -e "$journal_dir" ] || [ -L "$journal_dir" ] || continue
        if [ -L "$journal_dir" ] || [ ! -d "$journal_dir" ]; then
            error "Invalid link transaction journal: $journal_dir"
            return 1
        fi
        if ! _recover_one_link_transaction "$journal_dir"; then
            return 1
        fi
        # An incomplete pre-ready journal may lack metadata. Its fixed entries
        # are still safe to clean, and rmdir rejects any unexpected payload.
        if [ -d "$journal_dir" ] &&
                ! _cleanup_link_journal "$journal_dir"; then
            return 1
        fi
    done
}

# Create an absolute symlink while preserving the exact displaced path. The
# replacement is staged before the live path moves. The second rename is the
# commit point; every earlier error or signal restores the displaced file,
# directory, or symlink.
_link_path() (
    local src="$1"
    local dst="$2"
    local state_root="$HOME/.local/state/dotfiles"
    local lock_path="$state_root/home-transaction.lock"
    local destination_parent=""
    local destination_name=""
    local transaction_token=""
    local backup_container=""
    local backup_path=""
    local staging_container=""
    local replacement_path=""
    local journal_dir=""
    local journal_ready_path=""
    local lock_open=false
    local journal_ready=false
    local publication_started=false
    local committed=false

    # Invoked indirectly by the signal and EXIT traps below.
    # shellcheck disable=SC2317
    cleanup_link_transaction() {
        local exit_status=$?
        local cleanup_failed=false
        local current_target=""
        trap - EXIT
        trap '' HUP INT TERM

        if [ "$committed" != true ]; then
            # If the publication rename completed immediately before a signal,
            # the staged path is gone even though committed was not yet set.
            if [ "$publication_started" = true ] &&
                    [ -n "$replacement_path" ] &&
                    [ ! -e "$replacement_path" ] &&
                    [ ! -L "$replacement_path" ]; then
                if [ -L "$dst" ]; then
                    if current_target=$(readlink "$dst") &&
                            [ "$current_target" = "$src" ]; then
                        if ! unlink "$dst"; then
                            error "Could not remove uncommitted managed symlink: $dst"
                            cleanup_failed=true
                        elif ! _fsync_transaction_paths "$destination_parent"; then
                            error "Could not persist managed symlink rollback: $dst"
                            cleanup_failed=true
                        fi
                    else
                        error "Refusing to overwrite a changed path during rollback: $dst"
                        cleanup_failed=true
                    fi
                elif [ -e "$dst" ]; then
                    error "Refusing to overwrite a changed path during rollback: $dst"
                    cleanup_failed=true
                fi
            fi

            if [ -n "$backup_path" ] &&
                    { [ -e "$backup_path" ] || [ -L "$backup_path" ]; }; then
                if [ -e "$dst" ] || [ -L "$dst" ]; then
                    error "Could not restore $dst because the path is occupied"
                    cleanup_failed=true
                elif ! _atomic_rename_path "$backup_path" "$dst"; then
                    error "Could not restore managed symlink destination: $dst"
                    cleanup_failed=true
                elif ! _fsync_transaction_paths \
                        "$backup_container" "$destination_parent"; then
                    error "Could not persist restored managed symlink: $dst"
                    cleanup_failed=true
                fi
            fi
        fi

        if [ -n "$replacement_path" ] &&
                { [ -e "$replacement_path" ] || [ -L "$replacement_path" ]; } &&
                ! unlink "$replacement_path"; then
            error "Could not clean staged managed symlink: $replacement_path"
            cleanup_failed=true
        fi
        if [ -n "$staging_container" ] && [ -d "$staging_container" ] &&
                ! rmdir "$staging_container"; then
            error "Could not clean managed symlink staging directory: $staging_container"
            cleanup_failed=true
        fi
        if [ -n "$backup_container" ] && [ -d "$backup_container" ] &&
                { [ -z "$backup_path" ] ||
                    { [ ! -e "$backup_path" ] && [ ! -L "$backup_path" ]; }; } &&
                ! rmdir "$backup_container"; then
            error "Could not clean empty symlink backup container: $backup_container"
            cleanup_failed=true
        fi
        if [ -n "$destination_parent" ] &&
                [ -d "$destination_parent" ] &&
                ! _fsync_transaction_paths "$destination_parent"; then
            error "Could not persist managed symlink staging cleanup"
            cleanup_failed=true
        fi
        if [ -n "$backup_container" ] &&
                [ ! -d "$backup_container" ] &&
                [ -d "$BACKUP_DIR" ] &&
                ! _fsync_transaction_paths "$BACKUP_DIR"; then
            error "Could not persist managed symlink backup cleanup"
            cleanup_failed=true
        fi

        if [ -n "$journal_dir" ] && [ -d "$journal_dir" ]; then
            if [ "$journal_ready" = true ] &&
                    [ "$cleanup_failed" = true ]; then
                error "Retaining link journal for next-invocation recovery: $journal_dir"
            elif ! _cleanup_link_journal "$journal_dir"; then
                cleanup_failed=true
            fi
        fi
        if [ "$lock_open" = true ] && ! exec 9>&-; then
            error "Could not close dotfiles HOME transaction lock"
            cleanup_failed=true
        fi

        if [ "$cleanup_failed" = true ]; then
            exit_status=1
        fi
        exit "$exit_status"
    }
    trap cleanup_link_transaction EXIT
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

    if ! _validate_home_destination "$dst"; then
        error "Managed symlink destination escapes HOME: $dst"
        return 1
    fi
    if [ ! -e "$src" ]; then
        error "Managed symlink source does not exist: $src"
        return 1
    fi

    if ! destination_parent=$(dirname "$dst"); then
        error "Could not resolve managed symlink parent: $dst"
        return 1
    fi
    if ! destination_name=$(basename "$dst"); then
        error "Could not resolve managed symlink name: $dst"
        return 1
    fi
    if ! _mkdir_p_durable "$destination_parent"; then
        error "Could not create managed symlink directory: $destination_parent"
        return 1
    fi

    if [ -L "$dst" ]; then
        local current_target=""
        if ! current_target=$(readlink "$dst"); then
            error "Could not read managed symlink destination: $dst"
            return 1
        fi
        if [ "$current_target" = "$src" ]; then
            info "Already linked $dst -> $src"
            return 0
        fi
    fi

    if ! journal_dir=$(mktemp -d \
            "$state_root/link-transaction.XXXXXX"); then
        error "Could not create managed symlink transaction journal"
        return 1
    fi
    if ! transaction_token=$(_new_link_transaction_token) ||
            [[ ! "$transaction_token" =~ ^[0-9a-f]{32}$ ]]; then
        error "Could not create managed symlink transaction identity"
        return 1
    fi
    staging_container="$destination_parent/.dotfiles-link.$transaction_token"
    replacement_path="$staging_container/replacement"
    if ! ln -s "$dst" "$journal_dir/destination" ||
            ! ln -s "$src" "$journal_dir/source" ||
            ! ln -s "$staging_container" "$journal_dir/staging"; then
        error "Could not write managed symlink transaction metadata"
        return 1
    fi

    # Give each displaced path its own permanent container. Differing symlinks
    # are preserved too; an already-canonical symlink returned above.
    if [ -e "$dst" ] || [ -L "$dst" ]; then
        if ! _mkdir_p_durable "$BACKUP_DIR"; then
            error "Could not create symlink backup directory: $BACKUP_DIR"
            return 1
        fi
        backup_container="$BACKUP_DIR/.dotfiles-link-backup.$transaction_token"
        backup_path="$backup_container/$destination_name"
        if ! ln -s "$backup_path" "$journal_dir/backup"; then
            error "Could not write managed symlink backup metadata"
            return 1
        fi
    fi

    # Persist every off-path identity before creating it. A hard crash from
    # this point forward leaves a discoverable journal, never an orphan whose
    # ownership must be inferred from a broad filename pattern.
    if [ -n "$backup_path" ]; then
        if ! _fsync_transaction_paths \
                "$destination_parent" \
                "$BACKUP_DIR" \
                "$journal_dir" \
                "$state_root"; then
            error "Could not persist managed symlink transaction identities"
            return 1
        fi
    elif ! _fsync_transaction_paths \
            "$destination_parent" \
            "$journal_dir" \
            "$state_root"; then
        error "Could not persist managed symlink transaction identities"
        return 1
    fi

    if ! mkdir "$staging_container"; then
        error "Could not create managed symlink staging directory: $staging_container"
        return 1
    fi
    if ! ln -s "$src" "$replacement_path"; then
        error "Could not stage managed symlink: $dst"
        return 1
    fi
    if [ -n "$backup_container" ] &&
            ! mkdir "$backup_container"; then
        error "Could not reserve symlink backup container for $dst"
        return 1
    fi

    journal_ready_path="$journal_dir/ready"
    if ! : > "$journal_ready_path"; then
        error "Could not mark managed symlink transaction ready"
        return 1
    fi
    if [ -n "$backup_container" ]; then
        if ! _fsync_transaction_paths \
                "$destination_parent" \
                "$staging_container" \
                "$BACKUP_DIR" \
                "$backup_container" \
                "$journal_ready_path" \
                "$journal_dir" \
                "$state_root"; then
            error "Could not persist managed symlink transaction journal"
            return 1
        fi
    elif ! _fsync_transaction_paths \
            "$destination_parent" \
            "$staging_container" \
            "$journal_ready_path" \
            "$journal_dir" \
            "$state_root"; then
        error "Could not persist managed symlink transaction journal"
        return 1
    fi
    journal_ready=true

    if [ -n "$backup_path" ]; then
        if ! _atomic_rename_path "$dst" "$backup_path"; then
            error "Could not back up managed symlink destination: $dst"
            error "The destination and backup directory must share a filesystem"
            return 1
        fi
        if ! _fsync_transaction_paths \
                "$backup_container" "$destination_parent"; then
            error "Could not persist managed symlink displacement: $dst"
            return 1
        fi
        warn "Backed up $dst to $backup_path"
    fi

    publication_started=true
    if ! _atomic_rename_path "$replacement_path" "$dst"; then
        error "Could not publish managed symlink: $dst"
        return 1
    fi
    if ! _fsync_transaction_paths \
            "$staging_container" "$destination_parent"; then
        error "Could not persist managed symlink publication: $dst"
        return 1
    fi
    committed=true
    info "Linked $dst -> $src"
)
