# shellcheck shell=bash
# Two-destination transactional publication of provider agent contracts.
# Sourced by bin/dotfiles after managed-copy-publication.sh.

_cleanup_agent_contract_journal() {
    local transaction_dir="$1"
    local transaction_path=""
    local transaction_parent=""

    if ! transaction_parent=$(dirname "$transaction_dir"); then
        error "Could not resolve agent contract journal parent: $transaction_dir"
        return 1
    fi

    # Clear readiness first so an interrupted cleanup cannot trigger another
    # rollback after its snapshots begin disappearing.
    transaction_path="$transaction_dir/rollback-ready"
    if [ -e "$transaction_path" ] || [ -L "$transaction_path" ]; then
        if [ ! -f "$transaction_path" ] || [ -L "$transaction_path" ]; then
            error "Invalid agent contract readiness marker: $transaction_path"
            return 1
        fi
        if ! unlink "$transaction_path"; then
            error "Could not clear agent contract readiness: $transaction_path"
            return 1
        fi
        if ! _fsync_transaction_paths \
                "$transaction_dir" "$transaction_parent"; then
            error "Could not persist retired agent contract readiness"
            return 1
        fi
    fi

    for transaction_path in \
            "$transaction_dir/AGENTS.before" \
            "$transaction_dir/CLAUDE.before" \
            "$transaction_dir/WORKING_CONTRACT.md"; do
        if [ -L "$transaction_path" ] || [ -f "$transaction_path" ]; then
            if ! unlink "$transaction_path"; then
                error "Could not clean agent contract journal: $transaction_path"
                return 1
            fi
        elif [ -e "$transaction_path" ]; then
            error "Invalid agent contract journal payload: $transaction_path"
            return 1
        fi
    done
    if ! _fsync_transaction_paths "$transaction_dir"; then
        error "Could not persist cleaned agent contract journal metadata"
        return 1
    fi
    if ! rmdir "$transaction_dir"; then
        error "Could not clean agent contract journal directory: $transaction_dir"
        return 1
    fi
    if ! _fsync_transaction_paths "$transaction_parent"; then
        error "Could not persist agent contract journal retirement"
        return 1
    fi
}

_recover_agent_contract_transactions() {
    local state_root="$1"
    local claude_destination="$2"
    local codex_destination="$3"
    local transaction_dir=""
    local ready_path=""
    local claude_snapshot=""
    local codex_snapshot=""
    local destination_snapshot=""

    for transaction_dir in "$state_root"/agent-contract-transaction.*; do
        [ -e "$transaction_dir" ] || [ -L "$transaction_dir" ] || continue
        if [ -L "$transaction_dir" ] || [ ! -d "$transaction_dir" ]; then
            error "Invalid agent contract transaction journal: $transaction_dir"
            return 1
        fi
        ready_path="$transaction_dir/rollback-ready"
        claude_snapshot="$transaction_dir/CLAUDE.before"
        codex_snapshot="$transaction_dir/AGENTS.before"

        if [ -e "$ready_path" ] || [ -L "$ready_path" ]; then
            if [ ! -f "$ready_path" ] || [ -L "$ready_path" ]; then
                error "Invalid agent contract readiness marker: $ready_path"
                return 1
            fi
            if [ ! -f "$transaction_dir/WORKING_CONTRACT.md" ] ||
                    [ -L "$transaction_dir/WORKING_CONTRACT.md" ]; then
                error "Invalid agent contract source snapshot: $transaction_dir"
                return 1
            fi
            for destination_snapshot in \
                    "$claude_snapshot" \
                    "$codex_snapshot"; do
                if [ -e "$destination_snapshot" ] &&
                        [ ! -f "$destination_snapshot" ] &&
                        [ ! -L "$destination_snapshot" ]; then
                    error "Invalid agent contract rollback snapshot: $destination_snapshot"
                    return 1
                fi
            done
            if ! _restore_copy_destination \
                    "$codex_snapshot" "$codex_destination" \
                    "$transaction_dir/WORKING_CONTRACT.md"; then
                error "Could not recover Codex working contract"
                return 1
            fi
            if ! _restore_copy_destination \
                    "$claude_snapshot" "$claude_destination" \
                    "$transaction_dir/WORKING_CONTRACT.md"; then
                error "Could not recover Claude working contract"
                return 1
            fi
        fi

        if ! _cleanup_agent_contract_journal "$transaction_dir"; then
            return 1
        fi
    done
}

