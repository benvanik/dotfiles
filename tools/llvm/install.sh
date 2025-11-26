#!/bin/bash
# Install LLVM/Clang from GitHub releases.
# Usage: llvm/install.sh [version]
set -e

TOOL_NAME="llvm"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../install-utils.sh"

LLVM_DIR="$TOOLS_DIR/llvm"

# Handle flags.
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            show_install_usage "llvm" "VERSION"
            echo ""
            echo "Downloads pre-built LLVM/Clang from GitHub releases."
            echo "Example: llvm/install.sh 21.1.6"
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

# Get version (required for LLVM - too many options to auto-detect).
if [ -z "$1" ]; then
    error "Version required. Example: llvm/install.sh 21.1.6"
    echo ""
    echo "Find versions at: https://github.com/llvm/llvm-project/releases"
    exit 1
fi
VERSION="$1"

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
if [ "$PLATFORM" = "linux" ] && [ "$ARCH" = "x86_64" ]; then
    TARBALL="clang+llvm-$VERSION-x86_64-linux-gnu-ubuntu-22.04.tar.xz"
    # Try ubuntu-22.04 first, fall back to generic linux-gnu.
    URL="https://github.com/llvm/llvm-project/releases/download/llvmorg-$VERSION/$TARBALL"
elif [ "$PLATFORM" = "darwin" ] && [ "$ARCH" = "aarch64" ]; then
    TARBALL="clang+llvm-$VERSION-arm64-apple-darwin24.tar.xz"
    URL="https://github.com/llvm/llvm-project/releases/download/llvmorg-$VERSION/$TARBALL"
elif [ "$PLATFORM" = "darwin" ] && [ "$ARCH" = "x86_64" ]; then
    TARBALL="clang+llvm-$VERSION-x86_64-apple-darwin.tar.xz"
    URL="https://github.com/llvm/llvm-project/releases/download/llvmorg-$VERSION/$TARBALL"
else
    error "Unsupported platform: $PLATFORM/$ARCH"
    exit 1
fi

# Download.
download "$URL" "$TARBALL"

# Extract.
info "Extracting..."
tar xf "$TARBALL"

# Rename extracted directory to version.
EXTRACTED=$(ls -d clang+llvm-$VERSION* 2>/dev/null | head -1)
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
