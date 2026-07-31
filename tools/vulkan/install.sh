#!/bin/bash
# Install a verified LunarG Vulkan SDK into the versioned tool root.
# Usage: vulkan/install.sh [--force] [version]
set -euo pipefail

TOOL_NAME="vulkan"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../install-utils.sh
source "$SCRIPT_DIR/../install-utils.sh"

VULKAN_DIR="$TOOLS_DIR/vulkan"
INSTALLATION_RECORD=".dotfiles-vulkan-installation"
VERSION=""
FORCE=false
TARBALL=""
ARCHIVE_ROOT=""
VULKAN_STAGING_DIR=""

show_help() {
    cat << 'EOF'
Usage: vulkan/install.sh [options] [version]

Install a verified LunarG Vulkan SDK to ~/tools/vulkan/<version>/.
The LunarG Linux tarball is published for x86-64 Linux only.
Without a version, installs the latest stable Linux SDK.

Options:
  -f, --force  Replace an existing managed version after the replacement passes
               LunarG checksum and payload validation
  -h, --help   Show this help

Examples:
  vulkan/install.sh
  vulkan/install.sh 1.4.350.1
  vulkan/install.sh --force 1.4.350.1
EOF
}

parse_arguments() {
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
                    error "Expected at most one Vulkan SDK version"
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
                    error "Expected at most one Vulkan SDK version"
                    exit 1
                fi
                VERSION="$1"
                shift
                ;;
        esac
    done
}

validate_vulkan_version() {
    validate_version_component "$1" "Vulkan SDK version" || return 1
    if [[ ! "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        error "Invalid Vulkan SDK version: $1"
        error "Expected a numeric major.minor.patch.revision version"
        return 1
    fi
}

fetch_latest_version() {
    local latest

    info "Fetching latest Vulkan SDK version..."
    latest=$(curl -fsSL https://vulkan.lunarg.com/sdk/latest/linux.txt) || {
        error "Failed to fetch the latest Vulkan SDK version"
        return 1
    }
    validate_vulkan_version "$latest" || return 1
    VERSION="$latest"
}

select_archive() {
    if [ "$PLATFORM" != "linux" ]; then
        error "Vulkan SDK tarball installation is supported only on Linux"
        error "macOS: install the Vulkan SDK through Homebrew"
        return 1
    fi
    if [ "$ARCH" != "x86_64" ]; then
        error "LunarG does not publish this Linux Vulkan SDK tarball for $ARCH"
        return 1
    fi
    TARBALL="vulkansdk-linux-x86_64-$VERSION.tar.xz"
    ARCHIVE_ROOT="$VERSION"
}

read_record() {
    local vulkan_root="$1"
    local record="$vulkan_root/$INSTALLATION_RECORD"
    local key
    local value
    local line_count=0

    RECORD_SCHEMA=""
    RECORD_TOOL=""
    RECORD_VERSION=""
    RECORD_PLATFORM=""
    RECORD_ARCH=""
    RECORD_ARCHIVE=""
    RECORD_SHA256=""

    [ -f "$record" ] && [ ! -L "$record" ] || return 1
    while IFS='=' read -r key value || [ -n "$key" ]; do
        line_count=$((line_count + 1))
        case "$key" in
            schema) [ -z "$RECORD_SCHEMA" ] || return 1; RECORD_SCHEMA="$value" ;;
            tool) [ -z "$RECORD_TOOL" ] || return 1; RECORD_TOOL="$value" ;;
            version) [ -z "$RECORD_VERSION" ] || return 1; RECORD_VERSION="$value" ;;
            platform) [ -z "$RECORD_PLATFORM" ] || return 1; RECORD_PLATFORM="$value" ;;
            arch) [ -z "$RECORD_ARCH" ] || return 1; RECORD_ARCH="$value" ;;
            archive) [ -z "$RECORD_ARCHIVE" ] || return 1; RECORD_ARCHIVE="$value" ;;
            sha256) [ -z "$RECORD_SHA256" ] || return 1; RECORD_SHA256="$value" ;;
            *) return 1 ;;
        esac
    done < "$record"

    [ "$line_count" -eq 7 ] &&
        [ "$RECORD_SCHEMA" = "1" ] &&
        [ "$RECORD_TOOL" = "vulkan" ] &&
        [ "$RECORD_VERSION" = "$VERSION" ] &&
        [ "$RECORD_PLATFORM" = "$PLATFORM" ] &&
        [ "$RECORD_ARCH" = "$ARCH" ] &&
        [ "$RECORD_ARCHIVE" = "$TARBALL" ] &&
        [ "${#RECORD_SHA256}" -eq 64 ] &&
        [[ "$RECORD_SHA256" =~ ^[0-9a-f]{64}$ ]]
}

managed_executable_valid() {
    local root="$1"
    local executable="$2"

    python3 - "$root" "$executable" << 'PY'
import os
import sys

root = os.path.realpath(sys.argv[1])
executable = os.path.realpath(sys.argv[2])
try:
    contained = os.path.commonpath((root, executable)) == root
except ValueError:
    contained = False
if not contained or not os.path.isfile(executable) or not os.access(executable, os.X_OK):
    raise SystemExit(1)
PY
}

