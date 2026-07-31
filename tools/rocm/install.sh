#!/bin/bash
# Install ROCm from TheRock pip index.
# Usage: rocm/install.sh [--force] [--prune-old] [version] [gpu-target]
set -euo pipefail

TOOL_NAME="rocm"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../install-utils.sh"

# TheRock's host-code wheels for this index are Linux x86-64 artifacts.
if [ "$PLATFORM/$ARCH" != "linux/x86_64" ]; then
    error "TheRock ROCm packages are only available for Linux x86_64"
    exit 1
fi

ROCM_DIR="$TOOLS_DIR/rocm"

show_help() {
    cat << EOF
Usage: rocm/install.sh [--force] [--prune-old] [version] [gpu-target]

Install ROCm from TheRock pip index to ~/tools/rocm/<version>/.

TheRock publishes the SDK as Python packages. This installer keeps that
packaging environment hidden under .venv and exposes a conventional ROCm root:

    ~/tools/rocm/<version>/bin
    ~/tools/rocm/<version>/include
    ~/tools/rocm/<version>/lib
    ~/tools/rocm/<version>/share

Arguments:
    version     ROCm version (e.g., 7.14.0a20260612); fetches latest if omitted
    gpu-target  GPU target (default: \$ROCM_GPU_TARGET or gfx1100)
                Supported shortcuts: gfx110*, gfx90*, gfx94*

Options:
    --force     Replace the managed version directory before installing
    --prune-old Remove other installed ROCm version directories after success

Examples:
    rocm/install.sh                         # Install latest for default GPU
    rocm/install.sh 7.14.0a20260612         # Uses ROCM_GPU_TARGET
    rocm/install.sh 7.14.0a20260612 gfx90a  # Override GPU target
    rocm/install.sh --force                 # Rebuild latest from scratch
    rocm/install.sh --prune-old             # Keep only the selected version

Set default GPU in ~/.shrc.local:
    export ROCM_GPU_TARGET=gfx1100  # RDNA3
EOF
}

index_suffix_for_gpu() {
    local gpu_target="$1"
    case "$gpu_target" in
        gfx110*) echo "gfx110X-all" ;;
        gfx90*)  echo "gfx90X-all" ;;
        gfx94*)  echo "gfx94X-all" ;;
        *)       echo "$gpu_target" ;;
    esac
}

validate_version() {
    local version="$1"
    if [[ ! "$version" =~ ^[0-9][0-9A-Za-z._+-]*$ ]]; then
        error "Invalid ROCm version: $version"
        exit 1
    fi
}

validate_gpu_target() {
    local gpu_target="$1"
    if [[ ! "$gpu_target" =~ ^[A-Za-z0-9_.+-]+$ ]]; then
        error "Invalid ROCm GPU target: $gpu_target"
        exit 1
    fi
}

# Fetch latest ROCm version from pip index.
# Sets FETCHED_VERSION on success.
fetch_latest_version() {
    local gpu_target="${1:-gfx1100}"
    local index_suffix
    local index_page
    index_suffix="$(index_suffix_for_gpu "$gpu_target")"
    local index_url="https://rocm.nightlies.amd.com/v2/${index_suffix}"

    info "Fetching latest ROCm version from pip index..."
    # Parse the pip index page for rocm package versions.
    # Format: rocm-7.11.0a20251127.tar.gz (version with date suffix).
    index_page=$(curl -fsSL "$index_url/rocm/") || {
        error "Failed to fetch latest version from $index_url"
        exit 1
    }
    FETCHED_VERSION=$(printf '%s' "$index_page" | python3 -c '
import re
import sys

versions = set(
    re.findall(r"rocm-([0-9]+\.[0-9]+\.[0-9]+[a-z0-9]*)\.tar\.gz",
               sys.stdin.read())
)
if not versions:
    raise SystemExit("pip index contains no recognized ROCm releases")

def natural_key(version):
    return tuple(
        int(part) if part.isdigit() else part
        for part in re.split(r"([0-9]+)", version)
        if part
    )

print(max(versions, key=natural_key))
') || {
        error "Failed to parse latest version from $index_url"
        exit 1
    }
    if [ -z "$FETCHED_VERSION" ]; then
        error "Pip index returned an empty latest version: $index_url"
        exit 1
    fi
}

