#!/bin/bash
# Install Bazel development tools (bazelisk, buildifier, buildozer).
# Usage: bazel/install.sh [buildtools-version]
set -e

TOOL_NAME="bazel"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../install-utils.sh"

BAZEL_DIR="$TOOLS_DIR/bazel"

# Handle flags.
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            cat << EOF
Usage: bazel/install.sh [buildtools-version]

Install Bazel development tools to ~/tools/bazel/<version>/

Installs:
  - bazelisk (as 'bazel') - Bazel version manager
  - buildifier - BUILD file formatter
  - buildozer - BUILD file editor

Options:
  --force    Reinstall even if version exists

Examples:
  bazel/install.sh           # Install latest
  bazel/install.sh 8.2.1     # Install specific buildtools version

Releases:
  https://github.com/bazelbuild/bazelisk/releases
  https://github.com/bazelbuild/buildtools/releases
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

# Map platform/arch to download suffix.
get_platform_suffix() {
    local os arch
    case "$PLATFORM" in
        linux)  os="linux" ;;
        darwin) os="darwin" ;;
        *)      error "Unsupported platform: $PLATFORM"; exit 1 ;;
    esac
    case "$ARCH" in
        x86_64)  arch="amd64" ;;
        aarch64) arch="arm64" ;;
        *)       error "Unsupported architecture: $ARCH"; exit 1 ;;
    esac
    echo "${os}-${arch}"
}

SUFFIX=$(get_platform_suffix)

# Get buildtools version (used as directory version).
if [ -z "$1" ]; then
    info "Fetching latest buildtools version..."
    VERSION=$(curl -s https://api.github.com/repos/bazelbuild/buildtools/releases/latest | \
              grep '"tag_name"' | sed -E 's/.*"v([^"]+)".*/\1/')
    if [ -z "$VERSION" ]; then
        error "Failed to fetch latest version. Specify manually: bazel/install.sh 8.2.1"
        exit 1
    fi
else
    VERSION="$1"
fi

info "Installing Bazel tools (buildtools $VERSION)"

# Create directory.
mkdir -p "$BAZEL_DIR"
cd "$BAZEL_DIR"

# Check if already installed.
if version_installed "$BAZEL_DIR" "$VERSION"; then
    warn "Version $VERSION already installed"
    update_latest "$BAZEL_DIR" "$VERSION"
    exit 0
fi

# Create version directory.
mkdir -p "$VERSION/bin"

# Get latest bazelisk version.
info "Fetching latest bazelisk version..."
BAZELISK_VER=$(curl -s https://api.github.com/repos/bazelbuild/bazelisk/releases/latest | \
               grep '"tag_name"' | sed -E 's/.*"v([^"]+)".*/\1/')

# Download bazelisk as 'bazel'.
info "Downloading bazelisk $BAZELISK_VER..."
curl -L --progress-bar -o "$VERSION/bin/bazel" \
    "https://github.com/bazelbuild/bazelisk/releases/download/v$BAZELISK_VER/bazelisk-$SUFFIX"
chmod +x "$VERSION/bin/bazel"

# Download buildifier.
info "Downloading buildifier $VERSION..."
curl -L --progress-bar -o "$VERSION/bin/buildifier" \
    "https://github.com/bazelbuild/buildtools/releases/download/v$VERSION/buildifier-$SUFFIX"
chmod +x "$VERSION/bin/buildifier"

# Download buildozer.
info "Downloading buildozer $VERSION..."
curl -L --progress-bar -o "$VERSION/bin/buildozer" \
    "https://github.com/bazelbuild/buildtools/releases/download/v$VERSION/buildozer-$SUFFIX"
chmod +x "$VERSION/bin/buildozer"

# Update latest symlink.
update_latest "$BAZEL_DIR" "$VERSION"

# Create env.sh if it doesn't exist.
if [ ! -f "env.sh" ]; then
    cat > env.sh << 'EOF'
# Bazel tools environment.
# Sourced by direnvrc when using use_bazel.
if [ -n "$BAZEL_ROOT" ]; then
    export PATH="$BAZEL_ROOT/bin:$PATH"
fi
EOF
    info "Created env.sh"
fi

info "Bazel tools installed successfully!"
echo "  bazel (bazelisk $BAZELISK_VER)"
echo "  buildifier $VERSION"
echo "  buildozer $VERSION"
