#!/bin/bash
# Machine-local Bazel cache placement and default-root protection.

BAZEL_CACHE_MANAGED_HOME_RC_HEADER='# Managed by dotfiles. Set BAZEL_CACHE_ROOT in ~/.shrc.local.'
BAZEL_CACHE_GUARD_MARKER=USE_THE_WORKTREE_BAZELRC
BAZEL_CACHE_PROJECT_RC=.bazelrc.cache

bazel_cache_configured_root() {
    local machine_config="$HOME/.shrc.local"

    if [ -n "${BAZEL_CACHE_ROOT:-}" ]; then
        printf '%s\n' "$BAZEL_CACHE_ROOT"
        return 0
    fi
    if [ -L "$machine_config" ] || [ ! -f "$machine_config" ]; then
        return 1
    fi

    (
        unset BAZEL_CACHE_ROOT
        # Machine configuration may use this normal shell helper for unrelated
        # paths. Cache discovery needs values, not PATH mutation.
        # shellcheck disable=SC2317
        _add_path() { :; }
        # shellcheck disable=SC1090
        . "$machine_config"
        [ -n "${BAZEL_CACHE_ROOT:-}" ] || exit 1
        printf '%s\n' "$BAZEL_CACHE_ROOT"
    )
}

bazel_cache_user_name() {
    local user_name="${USER:-}"
    if [ -z "$user_name" ]; then
        user_name=$(id -un) || return 1
    fi
    if [[ ! "$user_name" =~ ^[A-Za-z0-9._-]+$ ]]; then
        error "Bazel cache user name is not path-safe: $user_name"
        return 1
    fi
    printf '%s\n' "$user_name"
}

