#!/bin/bash
# Install an attested mold release without making it an ambient linker.
set -euo pipefail

# shellcheck disable=SC2034  # Read by install-utils.sh.
TOOL_NAME="mold"
SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../install-utils.sh
source "$SCRIPT_DIRECTORY/../install-utils.sh"

if [ "$PLATFORM" != "linux" ]; then
    error "mold release bundles are supported only on Linux"
    exit 1
fi

MOLD_DIRECTORY="$TOOLS_DIR/mold"
VERSION=""

show_help() {
    cat << 'EOF'
Usage: mold/install.sh [OPTIONS] [VERSION]

Install an attested mold release under ~/tools/mold/<version>/.
mold remains explicit-only: activate it in a project with use_mold.

Options:
    --force     Transactionally replace the selected managed version
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
                error "Expected at most one mold version"
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
                error "Expected at most one mold version"
                exit 1
            fi
            VERSION="$1"
            shift
            ;;
    esac
done

if [ -n "$VERSION" ] &&
        [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    error "Invalid mold version: $VERSION"
    exit 1
fi
prepare_managed_directory_root "$MOLD_DIRECTORY" "mold installation root"

if [ -z "$VERSION" ]; then
    info "Fetching latest mold version..."
    VERSION=$(
        curl -fsSL https://api.github.com/repos/rui314/mold/releases/latest |
            python3 -c '
import json
import sys

tag = json.load(sys.stdin).get("tag_name", "")
if not tag.startswith("v"):
    raise SystemExit("latest mold release has no v-prefixed tag")
print(tag[1:])
'
    )
fi
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    error "Invalid mold version: $VERSION"
    exit 1
fi

case "$ARCH" in
    x86_64) RELEASE_ARCHITECTURE="x86_64" ;;
    aarch64) RELEASE_ARCHITECTURE="aarch64" ;;
    *)
        error "Unsupported mold architecture: $ARCH"
        exit 1
        ;;
esac

ASSET_NAME="mold-$VERSION-$RELEASE_ARCHITECTURE-linux.tar.gz"
ARCHIVE_ROOT="mold-$VERSION-$RELEASE_ARCHITECTURE-linux"
INSTALL_DIRECTORY="$MOLD_DIRECTORY/$VERSION"
IDENTITY_PATH="$INSTALL_DIRECTORY/.dotfiles-install-identity"
acquire_managed_installation_guard "$MOLD_DIRECTORY" "$VERSION"
recover_managed_installation "$MOLD_DIRECTORY" "$VERSION"

identity_value() {
    sed -n "s/^$2=//p" "$1"
}

installation_valid() {
    local mold_sha256

    [ -d "$INSTALL_DIRECTORY" ] && [ ! -L "$INSTALL_DIRECTORY" ] || return 1
    [ -f "$INSTALL_DIRECTORY/bin/mold" ] &&
        [ ! -L "$INSTALL_DIRECTORY/bin/mold" ] &&
        [ -x "$INSTALL_DIRECTORY/bin/mold" ] || return 1
    [ -f "$IDENTITY_PATH" ] && [ ! -L "$IDENTITY_PATH" ] || return 1
    [ "$(wc -l < "$IDENTITY_PATH")" -eq 6 ] || return 1
    [ "$(identity_value "$IDENTITY_PATH" format)" = "1" ] || return 1
    [ "$(identity_value "$IDENTITY_PATH" version)" = "$VERSION" ] || return 1
    [ "$(identity_value "$IDENTITY_PATH" platform)" = "linux-$ARCH" ] || return 1
    [ "$(identity_value "$IDENTITY_PATH" asset)" = "$ASSET_NAME" ] || return 1
    mold_sha256=$(identity_value "$IDENTITY_PATH" mold_sha256)
    verify_sha256 "$INSTALL_DIRECTORY/bin/mold" "$mold_sha256" || return 1
    "$INSTALL_DIRECTORY/bin/mold" --version |
        grep -Fq "mold $VERSION"
}

