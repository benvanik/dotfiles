#!/bin/bash
# Install ROCm from TheRock pip index.
# Usage: rocm/install.sh [--force] [--prune-old] [version] [gpu-target]
set -euo pipefail

TOOL_NAME="rocm"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../install-utils.sh"

# Platform check - ROCm is Linux only.
if [ "$PLATFORM" != "linux" ]; then
    error "ROCm is only available for Linux"
    exit 1
fi

ROCM_DIR="$TOOLS_DIR/rocm"

show_help() {
    cat << EOF
Usage: rocm/install.sh [--force] [--prune-old] [version] [gpu-target]

Install ROCm from TheRock pip index to ~/tools/rocm/<version>/.

TheRock publishes the SDK as Python packages. This installer keeps that
packaging environment hidden under .venv and exposes a conventional ROCm root:

    ~/tools/rocm/<version>/bin
    ~/tools/rocm/<version>/include
    ~/tools/rocm/<version>/lib
    ~/tools/rocm/<version>/share

Arguments:
    version     ROCm version (e.g., 7.14.0a20260612); fetches latest if omitted
    gpu-target  GPU target (default: \$ROCM_GPU_TARGET or gfx1100)
                Supported shortcuts: gfx110*, gfx90*, gfx94*

Options:
    --force     Replace the managed version directory before installing
    --prune-old Remove other installed ROCm version directories after success

Examples:
    rocm/install.sh                         # Install latest for default GPU
    rocm/install.sh 7.14.0a20260612         # Uses ROCM_GPU_TARGET
    rocm/install.sh 7.14.0a20260612 gfx90a  # Override GPU target
    rocm/install.sh --force                 # Rebuild latest from scratch
    rocm/install.sh --prune-old             # Keep only the selected version

Set default GPU in ~/.shrc.local:
    export ROCM_GPU_TARGET=gfx1100  # RDNA3
EOF
}

index_suffix_for_gpu() {
    local gpu_target="$1"
    case "$gpu_target" in
        gfx110*) echo "gfx110X-all" ;;
        gfx90*)  echo "gfx90X-all" ;;
        gfx94*)  echo "gfx94X-all" ;;
        *)       echo "$gpu_target" ;;
    esac
}

validate_version() {
    local version="$1"
    if [[ ! "$version" =~ ^[0-9][0-9A-Za-z._+-]*$ ]]; then
        error "Invalid ROCm version: $version"
        exit 1
    fi
}

validate_gpu_target() {
    local gpu_target="$1"
    if [[ ! "$gpu_target" =~ ^[A-Za-z0-9_.+-]+$ ]]; then
        error "Invalid ROCm GPU target: $gpu_target"
        exit 1
    fi
}

# Fetch latest ROCm version from pip index.
# Sets FETCHED_VERSION on success.
fetch_latest_version() {
    local gpu_target="${1:-gfx1100}"
    local index_suffix
    index_suffix="$(index_suffix_for_gpu "$gpu_target")"
    local index_url="https://rocm.nightlies.amd.com/v2/${index_suffix}"

    info "Fetching latest ROCm version from pip index..."
    # Parse the pip index page for rocm package versions.
    # Format: rocm-7.11.0a20251127.tar.gz (version with date suffix).
    FETCHED_VERSION=$(curl -fsSL "$index_url/rocm/" 2>/dev/null | \
        grep -oE 'rocm-[0-9]+\.[0-9]+\.[0-9]+[a-z0-9]*\.tar\.gz' | \
        sed 's/\.tar\.gz//' | sed 's/rocm-//' | sort -V | tail -1 || true)
    if [ -z "$FETCHED_VERSION" ]; then
        error "Failed to fetch latest version from $index_url"
        exit 1
    fi
}

sdk_surface_valid() {
    local install_dir="$1"

    [ -d "$install_dir/.venv" ] || return 1
    [ -x "$install_dir/bin/hipcc" ] || return 1
    [ -x "$install_dir/bin/hipconfig" ] || return 1
    [ -e "$install_dir/include/hip/hip_runtime.h" ] || return 1
    [ -e "$install_dir/lib/libamdhip64.so" ] || return 1
    [ -e "$install_dir/lib/cmake/hip/hip-config.cmake" ] || return 1
}

