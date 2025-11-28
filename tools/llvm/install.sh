#!/bin/bash
# Install LLVM/Clang from GitHub releases.
# Usage: llvm/install.sh [version]
set -e

TOOL_NAME="llvm"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../install-utils.sh"

LLVM_DIR="$TOOLS_DIR/llvm"

# Fetch latest LLVM version from GitHub releases.
# Sets FETCHED_VERSION on success.
fetch_latest_version() {
    info "Fetching latest LLVM version..."
    FETCHED_VERSION=$(curl -fsSL "https://api.github.com/repos/llvm/llvm-project/releases/latest" | \
        grep '"tag_name"' | sed -E 's/.*"llvmorg-([^"]+)".*/\1/')
    if [ -z "$FETCHED_VERSION" ]; then
        error "Failed to fetch latest version"
        exit 1
    fi
}

# Handle flags.
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            show_install_usage "llvm" "[VERSION]"
            echo ""
            echo "Downloads pre-built LLVM/Clang from GitHub releases."
            echo "Without a version, installs the latest release."
            echo ""
            echo "Examples:"
            echo "  llvm/install.sh           # Install latest"
            echo "  llvm/install.sh 21.1.6    # Install specific version"
            echo ""
            echo "Options:"
            echo "  --force    Reinstall even if version exists"
            echo ""
            echo "Releases: https://github.com/llvm/llvm-project/releases"
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

# Get version (fetch latest if not specified).
if [ -z "$1" ]; then
    fetch_latest_version
    VERSION="$FETCHED_VERSION"
else
    VERSION="$1"
fi

info "Installing LLVM $VERSION"

# Create directory.
mkdir -p "$LLVM_DIR"
cd "$LLVM_DIR"

# Check if already installed.
if version_installed "$LLVM_DIR" "$VERSION"; then
    warn "Version $VERSION already installed"
    update_latest "$LLVM_DIR" "$VERSION"
    exit 0
fi

# Determine download URL based on platform/arch.
# LLVM 21+ uses new naming: LLVM-$VERSION-Linux-X64.tar.xz
if [ "$PLATFORM" = "linux" ] && [ "$ARCH" = "x86_64" ]; then
    TARBALL="LLVM-$VERSION-Linux-X64.tar.xz"
elif [ "$PLATFORM" = "linux" ] && [ "$ARCH" = "aarch64" ]; then
    TARBALL="LLVM-$VERSION-Linux-ARM64.tar.xz"
elif [ "$PLATFORM" = "darwin" ] && [ "$ARCH" = "aarch64" ]; then
    TARBALL="LLVM-$VERSION-macOS-ARM64.tar.xz"
else
    error "Unsupported platform: $PLATFORM/$ARCH"
    exit 1
fi
URL="https://github.com/llvm/llvm-project/releases/download/llvmorg-$VERSION/$TARBALL"

# Download.
download "$URL" "$TARBALL"

# Extract.
info "Extracting..."
tar xf "$TARBALL"

# Rename extracted directory to version.
# New naming: LLVM-$VERSION-* (e.g., LLVM-21.1.6-Linux-X64).
EXTRACTED=$(ls -d LLVM-$VERSION* 2>/dev/null | head -1)
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
update_latest "$LLVM_DIR" "$VERSION"

# Cleanup tarball.
rm -f "$TARBALL"

# Create env.sh if it doesn't exist.
if [ ! -f "env.sh" ]; then
    cat > env.sh << 'EOF'
# LLVM/Clang environment.
# Sourced by direnvrc when using use_llvm.
if [ -n "$LLVM_ROOT" ]; then
    export PATH="$LLVM_ROOT/bin:$PATH"
    export CC="$LLVM_ROOT/bin/clang"
    export CXX="$LLVM_ROOT/bin/clang++"
    export LLVM_DIR="$LLVM_ROOT/lib/cmake/llvm"
    export CLANG_DIR="$LLVM_ROOT/lib/cmake/clang"
    export MLIR_DIR="$LLVM_ROOT/lib/cmake/mlir"
    export LD_LIBRARY_PATH="$LLVM_ROOT/lib:${LD_LIBRARY_PATH:-}"
fi
EOF
    info "Created env.sh"
fi

info "LLVM $VERSION installed successfully!"