bazel_cache_validate_root() {
    local cache_root="$1"

    if [ -z "$cache_root" ]; then
        error "BAZEL_CACHE_ROOT is not configured"
        return 1
    fi
    case "$cache_root" in
        /*) ;;
        *)
            error "BAZEL_CACHE_ROOT must be absolute: $cache_root"
            return 1
            ;;
    esac
    case "$cache_root" in
        *$'\n'*|*$'\r'*|*[[:space:]]*)
            error "BAZEL_CACHE_ROOT cannot contain whitespace: $cache_root"
            return 1
            ;;
    esac

    if ! python3 - "$cache_root" "$HOME" << 'PY'
import os
import sys

cache_root = os.path.realpath(sys.argv[1])
home = os.path.realpath(sys.argv[2])
if os.path.commonpath((cache_root, home)) == home:
    raise SystemExit(1)
PY
    then
        error "BAZEL_CACHE_ROOT must resolve outside HOME: $cache_root"
        return 1
    fi
}

bazel_cache_output_user_root() {
    local cache_root="$1"
    local user_name
    user_name=$(bazel_cache_user_name) || return 1
    printf '%s/_bazel_%s\n' "$cache_root" "$user_name"
}

bazel_cache_default_output_user_root() {
    local cache_home="${XDG_CACHE_HOME:-$HOME/.cache}"
    local user_name
    user_name=$(bazel_cache_user_name) || return 1
    printf '%s/bazel/_bazel_%s\n' "$cache_home" "$user_name"
}

bazel_cache_print_home_bazelrc() {
    local cache_root="$1"
    local output_user_root
    output_user_root=$(bazel_cache_output_user_root "$cache_root") || return 1

    printf '%s\n' "$BAZEL_CACHE_MANAGED_HOME_RC_HEADER"
    printf 'startup --output_user_root=%s\n' "$output_user_root"
    printf 'build --disk_cache=%s/cache/disk\n' "$output_user_root"
    printf 'try-import %%workspace%%/%s\n' "$BAZEL_CACHE_PROJECT_RC"
}

bazel_cache_render_home_bazelrc() {
    local output_path="$1"
    local cache_root="$2"
    bazel_cache_print_home_bazelrc "$cache_root" > "$output_path"
}

bazel_cache_print_guard_marker() {
    local cache_root="$1"

    cat << EOF
This default Bazel output user root is intentionally read-only.

Projects using the project infrastructure place cache location in the primary
worktree's .bazelrc.cache and other machine-local Bazel policy in
.bazelrc.local. Create siblings with project-worktree-init so both files are
linked into every worktree.

All projects inherit ~/.bazelrc and place default Bazel state under this
machine's configured cache root:
  $cache_root

Projects that deliberately disable the home rc must provide their own
non-HOME --output_user_root.

Set BAZEL_CACHE_ROOT in ~/.shrc.local when the machine's cache filesystem
changes, then run dotfiles install. Run dotfiles doctor to verify the guard.
EOF
}

bazel_cache_render_guard_marker() {
    local output_path="$1"
    local cache_root="$2"
    bazel_cache_print_guard_marker "$cache_root" > "$output_path"
}

bazel_cache_default_root_state_count() (
    local default_output_user_root="$1"
    local candidate=""
    local candidate_name=""
    local count=0

    shopt -s dotglob nullglob
    for candidate in "$default_output_user_root"/*; do
        candidate_name=$(basename "$candidate")
        [ "$candidate_name" = "$BAZEL_CACHE_GUARD_MARKER" ] ||
            count=$((count + 1))
    done
    printf '%d\n' "$count"
)

# Publishes fallback cache placement and protects Bazel's default HOME root.
# bin/dotfiles supplies the local-file publication and fsync helpers used here.
bazel_cache_configure() (
    local cache_root="$1"
    local home_bazelrc="$HOME/.bazelrc"
    local output_user_root=""
    local default_output_user_root=""
    local marker_path=""
    local marker_staging=""
    local guard_unlocked=false

    # Invoked indirectly by the EXIT trap below.
    # shellcheck disable=SC2317
    cleanup_bazel_cache_configuration() {
        local exit_status=$?
        trap - EXIT
        trap '' HUP INT TERM
        if [ -n "$marker_staging" ] &&
                { [ -e "$marker_staging" ] || [ -L "$marker_staging" ]; } &&
                ! unlink "$marker_staging"; then
            error "Could not clean Bazel guard staging: $marker_staging"
            exit_status=1
        fi
        if [ "$guard_unlocked" = true ] &&
                [ -n "$default_output_user_root" ] &&
                [ -d "$default_output_user_root" ] &&
                ! chmod 0555 "$default_output_user_root"; then
            error "Could not restore Bazel default-root protection"
            exit_status=1
        fi
        exit "$exit_status"
    }
    trap cleanup_bazel_cache_configuration EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    bazel_cache_validate_root "$cache_root" || return 1
    output_user_root=$(bazel_cache_output_user_root "$cache_root") || return 1

    if [ -L "$home_bazelrc" ]; then
        error "Refusing symlinked machine Bazel configuration: $home_bazelrc"
        return 1
    elif [ ! -f "$home_bazelrc" ]; then
        if [ -e "$home_bazelrc" ]; then
            error "Machine Bazel configuration is not a regular file: $home_bazelrc"
            return 1
        fi
    elif [ "$(sed -n '1p' "$home_bazelrc")" != \
            "$BAZEL_CACHE_MANAGED_HOME_RC_HEADER" ]; then
        error "Refusing unmanaged machine Bazel configuration: $home_bazelrc"
        return 1
    fi

    default_output_user_root=$(bazel_cache_default_output_user_root) || return 1
    if [ -L "$default_output_user_root" ]; then
        error "Refusing symlinked Bazel default output root: $default_output_user_root"
        return 1
    elif [ -e "$default_output_user_root" ] &&
            [ ! -d "$default_output_user_root" ]; then
        error "Bazel default output root is not a directory: $default_output_user_root"
        return 1
    fi
    marker_path="$default_output_user_root/$BAZEL_CACHE_GUARD_MARKER"
    if [ -L "$marker_path" ] ||
            { [ -e "$marker_path" ] && [ ! -f "$marker_path" ]; }; then
        error "Invalid Bazel guard marker: $marker_path"
        return 1
    fi

    if ! mkdir -p "$output_user_root/cache/disk"; then
        error "Could not create Bazel cache root: $output_user_root"
        return 1
    fi
    if [ ! -e "$home_bazelrc" ]; then
        _create_local_file_locked \
            "$home_bazelrc" 644 bazel_cache_render_home_bazelrc "$cache_root" ||
            return 1
    elif [ "$(cat "$home_bazelrc")" != \
            "$(bazel_cache_print_home_bazelrc "$cache_root")" ]; then
        _replace_local_file_locked \
            "$home_bazelrc" bazel_cache_render_home_bazelrc "$cache_root" ||
            return 1
    fi
    if ! mkdir -p "$default_output_user_root"; then
        error "Could not create Bazel default output root: $default_output_user_root"
        return 1
    fi

    if ! chmod 0755 "$default_output_user_root"; then
        error "Could not unlock Bazel default output root for configuration"
        return 1
    fi
    guard_unlocked=true
    if ! marker_staging=$(mktemp \
            "$default_output_user_root/.bazel-cache-guard.XXXXXX"); then
        error "Could not stage Bazel guard marker"
        return 1
    fi
    if ! bazel_cache_render_guard_marker "$marker_staging" "$cache_root" ||
            ! chmod 0444 "$marker_staging" ||
            ! mv -f "$marker_staging" "$marker_path"; then
        error "Could not publish Bazel guard marker: $marker_path"
        return 1
    fi
    marker_staging=""
    if ! _fsync_transaction_paths \
            "$marker_path" "$default_output_user_root"; then
        error "Could not persist Bazel guard marker"
        return 1
    fi
    if ! chmod 0555 "$default_output_user_root"; then
        error "Could not protect Bazel default output root"
        return 1
    fi
    guard_unlocked=false
)
