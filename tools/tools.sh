# shellcheck shell=bash
# Load default tool versions when no explicit selection is active.
# Sourced from ~/.shrc.
# Uses 'local' which is supported by bash, zsh, and dash.

TOOLS_DIR="$HOME/tools"
[ -d "$TOOLS_DIR" ] || return 0

# Source platform utilities.
. "$HOME/.dotfiles/tools/platform.sh" 2>/dev/null || return 0
. "$HOME/.dotfiles/tools/versions.sh" 2>/dev/null || return 0

# Helper to load a tool by setting ROOT and sourcing its tracked environment.
_load_tool() {
    local tool="$1"
    local tool_dir="$TOOLS_DIR/$tool"
    local latest="$tool_dir/latest"

    # Skip if no latest symlink.
    [ -L "$latest" ] || return 0

    # An existing root is an explicit selection, usually made by direnv. It
    # must survive ~/.shrc being sourced again through BASH_ENV in child shells.
    local root_var
    local current_root
    root_var="$(echo "$tool" | tr '[:lower:]' '[:upper:]')_ROOT"
    eval "current_root=\${${root_var}:-}"
    [ -n "$current_root" ] && return 0

    local tool_root
    if ! tool_root=$(_find_version "$tool_dir" latest); then
        printf '\033[31m[tools]\033[0m Broken latest link: %s\n' "$latest" >&2
        return 1
    fi
    export "$root_var=$tool_root"

    local environment_file="$HOME/.dotfiles/tools/$tool/env.sh"
    if [ ! -f "$environment_file" ]; then
        printf '\033[31m[tools]\033[0m Missing environment: %s\n' \
            "$environment_file" >&2
        return 1
    fi
    . "$environment_file"
}

# Load core development tools (latest versions).
for tool in bazel llvm cmake ninja vulkan; do
    _load_tool "$tool"
done

# ROCm only on Linux (silent skip otherwise).
if _platform_supports rocm; then
    _load_tool rocm
fi

unset -f _load_tool