write_record() {
    local vulkan_root="$1"
    local sha256="$2"

    {
        printf 'schema=1\n'
        printf 'tool=vulkan\n'
        printf 'version=%s\n' "$VERSION"
        printf 'platform=%s\n' "$PLATFORM"
        printf 'arch=%s\n' "$ARCH"
        printf 'archive=%s\n' "$TARBALL"
        printf 'sha256=%s\n' "$sha256"
    } > "$vulkan_root/$INSTALLATION_RECORD"
}

vulkan_payload_valid() {
    local vulkan_root="$1"
    local sdk_root="$vulkan_root/x86_64"
    local executable

    read_record "$vulkan_root" || return 1
    for executable in glslangValidator spirv-val; do
        managed_executable_valid \
            "$vulkan_root" "$sdk_root/bin/$executable" || return 1
    done
    [ -f "$sdk_root/include/vulkan/vulkan.h" ] &&
        [ ! -L "$sdk_root/include/vulkan/vulkan.h" ] &&
        [ -d "$sdk_root/lib" ] &&
        [ -f "$vulkan_root/setup-env.sh" ] &&
        [ ! -L "$vulkan_root/setup-env.sh" ] || return 1
    "$sdk_root/bin/glslangValidator" --version >/dev/null
}

read_checksum() {
    local checksum_file="$1"
    local checksum=""
    local candidate
    local name

    while read -r candidate name; do
        if [ "$name" = "$TARBALL" ]; then
            [ -z "$checksum" ] || {
                error "Duplicate checksum entry for $TARBALL"
                return 1
            }
            checksum="$candidate"
        fi
    done < "$checksum_file"
    if [ -z "$checksum" ]; then
        error "No LunarG checksum entry found for $TARBALL"
        return 1
    fi
    printf '%s\n' "$checksum"
}

main() {
    local install_dir
    local archive_path
    local checksum_path
    local expected_sha256
    local payload_dir

    parse_arguments "$@"
    if [ -z "$VERSION" ]; then
        fetch_latest_version
    else
        validate_vulkan_version "$VERSION"
    fi
    select_archive

    if [ -L "$VULKAN_DIR" ] ||
            { [ -e "$VULKAN_DIR" ] && [ ! -d "$VULKAN_DIR" ]; }; then
        error "Managed Vulkan root is not an ordinary directory: $VULKAN_DIR"
        exit 1
    fi
    mkdir -p "$VULKAN_DIR"
    install_dir="$VULKAN_DIR/$VERSION"
    acquire_managed_installation_guard "$VULKAN_DIR" "$VERSION"
    recover_managed_installation "$VULKAN_DIR" "$VERSION"
    if [ -L "$install_dir" ] ||
            { [ -e "$install_dir" ] && [ ! -d "$install_dir" ]; }; then
        error "Refusing non-directory Vulkan SDK installation: $install_dir"
        exit 1
    fi
    if [ -d "$install_dir" ]; then
        if vulkan_payload_valid "$install_dir"; then
            if [ "$FORCE" != "true" ]; then
                warn "Vulkan SDK $VERSION is already installed and verified"
                update_latest "$VULKAN_DIR" "$VERSION"
                exit 0
            fi
        elif [ "$FORCE" != "true" ]; then
            error "Incomplete or unidentified Vulkan SDK installation: $install_dir"
            error "Inspect it, then rerun with --force to replace it"
            exit 1
        fi
    fi

    VULKAN_STAGING_DIR=$(
        create_managed_staging_directory "$VULKAN_DIR" "$VERSION"
    )
    cleanup_staging() {
        local final_status=$?

        trap - EXIT HUP INT TERM
        if { [ -e "$VULKAN_STAGING_DIR" ] ||
                [ -L "$VULKAN_STAGING_DIR" ]; } &&
                ! remove_managed_tree \
                    "$VULKAN_DIR" "$VULKAN_STAGING_DIR"; then
            error "Vulkan staging directory requires inspection: $VULKAN_STAGING_DIR"
            final_status=1
        fi
        exit "$final_status"
    }
    trap cleanup_staging EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    archive_path="$VULKAN_STAGING_DIR/$TARBALL"
    checksum_path="$VULKAN_STAGING_DIR/$TARBALL.sha256"

    download \
        "https://sdk.lunarg.com/sdk/sha/$VERSION/linux/vulkan_sdk.tar.xz.txt" \
        "$checksum_path"
    expected_sha256=$(read_checksum "$checksum_path")
    download \
        "https://sdk.lunarg.com/sdk/download/$VERSION/linux/vulkan_sdk.tar.xz" \
        "$archive_path"
    verify_sha256 "$archive_path" "$expected_sha256"
    validate_single_root_tar_archive "$archive_path" "$ARCHIVE_ROOT"

    mkdir "$VULKAN_STAGING_DIR/extract"
    tar -xf "$archive_path" -C "$VULKAN_STAGING_DIR/extract"
    payload_dir="$VULKAN_STAGING_DIR/extract/$ARCHIVE_ROOT"
    if [ ! -d "$payload_dir" ] || [ -L "$payload_dir" ]; then
        error "Vulkan SDK archive did not produce $ARCHIVE_ROOT"
        exit 1
    fi
    write_record "$payload_dir" "$expected_sha256"
    vulkan_payload_valid "$payload_dir" || {
        error "Vulkan SDK payload validation failed"
        exit 1
    }

    publish_staged_directory "$VULKAN_DIR" "$VERSION" "$payload_dir"
    update_latest "$VULKAN_DIR" "$VERSION"
    info "Vulkan SDK $VERSION installed successfully"
    info "Use use_vulkan in .envrc or restart your shell to load the default SDK"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
