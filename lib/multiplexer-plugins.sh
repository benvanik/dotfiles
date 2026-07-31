#!/bin/bash
# Publish one complete, clean tmux-plugin root after updating it off-path.
# The caller provides info/error plus the shared installer helpers.

update_multiplexer_plugins() (
    set -euo pipefail

    local plugin_root="$1"
    local tpm_origin="$2"
    local tpm_commit="$3"
    local plugin_parent
    local plugin_root_name
    local staging_root=""
    local payload_root
    local tpm_directory
    local plugin_directory
    local plugin_name
    local plugin_status
    local final_status
    local -a plugin_names=()
    local -a plugin_directories=()

    plugin_parent=$(dirname "$plugin_root")
    plugin_root_name=$(basename "$plugin_root")
    validate_managed_child_component \
        "$plugin_root_name" "tmux plugin-root name" || return 1
    prepare_managed_directory_root \
        "$plugin_parent" "tmux plugin parent" || return 1
    if [ -L "$plugin_root" ] ||
            { [ -e "$plugin_root" ] && [ ! -d "$plugin_root" ]; }; then
        error "Plugin root is not an ordinary directory: $plugin_root"
        return 1
    fi
    acquire_managed_installation_guard \
        "$plugin_parent" "$plugin_root_name" || return 1
    recover_managed_installation \
        "$plugin_parent" "$plugin_root_name" || return 1

    # shellcheck disable=SC2317  # Invoked by the EXIT trap below.
    cleanup_plugin_staging() {
        final_status=$?
        trap - EXIT HUP INT TERM
        if [ -n "$staging_root" ] &&
                { [ -e "$staging_root" ] || [ -L "$staging_root" ]; } &&
                ! remove_managed_tree "$plugin_parent" "$staging_root"; then
            error "Plugin staging directory requires inspection: $staging_root"
            final_status=1
        fi
        exit "$final_status"
    }
    trap cleanup_plugin_staging EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    audit_plugin_root() {
        local root="$1"
        local collect_names="${2:-false}"

        shopt -s nullglob dotglob
        plugin_directories=("$root"/*)
        shopt -u nullglob dotglob
        for plugin_directory in "${plugin_directories[@]}"; do
            plugin_name=$(basename "$plugin_directory")
            if [[ "$plugin_name" =~ ^\.publish-[A-Za-z0-9._+-]+\.guard$ ]]; then
                if [ ! -f "$plugin_directory" ] ||
                        [ -L "$plugin_directory" ]; then
                    error "Plugin publication guard is not an ordinary file: $plugin_directory"
                    return 1
                fi
                continue
            fi
            if [ -L "$plugin_directory" ] ||
                    [ ! -d "$plugin_directory" ]; then
                error "Plugin entry is not an ordinary directory: $plugin_directory"
                return 1
            fi
            validate_managed_child_component \
                "$plugin_name" "tmux plugin name" || return 1
            if [ ! -d "$plugin_directory/.git" ] ||
                    [ -L "$plugin_directory/.git" ]; then
                error "Plugin is not an ordinary Git checkout: $plugin_directory"
                return 1
            fi
            plugin_status=$(
                git -c core.fsmonitor=false -c core.hooksPath=/dev/null \
                    -C "$plugin_directory" status --porcelain=v1 \
                    --untracked-files=all --ignore-submodules=none
            ) || {
                error "Could not inspect plugin repository: $plugin_directory"
                return 1
            }
            if [ -n "$plugin_status" ]; then
                error "Plugin repository has local changes: $plugin_directory"
                return 1
            fi
            if [ "$collect_names" = "true" ] && [ "$plugin_name" != "tpm" ]; then
                plugin_names+=("$plugin_name")
            fi
        done
    }

    if [ -d "$plugin_root" ]; then
        audit_plugin_root "$plugin_root" false || return 1
    fi

    staging_root=$(
        create_managed_staging_directory "$plugin_parent" "$plugin_root_name"
    ) || return 1
    payload_root="$staging_root/payload"
    mkdir "$payload_root" || return 1
    if [ -d "$plugin_root" ]; then
        cp -a "$plugin_root/." "$payload_root/" || return 1
    fi

    tpm_directory="$payload_root/tpm"
    install_pinned_git_checkout \
        "TPM" "$tpm_origin" "$tpm_commit" "$tpm_directory" || return 1

    TMUX_PLUGIN_MANAGER_PATH="$payload_root/" \
        "$tpm_directory/bin/install_plugins" || return 1

    # TPM's "all" target includes TPM itself and would move the reviewed
    # checkout back to a branch tip. Update every other installed plugin by
    # exact directory name inside the staged root.
    plugin_names=()
    audit_plugin_root "$payload_root" true || return 1
    if [ "${#plugin_names[@]}" -gt 0 ]; then
        TMUX_PLUGIN_MANAGER_PATH="$payload_root/" \
            "$tpm_directory/bin/update_plugins" "${plugin_names[@]}" || return 1
    fi
    plugin_names=()
    audit_plugin_root "$payload_root" true || return 1
    if ! pinned_git_checkout_valid \
            "$tpm_directory" "$tpm_origin" "$tpm_commit"; then
        error "TPM changed while updating staged plugins"
        return 1
    fi

    publish_staged_child_directory \
        "$plugin_parent" "$plugin_root_name" "$payload_root" || return 1
    remove_managed_tree "$plugin_parent" "$staging_root" || return 1
    staging_root=""
)
