#!/bin/bash
# Install the official Hugging Face CLI in one versioned private environment.
set -euo pipefail

# shellcheck disable=SC2034  # Read by install-utils.sh.
TOOL_NAME="hf"
SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIRECTORY/../install-utils.sh"

DEFAULT_VERSION="1.24.0"
HF_DIRECTORY="$TOOLS_DIR/hf"
VERSION=""

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
        --)
            shift
            if [ $# -gt 1 ]; then
                error "Expected at most one hf version"
                exit 1
            fi
            VERSION="${1:-}"
            shift "$#"
            ;;
        -*)
            error "Unknown option: $1"
            exit 1
            ;;
        *)
            if [ -n "$VERSION" ]; then
                error "Expected at most one hf version"
                exit 1
            fi
            VERSION="$1"
            shift
            ;;
    esac
done

VERSION="${VERSION:-$DEFAULT_VERSION}"
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    error "Invalid hf version: $VERSION"
    exit 1
fi
prepare_managed_directory_root "$HF_DIRECTORY" "hf installation root"
if ! command -v uv >/dev/null 2>&1; then
    error "uv is required to install hf"
    exit 1
fi

INSTALL_DIRECTORY="$HF_DIRECTORY/$VERSION"
IDENTITY_PATH="$INSTALL_DIRECTORY/.dotfiles-install-identity"
CLOSURE_PATH="$INSTALL_DIRECTORY/.dotfiles-python-closure"
acquire_managed_installation_guard "$HF_DIRECTORY" "$VERSION"
recover_managed_installation "$HF_DIRECTORY" "$VERSION"

python_closure() {
    "$1" -c '
import importlib.metadata

entries = sorted(
    (distribution.metadata["Name"].lower(), distribution.version)
    for distribution in importlib.metadata.distributions()
)
for name, version in entries:
    print(f"{name}=={version}")
'
}

identity_value() {
    sed -n "s/^$2=//p" "$1"
}

installation_valid() {
    local installed_version
    local installed_closure
    local recorded_closure
    local hf_sha256

    [ -d "$INSTALL_DIRECTORY" ] && [ ! -L "$INSTALL_DIRECTORY" ] || return 1
    [ -d "$INSTALL_DIRECTORY/bin" ] &&
        [ ! -L "$INSTALL_DIRECTORY/bin" ] || return 1
    [ -f "$INSTALL_DIRECTORY/bin/hf" ] &&
        [ ! -L "$INSTALL_DIRECTORY/bin/hf" ] &&
        [ -x "$INSTALL_DIRECTORY/bin/hf" ] || return 1
    [ -x "$INSTALL_DIRECTORY/bin/python" ] || return 1
    [ -f "$IDENTITY_PATH" ] && [ ! -L "$IDENTITY_PATH" ] || return 1
    [ -f "$CLOSURE_PATH" ] && [ ! -L "$CLOSURE_PATH" ] || return 1
    [ "$(wc -l < "$IDENTITY_PATH")" -eq 5 ] || return 1
    [ "$(identity_value "$IDENTITY_PATH" format)" = "1" ] || return 1
    [ "$(identity_value "$IDENTITY_PATH" version)" = "$VERSION" ] || return 1
    [ "$(identity_value "$IDENTITY_PATH" closure)" = \
        "python-distributions-v1" ] || return 1
    [ "$(identity_value "$IDENTITY_PATH" python)" = \
        "$("$INSTALL_DIRECTORY/bin/python" --version)" ] || return 1
    installed_version=$(
        "$INSTALL_DIRECTORY/bin/python" -c \
            'import importlib.metadata; print(importlib.metadata.version("hf"))'
    ) || return 1
    [ "$installed_version" = "$VERSION" ] || return 1
    installed_closure=$(python_closure "$INSTALL_DIRECTORY/bin/python") ||
        return 1
    recorded_closure=$(cat "$CLOSURE_PATH") || return 1
    [ "$installed_closure" = "$recorded_closure" ] || return 1
    hf_sha256=$(identity_value "$IDENTITY_PATH" hf_sha256)
    verify_sha256 "$INSTALL_DIRECTORY/bin/hf" "$hf_sha256" || return 1
    HF_HUB_DISABLE_UPDATE_CHECK=1 \
        "$INSTALL_DIRECTORY/bin/hf" --help >/dev/null
}

if [ -L "$INSTALL_DIRECTORY" ] ||
        { [ -e "$INSTALL_DIRECTORY" ] && [ ! -d "$INSTALL_DIRECTORY" ]; }; then
    error "Refusing non-directory hf installation: $INSTALL_DIRECTORY"
    exit 1
fi
if [ "$FORCE" != "true" ] && installation_valid; then
    warn "hf $VERSION is already installed"
    update_latest "$HF_DIRECTORY" "$VERSION"
    exit 0
fi
if [ -e "$INSTALL_DIRECTORY" ] && [ "$FORCE" != "true" ]; then
    error "Existing hf $VERSION installation has no valid recorded identity"
    error "Inspect it, then rerun with --force to replace it transactionally"
    exit 1
fi

STAGING_DIRECTORY="$(
    create_managed_staging_directory "$HF_DIRECTORY" "$VERSION"
)"
cleanup() {
    local final_status=$?

    trap - EXIT HUP INT TERM
    if { [ -e "$STAGING_DIRECTORY" ] || [ -L "$STAGING_DIRECTORY" ]; } &&
            ! remove_managed_tree "$HF_DIRECTORY" "$STAGING_DIRECTORY"; then
        error "hf staging directory requires inspection: $STAGING_DIRECTORY"
        final_status=1
    fi
    exit "$final_status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

info "Creating Python 3.13 environment for hf $VERSION..."
uv venv --no-config --no-project --relocatable --python 3.13 \
    "$STAGING_DIRECTORY"
info "Installing official hf==$VERSION from PyPI..."
uv pip install --no-config --no-sources --strict --only-binary :all: \
    --python "$STAGING_DIRECTORY/bin/python" "hf==$VERSION"
uv pip check --python "$STAGING_DIRECTORY/bin/python"

INSTALLED_VERSION="$(
    "$STAGING_DIRECTORY/bin/python" -c \
        'import importlib.metadata; print(importlib.metadata.version("hf"))'
)"
if [ "$INSTALLED_VERSION" != "$VERSION" ]; then
    error "Installed hf version $INSTALLED_VERSION does not match $VERSION"
    exit 1
fi

python_closure "$STAGING_DIRECTORY/bin/python" \
    > "$STAGING_DIRECTORY/.dotfiles-python-closure"
HF_SHA256=$(
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$STAGING_DIRECTORY/bin/hf"
    else
        shasum -a 256 "$STAGING_DIRECTORY/bin/hf"
    fi
)
HF_SHA256="${HF_SHA256%% *}"
PYTHON_VERSION=$("$STAGING_DIRECTORY/bin/python" --version)
printf '%s\n' \
    "format=1" \
    "version=$VERSION" \
    "python=$PYTHON_VERSION" \
    "hf_sha256=$HF_SHA256" \
    "closure=python-distributions-v1" \
    > "$STAGING_DIRECTORY/.dotfiles-install-identity"

if ! HF_HUB_DISABLE_UPDATE_CHECK=1 \
        "$STAGING_DIRECTORY/bin/hf" --help >/dev/null; then
    error "Installed hf executable failed its smoke test"
    exit 1
fi

managed_installer_test_fault before-publication
publish_staged_directory "$HF_DIRECTORY" "$VERSION" "$STAGING_DIRECTORY"
update_latest "$HF_DIRECTORY" "$VERSION"
info "hf $VERSION installed"
