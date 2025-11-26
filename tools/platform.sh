# shellcheck shell=bash
# Platform detection utilities.
# Sourced by tools.sh and direnvrc.
# Uses 'local' which is supported by bash, zsh, and dash.

_detect_os() {
    case "$(uname -s)" in
        Linux)
            if grep -qi microsoft /proc/version 2>/dev/null; then
                echo "wsl"
            else
                echo "linux"
            fi
            ;;
        Darwin) echo "darwin" ;;
        *) echo "unknown" ;;
    esac
}

_detect_arch() {
    case "$(uname -m)" in
        x86_64|amd64) echo "x86_64" ;;
        aarch64|arm64) echo "arm64" ;;
        *) echo "unknown" ;;
    esac
}

# Check if platform supports tool (silent skip if not).
_platform_supports() {
    local tool="$1"
    case "$tool" in
        rocm) [ "$(_detect_os)" = "linux" ] ;;
        *) return 0 ;;
    esac
}

# Export detected values (cache to avoid repeated detection).
TOOLS_OS="${TOOLS_OS:-$(_detect_os)}"
TOOLS_ARCH="${TOOLS_ARCH:-$(_detect_arch)}"
export TOOLS_OS TOOLS_ARCH
