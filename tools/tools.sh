# shellcheck shell=bash
# Load default tool versions for interactive shells.
# Sourced from ~/.shrc.
# Uses 'local' which is supported by bash, zsh, and dash.

TOOLS_DIR="$HOME/tools"
[ -d "$TOOLS_DIR" ] || return 0

# Source platform utilities.
. "$HOME/.dotfiles/tools/platform.sh" 2>/dev/null || return 0

# Helper to add tool to PATH and source env.sh.
_load_tool() {
    local tool="$1"
    local tool_dir="$TOOLS_DIR/$tool"
    local latest="$tool_dir/latest"

    # Skip if no latest symlink.
    [ -L "$latest" ] || return 0

    # Add bin to PATH.
    _add_path "$latest/bin"

    # Export root variable and source env.sh if exists.
    local root_var
    root_var="$(echo "$tool" | tr '[:lower:]' '[:upper:]')_ROOT"
    eval "export ${root_var}=\"\$(readlink -f \"$latest\")\""

    local env_file="$tool_dir/env.sh"
    [ -f "$env_file" ] && . "$env_file"
}

# Load core development tools (latest versions).
for tool in llvm cmake ninja mold vulkan; do
    _load_tool "$tool"
done

# ROCm only on Linux (silent skip otherwise).
if _platform_supports rocm; then
    _load_tool rocm
fi

unset -f _load_tool
