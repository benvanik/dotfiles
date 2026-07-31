#!/bin/bash
# Install an attested Bazel development-tool bundle.
# Usage: bazel/install.sh [buildtools-version]
set -euo pipefail

TOOL_NAME="bazel"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../install-utils.sh"

BAZEL_DIR="$TOOLS_DIR/bazel"
BAZELISK_VERSION="1.29.0"
IBAZEL_VERSION="0.32.0"
VERSION=""

# Handle flags.
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            cat << EOF
Usage: bazel/install.sh [buildtools-version]

Install Bazel development tools to ~/tools/bazel/<version>/

Installs:
  - bazelisk $BAZELISK_VERSION (as 'bazel') - Bazel version manager
  - buildifier - BUILD file formatter
  - buildozer - BUILD file editor
  - ibazel $IBAZEL_VERSION - Bazel file watcher

Options:
  --force    Reinstall even if version exists

Examples:
  bazel/install.sh           # Install latest
  bazel/install.sh 8.2.1     # Install specific buildtools version

Releases:
  https://github.com/bazelbuild/bazelisk/releases
  https://github.com/bazelbuild/buildtools/releases
  https://github.com/bazelbuild/bazel-watcher/releases
EOF
            exit 0
            ;;
        -f|--force)
            FORCE=true
            shift
            ;;
        --)
            shift
            if [ $# -gt 1 ]; then
                error "Expected at most one buildtools version"
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
                error "Expected at most one buildtools version"
                exit 1
            fi
            VERSION="$1"
            shift
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
if [ -z "$VERSION" ]; then
    info "Fetching latest buildtools version..."
    VERSION=$(
        curl -fsSL \
            https://api.github.com/repos/bazelbuild/buildtools/releases/latest |
            python3 -c '
import json
import sys

tag = json.load(sys.stdin).get("tag_name", "")
if not isinstance(tag, str) or not tag.startswith("v"):
    raise SystemExit("latest buildtools release has no canonical v-prefixed tag")
print(tag[1:])
'
    ) || {
        error "Failed to fetch latest version. Specify manually: bazel/install.sh 8.2.1"
        exit 1
    }
fi
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    error "Invalid buildtools version: $VERSION"
    error "Expected a numeric major.minor.patch version such as 8.2.1"
    exit 1
fi

info "Installing Bazel tools (buildtools $VERSION)"

# Create the managed root only after arguments and platform are valid.
if [ -L "$BAZEL_DIR" ] ||
        { [ -e "$BAZEL_DIR" ] && [ ! -d "$BAZEL_DIR" ]; }; then
    error "Managed Bazel root is not an ordinary directory: $BAZEL_DIR"
    exit 1
fi
mkdir -p "$BAZEL_DIR"
cd "$BAZEL_DIR"
INSTALL_DIR="$BAZEL_DIR/$VERSION"
acquire_managed_installation_guard "$BAZEL_DIR" "$VERSION"
recover_managed_installation "$BAZEL_DIR" "$VERSION"
if [ -L "$INSTALL_DIR" ] || { [ -e "$INSTALL_DIR" ] && [ ! -d "$INSTALL_DIR" ]; }; then
    error "Refusing non-directory Bazel installation: $INSTALL_DIR"
    exit 1
fi
if [ -L "$INSTALL_DIR/bin" ] ||
        { [ -e "$INSTALL_DIR/bin" ] && [ ! -d "$INSTALL_DIR/bin" ]; }; then
    error "Refusing non-directory Bazel binary directory: $INSTALL_DIR/bin"
    exit 1
fi

# Map the pinned ibazel release to its attested platform asset.
IBAZEL_SUFFIX=${SUFFIX/-/_}
case "$IBAZEL_SUFFIX" in
    linux_amd64)
        IBAZEL_SHA256="761cb60545f3de5bc0615d2b0f58accd4186161ac6cdd2a168ad6ee59731b92e"
        ;;
    linux_arm64)
        IBAZEL_SHA256="3f2c3c0b629a426cb5452fdf54b88c92b554344689e67c592046dbbc017fa562"
        ;;
    darwin_amd64)
        IBAZEL_SHA256="781e6113fc8f3d41299a001fe2e4780c1f6cc3d236ace8af69a87558ade07df4"
        ;;
    darwin_arm64)
        IBAZEL_SHA256="1cfec3c53213520ddba3d8ff6dbc85ac0ec0c07e9703dc44695e1af166b009ab"
        ;;
    *)
        error "No ibazel attestation for $IBAZEL_SUFFIX"
        exit 1
        ;;
esac

metadata_value() {
    local metadata_path="$1"
    local key="$2"
    sed -n "s/^${key}=//p" "$metadata_path"
}

ordinary_executable() {
    [ -f "$1" ] && [ ! -L "$1" ] && [ -x "$1" ]
}

