#!/bin/bash
# Install a conventional ROCm SDK root from a TheRock multi-arch tarball.
# Usage: rocm/install.sh [--force] [--prune-old] [version] [gpu-target]
set -euo pipefail

TOOL_NAME="rocm"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../install-utils.sh
source "$SCRIPT_DIR/../install-utils.sh"

if [ "$PLATFORM/$ARCH" != "linux/x86_64" ]; then
    error "TheRock ROCm tarballs are only available for Linux x86_64"
    exit 1
fi

ROCM_DIR="$TOOLS_DIR/rocm"
ROCM_TARBALL_URL="https://rocm.nightlies.amd.com/tarball-multi-arch"
FORCE=false
PRUNE_OLD=false
VERSION=""
GPU_TARGET=""
TARBALL=""
STAGING_DIR=""
INSTALLATION_RECORD=".dotfiles-rocm-installation"

show_help() {
    cat << 'EOF'
Usage: rocm/install.sh [options] [version] [gpu-target]

Install a flattened TheRock ROCm SDK to ~/tools/rocm/<version>/.
The installed root directly contains bin/, include/, lib/, and share/; it does
not retain a pip environment or Python package layout.

Arguments:
  version     Nightly ROCm version; fetches the latest tarball when omitted
  gpu-target  Exact GPU ISA, such as gfx1150; required unless
              ROCM_GPU_TARGET is set

Options:
  -f, --force  Replace an existing managed version after validation
  --prune-old  Remove other managed ROCm versions after success
  -h, --help   Show this help

Examples:
  ROCM_GPU_TARGET=gfx1150 rocm/install.sh
  rocm/install.sh 10.1.0a20260819 gfx1150
  ROCM_GPU_TARGET=gfx1150 rocm/install.sh --force
EOF
}

parse_arguments() {
    local positional=()

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
            --prune-old)
                PRUNE_OLD=true
                shift
                ;;
            --)
                shift
                while [ $# -gt 0 ]; do
                    positional+=("$1")
                    shift
                done
                ;;
            -*)
                error "Unknown option: $1"
                exit 1
                ;;
            *)
                positional+=("$1")
                shift
                ;;
        esac
    done
    if [ "${#positional[@]}" -gt 2 ]; then
        error "Expected at most a ROCm version and GPU target"
        exit 1
    fi
    VERSION="${positional[0]:-}"
    GPU_TARGET="${positional[1]:-${ROCM_GPU_TARGET:-}}"
}

