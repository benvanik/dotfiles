#!/bin/bash
# Install a verified Ninja release into the versioned tool root.
# Usage: ninja/install.sh [--force] [version]
set -euo pipefail

TOOL_NAME="ninja"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../install-utils.sh
source "$SCRIPT_DIR/../install-utils.sh"

NINJA_DIR="$TOOLS_DIR/ninja"
INSTALLATION_RECORD=".dotfiles-ninja-installation"
VERSION=""
FORCE=false
ZIPFILE=""
NINJA_STAGING_DIR=""

show_help() {
    cat << 'EOF'
Usage: ninja/install.sh [options] [version]

Install a verified Ninja release to ~/tools/ninja/<version>/.
Without a version, installs the latest stable release. Releases that predate
GitHub's per-asset SHA-256 attestations are intentionally refused.

Options:
  -f, --force  Replace an existing managed version after the replacement passes
               checksum and executable validation
  -h, --help   Show this help

Examples:
  ninja/install.sh
  ninja/install.sh 1.13.2
  ninja/install.sh --force 1.13.2
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
                    error "Expected at most one Ninja version"
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
                    error "Expected at most one Ninja version"
                    exit 1
                fi
                VERSION="$1"
                shift
                ;;
        esac
    done
}

validate_ninja_version() {
    validate_version_component "$1" "Ninja version" || return 1
    if [[ ! "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        error "Invalid Ninja version: $1"
        error "Expected a numeric major.minor.patch version"
        return 1
    fi
}

fetch_latest_version() {
    local release_json
    local release_tag

    info "Fetching latest Ninja version..."
    release_json=$(curl -fsSL \
        "https://api.github.com/repos/ninja-build/ninja/releases/latest") || {
        error "Failed to fetch the latest Ninja release"
        return 1
    }
    release_tag=$(printf '%s' "$release_json" | python3 -c '
import json
import sys

tag = json.load(sys.stdin).get("tag_name", "")
if not isinstance(tag, str) or not tag.startswith("v"):
    raise SystemExit("latest Ninja release has no canonical v-prefixed tag")
print(tag[1:])
') || {
        error "Failed to resolve the latest Ninja release tag"
        return 1
    }
    validate_ninja_version "$release_tag" || return 1
    VERSION="$release_tag"
}

select_archive() {
    case "$PLATFORM/$ARCH" in
        linux/x86_64)
            ZIPFILE="ninja-linux.zip"
            ;;
        linux/aarch64)
            ZIPFILE="ninja-linux-aarch64.zip"
            ;;
        darwin/x86_64|darwin/aarch64)
            ZIPFILE="ninja-mac.zip"
            ;;
        *)
            error "Unsupported platform: $PLATFORM/$ARCH"
            return 1
            ;;
    esac
}

read_record() {
    local ninja_root="$1"
    local record="$ninja_root/$INSTALLATION_RECORD"
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
        [ "$RECORD_TOOL" = "ninja" ] &&
        [ "$RECORD_VERSION" = "$VERSION" ] &&
        [ "$RECORD_PLATFORM" = "$PLATFORM" ] &&
        [ "$RECORD_ARCH" = "$ARCH" ] &&
        [ "$RECORD_ARCHIVE" = "$ZIPFILE" ] &&
        [ "${#RECORD_SHA256}" -eq 64 ] &&
        [[ "$RECORD_SHA256" =~ ^[0-9a-f]{64}$ ]]
}

write_record() {
    local ninja_root="$1"
    local sha256="$2"

    {
        printf 'schema=1\n'
        printf 'tool=ninja\n'
        printf 'version=%s\n' "$VERSION"
        printf 'platform=%s\n' "$PLATFORM"
        printf 'arch=%s\n' "$ARCH"
        printf 'archive=%s\n' "$ZIPFILE"
        printf 'sha256=%s\n' "$sha256"
    } > "$ninja_root/$INSTALLATION_RECORD"
}

ninja_payload_valid() {
    local ninja_root="$1"
    local reported_version

    read_record "$ninja_root" || return 1
    [ -f "$ninja_root/bin/ninja" ] &&
        [ ! -L "$ninja_root/bin/ninja" ] &&
        [ -x "$ninja_root/bin/ninja" ] || return 1
    reported_version=$("$ninja_root/bin/ninja" --version) || return 1
    [ "$reported_version" = "$VERSION" ]
}

