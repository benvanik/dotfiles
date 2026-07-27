#!/bin/bash
# Install LLVM/Clang from GitHub releases.
# Usage: llvm/install.sh [version]
set -e

TOOL_NAME="llvm"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../install-utils.sh"

LLVM_DIR="$TOOLS_DIR/llvm"

# LLVM's Linux release binaries are built against the libxml2.so.2 ABI. Newer
# Linux distributions ship the incompatible libxml2.so.16 ABI instead, so keep
# the final libxml2.so.2 release private to the LLVM installation when the host
# cannot satisfy that dependency.
LIBXML2_COMPAT_VERSION="2.13.9"
LIBXML2_COMPAT_SHA256="a2c9ae7b770da34860050c309f903221c67830c86e4a7e760692b803df95143a"

# Installs a private libxml2.so.2 runtime when the LLVM release requires it and
# the host distribution does not provide it.
install_linux_libxml2_compat() {
    local llvm_root="$1"
    local linker_binary="$llvm_root/bin/ld.lld"
    if [ "$PLATFORM" != "linux" ] || [ ! -x "$linker_binary" ]; then
        return 0
    fi
    if "$linker_binary" --version >/dev/null 2>&1; then
        return 0
    fi
    if ! ldd "$linker_binary" 2>/dev/null | grep -q \
        'libxml2\.so\.2 => not found'; then
        error "ld.lld cannot execute for a reason other than missing libxml2.so.2"
        ldd "$linker_binary" >&2 || true
        return 1
    fi

    local source_archive="libxml2-$LIBXML2_COMPAT_VERSION.tar.xz"
    local source_url="https://download.gnome.org/sources/libxml2/2.13/$source_archive"
    local build_root
    build_root=$(mktemp -d "${TMPDIR:-/tmp}/llvm-libxml2-compat.XXXXXXXX")
    cleanup_libxml2_build() {
        rm -rf -- "$build_root"
    }
    trap cleanup_libxml2_build EXIT

    info "Building private libxml2.so.2 compatibility runtime..."
    download "$source_url" "$build_root/$source_archive"
    printf '%s  %s\n' \
        "$LIBXML2_COMPAT_SHA256" "$build_root/$source_archive" | \
        sha256sum --check
    tar -xf "$build_root/$source_archive" -C "$build_root"
    mkdir "$build_root/build"
    (
        cd "$build_root/build"
        CC=/usr/bin/cc CFLAGS="-O2 -fPIC" \
            "../libxml2-$LIBXML2_COMPAT_VERSION/configure" \
            --without-icu \
            --without-lzma \
            --without-python \
            --without-zlib
        make -j"$(getconf _NPROCESSORS_ONLN)" libxml2.la
    )

    install -m 0755 \
        "$build_root/build/.libs/libxml2.so.$LIBXML2_COMPAT_VERSION" \
        "$llvm_root/lib/libxml2.so.$LIBXML2_COMPAT_VERSION"
    ln -sfn "libxml2.so.$LIBXML2_COMPAT_VERSION" \
        "$llvm_root/lib/libxml2.so.2"
    install -D -m 0644 \
        "$build_root/libxml2-$LIBXML2_COMPAT_VERSION/Copyright" \
        "$llvm_root/share/licenses/libxml2/Copyright"

    cleanup_libxml2_build
    trap - EXIT
}

# Verifies that the installed compiler and linker can produce and run a native
# executable. Version banners alone miss unresolved linker dependencies.
verify_installation() {
    local llvm_root="$1"
    local smoke_binary
    smoke_binary=$(mktemp "${TMPDIR:-/tmp}/llvm-link-smoke.XXXXXXXX")
    if ! printf '%s\n' 'int main(void) { return 0; }' | \
        "$llvm_root/bin/clang" -fuse-ld=lld -x c - -o "$smoke_binary"; then
        rm -f -- "$smoke_binary"
        error "LLVM host compile/link verification failed"
        return 1
    fi
    if ! "$smoke_binary"; then
        rm -f -- "$smoke_binary"
        error "LLVM host executable verification failed"
        return 1
    fi
    rm -f -- "$smoke_binary"
}

# Repairs host-runtime dependencies and verifies an installed LLVM tree.
finalize_installation() {
    local llvm_root="$1"
    install_linux_libxml2_compat "$llvm_root"
    verify_installation "$llvm_root"
}

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
    finalize_installation "$LLVM_DIR/$VERSION"
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

# Repair release-runtime dependencies and prove that host linking works before
# publishing this version through the latest symlink.
finalize_installation "$LLVM_DIR/$VERSION"

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
