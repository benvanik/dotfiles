# shellcheck shell=bash
# Load default tool versions when no explicit selection is active.
# Sourced from ~/.shrc.
# Uses 'local' which is supported by bash, zsh, and dash.

TOOLS_DIR="$HOME/tools"
[ -d "$TOOLS_DIR" ] || return 0

# Source platform utilities.
if [ ! -f "$HOME/.dotfiles/tools/platform.sh" ] ||
        ! . "$HOME/.dotfiles/tools/platform.sh"; then
    printf '\033[31m[tools]\033[0m Could not load platform helpers\n' >&2
    return 1
fi
if [ ! -f "$HOME/.dotfiles/tools/versions.sh" ] ||
        ! . "$HOME/.dotfiles/tools/versions.sh"; then
    printf '\033[31m[tools]\033[0m Could not load version helpers\n' >&2
    return 1
fi

# Helper to load a tool by setting ROOT and sourcing its tracked environment.
_load_tool() {
    local tool="$1"
    local tool_dir="$TOOLS_DIR/$tool"
    local latest="$tool_dir/latest"

    # An existing root is an explicit selection, usually made by direnv. Keep
    # that selection while still applying the repository-owned environment in
    # this shell. A selected root does not require a default latest link.
    local root_var
    local current_root
    local tool_root
    root_var="$(echo "$tool" | tr '[:lower:]' '[:upper:]')_ROOT"
    eval "current_root=\${${root_var}:-}"

    if [ -n "$current_root" ]; then
        tool_root="$current_root"
    else
        # Skip an uninstalled default tool.
        [ -L "$latest" ] || return 0
        if ! tool_root=$(_find_version "$tool_dir" latest); then
            printf '\033[31m[tools]\033[0m Broken latest link: %s\n' \
                "$latest" >&2
            return 1
        fi
        export "$root_var=$tool_root"
    fi

    local environment_file="$HOME/.dotfiles/tools/$tool/env.sh"
    if [ ! -f "$environment_file" ]; then
        printf '\033[31m[tools]\033[0m Missing environment: %s\n' \
            "$environment_file" >&2
        return 1
    fi
    if ! . "$environment_file"; then
        printf '\033[31m[tools]\033[0m Could not activate %s from %s\n' \
            "$tool" "$tool_root" >&2
        return 1
    fi
}

# Load core development tools (latest versions).
_tools_load_failed=0
for tool in bazel llvm cmake ninja vulkan; do
    if _platform_supports "$tool" && ! _load_tool "$tool"; then
        _tools_load_failed=1
    fi
done

# CUDA and ROCm only on Linux (silent skip otherwise).
if _platform_supports cuda; then
    if ! _load_tool cuda; then
        _tools_load_failed=1
    fi
fi
if _platform_supports rocm; then
    if ! _load_tool rocm; then
        _tools_load_failed=1
    fi
fi

unset -f _load_tool
if [ "$_tools_load_failed" -ne 0 ]; then
    unset _tools_load_failed
    return 1
fi
unset _tools_load_failed
