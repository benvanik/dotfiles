#!/bin/bash
# Install a verified CMake release into the versioned tool root.
# Usage: cmake/install.sh [--force] [version]
set -euo pipefail

TOOL_NAME="cmake"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../install-utils.sh
source "$SCRIPT_DIR/../install-utils.sh"

CMAKE_DIR="$TOOLS_DIR/cmake"
INSTALLATION_RECORD=".dotfiles-cmake-installation"
VERSION=""
FORCE=false
TARBALL=""
ARCHIVE_ROOT=""
CMAKE_EXECUTABLE_DIRECTORY=""
CMAKE_STAGING_DIR=""

show_help() {
    cat << 'EOF'
Usage: cmake/install.sh [options] [version]

Install a verified CMake release to ~/tools/cmake/<version>/.
Without a version, installs the latest stable release.

Options:
  -f, --force  Replace an existing managed version after the replacement passes
               checksum and payload validation
  -h, --help   Show this help

Examples:
  cmake/install.sh
  cmake/install.sh 4.2.0
  cmake/install.sh --force 4.2.0
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
                    error "Expected at most one CMake version"
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
                    error "Expected at most one CMake version"
                    exit 1
                fi
                VERSION="$1"
                shift
                ;;
        esac
    done
}

validate_cmake_version() {
    validate_version_component "$1" "CMake version" || return 1
    if [[ ! "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-rc[0-9]+)?$ ]]; then
        error "Invalid CMake version: $1"
        error "Expected major.minor.patch or major.minor.patch-rcN"
        return 1
    fi
}

fetch_latest_version() {
    local release_json
    local release_tag

    info "Fetching latest CMake version..."
    release_json=$(curl -fsSL \
        "https://api.github.com/repos/Kitware/CMake/releases/latest") || {
        error "Failed to fetch the latest CMake release"
        return 1
    }
    release_tag=$(printf '%s' "$release_json" | python3 -c '
import json
import sys

tag = json.load(sys.stdin).get("tag_name", "")
if not isinstance(tag, str) or not tag.startswith("v"):
    raise SystemExit("latest CMake release has no canonical v-prefixed tag")
print(tag[1:])
') || {
        error "Failed to resolve the latest CMake release tag"
        return 1
    }
    validate_cmake_version "$release_tag" || return 1
    VERSION="$release_tag"
}

select_archive() {
    case "$PLATFORM/$ARCH" in
        linux/x86_64)
            TARBALL="cmake-$VERSION-linux-x86_64.tar.gz"
            ;;
        linux/aarch64)
            TARBALL="cmake-$VERSION-linux-aarch64.tar.gz"
            ;;
        darwin/x86_64|darwin/aarch64)
            TARBALL="cmake-$VERSION-macos-universal.tar.gz"
            ;;
        *)
            error "Unsupported platform: $PLATFORM/$ARCH"
            return 1
            ;;
    esac
    ARCHIVE_ROOT="${TARBALL%.tar.gz}"
    if [ "$PLATFORM" = "darwin" ]; then
        CMAKE_EXECUTABLE_DIRECTORY="CMake.app/Contents/bin"
    else
        CMAKE_EXECUTABLE_DIRECTORY="bin"
    fi
}