bundle_installed() {
    local bundle_root="$1"
    local metadata_path="$bundle_root/.dotfiles-install-identity"
    local bazel_sha256
    local buildifier_sha256
    local buildozer_sha256
    local ibazel_sha256

    ordinary_executable "$bundle_root/bin/bazel" || return 1
    ordinary_executable "$bundle_root/bin/buildifier" || return 1
    ordinary_executable "$bundle_root/bin/buildozer" || return 1
    ordinary_executable "$bundle_root/bin/ibazel" || return 1
    [ -f "$metadata_path" ] && [ ! -L "$metadata_path" ] || return 1
    [ "$(wc -l < "$metadata_path")" -eq 9 ] || return 1
    [ "$(metadata_value "$metadata_path" format)" = "1" ] || return 1
    [ "$(metadata_value "$metadata_path" buildtools_version)" = "$VERSION" ] ||
        return 1
    [ "$(metadata_value "$metadata_path" platform)" = "$SUFFIX" ] || return 1
    [ "$(metadata_value "$metadata_path" bazelisk_version)" = "$BAZELISK_VERSION" ] ||
        return 1
    [ "$(metadata_value "$metadata_path" ibazel_version)" = "$IBAZEL_VERSION" ] ||
        return 1

    bazel_sha256=$(metadata_value "$metadata_path" bazel_sha256)
    buildifier_sha256=$(metadata_value "$metadata_path" buildifier_sha256)
    buildozer_sha256=$(metadata_value "$metadata_path" buildozer_sha256)
    ibazel_sha256=$(metadata_value "$metadata_path" ibazel_sha256)
    [ "$ibazel_sha256" = "$IBAZEL_SHA256" ] || return 1
    verify_sha256 "$bundle_root/bin/bazel" "$bazel_sha256" &&
        verify_sha256 "$bundle_root/bin/buildifier" "$buildifier_sha256" &&
        verify_sha256 "$bundle_root/bin/buildozer" "$buildozer_sha256" &&
        verify_sha256 "$bundle_root/bin/ibazel" "$ibazel_sha256"
}

if [ "$FORCE" != "true" ] && bundle_installed "$INSTALL_DIR"; then
    warn "Version $VERSION already installed"
    update_latest "$BAZEL_DIR" "$VERSION"
    exit 0
fi

STAGING_DIR=$(create_managed_staging_directory "$BAZEL_DIR" "$VERSION")
PAYLOAD_DIR="$STAGING_DIR/payload"
mkdir -p "$PAYLOAD_DIR/bin"
cleanup_staging() {
    local final_status=$?

    trap - EXIT HUP INT TERM
    if { [ -e "$STAGING_DIR" ] || [ -L "$STAGING_DIR" ]; } &&
            ! remove_managed_tree "$BAZEL_DIR" "$STAGING_DIR"; then
        error "Bazel staging directory requires inspection: $STAGING_DIR"
        final_status=1
    fi
    exit "$final_status"
}
trap cleanup_staging EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

install_binary() {
    local url="$1"
    local destination="$2"
    local expected_sha256="$3"

    download "$url" "$destination"
    verify_sha256 "$destination" "$expected_sha256"
    chmod 755 "$destination"
}

BAZELISK_ASSET="bazelisk-$SUFFIX"
BUILDIFIER_ASSET="buildifier-$SUFFIX"
BUILDOZER_ASSET="buildozer-$SUFFIX"
IBAZEL_ASSET="ibazel_$IBAZEL_SUFFIX"

info "Resolving release attestations..."
BAZEL_SHA256=$(github_release_asset_sha256 \
    "bazelbuild/bazelisk" "v$BAZELISK_VERSION" "$BAZELISK_ASSET")
BUILDIFIER_SHA256=$(github_release_asset_sha256 \
    "bazelbuild/buildtools" "v$VERSION" "$BUILDIFIER_ASSET")
BUILDOZER_SHA256=$(github_release_asset_sha256 \
    "bazelbuild/buildtools" "v$VERSION" "$BUILDOZER_ASSET")

info "Downloading bazelisk $BAZELISK_VERSION..."
install_binary \
    "https://github.com/bazelbuild/bazelisk/releases/download/v$BAZELISK_VERSION/$BAZELISK_ASSET" \
    "$PAYLOAD_DIR/bin/bazel" \
    "$BAZEL_SHA256"

info "Downloading buildifier $VERSION..."
install_binary \
    "https://github.com/bazelbuild/buildtools/releases/download/v$VERSION/$BUILDIFIER_ASSET" \
    "$PAYLOAD_DIR/bin/buildifier" \
    "$BUILDIFIER_SHA256"

info "Downloading buildozer $VERSION..."
install_binary \
    "https://github.com/bazelbuild/buildtools/releases/download/v$VERSION/$BUILDOZER_ASSET" \
    "$PAYLOAD_DIR/bin/buildozer" \
    "$BUILDOZER_SHA256"

info "Downloading ibazel $IBAZEL_VERSION..."
install_binary \
    "https://github.com/bazelbuild/bazel-watcher/releases/download/v$IBAZEL_VERSION/$IBAZEL_ASSET" \
    "$PAYLOAD_DIR/bin/ibazel" \
    "$IBAZEL_SHA256"

printf '%s\n' \
    "format=1" \
    "buildtools_version=$VERSION" \
    "platform=$SUFFIX" \
    "bazelisk_version=$BAZELISK_VERSION" \
    "ibazel_version=$IBAZEL_VERSION" \
    "bazel_sha256=$BAZEL_SHA256" \
    "buildifier_sha256=$BUILDIFIER_SHA256" \
    "buildozer_sha256=$BUILDOZER_SHA256" \
    "ibazel_sha256=$IBAZEL_SHA256" \
    > "$PAYLOAD_DIR/.dotfiles-install-identity"

if ! bundle_installed "$PAYLOAD_DIR"; then
    error "Bazel tool bundle validation failed"
    exit 1
fi

publish_staged_directory "$BAZEL_DIR" "$VERSION" "$PAYLOAD_DIR"
update_latest "$BAZEL_DIR" "$VERSION"

info "Bazel tools installed successfully!"
echo "  bazel (bazelisk $BAZELISK_VERSION)"
echo "  buildifier $VERSION"
echo "  buildozer $VERSION"
echo "  ibazel $IBAZEL_VERSION"
