#!/bin/bash
# Install CUDA SDK from NVIDIA redistributable packages.
# Usage: cuda/install.sh [version]
set -e

TOOL_NAME="cuda"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../install-utils.sh"

CUDA_DIR="$TOOLS_DIR/cuda"
REDIST_BASE="https://developer.download.nvidia.com/compute/cuda/redist"

# Core mode installs the compiler, headers, runtime, and libdevice. Full mode
# adds math libraries, profiling, runtime compilation, and related SDK pieces.
CORE_PACKAGES="cuda_cudart cuda_cccl cuda_nvcc"
FULL_PACKAGES_COMMON="cuda_cudart cuda_nvcc cuda_cccl cuda_cupti cuda_nvdisasm
cuda_nvml_dev cuda_nvrtc cuda_nvtx cuda_profiler_api
libcublas libcufft libcufile libcurand libcusolver libcusparse
libnpp libnvfatbin libnvjitlink libnvjpeg"

# Fetch latest CUDA version from redistribution index.
# Probes known version patterns to find the latest available.
fetch_latest_version() {
    info "Probing for latest CUDA version..."
    # Try recent versions in descending order.
    for major in 13 12; do
        for minor in $(seq 9 -1 0); do
            for patch in $(seq 3 -1 0); do
                local ver="${major}.${minor}.${patch}"
                local code
                if code=$(
                    curl -fsSIL -o /dev/null -w '%{http_code}' \
                        "${REDIST_BASE}/redistrib_${ver}.json" 2>/dev/null
                ) && [ "$code" = "200" ]; then
                    FETCHED_VERSION="$ver"
                    return 0
                fi
            done
        done
    done
    error "Failed to find any CUDA redistribution version"
    exit 1
}

FULL_MODE=false
VERSION=""

# Handle flags.
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            cat << EOF
Usage: cuda/install.sh [OPTIONS] [VERSION]

Install CUDA SDK to ~/tools/cuda/<version>/ from NVIDIA redistributable packages.

Arguments:
    VERSION     CUDA version (e.g., 12.9.1) - probes for latest if omitted

Options:
    --full      Install all development packages (~3 GB vs ~80 MB core)
    --force     Reinstall even if version exists

Core packages (default):
    cuda_cudart (headers: cuda.h), cuda_nvcc (compiler, libdevice)
    Suitable for compiling CUDA kernels without the full numerical library set.

Full packages (--full):
    Adds math libraries (cublas, cufft, cusparse, etc.), profiling (cupti),
    runtime compilation (nvrtc), and other development packages.

Examples:
    cuda/install.sh               # Install latest core SDK
    cuda/install.sh 12.9.1        # Install specific version
    cuda/install.sh --full        # Install with all dev libraries
EOF
            exit 0
            ;;
        -f|--force)
            FORCE=true
            shift
            ;;
        --full)
            FULL_MODE=true
            shift
            ;;
        --)
            shift
            if [ $# -gt 1 ]; then
                error "Expected at most one CUDA version"
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
                error "Expected at most one CUDA version"
                exit 1
            fi
            VERSION="$1"
            shift
            ;;
    esac
done

# Get version (fetch latest if not specified).
if [ -z "$VERSION" ]; then
    fetch_latest_version
    VERSION="$FETCHED_VERSION"
fi

# The version is part of an installation path and NVIDIA manifest name. Keep
# that path a single, canonical child of CUDA_DIR before any filesystem work.
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    error "Invalid CUDA version: $VERSION"
    error "Expected a numeric major.minor.patch version such as 12.9.1"
    exit 1
fi

# Platform check - CUDA SDK is Linux only (macOS uses Metal).
if [ "$PLATFORM" != "linux" ]; then
    error "CUDA SDK is only available for Linux"
    exit 1
fi

# Determine the NVIDIA manifest platform before validating an existing closure.
case "$ARCH" in
    x86_64)
        PLATFORM_KEY="linux-x86_64"
        FULL_PACKAGES="$FULL_PACKAGES_COMMON cuda_opencl"
        ;;
    aarch64)
        # NVIDIA calls its server-class Arm redistribution target SBSA.
        # cuda_opencl is not published for this target.
        PLATFORM_KEY="linux-sbsa"
        FULL_PACKAGES="$FULL_PACKAGES_COMMON"
        ;;
    *)
        error "Unsupported architecture: $ARCH"
        exit 1
        ;;
esac

