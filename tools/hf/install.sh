#!/bin/bash
# Install the official Hugging Face CLI in one versioned private environment.
set -euo pipefail

# shellcheck disable=SC2034  # Read by install-utils.sh.
TOOL_NAME="hf"
SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIRECTORY/../install-utils.sh"

DEFAULT_VERSION="1.24.0"
HF_DIRECTORY="$TOOLS_DIR/hf"

show_help() {
    cat << EOF
Usage: hf/install.sh [--force] [VERSION]

Install the official hf CLI into ~/tools/hf/<version>/ with uv.
The dotfiles wrapper pins version $DEFAULT_VERSION and keeps credentials out
of the environment and model cache.

Options:
    --force     Replace only the selected managed version directory
EOF
}

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
        *)
            break
            ;;
    esac
done

VERSION="${1:-$DEFAULT_VERSION}"
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    error "Invalid hf version: $VERSION"
    exit 1
fi
if [ $# -gt 1 ]; then
    error "Unexpected argument: $2"
    exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
    error "uv is required to install hf"
    exit 1
fi

INSTALL_DIRECTORY="$HF_DIRECTORY/$VERSION"
if version_installed "$HF_DIRECTORY" "$VERSION"; then
    if [ ! -x "$INSTALL_DIRECTORY/bin/hf" ]; then
        error "Existing hf $VERSION installation is incomplete"
        exit 1
    fi
    warn "hf $VERSION is already installed"
    update_latest "$HF_DIRECTORY" "$VERSION"
    exit 0
fi

mkdir -p "$HF_DIRECTORY"
STAGING_DIRECTORY="$(mktemp -d "$HF_DIRECTORY/.install-$VERSION.XXXXXX")"
INSTALL_COMPLETE=false
cleanup() {
    if [ "$INSTALL_COMPLETE" = false ] && [ -d "$STAGING_DIRECTORY" ]; then
        rm -rf -- "$STAGING_DIRECTORY"
    fi
}
trap cleanup EXIT

info "Creating Python 3.13 environment for hf $VERSION..."
uv venv --no-config --no-project --relocatable --python 3.13 \
    "$STAGING_DIRECTORY"
info "Installing official hf==$VERSION from PyPI..."
uv pip install --no-config --no-sources --strict --only-binary :all: \
    --python "$STAGING_DIRECTORY/bin/python" "hf==$VERSION"

INSTALLED_VERSION="$(
    "$STAGING_DIRECTORY/bin/python" -c \
        'import importlib.metadata; print(importlib.metadata.version("hf"))'
)"
if [ "$INSTALLED_VERSION" != "$VERSION" ]; then
    error "Installed hf version $INSTALLED_VERSION does not match $VERSION"
    exit 1
fi

if [ -e "$INSTALL_DIRECTORY" ]; then
    if [ "$FORCE" != true ]; then
        error "Refusing to replace existing hf directory: $INSTALL_DIRECTORY"
        exit 1
    fi
    rm -rf -- "$INSTALL_DIRECTORY"
fi
mv "$STAGING_DIRECTORY" "$INSTALL_DIRECTORY"
INSTALL_COMPLETE=true
update_latest "$HF_DIRECTORY" "$VERSION"
info "hf $VERSION installed"