archive_surface_valid() {
    local archive="$1"

    python3 - "$archive" << 'PY'
import stat
import sys
import zipfile

with zipfile.ZipFile(sys.argv[1]) as archive:
    members = archive.infolist()
    if len(members) != 1 or members[0].filename != "ninja":
        raise SystemExit("Ninja archive must contain exactly one file named ninja")
    member = members[0]
    unix_mode = member.external_attr >> 16
    file_type = stat.S_IFMT(unix_mode)
    if member.is_dir() or file_type not in (0, stat.S_IFREG):
        raise SystemExit("Ninja archive member is not an ordinary file")
    if member.flag_bits & 0x1:
        raise SystemExit("Ninja archive member is encrypted")
    with archive.open(member) as source:
        while source.read(1024 * 1024):
            pass
PY
}

extract_ninja() {
    local archive="$1"
    local destination="$2"

    python3 - "$archive" "$destination" << 'PY'
import shutil
import sys
import zipfile

archive_path, destination = sys.argv[1:]
with zipfile.ZipFile(archive_path) as archive:
    member = archive.infolist()[0]
    with archive.open(member) as source, open(destination, "xb") as output:
        shutil.copyfileobj(source, output)
PY
}

main() {
    local install_dir
    local archive_path
    local expected_sha256
    local payload_dir
    local release_tag
    local release_url

    parse_arguments "$@"
    if [ -z "$VERSION" ]; then
        fetch_latest_version
    else
        validate_ninja_version "$VERSION"
    fi
    select_archive

    if [ -L "$NINJA_DIR" ] ||
            { [ -e "$NINJA_DIR" ] && [ ! -d "$NINJA_DIR" ]; }; then
        error "Managed Ninja root is not an ordinary directory: $NINJA_DIR"
        exit 1
    fi
    mkdir -p "$NINJA_DIR"
    install_dir="$NINJA_DIR/$VERSION"
    acquire_managed_installation_guard "$NINJA_DIR" "$VERSION"
    recover_managed_installation "$NINJA_DIR" "$VERSION"
    if [ -L "$install_dir" ] ||
            { [ -e "$install_dir" ] && [ ! -d "$install_dir" ]; }; then
        error "Refusing non-directory Ninja installation: $install_dir"
        exit 1
    fi
    if [ -d "$install_dir" ]; then
        if ninja_payload_valid "$install_dir"; then
            if [ "$FORCE" != "true" ]; then
                warn "Ninja $VERSION is already installed and verified"
                update_latest "$NINJA_DIR" "$VERSION"
                exit 0
            fi
        elif [ "$FORCE" != "true" ]; then
            error "Incomplete or unidentified Ninja installation: $install_dir"
            error "Inspect it, then rerun with --force to replace it"
            exit 1
        fi
    fi

    release_tag="v$VERSION"
    expected_sha256=$(
        github_release_asset_sha256 \
            "ninja-build/ninja" "$release_tag" "$ZIPFILE"
    ) || {
        error "Ninja $VERSION lacks an exact GitHub SHA-256 attestation"
        error "Choose an attested release rather than accepting unchecked bytes"
        exit 1
    }

    NINJA_STAGING_DIR=$(
        create_managed_staging_directory "$NINJA_DIR" "$VERSION"
    )
    cleanup_staging() {
        local final_status=$?

        trap - EXIT HUP INT TERM
        if { [ -e "$NINJA_STAGING_DIR" ] ||
                [ -L "$NINJA_STAGING_DIR" ]; } &&
                ! remove_managed_tree "$NINJA_DIR" "$NINJA_STAGING_DIR"; then
            error "Ninja staging directory requires inspection: $NINJA_STAGING_DIR"
            final_status=1
        fi
        exit "$final_status"
    }
    trap cleanup_staging EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    archive_path="$NINJA_STAGING_DIR/$ZIPFILE"
    payload_dir="$NINJA_STAGING_DIR/payload"
    release_url="https://github.com/ninja-build/ninja/releases/download/$release_tag"

    download "$release_url/$ZIPFILE" "$archive_path"
    verify_sha256 "$archive_path" "$expected_sha256"
    archive_surface_valid "$archive_path"

    mkdir -p "$payload_dir/bin"
    extract_ninja "$archive_path" "$payload_dir/bin/ninja"
    chmod 0755 "$payload_dir/bin/ninja"
    write_record "$payload_dir" "$expected_sha256"
    ninja_payload_valid "$payload_dir" || {
        error "Ninja payload validation failed"
        exit 1
    }

    publish_staged_directory "$NINJA_DIR" "$VERSION" "$payload_dir"
    update_latest "$NINJA_DIR" "$VERSION"
    info "Ninja $VERSION installed successfully"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
