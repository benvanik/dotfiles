#!/bin/bash
# Install a verified LLVM/Clang release into the versioned tool root.
# Usage: llvm/install.sh [--force] [version]
set -euo pipefail

TOOL_NAME="llvm"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../install-utils.sh
source "$SCRIPT_DIR/../install-utils.sh"

LLVM_DIR="$TOOLS_DIR/llvm"
INSTALLATION_RECORD=".dotfiles-llvm-installation"
LIBXML2_COMPAT_VERSION="2.13.9"
LIBXML2_COMPAT_SHA256="a2c9ae7b770da34860050c309f903221c67830c86e4a7e760692b803df95143a"
VERSION=""
FORCE=false
TARBALL=""
ARCHIVE_ROOT=""
LLVM_STAGING_DIR=""

show_help() {
    cat << 'EOF'
Usage: llvm/install.sh [options] [version]

Install a verified LLVM/Clang release to ~/tools/llvm/<version>/.
This installer supports the official LLVM 21+ binary release layout.
Without a version, installs the latest stable release.

Options:
  -f, --force  Replace an existing managed version after the replacement passes
               checksum, compiler, linker, and payload validation
  -h, --help   Show this help

Examples:
  llvm/install.sh
  llvm/install.sh 22.1.8
  llvm/install.sh --force 22.1.8
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
                    error "Expected at most one LLVM version"
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
                    error "Expected at most one LLVM version"
                    exit 1
                fi
                VERSION="$1"
                shift
                ;;
        esac
    done
}

validate_llvm_version() {
    local major

    validate_version_component "$1" "LLVM version" || return 1
    if [[ ! "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-rc[0-9]+)?$ ]]; then
        error "Invalid LLVM version: $1"
        error "Expected major.minor.patch or major.minor.patch-rcN"
        return 1
    fi
    major="${1%%.*}"
    if [ "$major" -lt 21 ]; then
        error "LLVM $1 predates the supported LLVM 21+ binary archive layout"
        return 1
    fi
}

fetch_latest_version() {
    local release_json
    local release_tag

    info "Fetching latest LLVM version..."
    release_json=$(curl -fsSL \
        "https://api.github.com/repos/llvm/llvm-project/releases/latest") || {
        error "Failed to fetch the latest LLVM release"
        return 1
    }
    release_tag=$(printf '%s' "$release_json" | python3 -c '
import json
import sys

tag = json.load(sys.stdin).get("tag_name", "")
prefix = "llvmorg-"
if not isinstance(tag, str) or not tag.startswith(prefix):
    raise SystemExit("latest LLVM release has no canonical llvmorg- tag")
print(tag.removeprefix(prefix))
') || {
        error "Failed to resolve the latest LLVM release tag"
        return 1
    }
    validate_llvm_version "$release_tag" || return 1
    VERSION="$release_tag"
}

select_archive() {
    case "$PLATFORM/$ARCH" in
        linux/x86_64)
            TARBALL="LLVM-$VERSION-Linux-X64.tar.xz"
            ;;
        linux/aarch64)
            TARBALL="LLVM-$VERSION-Linux-ARM64.tar.xz"
            ;;
        darwin/aarch64)
            TARBALL="LLVM-$VERSION-macOS-ARM64.tar.xz"
            ;;
        darwin/x86_64)
            error "LLVM does not publish the supported LLVM 21+ archive for Intel macOS"
            return 1
            ;;
        *)
            error "Unsupported platform: $PLATFORM/$ARCH"
            return 1
            ;;
    esac
    ARCHIVE_ROOT="${TARBALL%.tar.xz}"
}

read_record() {
    local llvm_root="$1"
    local record="$llvm_root/$INSTALLATION_RECORD"
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
        [ "$RECORD_TOOL" = "llvm" ] &&
        [ "$RECORD_VERSION" = "$VERSION" ] &&
        [ "$RECORD_PLATFORM" = "$PLATFORM" ] &&
        [ "$RECORD_ARCH" = "$ARCH" ] &&
        [ "$RECORD_ARCHIVE" = "$TARBALL" ] &&
        [ "${#RECORD_SHA256}" -eq 64 ] &&
        [[ "$RECORD_SHA256" =~ ^[0-9a-f]{64}$ ]]
}

