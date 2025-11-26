#!/bin/bash
# Install ROCm from TheRock pip index.
# Usage: rocm/install.sh <version> [gpu-target]
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

# Handle flags.
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            cat << EOF
Usage: rocm/install.sh <version> [gpu-target]

Install ROCm from TheRock pip index to ~/tools/rocm/<version>/

Arguments:
    version     ROCm version (e.g., 7.9.0)
    gpu-target  GPU target (default: \$ROCM_GPU_TARGET or gfx1100)
                Supported: gfx110*, gfx90*, gfx94*

Options:
    --force     Reinstall even if version exists

Examples:
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

# Get version (required).
if [ -z "$1" ]; then
    error "Version required. Example: rocm/install.sh 7.9.0"
    exit 1
fi

VERSION="$1"
GPU_TARGET="${2:-${ROCM_GPU_TARGET:-gfx1100}}"

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
