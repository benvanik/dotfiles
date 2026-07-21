#!/bin/bash
# Install CUDA SDK from NVIDIA redistributable packages.
# Usage: cuda/install.sh [version]
set -e

TOOL_NAME="cuda"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../install-utils.sh"

# Platform check - CUDA SDK is Linux only (macOS uses Metal).
if [ "$PLATFORM" != "linux" ]; then
    error "CUDA SDK is only available for Linux"
    exit 1
fi

CUDA_DIR="$TOOLS_DIR/cuda"
REDIST_BASE="https://developer.download.nvidia.com/compute/cuda/redist"

# IREE needs exactly two packages: headers (cuda.h) and nvcc (libdevice).
# Full mode adds math libraries, profiling, etc.
CORE_PACKAGES="cuda_cudart cuda_cccl cuda_nvcc"
FULL_PACKAGES="cuda_cudart cuda_nvcc cuda_cccl cuda_cupti cuda_nvdisasm
cuda_nvml_dev cuda_nvrtc cuda_nvtx cuda_opencl cuda_profiler_api
libcublas libcufft libcufile libcurand libcusolver libcusparse
libnpp libnvfatbin libnvjitlink libnvjpeg"

# Fetch latest CUDA version from redistribution index.
# Probes known version patterns to find the latest available.
fetch_latest_version() {
    info "Probing for latest CUDA version..."
    # Try recent versions in descending order.
    for major in 13 12; do
        for minor in $(seq 9 -1 0); do
            for patch in $(seq 3 -1 0); do
                local ver="${major}.${minor}.${patch}"
                local code
                code=$(curl -fsSI "${REDIST_BASE}/redistrib_${ver}.json" 2>/dev/null | head -1)
                if echo "$code" | grep -q "200"; then
                    FETCHED_VERSION="$ver"
                    return 0
                fi
            done
        done
    done
    error "Failed to find any CUDA redistribution version"
    exit 1
}

FULL_MODE=false

# Handle flags.
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            cat << EOF
Usage: cuda/install.sh [OPTIONS] [VERSION]

Install CUDA SDK to ~/tools/cuda/<version>/ from NVIDIA redistributable packages.

Arguments:
    VERSION     CUDA version (e.g., 12.9.1) - probes for latest if omitted

Options:
    --full      Install all development packages (~3 GB vs ~80 MB core)
    --force     Reinstall even if version exists

Core packages (default):
    cuda_cudart (headers: cuda.h), cuda_nvcc (compiler, libdevice)
    This is everything IREE needs to build the CUDA driver.

Full packages (--full):
    Adds math libraries (cublas, cufft, cusparse, etc.), profiling (cupti),
    runtime compilation (nvrtc), and other development packages.

Examples:
    cuda/install.sh               # Install latest core SDK
    cuda/install.sh 12.9.1        # Install specific version
    cuda/install.sh --full        # Install with all dev libraries
EOF
            exit 0
            ;;
        -f|--force)
            FORCE=true
            shift
            ;;
        --full)
            FULL_MODE=true
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

# Select package set.
if [ "$FULL_MODE" = true ]; then
    PACKAGES="$FULL_PACKAGES"
    info "Installing CUDA $VERSION (full SDK)"
else
    PACKAGES="$CORE_PACKAGES"
    info "Installing CUDA $VERSION (core: headers + libdevice)"
fi

# Create directory.
mkdir -p "$CUDA_DIR"
cd "$CUDA_DIR"

# Check if already installed.
if version_installed "$CUDA_DIR" "$VERSION"; then
    warn "Version $VERSION already installed"
    update_latest "$CUDA_DIR" "$VERSION"
    exit 0
fi

# Fetch redistribution manifest.
MANIFEST_URL="${REDIST_BASE}/redistrib_${VERSION}.json"
info "Fetching package manifest..."
MANIFEST=$(curl -fsSL "$MANIFEST_URL" 2>/dev/null) || {
    error "Failed to fetch manifest from $MANIFEST_URL"
    error "Check that CUDA $VERSION exists at: $REDIST_BASE/"
    exit 1
}

# Determine platform key.
case "$ARCH" in
    x86_64)  PLATFORM_KEY="linux-x86_64" ;;
    aarch64) PLATFORM_KEY="linux-aarch64" ;;
    *)
        error "Unsupported architecture: $ARCH"
        exit 1
        ;;
esac

# Create version directory.
INSTALL_DIR="$CUDA_DIR/$VERSION"
mkdir -p "$INSTALL_DIR"

# Download and extract each package.
for pkg in $PACKAGES; do
    rel_path=$(echo "$MANIFEST" | python3 -c "
import json, sys
d = json.load(sys.stdin)
p = d.get('$pkg', {}).get('$PLATFORM_KEY', {})
print(p.get('relative_path', ''))" 2>/dev/null)

    if [ -z "$rel_path" ]; then
        warn "Package $pkg not available for $PLATFORM_KEY, skipping"
        continue
    fi

    download_url="${REDIST_BASE}/${rel_path}"
    tarball=$(basename "$rel_path")

    download "$download_url" "$tarball"

    info "Extracting $pkg..."
    # NVIDIA archives extract to <package>-<platform>-<version>-archive/
    tar xf "$tarball" -C "$INSTALL_DIR"

    # Merge extracted contents into the install directory.
    # Each archive has include/, lib64/, bin/, etc. at its root.
    extracted_dir=$(tar tf "$tarball" | head -1 | cut -d/ -f1)
    if [ -d "$INSTALL_DIR/$extracted_dir" ] && [ "$extracted_dir" != "." ]; then
        # Merge directories (cp -rn won't overwrite existing files).
        cp -a "$INSTALL_DIR/$extracted_dir/"* "$INSTALL_DIR/" 2>/dev/null || true
        rm -rf "$INSTALL_DIR/${extracted_dir:?}"
    fi

    rm -f "$tarball"
done

# Verify critical files exist.
if [ ! -f "$INSTALL_DIR/include/cuda.h" ]; then
    error "Installation failed: include/cuda.h not found"
    rm -rf "$INSTALL_DIR"
    exit 1
fi
if [ ! -f "$INSTALL_DIR/nvvm/libdevice/libdevice.10.bc" ]; then
    error "Installation failed: nvvm/libdevice/libdevice.10.bc not found"
    rm -rf "$INSTALL_DIR"
    exit 1
fi

# Update latest symlink.
update_latest "$CUDA_DIR" "$VERSION"

# Create env.sh if it doesn't exist.
if [ ! -f "env.sh" ]; then
    cat > env.sh << 'EOF'
# CUDA SDK environment.
# Sourced by direnvrc when using use_cuda.
if [ -n "$CUDA_ROOT" ]; then
    export IREE_CUDA_TOOLKIT_ROOT="$CUDA_ROOT"
    export CUDA_TOOLKIT_ROOT_DIR="$CUDA_ROOT"
    export PATH="$CUDA_ROOT/bin:$PATH"
    export LD_LIBRARY_PATH="$CUDA_ROOT/lib64:${LD_LIBRARY_PATH:-}"
fi
EOF
    info "Created env.sh"
fi

info "CUDA $VERSION installed successfully!"
info "Directory: $INSTALL_DIR"
info "Headers: $INSTALL_DIR/include/cuda.h"
info "Libdevice: $INSTALL_DIR/nvvm/libdevice/libdevice.10.bc"