write_record() {
    local llvm_root="$1"
    local sha256="$2"

    {
        printf 'schema=1\n'
        printf 'tool=llvm\n'
        printf 'version=%s\n' "$VERSION"
        printf 'platform=%s\n' "$PLATFORM"
        printf 'arch=%s\n' "$ARCH"
        printf 'archive=%s\n' "$TARBALL"
        printf 'sha256=%s\n' "$sha256"
    } > "$llvm_root/$INSTALLATION_RECORD"
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

# LLVM's Linux release linker may require the libxml2.so.2 ABI that newer host
# distributions no longer provide. Build the pinned final libxml2.so.2 release
# privately in the staged LLVM payload only when that is the exact failure.
install_linux_libxml2_compat() {
    local llvm_root="$1"
    local linker_binary="$llvm_root/bin/ld.lld"
    local source_archive
    local source_url
    local build_root
    local compiler
    local job_count

    if [ "$PLATFORM" != "linux" ] || [ ! -x "$linker_binary" ]; then
        return 0
    fi
    if "$linker_binary" --version >/dev/null 2>&1; then
        return 0
    fi
    if ! command -v ldd >/dev/null 2>&1; then
        error "ld.lld cannot execute and ldd is unavailable to diagnose it"
        return 1
    fi
    if ! ldd "$linker_binary" 2>/dev/null |
            grep -q 'libxml2\.so\.2 => not found'; then
        error "ld.lld cannot execute for a reason other than missing libxml2.so.2"
        ldd "$linker_binary" >&2 || true
        return 1
    fi
    compiler=$(command -v cc) || {
        error "Building the private libxml2 runtime requires a host C compiler"
        return 1
    }
    command -v make >/dev/null 2>&1 || {
        error "Building the private libxml2 runtime requires make"
        return 1
    }
    job_count=$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1\n')
    case "$job_count" in
        ""|*[!0-9]*) job_count=1 ;;
    esac

    source_archive="libxml2-$LIBXML2_COMPAT_VERSION.tar.xz"
    source_url="https://download.gnome.org/sources/libxml2/2.13/$source_archive"
    (
        build_root=$(mktemp -d "${TMPDIR:-/tmp}/llvm-libxml2-compat.XXXXXXXX")
        # shellcheck disable=SC2317,SC2329  # Invoked by the EXIT trap below.
        cleanup_libxml2_build() {
            local final_status=$?

            trap - EXIT HUP INT TERM
            case "$build_root" in
                "${TMPDIR:-/tmp}"/llvm-libxml2-compat.*)
                    if [ -e "$build_root" ] &&
                            ! find "$build_root" -xdev -depth -delete; then
                        error "libxml2 build scratch requires inspection: $build_root"
                        final_status=1
                    fi
                    ;;
                *)
                    error "Refusing unexpected libxml2 cleanup path: $build_root"
                    final_status=1
                    ;;
            esac
            exit "$final_status"
        }
        trap cleanup_libxml2_build EXIT
        trap 'exit 129' HUP
        trap 'exit 130' INT
        trap 'exit 143' TERM

        info "Building private libxml2.so.2 compatibility runtime..."
        download "$source_url" "$build_root/$source_archive"
        verify_sha256 "$build_root/$source_archive" "$LIBXML2_COMPAT_SHA256"
        validate_single_root_tar_archive \
            "$build_root/$source_archive" "libxml2-$LIBXML2_COMPAT_VERSION"
        tar -xf "$build_root/$source_archive" -C "$build_root"
        mkdir "$build_root/build"
        (
            cd "$build_root/build"
            CC="$compiler" CFLAGS="-O2 -fPIC" \
                "../libxml2-$LIBXML2_COMPAT_VERSION/configure" \
                --without-icu \
                --without-lzma \
                --without-python \
                --without-zlib
            make -j"$job_count" libxml2.la
        )

        install -m 0755 \
            "$build_root/build/.libs/libxml2.so.$LIBXML2_COMPAT_VERSION" \
            "$llvm_root/lib/libxml2.so.$LIBXML2_COMPAT_VERSION"
        ln -sfn "libxml2.so.$LIBXML2_COMPAT_VERSION" \
            "$llvm_root/lib/libxml2.so.2"
        mkdir -p "$llvm_root/share/licenses/libxml2"
        install -m 0644 \
            "$build_root/libxml2-$LIBXML2_COMPAT_VERSION/Copyright" \
            "$llvm_root/share/licenses/libxml2/Copyright"
    )
}

verify_tool_surface() {
    local llvm_root="$1"
    local executable
    local reported_version

    for executable in clang clang++ ld.lld llvm-config mlir-opt; do
        managed_executable_valid \
            "$llvm_root" "$llvm_root/bin/$executable" || return 1
    done
    reported_version=$("$llvm_root/bin/llvm-config" --version) || return 1
    [ "$reported_version" = "$VERSION" ] || return 1
    reported_version=$("$llvm_root/bin/clang" --version) || return 1
    case "${reported_version%%$'\n'*}" in
        "clang version $VERSION "*) ;;
        "clang version $VERSION") ;;
        *) return 1 ;;
    esac
}

