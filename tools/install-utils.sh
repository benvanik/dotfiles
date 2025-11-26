#!/bin/bash
# Common utilities for tool install scripts.
# Source this from individual tool install.sh files.

# Colors.
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

# Tool name (set by caller before sourcing).
TOOL_NAME="${TOOL_NAME:-tool}"

info() { printf "${GREEN}[$TOOL_NAME]${NC} %s\n" "$1"; }
warn() { printf "${YELLOW}[$TOOL_NAME]${NC} %s\n" "$1"; }
error() { printf "${RED}[$TOOL_NAME]${NC} %s\n" "$1" >&2; }

# Directories.
TOOLS_DIR="${TOOLS_DIR:-$HOME/tools}"

# Platform detection.
detect_platform() {
    case "$(uname -s)" in
        Linux*)  echo "linux" ;;
        Darwin*) echo "darwin" ;;
        *)       echo "unknown" ;;
    esac
}

detect_arch() {
    case "$(uname -m)" in
        x86_64|amd64) echo "x86_64" ;;
        aarch64|arm64) echo "aarch64" ;;
        *)            echo "unknown" ;;
    esac
}

# Exported for use by scripts that source this file.
export PLATFORM
export ARCH
PLATFORM=$(detect_platform)
ARCH=$(detect_arch)

# Download with resume support.
download() {
    local url="$1"
    local output="$2"
    info "Downloading $(basename "$output")..."
    curl -L -C - --progress-bar -o "$output" "$url"
}

# Create or update latest symlink.
update_latest() {
    local tool_dir="$1"
    local version="$2"
    ln -sfn "$version" "$tool_dir/latest"
    info "Updated latest -> $version"
}

# Force reinstall (set by individual scripts when --force is passed).
FORCE="${FORCE:-false}"

# Check if version already installed (non-empty directory).
version_installed() {
    local tool_dir="$1"
    local version="$2"
    # Skip check if forcing.
    [ "$FORCE" = "true" ] && return 1
    # Check directory exists and is non-empty.
    [ -d "$tool_dir/$version" ] && [ -n "$(ls -A "$tool_dir/$version" 2>/dev/null)" ]
}

# Show usage for a tool installer.
show_install_usage() {
    local tool="$1"
    local version_help="${2:-VERSION}"
    cat << EOF
Usage: $tool/install.sh [$version_help]

Install $tool to ~/tools/$tool/<version>/

If no version specified, installs the latest.
EOF
}
