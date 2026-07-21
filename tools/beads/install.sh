#!/bin/bash
# Install beads (br + bv) from GitHub releases.
# Usage: beads/install.sh [version]
#
# Installs:
#   br (beads CLI)     from Dicklesworthstone/beads_rust
#   bv (beads viewer)  from Dicklesworthstone/beads_viewer
#   bd -> br symlink   for command-name compatibility
set -e

TOOL_NAME="beads"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../install-utils.sh"

BEADS_DIR="$TOOLS_DIR/beads"
LOCAL_BIN="$HOME/.local/bin"

# Handle flags.
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            show_install_usage "beads" "BR_VERSION"
            echo ""
            echo "Downloads pre-built br and bv from GitHub releases."
            echo "Creates bd -> br symlink in ~/.local/bin/ for compatibility."
            echo ""
            echo "Options:"
            echo "  --force    Reinstall even if version exists"
            echo ""
            echo "Repos:"
            echo "  br: https://github.com/Dicklesworthstone/beads_rust"
            echo "  bv: https://github.com/Dicklesworthstone/beads_viewer"
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

# Map platform/arch to release asset names.
case "${PLATFORM}_${ARCH}" in
    linux_x86_64)
        BR_SUFFIX="linux_amd64"
        BV_SUFFIX="linux_amd64"
        ;;
    linux_aarch64)
        # br doesn't publish linux arm64; bv does.
        BR_SUFFIX=""
        BV_SUFFIX="linux_arm64"
        ;;
    darwin_x86_64)
        BR_SUFFIX="darwin_amd64"
        BV_SUFFIX="darwin_amd64"
        ;;
    darwin_aarch64)
        BR_SUFFIX="darwin_arm64"
        BV_SUFFIX="darwin_arm64"
        ;;
    *)
        error "Unsupported platform: ${PLATFORM}_${ARCH}"
        exit 1
        ;;
esac

# Fetch latest br version.
if [ -z "$1" ]; then
    info "Fetching latest br version..."
    BR_VERSION=$(curl -s https://api.github.com/repos/Dicklesworthstone/beads_rust/releases/latest | grep '"tag_name"' | sed -E 's/.*"v([^"]+)".*/\1/')
    if [ -z "$BR_VERSION" ]; then
        error "Failed to fetch latest br version. Specify manually: beads/install.sh 0.1.13"
        exit 1
    fi
else
    BR_VERSION="$1"
fi

# Fetch latest bv version (always latest, independent of br version).
info "Fetching latest bv version..."
BV_VERSION=$(curl -s https://api.github.com/repos/Dicklesworthstone/beads_viewer/releases/latest | grep '"tag_name"' | sed -E 's/.*"v([^"]+)".*/\1/')
if [ -z "$BV_VERSION" ]; then
    warn "Failed to fetch bv version, skipping bv install"
fi

info "Installing br $BR_VERSION + bv ${BV_VERSION:-skipped}"

# Create directories.
mkdir -p "$BEADS_DIR"
mkdir -p "$LOCAL_BIN"
cd "$BEADS_DIR"

# Install br.
if [ -n "$BR_SUFFIX" ]; then
    if version_installed "$BEADS_DIR" "br-$BR_VERSION"; then
        warn "br $BR_VERSION already installed"
    else
        BR_TARBALL="br-v${BR_VERSION}-${BR_SUFFIX}.tar.gz"
        BR_URL="https://github.com/Dicklesworthstone/beads_rust/releases/download/v${BR_VERSION}/${BR_TARBALL}"
        download "$BR_URL" "$BR_TARBALL"

        mkdir -p "br-$BR_VERSION/bin"
        info "Extracting br..."
        tar xzf "$BR_TARBALL" --strip-components=1 -C "br-$BR_VERSION/bin"
        chmod +x "br-$BR_VERSION/bin/br"
        rm -f "$BR_TARBALL"

        info "br $BR_VERSION installed"
    fi

    update_latest "$BEADS_DIR" "br-$BR_VERSION"

    # Symlink br and bd into ~/.local/bin/.
    ln -sf "$BEADS_DIR/br-$BR_VERSION/bin/br" "$LOCAL_BIN/br"
    ln -sf "$LOCAL_BIN/br" "$LOCAL_BIN/bd"
    info "Symlinked: ~/.local/bin/br, ~/.local/bin/bd -> br"
else
    warn "No br binary available for ${PLATFORM}_${ARCH}, skipping"
fi

# Install bv.
if [ -n "$BV_VERSION" ] && [ -n "$BV_SUFFIX" ]; then
    if version_installed "$BEADS_DIR" "bv-$BV_VERSION"; then
        warn "bv $BV_VERSION already installed"
    else
        BV_TARBALL="bv_${BV_VERSION}_${BV_SUFFIX}.tar.gz"
        BV_URL="https://github.com/Dicklesworthstone/beads_viewer/releases/download/v${BV_VERSION}/${BV_TARBALL}"
        download "$BV_URL" "$BV_TARBALL"

        mkdir -p "bv-$BV_VERSION/bin"
        info "Extracting bv..."
        tar xzf "$BV_TARBALL" -C "bv-$BV_VERSION/bin"
        chmod +x "bv-$BV_VERSION/bin/bv"
        rm -f "$BV_TARBALL"

        info "bv $BV_VERSION installed"
    fi

    # Symlink bv into ~/.local/bin/.
    ln -sf "$BEADS_DIR/bv-$BV_VERSION/bin/bv" "$LOCAL_BIN/bv"
    info "Symlinked: ~/.local/bin/bv"
fi

info "Beads installed successfully!"