if [ -L "$INSTALL_DIRECTORY" ] ||
        { [ -e "$INSTALL_DIRECTORY" ] && [ ! -d "$INSTALL_DIRECTORY" ]; }; then
    error "Refusing non-directory mold installation: $INSTALL_DIRECTORY"
    exit 1
fi
if [ "$FORCE" != "true" ] && installation_valid; then
    warn "mold $VERSION is already installed"
    update_latest "$MOLD_DIRECTORY" "$VERSION"
    exit 0
fi
if [ -e "$INSTALL_DIRECTORY" ] && [ "$FORCE" != "true" ]; then
    error "Existing mold $VERSION installation has no valid recorded identity"
    error "Inspect it, then rerun with --force to replace it transactionally"
    exit 1
fi

STAGING_DIRECTORY=$(
    create_managed_staging_directory "$MOLD_DIRECTORY" "$VERSION"
)
ARCHIVE_PATH="$STAGING_DIRECTORY/$ASSET_NAME"
EXTRACT_DIRECTORY="$STAGING_DIRECTORY/extract"
mkdir "$EXTRACT_DIRECTORY"
cleanup_staging() {
    local final_status=$?

    trap - EXIT HUP INT TERM
    if { [ -e "$STAGING_DIRECTORY" ] || [ -L "$STAGING_DIRECTORY" ]; } &&
            ! remove_managed_tree "$MOLD_DIRECTORY" "$STAGING_DIRECTORY"; then
        error "mold staging directory requires inspection: $STAGING_DIRECTORY"
        final_status=1
    fi
    exit "$final_status"
}
trap cleanup_staging EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

info "Resolving mold release attestation..."
ARCHIVE_SHA256=$(github_release_asset_sha256 \
    "rui314/mold" "v$VERSION" "$ASSET_NAME")
download \
    "https://github.com/rui314/mold/releases/download/v$VERSION/$ASSET_NAME" \
    "$ARCHIVE_PATH"
verify_sha256 "$ARCHIVE_PATH" "$ARCHIVE_SHA256"
validate_single_root_tar_archive "$ARCHIVE_PATH" "$ARCHIVE_ROOT"
tar xf "$ARCHIVE_PATH" -C "$EXTRACT_DIRECTORY" --no-same-owner
PAYLOAD_DIRECTORY="$EXTRACT_DIRECTORY/$ARCHIVE_ROOT"
if [ ! -d "$PAYLOAD_DIRECTORY" ] || [ -L "$PAYLOAD_DIRECTORY" ] ||
        [ ! -f "$PAYLOAD_DIRECTORY/bin/mold" ] ||
        [ -L "$PAYLOAD_DIRECTORY/bin/mold" ] ||
        [ ! -x "$PAYLOAD_DIRECTORY/bin/mold" ]; then
    error "mold archive did not contain the expected executable payload"
    exit 1
fi
if ! "$PAYLOAD_DIRECTORY/bin/mold" --version | grep -Fq "mold $VERSION"; then
    error "mold executable version does not match $VERSION"
    exit 1
fi
MOLD_SHA256=$(
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$PAYLOAD_DIRECTORY/bin/mold"
    else
        shasum -a 256 "$PAYLOAD_DIRECTORY/bin/mold"
    fi
)
MOLD_SHA256="${MOLD_SHA256%% *}"
printf '%s\n' \
    "format=1" \
    "version=$VERSION" \
    "platform=linux-$ARCH" \
    "asset=$ASSET_NAME" \
    "archive_sha256=$ARCHIVE_SHA256" \
    "mold_sha256=$MOLD_SHA256" \
    > "$PAYLOAD_DIRECTORY/.dotfiles-install-identity"

publish_staged_directory "$MOLD_DIRECTORY" "$VERSION" "$PAYLOAD_DIRECTORY"
update_latest "$MOLD_DIRECTORY" "$VERSION"
info "mold $VERSION installed successfully"