validate_rocm_version() {
    validate_version_component "$1" "ROCm version" || return 1
    if [[ ! "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+-]*$ ]]; then
        error "Invalid ROCm version: $1"
        return 1
    fi
}

validate_gpu_target() {
    if [[ ! "$1" =~ ^gfx[0-9a-f]+$ ]]; then
        error "ROCm GPU target must be one exact lowercase GFX ISA: $1"
        return 1
    fi
}

fetch_latest_version() {
    local index_page
    local prefix="therock-dist-linux-$GPU_TARGET-"
    local selected_name

    info "Fetching latest $GPU_TARGET ROCm tarball..."
    index_page=$(curl -fsSL "$ROCM_TARBALL_URL/") || {
        error "Failed to fetch TheRock tarball index"
        return 1
    }
    selected_name=$(printf '%s' "$index_page" |
        rg -o "\"name\": \"${prefix}[^\"]+\\.tar\\.gz\"" |
        sed 's/^"name": "//; s/"$//' |
        sort -V |
        tail -n 1)
    if [ -z "$selected_name" ]; then
        error "TheRock publishes no Linux tarball for $GPU_TARGET"
        return 1
    fi
    VERSION="${selected_name#"$prefix"}"
    VERSION="${VERSION%.tar.gz}"
    validate_rocm_version "$VERSION"
}

archive_sha256() {
    local archive="$1"
    local digest

    if command -v sha256sum >/dev/null 2>&1; then
        digest=$(sha256sum "$archive") || return 1
    else
        digest=$(shasum -a 256 "$archive") || return 1
    fi
    printf '%s\n' "${digest%% *}"
}

record_value() {
    sed -n "s/^$2=//p" "$1/$INSTALLATION_RECORD"
}

payload_valid() {
    local root="$1"
    local record="$root/$INSTALLATION_RECORD"
    local executable
    local resolved

    [ -d "$root" ] && [ ! -L "$root" ] || return 1
    [ -d "$root/bin" ] || return 1
    [ -d "$root/include" ] || return 1
    [ -d "$root/lib" ] || return 1
    [ -d "$root/share" ] || return 1
    [ -d "$root/.kpack" ] || return 1
    [ -n "$(find "$root/.kpack" -mindepth 1 -print -quit)" ] || return 1
    [ ! -e "$root/.venv" ] || return 1
    [ ! -e "$root/pyvenv.cfg" ] || return 1
    [ -e "$root/include/hip/hip_runtime.h" ] || return 1
    [ -e "$root/lib/libamdhip64.so" ] || return 1
    [ -e "$root/lib/cmake/hip/hip-config.cmake" ] || return 1
    for executable in hipcc hipconfig rocminfo; do
        [ -x "$root/bin/$executable" ] || return 1
        resolved=$(realpath -e "$root/bin/$executable") || return 1
        case "$resolved" in "$root"/*) ;; *) return 1 ;; esac
    done

    [ -f "$record" ] && [ ! -L "$record" ] || return 1
    [ "$(wc -l < "$record")" -eq 7 ] || return 1
    [ "$(record_value "$root" schema)" = "1" ] || return 1
    [ "$(record_value "$root" version)" = "$VERSION" ] || return 1
    [ "$(record_value "$root" gpu_target)" = "$GPU_TARGET" ] || return 1
    [ "$(record_value "$root" archive)" = "$TARBALL" ] || return 1
    [ "$(record_value "$root" source)" = "$ROCM_TARBALL_URL/$TARBALL" ] ||
        return 1
    [ "$(record_value "$root" layout)" = "therock-multiarch-flat-v1" ] ||
        return 1
    resolved=$(record_value "$root" sha256)
    [ "${#resolved}" -eq 64 ] && [[ "$resolved" =~ ^[0-9a-f]{64}$ ]]
}

write_record() {
    local root="$1"
    local sha256="$2"

    printf '%s\n' \
        "schema=1" \
        "version=$VERSION" \
        "gpu_target=$GPU_TARGET" \
        "archive=$TARBALL" \
        "source=$ROCM_TARBALL_URL/$TARBALL" \
        "sha256=$sha256" \
        "layout=therock-multiarch-flat-v1" \
        > "$root/$INSTALLATION_RECORD"
}

remove_install_dir() {
    remove_managed_tree "$ROCM_DIR" "$1"
}

prune_old_versions() {
    local keep_version="$1"
    local path
    local version

    for path in "$ROCM_DIR"/*; do
        [ -d "$path" ] || continue
        [ -L "$path" ] && continue
        version="$(basename "$path")"
        case "$version" in [0-9]*) ;; *) continue ;; esac
        if [ "$version" != "$keep_version" ]; then
            info "Pruning old ROCm install at $path..."
            remove_install_dir "$path"
        fi
    done
}

main() {
    local archive_path
    local install_dir
    local payload_dir
    local sha256

    parse_arguments "$@"
    if [ -z "$GPU_TARGET" ]; then
        error "ROCm GPU target is required as the second argument or ROCM_GPU_TARGET"
        exit 1
    fi
    validate_gpu_target "$GPU_TARGET" || exit 1
    if [ -z "$VERSION" ]; then
        fetch_latest_version || exit 1
    else
        validate_rocm_version "$VERSION" || exit 1
    fi
    TARBALL="therock-dist-linux-$GPU_TARGET-$VERSION.tar.gz"

    prepare_managed_directory_root "$ROCM_DIR" "ROCm installation root"
    install_dir="$ROCM_DIR/$VERSION"
    acquire_managed_installation_guard "$ROCM_DIR" "$VERSION"
    recover_managed_installation "$ROCM_DIR" "$VERSION"
    if [ -L "$install_dir" ] ||
            { [ -e "$install_dir" ] && [ ! -d "$install_dir" ]; }; then
        error "Refusing non-directory ROCm installation: $install_dir"
        exit 1
    fi
    if [ -d "$install_dir" ]; then
        if payload_valid "$install_dir"; then
            if [ "$FORCE" != "true" ]; then
                warn "ROCm $VERSION for $GPU_TARGET is already installed"
                update_latest "$ROCM_DIR" "$VERSION"
                exit 0
            fi
        elif [ "$FORCE" != "true" ]; then
            error "Incomplete or unidentified ROCm installation: $install_dir"
            error "Inspect it, then rerun with --force to replace it"
            exit 1
        fi
    fi

    STAGING_DIR=$(create_managed_staging_directory "$ROCM_DIR" "$VERSION")
    cleanup_staging() {
        local final_status=$?

        trap - EXIT HUP INT TERM
        if { [ -e "$STAGING_DIR" ] || [ -L "$STAGING_DIR" ]; } &&
                ! remove_managed_tree "$ROCM_DIR" "$STAGING_DIR"; then
            error "ROCm staging directory requires inspection: $STAGING_DIR"
            final_status=1
        fi
        exit "$final_status"
    }
    trap cleanup_staging EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    archive_path="$STAGING_DIR/$TARBALL"
    payload_dir="$STAGING_DIR/payload"
    download "$ROCM_TARBALL_URL/$TARBALL" "$archive_path"
    sha256=$(archive_sha256 "$archive_path")
    tar -tzf "$archive_path" >/dev/null
    mkdir "$payload_dir"
    tar -xzf "$archive_path" -C "$payload_dir" --no-same-owner
    if [ -e "$payload_dir/$INSTALLATION_RECORD" ] ||
            [ -L "$payload_dir/$INSTALLATION_RECORD" ]; then
        error "ROCm tarball contains reserved installer metadata"
        exit 1
    fi
    write_record "$payload_dir" "$sha256"
    payload_valid "$payload_dir" || {
        error "ROCm tarball did not produce a conventional SDK root"
        exit 1
    }

    publish_staged_directory "$ROCM_DIR" "$VERSION" "$payload_dir"
    update_latest "$ROCM_DIR" "$VERSION"
    if [ "$PRUNE_OLD" = "true" ]; then
        prune_old_versions "$VERSION"
    fi
    info "ROCm $VERSION for $GPU_TARGET installed successfully"
}

main "$@"
