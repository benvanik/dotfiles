#!/bin/bash
# Install Determinate Nix.
# Usage: nix/install.sh [--force]
#
# Unlike other tools in ~/tools/, Nix installs system-wide (/nix/) and manages
# its own package store. This script wraps the Determinate Systems installer.
#
# Determinate Nix is a downstream distribution of NixOS/nix with flakes enabled
# by default and clean install/uninstall.
# See: https://docs.determinate.systems/determinate-nix/
#
# Requires: curl, sudo (for multi-user daemon mode)
set -e

TOOL_NAME="nix"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../install-utils.sh"

# Handle flags.
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            cat << 'EOF'
Usage: nix/install.sh [--force]

Install Determinate Nix (system-wide to /nix/).

Unlike other tools in ~/tools/, Nix installs system-wide and manages its own
package store. Projects declare their development dependencies in a flake.nix
file, and `use_flake` in .envrc activates them automatically via direnv.

Options:
  --force    Reinstall even if already present

Requires: curl, sudo
EOF
            exit 0
            ;;
        -f|--force)
            FORCE=true
            shift
            ;;
        *)
            error "Unknown argument: $1"
            exit 1
            ;;
    esac
done

# Check if already installed.
NIX_BIN=""
if command -v nix &>/dev/null; then
    NIX_BIN="nix"
elif [ -x /nix/var/nix/profiles/default/bin/nix ]; then
    NIX_BIN="/nix/var/nix/profiles/default/bin/nix"
fi

if [ -n "$NIX_BIN" ] && [ "$FORCE" != "true" ]; then
    installed_version="$($NIX_BIN --version 2>/dev/null)"
    info "Already installed: $installed_version"
    info "Use --force to reinstall."
    exit 0
fi

if [ "$FORCE" = "true" ] && [ -n "$NIX_BIN" ]; then
    info "Force reinstalling — uninstalling current installation first..."
    if [ -x /nix/nix-installer ]; then
        sudo /nix/nix-installer uninstall --no-confirm
    else
        curl -fsSL https://install.determinate.systems/nix | sh -s -- uninstall --no-confirm
    fi
fi

info "Installing Determinate Nix..."
curl -fsSL https://install.determinate.systems/nix | sh -s -- install --no-confirm

# Source the daemon profile so nix is available in this shell.
if [ -f /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh ]; then
    . /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh
fi

if command -v nix &>/dev/null; then
    installed_version="$(nix --version)"
    info "Installed: $installed_version"
else
    error "nix not found after installation. Restart your shell."
    exit 1
fi

info "Nix installed successfully!"
info "Restart your shell or run: . /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh"