# Select the platform-specific package closure.
if [ "$FULL_MODE" = true ]; then
    PACKAGES="$FULL_PACKAGES"
    PROFILE="full"
    info "Installing CUDA $VERSION (full SDK)"
else
    PACKAGES="$CORE_PACKAGES"
    PROFILE="core"
    info "Installing CUDA $VERSION (core SDK)"
fi

CLOSURE_RECORD_NAME=".dotfiles-cuda-closure"

# A CUDA version has one active payload, but core and full are distinct
# closures. The record prevents a core payload from satisfying a later full
# request merely because their critical compiler files happen to overlap.
read_installed_profile() {
    local installation_dir="$1"
    local record="$installation_dir/$CLOSURE_RECORD_NAME"
    local schema=""
    local record_version=""
    local record_platform=""
    local profile=""
    local package_names=""
    local key
    local value
    local package_name
    local package_metadata
    local package_sha256
    local package_path
    local expected_package_names=""
    local package

    [ -f "$record" ] && [ ! -L "$record" ] || return 1
    while IFS='=' read -r key value; do
        case "$key" in
            schema)
                [ -z "$schema" ] || return 1
                schema="$value"
                ;;
            version)
                [ -z "$record_version" ] || return 1
                record_version="$value"
                ;;
            platform)
                [ -z "$record_platform" ] || return 1
                record_platform="$value"
                ;;
            profile)
                [ -z "$profile" ] || return 1
                profile="$value"
                ;;
            package)
                package_name="${value%%|*}"
                package_metadata="${value#*|}"
                [ "$package_metadata" != "$value" ] || return 1
                package_sha256="${package_metadata%%|*}"
                package_path="${package_metadata#*|}"
                [ "$package_path" != "$package_metadata" ] || return 1
                [[ "$package_name" =~ ^[a-z0-9_]+$ ]] || return 1
                [[ "$package_sha256" =~ ^[0-9a-f]{64}$ ]] || return 1
                [ -n "$package_path" ] || return 1
                package_names="${package_names}${package_names:+ }$package_name"
                ;;
            *)
                return 1
                ;;
        esac
    done < "$record"

    [ "$schema" = "1" ] || return 1
    [ "$record_version" = "$VERSION" ] || return 1
    [ "$record_platform" = "$PLATFORM_KEY" ] || return 1
    case "$profile" in
        core)
            for package in $CORE_PACKAGES; do
                expected_package_names="${expected_package_names}${expected_package_names:+ }$package"
            done
            ;;
        full)
            for package in $FULL_PACKAGES; do
                expected_package_names="${expected_package_names}${expected_package_names:+ }$package"
            done
            ;;
        *)
            return 1
            ;;
    esac
    [ "$package_names" = "$expected_package_names" ] || return 1
    printf '%s\n' "$profile"
}

installation_payload_is_complete() {
    local installation_dir="$1"

    [ -f "$installation_dir/include/cuda.h" ] &&
        [ ! -L "$installation_dir/include/cuda.h" ] &&
        [ -x "$installation_dir/bin/nvcc" ] &&
        [ ! -L "$installation_dir/bin/nvcc" ] &&
        [ -f "$installation_dir/nvvm/libdevice/libdevice.10.bc" ] &&
        [ ! -L "$installation_dir/nvvm/libdevice/libdevice.10.bc" ]
}

# The child guard spans manifest fetch, download, extraction, and publication.
# Early recovery removes only exact child-bound staging left by a dead holder.
prepare_managed_directory_root "$CUDA_DIR" "managed CUDA root"
INSTALL_DIR="$CUDA_DIR/$VERSION"
acquire_managed_installation_guard "$CUDA_DIR" "$VERSION"
recover_managed_installation "$CUDA_DIR" "$VERSION"

if [ -L "$INSTALL_DIR" ] || { [ -e "$INSTALL_DIR" ] && [ ! -d "$INSTALL_DIR" ]; }; then
    error "Refusing to replace a non-directory CUDA installation: $INSTALL_DIR"
    exit 1
fi
INSTALLED_PROFILE=""
if [ -d "$INSTALL_DIR" ] && installation_payload_is_complete "$INSTALL_DIR"; then
    INSTALLED_PROFILE=$(read_installed_profile "$INSTALL_DIR") ||
        INSTALLED_PROFILE=""