read_record() {
    local cmake_root="$1"
    local record="$cmake_root/$INSTALLATION_RECORD"
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
        [ "$RECORD_TOOL" = "cmake" ] &&
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

cmake_payload_valid() {
    local cmake_root="$1"
    local executable_directory="$cmake_root/$CMAKE_EXECUTABLE_DIRECTORY"
    local reported_version
    local executable

    read_record "$cmake_root" || return 1
    for executable in cmake cpack ctest; do
        managed_executable_valid \
            "$cmake_root" "$executable_directory/$executable" || return 1
    done
    reported_version=$("$executable_directory/cmake" --version) || return 1
    [ "${reported_version%%$'\n'*}" = "cmake version $VERSION" ]
}

write_record() {
    local cmake_root="$1"
    local sha256="$2"

    {
        printf 'schema=1\n'
        printf 'tool=cmake\n'
        printf 'version=%s\n' "$VERSION"
        printf 'platform=%s\n' "$PLATFORM"
        printf 'arch=%s\n' "$ARCH"
        printf 'archive=%s\n' "$TARBALL"
        printf 'sha256=%s\n' "$sha256"
    } > "$cmake_root/$INSTALLATION_RECORD"
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
        error "No checksum entry found for $TARBALL"
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
    local release_url

    parse_arguments "$@"
    if [ -z "$VERSION" ]; then
        fetch_latest_version
    else
        validate_cmake_version "$VERSION"
    fi
    select_archive

    if [ -L "$CMAKE_DIR" ] ||
            { [ -e "$CMAKE_DIR" ] && [ ! -d "$CMAKE_DIR" ]; }; then
        error "Managed CMake root is not an ordinary directory: $CMAKE_DIR"
        exit 1
    fi
    mkdir -p "$CMAKE_DIR"
    install_dir="$CMAKE_DIR/$VERSION"
    acquire_managed_installation_guard "$CMAKE_DIR" "$VERSION"
    recover_managed_installation "$CMAKE_DIR" "$VERSION"
    if [ -L "$install_dir" ] ||
            { [ -e "$install_dir" ] && [ ! -d "$install_dir" ]; }; then
        error "Refusing non-directory CMake installation: $install_dir"
        exit 1
    fi
    if [ -d "$install_dir" ]; then
        if cmake_payload_valid "$install_dir"; then
            if [ "$FORCE" != "true" ]; then
                warn "CMake $VERSION is already installed and verified"
                update_latest "$CMAKE_DIR" "$VERSION"
                exit 0
            fi
        elif [ "$FORCE" != "true" ]; then
            error "Incomplete or unidentified CMake installation: $install_dir"
            error "Inspect it, then rerun with --force to replace it"
            exit 1
        fi
    fi

    CMAKE_STAGING_DIR=$(
        create_managed_staging_directory "$CMAKE_DIR" "$VERSION"
    )
    cleanup_staging() {
        local final_status=$?

        trap - EXIT HUP INT TERM
        if { [ -e "$CMAKE_STAGING_DIR" ] ||
                [ -L "$CMAKE_STAGING_DIR" ]; } &&
                ! remove_managed_tree "$CMAKE_DIR" "$CMAKE_STAGING_DIR"; then
            error "CMake staging directory requires inspection: $CMAKE_STAGING_DIR"
            final_status=1
        fi
        exit "$final_status"
    }
    trap cleanup_staging EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    archive_path="$CMAKE_STAGING_DIR/$TARBALL"
    checksum_path="$CMAKE_STAGING_DIR/cmake-$VERSION-SHA-256.txt"
    release_url="https://github.com/Kitware/CMake/releases/download/v$VERSION"

    download "$release_url/$(basename "$checksum_path")" "$checksum_path"
    expected_sha256=$(read_checksum "$checksum_path")
    download "$release_url/$TARBALL" "$archive_path"
    verify_sha256 "$archive_path" "$expected_sha256"
    validate_single_root_tar_archive "$archive_path" "$ARCHIVE_ROOT"

    mkdir "$CMAKE_STAGING_DIR/extract"
    tar -xf "$archive_path" -C "$CMAKE_STAGING_DIR/extract"
    payload_dir="$CMAKE_STAGING_DIR/extract/$ARCHIVE_ROOT"
    if [ ! -d "$payload_dir" ] || [ -L "$payload_dir" ]; then
        error "CMake archive did not produce $ARCHIVE_ROOT"
        exit 1
    fi
    write_record "$payload_dir" "$expected_sha256"
    cmake_payload_valid "$payload_dir" || {
        error "CMake payload validation failed"
        exit 1
    }

    publish_staged_directory "$CMAKE_DIR" "$VERSION" "$payload_dir"
    update_latest "$CMAKE_DIR" "$VERSION"
    info "CMake $VERSION installed successfully"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
