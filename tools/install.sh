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

# List of tools with exact supported host targets. This is the authoritative
# filter for help, exact dispatch, and --all; it must match each installer's
# artifact matrix.
ALL_HOST_TARGETS="linux/x86_64,linux/aarch64,darwin/x86_64,darwin/aarch64"
TOOLS=(
    "bazel:$ALL_HOST_TARGETS"
    "beads:$ALL_HOST_TARGETS"
    "cmake:$ALL_HOST_TARGETS"
    "cuda:linux/x86_64,linux/aarch64"
    "hf:$ALL_HOST_TARGETS"
    "llvm:linux/x86_64,linux/aarch64,darwin/aarch64"
    "mold:linux/x86_64,linux/aarch64"
    "ninja:$ALL_HOST_TARGETS"
    "nix:linux/x86_64,linux/aarch64,darwin/aarch64"
    "nvm:$ALL_HOST_TARGETS"
    "rocm:linux/x86_64"
    "vulkan:linux/x86_64"
)

# Tools in this list remain available by exact name but are never ambient
# defaults and are not included in --all.
EXPLICIT_ONLY_TOOLS=(
    "mold"
)

# Check if a metadata target set includes the current platform and architecture.
tool_supported() {
    local supported_targets="$1"
    local target

    for target in ${supported_targets//,/ }; do
        [ "$target" != "$PLATFORM/$ARCH" ] || return 0
    done
    return 1
}

# Get list of supported tools for current platform.
tool_is_explicit_only() {
    local requested_tool="$1"
    local explicit_tool
    for explicit_tool in "${EXPLICIT_ONLY_TOOLS[@]}"; do
        [ "$requested_tool" != "$explicit_tool" ] || return 0
    done
    return 1
}

get_supported_tools() {
    local include_explicit="${1:-true}"
    for entry in "${TOOLS[@]}"; do
        local tool="${entry%%:*}"
        local supported_targets="${entry##*:}"
        if [ "$include_explicit" != "true" ] &&
                tool_is_explicit_only "$tool"; then
            continue
        fi
        if tool_supported "$supported_targets"; then
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

AVAILABLE TOOLS (on $PLATFORM/$ARCH)
EOF
    for entry in "${TOOLS[@]}"; do
        local tool="${entry%%:*}"
        local supported_targets="${entry##*:}"
        if tool_supported "$supported_targets"; then
            if tool_is_explicit_only "$tool"; then
                printf "    %-12s %s\n" "$tool" "(explicit only)"
            else
                printf "    %-12s\n" "$tool"
            fi
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
    info "Installing all supported tools for $PLATFORM/$ARCH..."
    echo ""

    local failed_tools=()
    for tool in $(get_supported_tools false); do
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
main() {
    local entry
    local registered_tool
    local requested_tool
    local supported_targets=""

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
            requested_tool="$1"
            shift

            for entry in "${TOOLS[@]}"; do
                registered_tool="${entry%%:*}"
                if [ "$registered_tool" = "$requested_tool" ]; then
                    supported_targets="${entry##*:}"
                    break
                fi
            done
            if [ -z "$supported_targets" ]; then
                error "Unknown tool: $requested_tool"
                echo ""
                echo "Available tools:"
                get_supported_tools | sed 's/^/  /'
                exit 1
            fi

            if ! tool_supported "$supported_targets"; then
                error "$requested_tool is not supported on $PLATFORM/$ARCH"
                exit 1
            fi
            if [ ! -f "$SCRIPT_DIR/$requested_tool/install.sh" ] ||
                    [ -L "$SCRIPT_DIR/$requested_tool/install.sh" ]; then
                error "Supported installer is missing or not a regular file: $requested_tool"
                exit 1
            fi

            # Run tool installer.
            exec "$SCRIPT_DIR/$requested_tool/install.sh" "$@"
            ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
