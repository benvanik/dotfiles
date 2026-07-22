# shellcheck shell=bash
# Load default tool versions when no explicit selection is active.
# Sourced from ~/.shrc.
# Uses 'local' which is supported by bash, zsh, and dash.

TOOLS_DIR="$HOME/tools"
[ -d "$TOOLS_DIR" ] || return 0

# Source platform utilities.
. "$HOME/.dotfiles/tools/platform.sh" 2>/dev/null || return 0

# Helper to load a tool by setting ROOT and sourcing env.sh.
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

    # Export the global default and let env.sh configure the tool.
    eval "export ${root_var}=\"\$(readlink -f \"$latest\")\""

    local env_file="$tool_dir/env.sh"
    [ -f "$env_file" ] && . "$env_file"
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
