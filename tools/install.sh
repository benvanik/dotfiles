#!/bin/bash
# Master tool installer - installs all supported tools for the current platform.
# Usage: tools/install.sh [tool] [tool-args...]
#
# Without arguments, shows available tools.
# With a tool name, runs that tool's installer.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export TOOL_NAME="tools"
source "$SCRIPT_DIR/install-utils.sh"

# List of tools with platform support.
# Format: tool:platforms (linux, darwin, all)
TOOLS=(
    "bazel:all"
    "cmake:all"
    "llvm:all"
    "mold:linux"
    "ninja:all"
    "nvm:all"
    "rocm:linux"
    "vulkan:linux"
)

# Check if tool is supported on current platform.
tool_supported() {
    local tool="$1"
    local platforms="$2"
    [ "$platforms" = "all" ] || [[ "$platforms" == *"$PLATFORM"* ]]
}

# Get list of supported tools for current platform.
get_supported_tools() {
    for entry in "${TOOLS[@]}"; do
        local tool="${entry%%:*}"
        local platforms="${entry##*:}"
        if tool_supported "$tool" "$platforms"; then
            echo "$tool"
        fi
    done
}

# Show help.
show_help() {
    cat << EOF
tools/install.sh - Install development tools

USAGE
    tools/install.sh                    Show available tools
    tools/install.sh <tool> [args...]   Install specific tool
    tools/install.sh --all              Install all supported tools

AVAILABLE TOOLS (on $PLATFORM)
EOF
    for entry in "${TOOLS[@]}"; do
        local tool="${entry%%:*}"
        local platforms="${entry##*:}"
        if tool_supported "$tool" "$platforms"; then
            printf "    %-12s\n" "$tool"
        fi
    done
    cat << EOF

EXAMPLES
    tools/install.sh cmake              Install latest CMake
    tools/install.sh llvm               Install latest LLVM
    tools/install.sh llvm 21.1.6        Install specific LLVM version
    tools/install.sh ninja              Install latest Ninja
    tools/install.sh --all              Install all tools (fetches latest versions)

TOOL HELP
    tools/install.sh <tool> --help      Show tool-specific options
EOF
}

# Install all supported tools.
install_all() {
    info "Installing all supported tools for $PLATFORM..."
    echo ""

    local failed_tools=()
    for tool in $(get_supported_tools); do
        info "Installing $tool..."
        if ! "$SCRIPT_DIR/$tool/install.sh"; then
            warn "Failed to install $tool"
            failed_tools+=("$tool")
        fi
        echo ""
    done

    if [ ${#failed_tools[@]} -gt 0 ]; then
        error "Failed to install: ${failed_tools[*]}"
        exit 1
    fi

    info "Done!"
}

# Main.
case "${1:-}" in
    -h|--help)
        show_help
        exit 0
        ;;
    --all)
        install_all
        exit 0
        ;;
    "")
        show_help
        exit 0
        ;;
    *)
        TOOL="$1"
        shift

        # Check if tool exists.
        if [ ! -f "$SCRIPT_DIR/$TOOL/install.sh" ]; then
            error "Unknown tool: $TOOL"
            echo ""
            echo "Available tools:"
            get_supported_tools | sed 's/^/  /'
            exit 1
        fi

        # Check platform support.
        for entry in "${TOOLS[@]}"; do
            local_tool="${entry%%:*}"
            local_platforms="${entry##*:}"
            if [ "$local_tool" = "$TOOL" ]; then
                if ! tool_supported "$TOOL" "$local_platforms"; then
                    error "$TOOL is not supported on $PLATFORM"
                    exit 1
                fi
                break
            fi
        done

        # Run tool installer.
        exec "$SCRIPT_DIR/$TOOL/install.sh" "$@"
        ;;
esac