verify_host_link() (
    local llvm_root="$1"
    local smoke_binary=""

    # shellcheck disable=SC2317,SC2329  # Invoked by the EXIT trap below.
    cleanup_smoke_binary() {
        if [ -n "$smoke_binary" ] && [ -e "$smoke_binary" ]; then
            unlink "$smoke_binary"
        fi
    }
    trap cleanup_smoke_binary EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    smoke_binary=$(mktemp "${TMPDIR:-/tmp}/llvm-link-smoke.XXXXXXXX")
    if ! printf '%s\n' 'int main(void) { return 0; }' |
            "$llvm_root/bin/clang" -fuse-ld=lld -x c - -o "$smoke_binary"; then
        error "LLVM host compile/link verification failed"
        return 1
    fi
    if ! "$smoke_binary"; then
        error "LLVM host executable verification failed"
        return 1
    fi
    unlink "$smoke_binary"
    smoke_binary=""
)

llvm_payload_valid() {
    local llvm_root="$1"

    read_record "$llvm_root" &&
        verify_tool_surface "$llvm_root" &&
        verify_host_link "$llvm_root"
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
        validate_llvm_version "$VERSION"
    fi
    select_archive

    if [ -L "$LLVM_DIR" ] ||
            { [ -e "$LLVM_DIR" ] && [ ! -d "$LLVM_DIR" ]; }; then
        error "Managed LLVM root is not an ordinary directory: $LLVM_DIR"
        exit 1
    fi
    mkdir -p "$LLVM_DIR"
    install_dir="$LLVM_DIR/$VERSION"
    acquire_managed_installation_guard "$LLVM_DIR" "$VERSION"
    recover_managed_installation "$LLVM_DIR" "$VERSION"
    if [ -L "$install_dir" ] ||
            { [ -e "$install_dir" ] && [ ! -d "$install_dir" ]; }; then
        error "Refusing non-directory LLVM installation: $install_dir"
        exit 1
    fi
    if [ -d "$install_dir" ]; then
        if llvm_payload_valid "$install_dir"; then
            if [ "$FORCE" != "true" ]; then
                warn "LLVM $VERSION is already installed and verified"
                update_latest "$LLVM_DIR" "$VERSION"
                exit 0
            fi
        elif [ "$FORCE" != "true" ]; then
            error "Incomplete or unidentified LLVM installation: $install_dir"
            error "Inspect it, then rerun with --force to replace it"
            exit 1
        fi
    fi

    release_tag="llvmorg-$VERSION"
    expected_sha256=$(
        github_release_asset_sha256 \
            "llvm/llvm-project" "$release_tag" "$TARBALL"
    ) || {
        error "LLVM release asset lacks an exact GitHub SHA-256 attestation"
        exit 1
    }

    LLVM_STAGING_DIR=$(
        create_managed_staging_directory "$LLVM_DIR" "$VERSION"
    )
    cleanup_staging() {
        local final_status=$?

        trap - EXIT HUP INT TERM
        if { [ -e "$LLVM_STAGING_DIR" ] ||
                [ -L "$LLVM_STAGING_DIR" ]; } &&
                ! remove_managed_tree "$LLVM_DIR" "$LLVM_STAGING_DIR"; then
            error "LLVM staging directory requires inspection: $LLVM_STAGING_DIR"
            final_status=1
        fi
        exit "$final_status"
    }
    trap cleanup_staging EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    archive_path="$LLVM_STAGING_DIR/$TARBALL"
    release_url="https://github.com/llvm/llvm-project/releases/download/$release_tag"

    download "$release_url/$TARBALL" "$archive_path"
    verify_sha256 "$archive_path" "$expected_sha256"
    validate_single_root_tar_archive "$archive_path" "$ARCHIVE_ROOT"

    mkdir "$LLVM_STAGING_DIR/extract"
    tar -xf "$archive_path" -C "$LLVM_STAGING_DIR/extract"
    payload_dir="$LLVM_STAGING_DIR/extract/$ARCHIVE_ROOT"
    if [ ! -d "$payload_dir" ] || [ -L "$payload_dir" ]; then
        error "LLVM archive did not produce $ARCHIVE_ROOT"
        exit 1
    fi
    verify_tool_surface "$payload_dir" || {
        error "LLVM archive is missing its required compiler/MLIR surface"
        exit 1
    }
    install_linux_libxml2_compat "$payload_dir"
    verify_host_link "$payload_dir"
    write_record "$payload_dir" "$expected_sha256"
    llvm_payload_valid "$payload_dir" || {
        error "LLVM payload validation failed"
        exit 1
    }

    publish_staged_directory "$LLVM_DIR" "$VERSION" "$payload_dir"
    update_latest "$LLVM_DIR" "$VERSION"
    info "LLVM $VERSION installed successfully"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