sdk_surface_valid() {
    local install_dir="$1"

    [ -d "$install_dir" ] && [ ! -L "$install_dir" ] || return 1
    [ -d "$install_dir/.venv" ] && [ ! -L "$install_dir/.venv" ] || return 1
    [ -d "$install_dir/bin" ] && [ ! -L "$install_dir/bin" ] || return 1
    [ -L "$install_dir/include" ] || return 1
    [ -L "$install_dir/lib" ] || return 1
    [ -x "$install_dir/bin/hipcc" ] || return 1
    [ -x "$install_dir/bin/hipconfig" ] || return 1
    [ -e "$install_dir/include/hip/hip_runtime.h" ] || return 1
    [ -e "$install_dir/lib/libamdhip64.so" ] || return 1
    [ -e "$install_dir/lib/cmake/hip/hip-config.cmake" ] || return 1
    python3 - "$install_dir" << 'PY'
import os
import sys

install_dir = os.path.realpath(sys.argv[1])
venv_dir = os.path.realpath(os.path.join(install_dir, ".venv"))
if os.path.commonpath((install_dir, venv_dir)) != install_dir:
    raise SystemExit("ROCm environment resolves outside its installation")

owned_links = []
for name in (
    "amdgcn",
    "etc",
    "include",
    "lib",
    "lib64",
    "libexec",
    "llvm",
    "share",
    ".info",
    ".kpack",
):
    path = os.path.join(install_dir, name)
    if os.path.lexists(path):
        owned_links.append(path)
for name in os.listdir(os.path.join(install_dir, "bin")):
    owned_links.append(os.path.join(install_dir, "bin", name))

for path in owned_links:
    if not os.path.islink(path):
        raise SystemExit(f"ROCm SDK surface path is not a symlink: {path}")
    if os.path.isabs(os.readlink(path)):
        raise SystemExit(f"ROCm SDK surface has an absolute symlink: {path}")
    resolved_path = os.path.realpath(path)
    if (
        not os.path.exists(resolved_path)
        or os.path.commonpath((venv_dir, resolved_path)) != venv_dir
    ):
        raise SystemExit(f"ROCm SDK surface escapes its environment: {path}")
PY
}

remove_install_dir() {
    local install_dir="$1"
    remove_managed_tree "$ROCM_DIR" "$install_dir"
}

