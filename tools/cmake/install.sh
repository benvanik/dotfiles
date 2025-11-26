#!/bin/bash
# Install CMake from GitHub releases.
# Usage: cmake/install.sh [version]
set -e

TOOL_NAME="cmake"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../install-utils.sh"

CMAKE_DIR="$TOOLS_DIR/cmake"

# Handle flags.
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            show_install_usage "cmake" "VERSION"
            echo ""
            echo "Downloads pre-built CMake from GitHub releases."
            echo "Example: cmake/install.sh 3.31.7"
            echo ""
            echo "Options:"
            echo "  --force    Reinstall even if version exists"
            echo ""
            echo "Releases: https://github.com/Kitware/CMake/releases"
            exit 0
            ;;
        -f|--force)
            FORCE=true
            shift
            ;;
        *)
            break
            ;;
    esac
done

# Get version.
if [ -z "$1" ]; then
    info "Fetching latest version..."
    VERSION=$(curl -s https://api.github.com/repos/Kitware/CMake/releases/latest | grep '"tag_name"' | sed -E 's/.*"v([^"]+)".*/\1/')
    if [ -z "$VERSION" ]; then
        error "Failed to fetch latest version. Specify manually: cmake/install.sh 3.31.7"
        exit 1
    fi
else
    VERSION="$1"
fi

info "Installing CMake $VERSION"

# Create directory.
mkdir -p "$CMAKE_DIR"
cd "$CMAKE_DIR"

# Check if already installed.
if version_installed "$CMAKE_DIR" "$VERSION"; then
    warn "Version $VERSION already installed"
    update_latest "$CMAKE_DIR" "$VERSION"
    exit 0
fi

# Determine download URL based on platform/arch.
if [ "$PLATFORM" = "linux" ] && [ "$ARCH" = "x86_64" ]; then
    TARBALL="cmake-$VERSION-linux-x86_64.tar.gz"
elif [ "$PLATFORM" = "linux" ] && [ "$ARCH" = "aarch64" ]; then
    TARBALL="cmake-$VERSION-linux-aarch64.tar.gz"
elif [ "$PLATFORM" = "darwin" ]; then
    TARBALL="cmake-$VERSION-macos-universal.tar.gz"
else
    error "Unsupported platform: $PLATFORM/$ARCH"
    exit 1
fi

URL="https://github.com/Kitware/CMake/releases/download/v$VERSION/$TARBALL"

# Download.
download "$URL" "$TARBALL"

# Extract.
info "Extracting..."
tar xf "$TARBALL"

# Rename extracted directory to version.
EXTRACTED=$(ls -d cmake-$VERSION* 2>/dev/null | head -1)
if [ -n "$EXTRACTED" ] && [ "$EXTRACTED" != "$VERSION" ]; then
    mv "$EXTRACTED" "$VERSION"
fi

# Verify extraction.
if [ ! -d "$VERSION" ]; then
    error "Extraction failed - directory $VERSION not found"
    rm -f "$TARBALL"
    exit 1
fi

# Update latest symlink.
update_latest "$CMAKE_DIR" "$VERSION"

# Cleanup tarball.
rm -f "$TARBALL"

info "CMake $VERSION installed successfully!"
