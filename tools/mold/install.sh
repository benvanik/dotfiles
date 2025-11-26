#!/bin/bash
# Install Mold linker from GitHub releases.
# Usage: mold/install.sh [version]
set -e

TOOL_NAME="mold"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../install-utils.sh"

# Platform check - mold is Linux only.
if [ "$PLATFORM" != "linux" ]; then
    error "Mold is only available for Linux"
    exit 1
fi

MOLD_DIR="$TOOLS_DIR/mold"

# Handle flags.
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            show_install_usage "mold" "VERSION"
            echo ""
            echo "Downloads pre-built Mold linker from GitHub releases."
            echo "Example: mold/install.sh 2.35.1"
            echo ""
            echo "Options:"
            echo "  --force    Reinstall even if version exists"
            echo ""
            echo "Releases: https://github.com/rui314/mold/releases"
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
    VERSION=$(curl -s https://api.github.com/repos/rui314/mold/releases/latest | grep '"tag_name"' | sed -E 's/.*"v([^"]+)".*/\1/')
    if [ -z "$VERSION" ]; then
        error "Failed to fetch latest version. Specify manually: mold/install.sh 2.35.1"
        exit 1
    fi
else
    VERSION="$1"
fi

info "Installing Mold $VERSION"

# Create directory.
mkdir -p "$MOLD_DIR"
cd "$MOLD_DIR"

# Check if already installed.
if version_installed "$MOLD_DIR" "$VERSION"; then
    warn "Version $VERSION already installed"
    update_latest "$MOLD_DIR" "$VERSION"
    exit 0
fi

# Determine download URL.
if [ "$ARCH" = "x86_64" ]; then
    TARBALL="mold-$VERSION-x86_64-linux.tar.gz"
elif [ "$ARCH" = "aarch64" ]; then
    TARBALL="mold-$VERSION-aarch64-linux.tar.gz"
else
    error "Unsupported architecture: $ARCH"
    exit 1
fi

URL="https://github.com/rui314/mold/releases/download/v$VERSION/$TARBALL"

# Download.
download "$URL" "$TARBALL"

# Extract.
info "Extracting..."
tar xf "$TARBALL"

# Rename extracted directory to version.
EXTRACTED=$(ls -d mold-$VERSION* 2>/dev/null | head -1)
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
update_latest "$MOLD_DIR" "$VERSION"

# Cleanup tarball.
rm -f "$TARBALL"

# Create env.sh if it doesn't exist.
if [ ! -f "env.sh" ]; then
    cat > env.sh << 'EOF'
# Mold linker environment.
# Sourced by direnvrc when using use_mold.
if [ -n "$MOLD_ROOT" ]; then
    export PATH="$MOLD_ROOT/bin:$PATH"
    # Set linker flags to use mold.
    export LDFLAGS="-fuse-ld=mold ${LDFLAGS:-}"
fi
EOF
    info "Created env.sh"
fi

info "Mold $VERSION installed successfully!"