# Publish the provider-neutral working contract as one two-destination
# transaction. The per-home advisory lock serializes concurrent dotfiles
# processes. One immutable source snapshot feeds both copies, and both prior
# destination states remain recoverable until journal retirement commits both
# publications.
_publish_agent_contracts() (
    local canonical_source="$DOTFILES/agents/WORKING_CONTRACT.md"
    local claude_destination="$HOME/.claude/CLAUDE.md"
    local codex_destination="$HOME/.codex/AGENTS.md"
    # The lock identity follows HOME itself. XDG_STATE_HOME is intentionally
    # not part of it: two processes managing one HOME must not be able to opt
    # into different locks through ambient environment.
    local state_root="$HOME/.local/state/dotfiles"
    local lock_path="$state_root/home-transaction.lock"
    local transaction_dir=""
    local source_snapshot=""
    local claude_snapshot=""
    local codex_snapshot=""
    local claude_generation=""
    local claude_payload_generation=""
    local codex_generation=""
    local codex_payload_generation=""
    local rollback_ready_path=""
    local lock_open=false
    local rollback_ready=false
    local committed=false

    # Invoked indirectly by the signal and EXIT traps below.
    # shellcheck disable=SC2317,SC2329
    cleanup_agent_contract_transaction() {
        local exit_status=$?
        local cleanup_failed=false
        local recovery_failed=false
        trap - EXIT
        trap '' HUP INT TERM

        if [ "$rollback_ready" = true ] && [ "$committed" != true ]; then
            if ! _restore_copy_destination \
                    "$codex_snapshot" "$codex_destination" \
                    "$source_snapshot"; then
                error "Could not roll back Codex working contract"
                cleanup_failed=true
                recovery_failed=true
            fi
            if ! _restore_copy_destination \
                    "$claude_snapshot" "$claude_destination" \
                    "$source_snapshot"; then
                error "Could not roll back Claude working contract"
                cleanup_failed=true
                recovery_failed=true
            fi
        fi

        if [ -n "$transaction_dir" ] && [ -d "$transaction_dir" ]; then
            if [ "$rollback_ready" = true ] &&
                    [ "$recovery_failed" = true ]; then
                error "Retaining agent contract journal for next-invocation recovery: $transaction_dir"
            elif ! _cleanup_agent_contract_journal "$transaction_dir"; then
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
    trap cleanup_agent_contract_transaction EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    info "Publishing agent working contracts..."

    if [ -L "$state_root" ] ||
            { [ -e "$state_root" ] && [ ! -d "$state_root" ]; }; then
        error "Agent contract state root is not a real directory: $state_root"
        return 1
    fi
    if ! _mkdir_p_durable "$state_root"; then
        error "Could not create agent contract state root: $state_root"
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
            "$state_root" "$claude_destination" "$codex_destination"; then
        return 1
    fi

    if [ ! -f "$canonical_source" ] || [ -L "$canonical_source" ]; then
        error "Managed copy source is not a regular file: $canonical_source"
        return 1
    fi
    if ! transaction_dir=$(mktemp -d \
            "$state_root/agent-contract-transaction.XXXXXX"); then
        error "Could not create agent contract transaction directory"
        return 1
    fi
    source_snapshot="$transaction_dir/WORKING_CONTRACT.md"
    claude_snapshot="$transaction_dir/CLAUDE.before"
    codex_snapshot="$transaction_dir/AGENTS.before"
    if ! cp -p "$canonical_source" "$source_snapshot"; then
        error "Could not snapshot canonical agent working contract"
        return 1
    fi

    if ! _preflight_copy_path "$source_snapshot" "$claude_destination"; then
        return 1
    fi
    if ! _preflight_copy_path "$source_snapshot" "$codex_destination"; then
        return 1
    fi
    if ! claude_generation=$(
            _managed_path_generation exact "$claude_destination"
        ) ||
            ! claude_payload_generation=$(
                _managed_path_generation payload "$claude_destination"
            ); then
        error "Could not capture Claude contract generation"
        return 1
    fi
    if ! _snapshot_copy_destination \
            "$claude_destination" "$claude_snapshot" \
            "$claude_generation" "$claude_payload_generation"; then
        return 1
    fi
    if ! codex_generation=$(
            _managed_path_generation exact "$codex_destination"
        ) ||
            ! codex_payload_generation=$(
                _managed_path_generation payload "$codex_destination"
            ); then
        error "Could not capture Codex contract generation"
        return 1
    fi
    if ! _snapshot_copy_destination \
            "$codex_destination" "$codex_snapshot" \
            "$codex_generation" "$codex_payload_generation"; then
        return 1
    fi
    rollback_ready_path="$transaction_dir/rollback-ready"
    if ! : > "$rollback_ready_path"; then
        error "Could not mark agent contract transaction ready"
        return 1
    fi
    if [ -f "$claude_snapshot" ] && [ ! -L "$claude_snapshot" ] &&
            [ -f "$codex_snapshot" ] && [ ! -L "$codex_snapshot" ]; then
        if ! _fsync_transaction_paths \
                "$source_snapshot" \
                "$claude_snapshot" \
                "$codex_snapshot" \
                "$rollback_ready_path" \
                "$transaction_dir" \
                "$state_root"; then
            error "Could not persist agent contract transaction journal"
            return 1
        fi
    elif [ -f "$claude_snapshot" ] && [ ! -L "$claude_snapshot" ]; then
        if ! _fsync_transaction_paths \
                "$source_snapshot" \
                "$claude_snapshot" \
                "$rollback_ready_path" \
                "$transaction_dir" \
                "$state_root"; then
            error "Could not persist agent contract transaction journal"
            return 1
        fi
    elif [ -f "$codex_snapshot" ] && [ ! -L "$codex_snapshot" ]; then
        if ! _fsync_transaction_paths \
                "$source_snapshot" \
                "$codex_snapshot" \
                "$rollback_ready_path" \
                "$transaction_dir" \
                "$state_root"; then
            error "Could not persist agent contract transaction journal"
            return 1
        fi
    elif ! _fsync_transaction_paths \
            "$source_snapshot" \
            "$rollback_ready_path" \
            "$transaction_dir" \
            "$state_root"; then
        error "Could not persist agent contract transaction journal"
        return 1
    fi
    rollback_ready=true

    if ! _copy_path \
            "$source_snapshot" "$claude_destination" \
            "$claude_generation"; then
        return 1
    fi
    if ! _copy_path \
            "$source_snapshot" "$codex_destination" \
            "$codex_generation"; then
        return 1
    fi

    committed=true
)
