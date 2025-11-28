#!/bin/bash
# Install ROCm from TheRock pip index.
# Usage: rocm/install.sh [version] [gpu-target]
set -e

TOOL_NAME="rocm"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../install-utils.sh"

# Platform check - ROCm is Linux only.
if [ "$PLATFORM" != "linux" ]; then
    error "ROCm is only available for Linux"
    exit 1
fi

ROCM_DIR="$TOOLS_DIR/rocm"

# Fetch latest ROCm version from pip index.
# Sets FETCHED_VERSION on success.
fetch_latest_version() {
    local gpu_target="${1:-gfx1100}"
    local index_suffix
    case "$gpu_target" in
        gfx110*) index_suffix="gfx110X-all" ;;
        gfx90*)  index_suffix="gfx90X-all" ;;
        gfx94*)  index_suffix="gfx94X-all" ;;
        *)       index_suffix="$gpu_target" ;;
    esac
    local index_url="https://rocm.nightlies.amd.com/v2/${index_suffix}"

    info "Fetching latest ROCm version from pip index..."
    # Parse the pip index page for rocm package versions.
    # Format: rocm-7.11.0a20251127.tar.gz (version with date suffix).
    FETCHED_VERSION=$(curl -fsSL "$index_url/rocm/" 2>/dev/null | \
        grep -oE 'rocm-[0-9]+\.[0-9]+\.[0-9]+[a-z0-9]*\.tar\.gz' | \
        sed 's/\.tar\.gz//' | sed 's/rocm-//' | sort -V | tail -1)
    if [ -z "$FETCHED_VERSION" ]; then
        error "Failed to fetch latest version from $index_url"
        exit 1
    fi
}

# Handle flags.
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            cat << EOF
Usage: rocm/install.sh [version] [gpu-target]

Install ROCm from TheRock pip index to ~/tools/rocm/<version>/

Arguments:
    version     ROCm version (e.g., 7.9.0) - fetches latest if omitted
    gpu-target  GPU target (default: \$ROCM_GPU_TARGET or gfx1100)
                Supported: gfx110*, gfx90*, gfx94*

Options:
    --force     Reinstall even if version exists

Examples:
    rocm/install.sh                 # Install latest for default GPU
    rocm/install.sh 7.9.0           # Uses ROCM_GPU_TARGET
    rocm/install.sh 7.9.0 gfx90a    # Override GPU target

Set default GPU in ~/.shrc.local:
    export ROCM_GPU_TARGET=gfx1100  # RDNA3
EOF
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

# Get GPU target first (needed for version lookup).
GPU_TARGET="${2:-${ROCM_GPU_TARGET:-gfx1100}}"

# Get version (fetch latest if not specified).
if [ -z "$1" ]; then
    fetch_latest_version "$GPU_TARGET"
    VERSION="$FETCHED_VERSION"
else
    VERSION="$1"
fi

# Map GPU target to index URL pattern.
case "$GPU_TARGET" in
    gfx110*) INDEX_SUFFIX="gfx110X-all" ;;
    gfx90*)  INDEX_SUFFIX="gfx90X-all" ;;
    gfx94*)  INDEX_SUFFIX="gfx94X-all" ;;
    *)       INDEX_SUFFIX="$GPU_TARGET" ;;
esac

INDEX_URL="https://rocm.nightlies.amd.com/v2/${INDEX_SUFFIX}/"

info "Installing ROCm $VERSION for $GPU_TARGET"
echo "  Index: $INDEX_URL"
echo "  Target: $ROCM_DIR/$VERSION"
echo ""

# Create directory.
mkdir -p "$ROCM_DIR"
cd "$ROCM_DIR"

# Check if already installed.
if version_installed "$ROCM_DIR" "$VERSION"; then
    warn "Version $VERSION already installed"
    update_latest "$ROCM_DIR" "$VERSION"
    exit 0
fi

# Create venv.
info "Creating virtual environment..."
python3 -m venv "$VERSION"

# Install ROCm packages.
info "Installing ROCm packages (this may take a while)..."
"$VERSION/bin/pip" install --upgrade pip --quiet
"$VERSION/bin/pip" install \
    --index-url "$INDEX_URL" \
    "rocm[libraries,devel]==$VERSION"

# Update latest symlink.
update_latest "$ROCM_DIR" "$VERSION"

# Create env.sh if it doesn't exist.
if [ ! -f "env.sh" ]; then
    cat > env.sh << 'EOF'
# ROCm environment.
# Sourced by direnvrc when using use_rocm.
if [ -n "$ROCM_ROOT" ]; then
    export ROCM_HOME="$ROCM_ROOT"
    export HIP_PATH="$ROCM_ROOT"
    export PATH="$ROCM_ROOT/bin:$PATH"
    export LD_LIBRARY_PATH="$ROCM_ROOT/lib:${LD_LIBRARY_PATH:-}"
fi
EOF
    info "Created env.sh"
fi

info "ROCm $VERSION installed successfully!"