fi
if [ "$FORCE" != "true" ] && [ -n "$INSTALLED_PROFILE" ]; then
    if [ "$INSTALLED_PROFILE" = "$PROFILE" ] ||
            { [ "$PROFILE" = "core" ] && [ "$INSTALLED_PROFILE" = "full" ]; }; then
        warn "CUDA $VERSION $INSTALLED_PROFILE closure already installed"
        update_latest "$CUDA_DIR" "$VERSION"
        exit 0
    fi
    if [ "$INSTALLED_PROFILE" = "core" ] && [ "$PROFILE" = "full" ]; then
        info "Expanding CUDA $VERSION from the core closure to the full closure"
    fi
elif { [ -e "$INSTALL_DIR" ] || [ -L "$INSTALL_DIR" ]; } &&
        [ "$FORCE" != "true" ]; then
    error "Incomplete or unidentified CUDA installation exists: $INSTALL_DIR"
    error "Inspect it, then rerun with --force to replace it"
    exit 1
fi

# Fetch redistribution manifest.
MANIFEST_URL="${REDIST_BASE}/redistrib_${VERSION}.json"
info "Fetching package manifest..."
MANIFEST=$(curl -fsSL "$MANIFEST_URL" 2>/dev/null) || {
    error "Failed to fetch manifest from $MANIFEST_URL"
    error "Check that CUDA $VERSION exists at: $REDIST_BASE/"
    exit 1
}

# Build into a sibling staging directory so a failed download or extraction
# can never be mistaken for an installed SDK on the next run.
STAGING_DIR=$(create_managed_staging_directory "$CUDA_DIR" "$VERSION")
PAYLOAD_DIR="$STAGING_DIR/payload"
DOWNLOAD_DIR="$STAGING_DIR/downloads"
PACKAGE_DIR="$STAGING_DIR/packages"
CLOSURE_RECORD="$STAGING_DIR/closure"
mkdir -p "$PAYLOAD_DIR" "$DOWNLOAD_DIR" "$PACKAGE_DIR"

cleanup_staging() {
    local exit_status=$?

    trap - EXIT HUP INT TERM
    if [ -n "${STAGING_DIR:-}" ] &&
            { [ -e "$STAGING_DIR" ] || [ -L "$STAGING_DIR" ]; }; then
        if ! remove_managed_tree "$CUDA_DIR" "$STAGING_DIR"; then
            error "Failed to clean CUDA transaction: $STAGING_DIR"
            [ "$exit_status" -ne 0 ] || exit_status=1
        fi
    fi
    exit "$exit_status"
}
trap cleanup_staging EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

{
    printf 'schema=1\n'
    printf 'version=%s\n' "$VERSION"
    printf 'platform=%s\n' "$PLATFORM_KEY"
    printf 'profile=%s\n' "$PROFILE"
} > "$CLOSURE_RECORD"

# Validate the archive namespace before tar is allowed to write. The root is
# printed for the caller only after every member and link target is proven to
# remain beneath one ordinary top-level directory.
validate_archive_root() {
    python3 - "$1" << 'PY'
import posixpath
import re
import sys
import tarfile

archive_path = sys.argv[1]
archive_root = None
safe_root = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\Z")

with tarfile.open(archive_path) as archive:
    members = archive.getmembers()
    if not members:
        raise SystemExit("archive is empty")
    for member in members:
        raw_components = member.name.split("/")
        normalized = posixpath.normpath(member.name)
        if (
            member.name.startswith("/")
            or ".." in raw_components
            or normalized in (".", "..")
            or normalized.startswith("../")
        ):
            raise SystemExit(f"unsafe archive member: {member.name}")
        root = normalized.split("/", 1)[0]
        if not safe_root.fullmatch(root):
            raise SystemExit(f"unsafe archive root: {root}")
        if archive_root is None:
            archive_root = root
        elif root != archive_root:
            raise SystemExit("archive has more than one top-level root")
        if "/" not in normalized and not member.isdir():
            raise SystemExit("archive top-level root is not a directory")
        if not (
            member.isfile()
            or member.isdir()
            or member.issym()
            or member.islnk()
        ):
            raise SystemExit(f"unsupported special archive member: {member.name}")
        if member.issym():
            target = posixpath.normpath(
                posixpath.join(posixpath.dirname(normalized), member.linkname)
            )
        elif member.islnk():
            target = posixpath.normpath(member.linkname)
        else:
            continue
        if (
            member.linkname.startswith("/")
            or target in (".", "..")
            or target.startswith("../")
            or target.split("/", 1)[0] != archive_root
        ):
            raise SystemExit(f"unsafe archive link: {member.name}")
        if member.issym():
            relative_name = normalized.split("/", 1)[1]
            rebased_target = posixpath.normpath(
                posixpath.join(
                    posixpath.dirname(relative_name),
                    member.linkname,
                )
            )
            if (
                rebased_target in (".", "..")
                or rebased_target.startswith("../")
            ):
                raise SystemExit(
                    f"archive link escapes after package merge: {member.name}"
                )

print(archive_root)
PY
}