prune_old_versions() {
    local keep_version="$1"
    local path

    for path in "$ROCM_DIR"/*; do
        [ -d "$path" ] || continue
        [ -L "$path" ] && continue

        local version
        version="$(basename "$path")"
        case "$version" in
            [0-9]*) ;;
            *) continue ;;
        esac

        if [ "$version" != "$keep_version" ]; then
            info "Pruning old ROCm install at $path..."
            remove_install_dir "$path"
        fi
    done
}

materialize_sdk_root() {
    local install_dir="$1"
    local venv_dir="$2"
    local sdk_root="$3"
    local physical_venv_dir
    local physical_sdk_root
    local relative_sdk_root

    if [ ! -d "$sdk_root/include" ] || [ ! -d "$sdk_root/lib" ] || [ ! -d "$sdk_root/bin" ]; then
        error "TheRock devel root is missing expected SDK directories: $sdk_root"
        return 1
    fi
    physical_venv_dir=$(cd "$venv_dir" && pwd -P)
    physical_sdk_root=$(cd "$sdk_root" && pwd -P)
    case "$physical_sdk_root" in
        "$physical_venv_dir"/*) ;;
        *)
            error "TheRock SDK root is outside its managed environment: $sdk_root"
            return 1
            ;;
    esac
    relative_sdk_root=$(python3 - "$physical_sdk_root" "$install_dir" << 'PY'
import os
import sys

print(os.path.relpath(sys.argv[1], sys.argv[2]))
PY
    )

    # The payload is a fresh staging root. Finding a pre-existing SDK surface
    # here means the staged environment crossed its ownership boundary.
    local owned_path
    for owned_path in \
        "${install_dir:?}/amdgcn" \
        "${install_dir:?}/bin" \
        "${install_dir:?}/etc" \
        "${install_dir:?}/include" \
        "${install_dir:?}/lib" \
        "${install_dir:?}/lib64" \
        "${install_dir:?}/libexec" \
        "${install_dir:?}/llvm" \
        "${install_dir:?}/share" \
        "${install_dir:?}/.info" \
        "${install_dir:?}/.kpack"; do
        if [ -e "$owned_path" ] || [ -L "$owned_path" ]; then
            error "Refusing pre-existing path in staged ROCm SDK root: $owned_path"
            return 1
        fi
    done

    local entry
    for entry in amdgcn etc include lib libexec llvm share .info .kpack; do
        if [ -e "$sdk_root/$entry" ]; then
            ln -s "$relative_sdk_root/$entry" "$install_dir/$entry"
        fi
    done

    if [ -e "$sdk_root/lib64" ]; then
        ln -s "$relative_sdk_root/lib64" "$install_dir/lib64"
    elif [ -e "$sdk_root/lib" ]; then
        ln -s lib "$install_dir/lib64"
    fi

    mkdir -p "$install_dir/bin"
    for entry in "$sdk_root/bin"/*; do
        [ -e "$entry" ] || continue
        ln -s "../$relative_sdk_root/bin/$(basename "$entry")" \
            "$install_dir/bin/$(basename "$entry")"
    done

    if [ -x "$venv_dir/bin/rocm-sdk" ]; then
        ln -s "../.venv/bin/rocm-sdk" "$install_dir/bin/rocm-sdk"
    fi

    if ! sdk_surface_valid "$install_dir"; then
        error "ROCm SDK surface did not validate after install"
        return 1
    fi
}

PRUNE_OLD=false
POSITIONAL_ARGUMENTS=()

STAGING_DIR=""
cleanup_staging() {
    local final_status=$?

    trap - EXIT HUP INT TERM
    if [ -n "$STAGING_DIR" ] &&
            { [ -e "$STAGING_DIR" ] || [ -L "$STAGING_DIR" ]; }; then
        if ! remove_managed_tree "$ROCM_DIR" "$STAGING_DIR"; then
            error "ROCm staging directory requires inspection: $STAGING_DIR"
            final_status=1
        fi
    fi
    exit "$final_status"
}
trap cleanup_staging EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# Handle flags.
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
                POSITIONAL_ARGUMENTS+=("$1")
                shift
            done
            ;;
        -*)
            error "Unknown option: $1"
            exit 1
            ;;
        *)
            POSITIONAL_ARGUMENTS+=("$1")
            shift
            ;;
    esac
done
if [ "${#POSITIONAL_ARGUMENTS[@]}" -gt 2 ]; then
    error "Expected at most a ROCm version and GPU target"
    exit 1
fi

# Get GPU target first (needed for version lookup).
GPU_TARGET="${POSITIONAL_ARGUMENTS[1]:-${ROCM_GPU_TARGET:-gfx1100}}"
validate_gpu_target "$GPU_TARGET"

# Validate an explicit version before creating installer state. For latest
# lookup, validate the root before network access.
if [ -n "${POSITIONAL_ARGUMENTS[0]:-}" ]; then
    VERSION="${POSITIONAL_ARGUMENTS[0]}"
    validate_version "$VERSION"
fi
prepare_managed_directory_root "$ROCM_DIR" "ROCm installation root"
if [ -z "${POSITIONAL_ARGUMENTS[0]:-}" ]; then
    fetch_latest_version "$GPU_TARGET"
    VERSION="$FETCHED_VERSION"
    validate_version "$VERSION"
fi

# Map GPU target to index URL pattern.
INDEX_SUFFIX="$(index_suffix_for_gpu "$GPU_TARGET")"

INDEX_URL="https://rocm.nightlies.amd.com/v2/${INDEX_SUFFIX}/"
INSTALL_DIR="$ROCM_DIR/$VERSION"
IDENTITY_PATH="$INSTALL_DIR/.dotfiles-install-identity"
CLOSURE_PATH="$INSTALL_DIR/.dotfiles-python-closure"
acquire_managed_installation_guard "$ROCM_DIR" "$VERSION"
recover_managed_installation "$ROCM_DIR" "$VERSION"

info "Installing ROCm $VERSION for $GPU_TARGET"
echo "  Index: $INDEX_URL"
echo "  Target: $INSTALL_DIR"
echo ""

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
    local installed_closure
    local recorded_closure

    sdk_surface_valid "$INSTALL_DIR" || return 1
    [ ! -L "$INSTALL_DIR" ] || return 1
    [ -f "$IDENTITY_PATH" ] && [ ! -L "$IDENTITY_PATH" ] || return 1
    [ -f "$CLOSURE_PATH" ] && [ ! -L "$CLOSURE_PATH" ] || return 1
    [ "$(wc -l < "$IDENTITY_PATH")" -eq 7 ] || return 1
    [ "$(identity_value "$IDENTITY_PATH" format)" = "1" ] || return 1
    [ "$(identity_value "$IDENTITY_PATH" version)" = "$VERSION" ] || return 1
    [ "$(identity_value "$IDENTITY_PATH" gpu_target)" = "$GPU_TARGET" ] ||
        return 1
    [ "$(identity_value "$IDENTITY_PATH" index_url)" = "$INDEX_URL" ] ||
        return 1
    [ "$(identity_value "$IDENTITY_PATH" closure)" = \
        "python-distributions-v1" ] || return 1
    [ "$(identity_value "$IDENTITY_PATH" sdk_layout)" = \
        "therock-relative-v1" ] || return 1
    [ "$(identity_value "$IDENTITY_PATH" python)" = \
        "$("$INSTALL_DIR/.venv/bin/python" --version)" ] || return 1
    installed_closure=$(python_closure "$INSTALL_DIR/.venv/bin/python") ||
        return 1
    recorded_closure=$(cat "$CLOSURE_PATH") || return 1
    [ "$installed_closure" = "$recorded_closure" ]
}

# Check if already installed.
if [ -L "$INSTALL_DIR" ] ||
        { [ -e "$INSTALL_DIR" ] && [ ! -d "$INSTALL_DIR" ]; }; then
    error "Refusing non-directory ROCm installation: $INSTALL_DIR"
    exit 1
fi
if [ "$FORCE" != "true" ] && installation_valid; then
    warn "Version $VERSION for $GPU_TARGET is already installed"
    update_latest "$ROCM_DIR" "$VERSION"
    if [ "$PRUNE_OLD" = "true" ]; then
        prune_old_versions "$VERSION"
    fi
    exit 0
fi

if [ -e "$INSTALL_DIR" ]; then
    if [ "$FORCE" != "true" ]; then
        error "Existing ROCm $VERSION installation is incomplete or belongs to another GPU target"
        error "Inspect it, then rerun with --force to replace it transactionally"
        exit 1
    fi
fi

STAGING_DIR=$(create_managed_staging_directory "$ROCM_DIR" "$VERSION")
PAYLOAD_DIR="$STAGING_DIR/payload"
VENV_DIR="$PAYLOAD_DIR/.venv"
mkdir "$PAYLOAD_DIR"

info "Creating virtual environment..."
python3 -m venv "$VENV_DIR"

# Install ROCm packages.
info "Installing ROCm packages (this may take a while)..."
"$VENV_DIR/bin/pip" --isolated install \
    --disable-pip-version-check \
    --no-input \
    --index-url "$INDEX_URL" \
    "rocm[libraries,devel]==$VERSION"
"$VENV_DIR/bin/pip" --isolated check --disable-pip-version-check

info "Initializing TheRock SDK payload..."
"$VENV_DIR/bin/rocm-sdk" init --quiet
SDK_ROOT="$("$VENV_DIR/bin/rocm-sdk" path --root)"

info "Materializing conventional ROCm SDK root..."
materialize_sdk_root "$PAYLOAD_DIR" "$VENV_DIR" "$SDK_ROOT"
python_closure "$VENV_DIR/bin/python" \
    > "$PAYLOAD_DIR/.dotfiles-python-closure"
PYTHON_VERSION=$("$VENV_DIR/bin/python" --version)
printf '%s\n' \
    "format=1" \
    "version=$VERSION" \
    "gpu_target=$GPU_TARGET" \
    "index_url=$INDEX_URL" \
    "python=$PYTHON_VERSION" \
    "closure=python-distributions-v1" \
    "sdk_layout=therock-relative-v1" \
    > "$PAYLOAD_DIR/.dotfiles-install-identity"
publish_staged_directory "$ROCM_DIR" "$VERSION" "$PAYLOAD_DIR"

# Update latest symlink.
update_latest "$ROCM_DIR" "$VERSION"
if [ "$PRUNE_OLD" = "true" ]; then
    prune_old_versions "$VERSION"
fi

info "ROCm $VERSION installed successfully!"
