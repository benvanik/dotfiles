#!/bin/bash
# Install Vulkan SDK from LunarG.
# Usage: tools/vulkan/install.sh [version]
set -e

TOOL_NAME="vulkan"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../install-utils.sh"

# Platform check.
if [ "$PLATFORM" != "linux" ]; then
    error "Vulkan SDK installer only supports Linux"
    error "macOS: Install via 'brew install vulkan-sdk'"
    exit 1
fi

VULKAN_DIR="$TOOLS_DIR/vulkan"

# Handle flags.
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            cat << 'EOF'
vulkan/install.sh - Install Vulkan SDK from LunarG

USAGE
    tools/vulkan/install.sh [options] [version]

OPTIONS
    -h, --help    Show this help
    -f, --force   Reinstall even if version exists

ARGUMENTS
    version       SDK version to install (default: latest)

EXAMPLES
    tools/vulkan/install.sh
        Install latest Vulkan SDK

    tools/vulkan/install.sh 1.4.328.1
        Install specific version

    tools/vulkan/install.sh --force 1.4.328.1
        Reinstall specific version

WHAT IT DOES
    - Downloads SDK from sdk.lunarg.com
    - Extracts to ~/tools/vulkan/<version>/
    - Creates env.sh for direnv integration
    - Updates 'latest' symlink

USAGE IN .envrc
    use_vulkan              # Use latest SDK
    use_vulkan "1.4.328.1"  # Specific version

SEE ALSO
    vulkan-build-layers     Build validation layers from source
    use_vulkan_layers       Override layers in .envrc
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

# Get version.
if [ -n "$1" ]; then
    VERSION="$1"
else
    info "Fetching latest version..."
    VERSION=$(curl -s https://vulkan.lunarg.com/sdk/latest/linux.txt)
    if [ -z "$VERSION" ]; then
        error "Failed to fetch latest version"
        exit 1
    fi
    info "Latest version: $VERSION"
fi

info "Installing Vulkan SDK $VERSION"

# Create directory.
mkdir -p "$VULKAN_DIR"
cd "$VULKAN_DIR"

# Check if already installed.
if version_installed "$VULKAN_DIR" "$VERSION"; then
    warn "Version $VERSION already installed"
    update_latest "$VULKAN_DIR" "$VERSION"
    exit 0
fi

# Download.
TARBALL="vulkansdk-linux-x86_64-$VERSION.tar.xz"
URL="https://sdk.lunarg.com/sdk/download/$VERSION/linux/vulkan_sdk.tar.xz"
download "$URL" "$TARBALL"

# Extract.
info "Extracting..."
tar xf "$TARBALL"

# Verify extraction.
if [ ! -d "$VERSION" ]; then
    error "Extraction failed - directory $VERSION not found"
    rm -f "$TARBALL"
    exit 1
fi

# Update latest symlink.
update_latest "$VULKAN_DIR" "$VERSION"

# Cleanup tarball.
rm -f "$TARBALL"

# Create env.sh (always overwrite to ensure it's current).
cat > env.sh << 'EOF'
# Vulkan SDK environment.
# Sourced by direnvrc when using use_vulkan.
# VULKAN_ROOT is set by direnvrc to the version directory.

if [ -n "$VULKAN_ROOT" ] && [ -d "$VULKAN_ROOT/x86_64" ]; then
    export VULKAN_SDK="$VULKAN_ROOT/x86_64"
    export PATH="$VULKAN_SDK/bin:$PATH"
    export LD_LIBRARY_PATH="$VULKAN_SDK/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    export VK_LAYER_PATH="$VULKAN_SDK/share/vulkan/explicit_layer.d"
    export PKG_CONFIG_PATH="$VULKAN_SDK/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
    # For CMake find_package(Vulkan).
    export CMAKE_PREFIX_PATH="$VULKAN_SDK${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}"
fi
EOF
info "Updated env.sh"

info "Vulkan SDK $VERSION installed successfully!"
info ""
info "Usage in .envrc:"
info "  use_vulkan           # Use latest"
info "  use_vulkan \"$VERSION\"  # This version"