remove_install_dir() {
    local install_dir="$1"
    if [[ "$install_dir" != "$ROCM_DIR"/* ]]; then
        error "Refusing to remove path outside $ROCM_DIR: $install_dir"
        exit 1
    fi
    rm -rf "$install_dir"
}

prune_old_versions() {
    local keep_version="$1"
    local path

    for path in "$ROCM_DIR"/*; do
        [ -d "$path" ] || continue
        [ -L "$path" ] && continue

        local version
        version="$(basename "$path")"
        case "$version" in
            [0-9]*) ;;
            *) continue ;;
        esac

        if [ "$version" != "$keep_version" ]; then
            info "Pruning old ROCm install at $path..."
            remove_install_dir "$path"
        fi
    done
}

materialize_sdk_root() {
    local install_dir="$1"
    local venv_dir="$2"
    local sdk_root="$3"

    if [ ! -d "$sdk_root/include" ] || [ ! -d "$sdk_root/lib" ] || [ ! -d "$sdk_root/bin" ]; then
        error "TheRock devel root is missing expected SDK directories: $sdk_root"
        exit 1
    fi

    # These paths are owned by this installer. Recreate them every time so a
    # forced reinstall cannot retain the legacy venv-root layout.
    rm -rf -- \
        "${install_dir:?}/amdgcn" \
        "${install_dir:?}/bin" \
        "${install_dir:?}/etc" \
        "${install_dir:?}/include" \
        "${install_dir:?}/lib" \
        "${install_dir:?}/lib64" \
        "${install_dir:?}/libexec" \
        "${install_dir:?}/llvm" \
        "${install_dir:?}/share" \
        "${install_dir:?}/.info" \
        "${install_dir:?}/.kpack"

    local entry
    for entry in amdgcn etc include lib libexec llvm share .info .kpack; do
        if [ -e "$sdk_root/$entry" ]; then
            ln -s "$sdk_root/$entry" "$install_dir/$entry"
        fi
    done

    if [ -e "$sdk_root/lib64" ]; then
        ln -s "$sdk_root/lib64" "$install_dir/lib64"
    elif [ -e "$sdk_root/lib" ]; then
        ln -s lib "$install_dir/lib64"
    fi

    mkdir -p "$install_dir/bin"
    for entry in "$sdk_root/bin"/*; do
        [ -e "$entry" ] || continue
        ln -s "$entry" "$install_dir/bin/$(basename "$entry")"
    done

    if [ -x "$venv_dir/bin/rocm-sdk" ]; then
        ln -s "$venv_dir/bin/rocm-sdk" "$install_dir/bin/rocm-sdk"
    fi

    if ! sdk_surface_valid "$install_dir"; then
        error "ROCm SDK surface did not validate after install"
        exit 1
    fi
}

write_env_file() {
    install -m 0644 "$SCRIPT_DIR/environment.sh" "$ROCM_DIR/env.sh"
    info "Updated env.sh"
}

INSTALL_IN_PROGRESS=false
INSTALL_DIR=""
PRUNE_OLD=false

cleanup_failed_install() {
    if [ "$INSTALL_IN_PROGRESS" = "true" ] && [ -n "$INSTALL_DIR" ]; then
        warn "Install failed; removing partial install at $INSTALL_DIR"
        remove_install_dir "$INSTALL_DIR"
    fi
}
trap cleanup_failed_install ERR

# Handle flags.
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            show_help
            exit 0
            ;;
        -f|--force)
            FORCE=true
            shift
            ;;
        --prune-old)
            PRUNE_OLD=true
            shift
            ;;
        *)
            break
            ;;
    esac
done

# Get GPU target first (needed for version lookup).
GPU_TARGET="${2:-${ROCM_GPU_TARGET:-gfx1100}}"
validate_gpu_target "$GPU_TARGET"

# Get version (fetch latest if not specified).
if [ -z "${1:-}" ]; then
    fetch_latest_version "$GPU_TARGET"
    VERSION="$FETCHED_VERSION"
else
    VERSION="$1"
fi
validate_version "$VERSION"

# Map GPU target to index URL pattern.
INDEX_SUFFIX="$(index_suffix_for_gpu "$GPU_TARGET")"

INDEX_URL="https://rocm.nightlies.amd.com/v2/${INDEX_SUFFIX}/"
INSTALL_DIR="$ROCM_DIR/$VERSION"
VENV_DIR="$INSTALL_DIR/.venv"

info "Installing ROCm $VERSION for $GPU_TARGET"
echo "  Index: $INDEX_URL"
echo "  Target: $INSTALL_DIR"
echo ""

mkdir -p "$ROCM_DIR"
write_env_file

# Check if already installed.
if [ "$FORCE" != "true" ] && version_installed "$ROCM_DIR" "$VERSION"; then
    if sdk_surface_valid "$INSTALL_DIR"; then
        warn "Version $VERSION already installed"
        update_latest "$ROCM_DIR" "$VERSION"
        if [ "$PRUNE_OLD" = "true" ]; then
            prune_old_versions "$VERSION"
        fi
        exit 0
    fi
    warn "Version $VERSION exists but does not expose a conventional ROCm SDK root; rebuilding"
    FORCE=true
fi

if [ -e "$INSTALL_DIR" ]; then
    info "Removing existing install at $INSTALL_DIR..."
    remove_install_dir "$INSTALL_DIR"
fi

mkdir -p "$INSTALL_DIR"
INSTALL_IN_PROGRESS=true

info "Creating virtual environment..."
python3 -m venv "$VENV_DIR"

# Install ROCm packages.
info "Installing ROCm packages (this may take a while)..."
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install \
    --index-url "$INDEX_URL" \
    "rocm[libraries,devel]==$VERSION"

info "Initializing TheRock SDK payload..."
"$VENV_DIR/bin/rocm-sdk" init --quiet
SDK_ROOT="$("$VENV_DIR/bin/rocm-sdk" path --root)"

info "Materializing conventional ROCm SDK root..."
materialize_sdk_root "$INSTALL_DIR" "$VENV_DIR" "$SDK_ROOT"
INSTALL_IN_PROGRESS=false

# Update latest symlink.
update_latest "$ROCM_DIR" "$VERSION"
if [ "$PRUNE_OLD" = "true" ]; then
    prune_old_versions "$VERSION"
fi

info "ROCm $VERSION installed successfully!"