# Download and extract each package.
for pkg in $PACKAGES; do
    if ! package_metadata=$(printf '%s\n' "$MANIFEST" | python3 -c "
import json, sys
d = json.load(sys.stdin)
p = d.get('$pkg', {}).get('$PLATFORM_KEY', {})
print(p.get('relative_path', ''))
print(p.get('sha256', ''))" 2>/dev/null); then
        error "NVIDIA returned an invalid redistribution manifest"
        exit 1
    fi
    rel_path=$(printf '%s\n' "$package_metadata" | sed -n '1p')
    expected_sha256=$(printf '%s\n' "$package_metadata" | sed -n '2p')

    if [ -z "$rel_path" ]; then
        error "Required $PROFILE package $pkg is unavailable for $PLATFORM_KEY"
        exit 1
    fi
    if [[ ! "$expected_sha256" =~ ^[0-9a-fA-F]{64}$ ]]; then
        error "Manifest has no valid SHA-256 for $pkg"
        exit 1
    fi
    expected_sha256=$(printf '%s' "$expected_sha256" |
        tr '[:upper:]' '[:lower:]')

    download_url="${REDIST_BASE}/${rel_path}"
    tarball=$(basename "$rel_path")
    tarball_path="$DOWNLOAD_DIR/$tarball"

    download "$download_url" "$tarball_path"
    if ! verify_sha256 "$tarball_path" "$expected_sha256"; then
        error "SHA-256 verification failed for $pkg"
        exit 1
    fi

    info "Extracting $pkg..."
    archive_root=$(validate_archive_root "$tarball_path") || {
        error "Archive namespace validation failed for $pkg"
        exit 1
    }
    package_extract_dir="$PACKAGE_DIR/$pkg"
    mkdir -p "$package_extract_dir"
    tar xf "$tarball_path" -C "$package_extract_dir" --no-same-owner
    cp -a -n "$package_extract_dir/$archive_root/." "$PAYLOAD_DIR/"
    printf 'package=%s|%s|%s\n' \
        "$pkg" "$expected_sha256" "$rel_path" >> "$CLOSURE_RECORD"
done

# Verify critical files exist.
if [ ! -f "$PAYLOAD_DIR/include/cuda.h" ] ||
        [ -L "$PAYLOAD_DIR/include/cuda.h" ]; then
    error "Installation failed: include/cuda.h not found"
    exit 1
fi
if [ ! -x "$PAYLOAD_DIR/bin/nvcc" ] ||
        [ -L "$PAYLOAD_DIR/bin/nvcc" ]; then
    error "Installation failed: executable bin/nvcc not found"
    exit 1
fi
if [ ! -f "$PAYLOAD_DIR/nvvm/libdevice/libdevice.10.bc" ] ||
        [ -L "$PAYLOAD_DIR/nvvm/libdevice/libdevice.10.bc" ]; then
    error "Installation failed: nvvm/libdevice/libdevice.10.bc not found"
    exit 1
fi
if [ -e "$PAYLOAD_DIR/$CLOSURE_RECORD_NAME" ] ||
        [ -L "$PAYLOAD_DIR/$CLOSURE_RECORD_NAME" ]; then
    error "Package payload contains reserved record: $CLOSURE_RECORD_NAME"
    exit 1
fi
cp "$CLOSURE_RECORD" "$PAYLOAD_DIR/$CLOSURE_RECORD_NAME"

# Publish only the fully validated payload. The durable journal restores the
# prior generation before the commit rename and accepts the complete new
# generation after it, including process death in either window.
managed_installer_test_fault before-publication
publish_staged_directory "$CUDA_DIR" "$VERSION" "$PAYLOAD_DIR"
remove_managed_tree "$CUDA_DIR" "$STAGING_DIR"
STAGING_DIR=""

# Update latest symlink.
update_latest "$CUDA_DIR" "$VERSION"

info "CUDA $VERSION installed successfully!"
info "Directory: $INSTALL_DIR"
info "Profile: $PROFILE"
info "Headers: $INSTALL_DIR/include/cuda.h"
info "Compiler: $INSTALL_DIR/bin/nvcc"
info "Libdevice: $INSTALL_DIR/nvvm/libdevice/libdevice.10.bc"
