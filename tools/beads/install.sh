#!/bin/bash
# Install attested beads CLI (br) and viewer (bv) release assets.
#
# Installs:
#   br (beads CLI)     from Dicklesworthstone/beads_rust
#   bv (beads viewer)  from Dicklesworthstone/beads_viewer
#   bd -> br symlink   for command-name compatibility
set -euo pipefail

# shellcheck disable=SC2034  # Read by install-utils.sh.
TOOL_NAME="beads"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../install-utils.sh
source "$SCRIPT_DIR/../install-utils.sh"

BEADS_DIR="$TOOLS_DIR/beads"
LOCAL_BIN="$HOME/.local/bin"
BR_VERSION=""

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
        --)
            shift
            if [ $# -gt 1 ]; then
                error "Expected at most one br version"
                exit 1
            fi
            BR_VERSION="${1:-}"
            shift "$#"
            ;;
        -*)
            error "Unknown option: $1"
            exit 1
            ;;
        *)
            if [ -n "$BR_VERSION" ]; then
                error "Expected at most one br version"
                exit 1
            fi
            BR_VERSION="$1"
            shift
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
        BR_SUFFIX="linux_arm64"
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

if [ -n "$BR_VERSION" ] &&
        [[ ! "$BR_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([A-Za-z0-9._+-]*)?$ ]]; then
    error "Invalid br version: $BR_VERSION"
    exit 1
fi
prepare_managed_directory_root "$BEADS_DIR" "Beads installation root"
prepare_managed_directory_root "$LOCAL_BIN" "local command directory"

# Fetch latest br version.
if [ -z "$BR_VERSION" ]; then
    info "Fetching latest br version..."
    BR_VERSION=$(
        curl -fsSL \
            https://api.github.com/repos/Dicklesworthstone/beads_rust/releases/latest |
            python3 -c '
import json
import sys

tag = json.load(sys.stdin).get("tag_name", "")
if not tag.startswith("v"):
    raise SystemExit("latest br release has no v-prefixed tag")
print(tag[1:])
'
    )
fi
if [[ ! "$BR_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([A-Za-z0-9._+-]*)?$ ]]; then
    error "Invalid br version: $BR_VERSION"
    exit 1
fi

# Fetch latest bv version (always latest, independent of br version).
info "Fetching latest bv version..."
BV_VERSION=$(
    curl -fsSL \
        https://api.github.com/repos/Dicklesworthstone/beads_viewer/releases/latest |
        python3 -c '
import json
import sys

tag = json.load(sys.stdin).get("tag_name", "")
if not tag.startswith("v"):
    raise SystemExit("latest bv release has no v-prefixed tag")
print(tag[1:])
'
)
if [[ ! "$BV_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([A-Za-z0-9._+-]*)?$ ]]; then
    error "Invalid bv version: $BV_VERSION"
    exit 1
fi

ACTIVE_STAGING_DIRECTORY=""
cleanup_staging() {
    local final_status=$?

    trap - EXIT HUP INT TERM
    if [ -n "$ACTIVE_STAGING_DIRECTORY" ] &&
            { [ -e "$ACTIVE_STAGING_DIRECTORY" ] ||
                [ -L "$ACTIVE_STAGING_DIRECTORY" ]; }; then
        if ! remove_managed_tree \
                "$BEADS_DIR" "$ACTIVE_STAGING_DIRECTORY"; then
            error "Beads staging directory requires inspection: $ACTIVE_STAGING_DIRECTORY"
            final_status=1
        fi
    fi
    exit "$final_status"
}
trap cleanup_staging EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

identity_value() {
    sed -n "s/^$2=//p" "$1"
}

component_valid() {
    local component="$1"
    local version="$2"
    local suffix="$3"
    local install_directory="$BEADS_DIR/$component-$version"
    local identity_path="$install_directory/.dotfiles-install-identity"
    local binary_path="$install_directory/bin/$component"
    local binary_sha256

    [ -d "$install_directory" ] && [ ! -L "$install_directory" ] || return 1
    [ -f "$binary_path" ] && [ ! -L "$binary_path" ] &&
        [ -x "$binary_path" ] || return 1
    [ -f "$identity_path" ] && [ ! -L "$identity_path" ] || return 1
    [ "$(wc -l < "$identity_path")" -eq 7 ] || return 1
    [ "$(identity_value "$identity_path" format)" = "1" ] || return 1
    [ "$(identity_value "$identity_path" component)" = "$component" ] || return 1
    [ "$(identity_value "$identity_path" version)" = "$version" ] || return 1
    [ "$(identity_value "$identity_path" platform)" = "$suffix" ] || return 1
    binary_sha256=$(identity_value "$identity_path" binary_sha256)
    verify_sha256 "$binary_path" "$binary_sha256"
}

install_component() {
    local component="$1"
    local repository="$2"
    local version="$3"
    local suffix="$4"
    shift 4
    local install_version="$component-$version"
    local install_directory="$BEADS_DIR/$install_version"
    local selection
    local asset_name
    local archive_sha256
    local staging_directory
    local archive_path
    local payload_directory
    local binary_path
    local binary_sha256

    acquire_managed_installation_guard \
        "$BEADS_DIR" "$install_version" || return 1
    recover_managed_installation \
        "$BEADS_DIR" "$install_version" || return 1
    if [ -L "$install_directory" ] ||
            { [ -e "$install_directory" ] && [ ! -d "$install_directory" ]; }; then
        error "Refusing non-directory $component installation: $install_directory"
        return 1
    fi
    if [ "$FORCE" != "true" ] &&
            component_valid "$component" "$version" "$suffix"; then
        warn "$component $version is already installed"
        return 0
    fi
    if [ -e "$install_directory" ] && [ "$FORCE" != "true" ]; then
        error "Existing $component $version installation has no valid recorded identity"
        error "Inspect it, then rerun with --force to replace it transactionally"
        return 1
    fi

    staging_directory=$(
        create_managed_staging_directory "$BEADS_DIR" "$install_version"
    )
    ACTIVE_STAGING_DIRECTORY="$staging_directory"
    payload_directory="$staging_directory/payload"
    mkdir -p "$payload_directory/bin"

    selection=$(github_release_asset_selection \
        "$repository" "v$version" "$@") || {
        remove_managed_tree "$BEADS_DIR" "$staging_directory"
        return 1
    }
    asset_name=$(printf '%s\n' "$selection" | sed -n '1p')
    archive_sha256=$(printf '%s\n' "$selection" | sed -n '2p')
    archive_path="$staging_directory/$asset_name"
    download \
        "https://github.com/$repository/releases/download/v$version/$asset_name" \
        "$archive_path" || {
        remove_managed_tree "$BEADS_DIR" "$staging_directory"
        return 1
    }
    verify_sha256 "$archive_path" "$archive_sha256" || {
        remove_managed_tree "$BEADS_DIR" "$staging_directory"
        return 1
    }
    binary_path="$payload_directory/bin/$component"
    extract_regular_tar_member "$archive_path" "$component" "$binary_path" || {
        remove_managed_tree "$BEADS_DIR" "$staging_directory"
        return 1
    }
    chmod 755 "$binary_path"
    "$binary_path" --version >/dev/null || {
        remove_managed_tree "$BEADS_DIR" "$staging_directory"
        error "$component release executable failed its version probe"
        return 1
    }
    binary_sha256=$(
        if command -v sha256sum >/dev/null 2>&1; then
            sha256sum "$binary_path"
        else
            shasum -a 256 "$binary_path"
        fi
    )
    binary_sha256="${binary_sha256%% *}"
    printf '%s\n' \
        "format=1" \
        "component=$component" \
        "version=$version" \
        "platform=$suffix" \
        "asset=$asset_name" \
        "archive_sha256=$archive_sha256" \
        "binary_sha256=$binary_sha256" \
        > "$payload_directory/.dotfiles-install-identity"
    publish_staged_directory "$BEADS_DIR" "$install_version" "$payload_directory" ||
        return 1
    remove_managed_tree "$BEADS_DIR" "$staging_directory"
    ACTIVE_STAGING_DIRECTORY=""
}

info "Installing br $BR_VERSION + bv $BV_VERSION"
install_component \
    br Dicklesworthstone/beads_rust "$BR_VERSION" "$BR_SUFFIX" \
    "br-$BR_VERSION-$BR_SUFFIX.tar.gz" \
    "br-v$BR_VERSION-$BR_SUFFIX.tar.gz"
install_component \
    bv Dicklesworthstone/beads_viewer "$BV_VERSION" "$BV_SUFFIX" \
    "bv_${BV_SUFFIX}.tar.gz" \
    "bv_${BV_VERSION}_${BV_SUFFIX}.tar.gz"

update_latest "$BEADS_DIR" "br-$BR_VERSION"
update_command_symlink "$BEADS_DIR/br-$BR_VERSION/bin/br" "$LOCAL_BIN/br"
update_command_symlink "$BEADS_DIR/br-$BR_VERSION/bin/br" "$LOCAL_BIN/bd"
update_command_symlink "$BEADS_DIR/bv-$BV_VERSION/bin/bv" "$LOCAL_BIN/bv"

info "Beads installed successfully!"
