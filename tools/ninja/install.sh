#!/bin/bash
# Install Ninja from GitHub releases.
# Usage: ninja/install.sh [version]
set -e

TOOL_NAME="ninja"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../install-utils.sh"

NINJA_DIR="$TOOLS_DIR/ninja"

# Handle flags.
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            show_install_usage "ninja" "VERSION"
            echo ""
            echo "Downloads pre-built Ninja from GitHub releases."
            echo "Example: ninja/install.sh 1.12.1"
            echo ""
            echo "Options:"
            echo "  --force    Reinstall even if version exists"
            echo ""
            echo "Releases: https://github.com/ninja-build/ninja/releases"
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
    VERSION=$(curl -s https://api.github.com/repos/ninja-build/ninja/releases/latest | grep '"tag_name"' | sed -E 's/.*"v([^"]+)".*/\1/')
    if [ -z "$VERSION" ]; then
        error "Failed to fetch latest version. Specify manually: ninja/install.sh 1.12.1"
        exit 1
    fi
else
    VERSION="$1"
fi

info "Installing Ninja $VERSION"

# Create directory.
mkdir -p "$NINJA_DIR"
cd "$NINJA_DIR"

# Check if already installed.
if version_installed "$NINJA_DIR" "$VERSION"; then
    warn "Version $VERSION already installed"
    update_latest "$NINJA_DIR" "$VERSION"
    exit 0
fi

# Determine download URL based on platform.
if [ "$PLATFORM" = "linux" ]; then
    ZIPFILE="ninja-linux.zip"
elif [ "$PLATFORM" = "darwin" ]; then
    ZIPFILE="ninja-mac.zip"
else
    error "Unsupported platform: $PLATFORM"
    exit 1
fi

URL="https://github.com/ninja-build/ninja/releases/download/v$VERSION/$ZIPFILE"

# Download.
download "$URL" "$ZIPFILE"

# Create version directory and extract.
mkdir -p "$VERSION/bin"
info "Extracting..."
unzip -q -o "$ZIPFILE" -d "$VERSION/bin"
chmod +x "$VERSION/bin/ninja"

# Update latest symlink.
update_latest "$NINJA_DIR" "$VERSION"

# Cleanup.
rm -f "$ZIPFILE"

info "Ninja $VERSION installed successfully!"
