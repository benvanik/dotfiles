#!/bin/bash
# Shared exclusion for benchmark lock state and machine-knob transactions.

benchmark_acquire_transaction_guard() {
    local state_directory="$1"
    local owner_uid="$2"
    local owner_gid="$3"
    local guard_file="${BENCHMARK_LOCK_GUARD_FILE:-${state_directory}.guard}"
    local guard_parent=""
    local prior_umask=""

    if ! command -v flock >/dev/null 2>&1; then
        printf 'benchmark transaction requires flock\n' >&2
        return 1
    fi
    if [ -L "$guard_file" ] ||
            { [ -e "$guard_file" ] && [ ! -f "$guard_file" ]; }; then
        printf 'benchmark transaction guard is not a regular file: %s\n' \
            "$guard_file" >&2
        return 1
    fi
    guard_parent=$(dirname "$guard_file") || return 1
    if [ -L "$guard_parent" ] ||
            { [ -e "$guard_parent" ] && [ ! -d "$guard_parent" ]; }; then
        printf 'benchmark transaction guard parent is not a directory: %s\n' \
            "$guard_parent" >&2
        return 1
    fi
    mkdir -p "$guard_parent" || return 1

    prior_umask=$(umask)
    umask 077
    if ! exec 8>>"$guard_file"; then
        umask "$prior_umask"
        printf 'could not open benchmark transaction guard: %s\n' \
            "$guard_file" >&2
        return 1
    fi
    umask "$prior_umask"

    # Revalidate the pathname after opening it so a redirect cannot silently
    # establish a different lock domain.
    if [ -L "$guard_file" ] || [ ! -f "$guard_file" ]; then
        exec 8>&-
        printf 'benchmark transaction guard changed while opening: %s\n' \
            "$guard_file" >&2
        return 1
    fi
    if [ "$(id -u)" -eq 0 ] &&
            [ -n "$owner_uid" ] && [ -n "$owner_gid" ]; then
        chown "$owner_uid:$owner_gid" "$guard_file" || {
            exec 8>&-
            return 1
        }
    fi
    chmod 600 "$guard_file" || {
        exec 8>&-
        return 1
    }
    if ! flock -x 8; then
        exec 8>&-
        printf 'could not lock benchmark transaction guard: %s\n' \
            "$guard_file" >&2
        return 1
    fi
    BENCHMARK_LOCK_GUARD_FILE="$guard_file"
    export BENCHMARK_LOCK_GUARD_FILE
}
