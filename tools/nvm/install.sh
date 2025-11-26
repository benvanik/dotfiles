#!/bin/bash
# Install NVM (Node Version Manager).
# Usage: nvm/install.sh
set -e

TOOL_NAME="nvm"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../install-utils.sh"

NVM_DIR="${NVM_DIR:-$HOME/.nvm}"

# Handle flags.
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            cat << EOF
Usage: nvm/install.sh

Install NVM (Node Version Manager) to ~/.nvm/

NVM is installed using the official install script from GitHub.
After installation, use 'nvm install <version>' to install Node.js.

Options:
    --force     Reinstall even if NVM already exists

Examples:
    nvm install --lts    # Install latest LTS
    nvm install 20       # Install Node 20.x
    nvm use 20           # Switch to Node 20

More info: https://github.com/nvm-sh/nvm
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

# Check if already installed.
if [ "$FORCE" != "true" ] && [ -d "$NVM_DIR" ]; then
    warn "NVM already installed at $NVM_DIR"
    info "To update: cd ~/.nvm && git pull"
    info "Use --force to reinstall"
    exit 0
fi

info "Installing NVM..."

# Install using official script.
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/master/install.sh | bash

info "NVM installed successfully!"
echo ""
echo "Restart your shell or run:"
echo "  source ~/.nvm/nvm.sh"
echo ""
echo "Then install Node.js:"
echo "  nvm install --lts"
