#!/bin/bash
# Common utilities for tool install scripts.
# Source this from individual tool install.sh files.

# Colors.
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

# Tool name (set by caller before sourcing).
TOOL_NAME="${TOOL_NAME:-tool}"
INSTALL_UTILS_DIRECTORY="$(
    cd "$(dirname "${BASH_SOURCE[0]}")" && pwd
)"
MANAGED_DIRECTORY_PUBLISHER="$INSTALL_UTILS_DIRECTORY/../lib/managed-directory-publication.py"

info() { printf "${GREEN}[$TOOL_NAME]${NC} %s\n" "$1"; }
warn() { printf "${YELLOW}[$TOOL_NAME]${NC} %s\n" "$1"; }
error() { printf "${RED}[$TOOL_NAME]${NC} %s\n" "$1" >&2; }

# Directories.
TOOLS_DIR="${TOOLS_DIR:-$HOME/tools}"

# Platform detection.
detect_platform() {
    case "$(uname -s)" in
        Linux*)  echo "linux" ;;
        Darwin*) echo "darwin" ;;
        *)       echo "unknown" ;;
    esac
}

detect_arch() {
    case "$(uname -m)" in
        x86_64|amd64) echo "x86_64" ;;
        aarch64|arm64) echo "aarch64" ;;
        *)            echo "unknown" ;;
    esac
}

# Exported for use by scripts that source this file.
export PLATFORM
export ARCH
PLATFORM=$(detect_platform)
ARCH=$(detect_arch)

# Download a fresh artifact. Installers download into private staging
# directories, so resuming a path with no URL/ETag identity only creates a
# chance of joining bytes from two different release assets.
download() {
    local url="$1"
    local output="$2"
    if [ -L "$output" ] ||
            { [ -e "$output" ] && [ ! -f "$output" ]; }; then
        error "Refusing non-regular download destination: $output"
        return 1
    fi
    info "Downloading $(basename "$output")..."
    if ! curl -fL --progress-bar -o "$output" "$url"; then
        [ ! -e "$output" ] || unlink "$output"
        return 1
    fi
}

# Verify a file against an expected SHA-256 on Linux or macOS.
verify_sha256() {
    local path="$1"
    local expected="$2"
    local actual

    if [ ! -f "$path" ] || [ -L "$path" ]; then
        error "Cannot verify non-regular artifact: $path"
        return 1
    fi
    if [ "${#expected}" -ne 64 ]; then
        error "Invalid expected SHA-256 for $(basename "$path")"
        return 1
    fi
    case "$expected" in
        *[!0-9a-fA-F]*)
            error "Invalid expected SHA-256 for $(basename "$path")"
            return 1
            ;;
    esac

    if command -v sha256sum >/dev/null 2>&1; then
        actual=$(sha256sum "$path") || return 1
    elif command -v shasum >/dev/null 2>&1; then
        actual=$(shasum -a 256 "$path") || return 1
    else
        error "No SHA-256 utility found (need sha256sum or shasum)"
        return 1
    fi
    actual="${actual%% *}"
    expected=$(printf '%s' "$expected" | tr '[:upper:]' '[:lower:]')
    if [ "$actual" != "$expected" ]; then
        error "SHA-256 mismatch for $(basename "$path")"
        error "Expected: $expected"
        error "Actual:   $actual"
        return 1
    fi
}

# Reject values that could resolve anywhere except one named child of a managed
# tool directory. Individual installers may impose a narrower release syntax.
validate_version_component() {
    local version="$1"
    local label="${2:-version}"

    if [ "${#version}" -gt 128 ] ||
            [ "$version" = "latest" ] ||
            [[ ! "$version" =~ ^[0-9A-Za-z][0-9A-Za-z._+-]*$ ]]; then
        error "Invalid $label: $version"
        error "$label must be one path component of at most 128 characters using letters, numbers, '.', '_', '+', or '-'"
        return 1
    fi
}

# Validate one exact child name for non-versioned managed payloads such as
# pinned Git checkouts. A leading dot is allowed, but "." and ".." are not.
validate_managed_child_component() {
    local component="$1"
    local label="${2:-managed child}"

    if [ "${#component}" -gt 128 ] ||
            [ "$component" = "." ] ||
            [ "$component" = ".." ] ||
            [[ ! "$component" =~ ^\.?[0-9A-Za-z][0-9A-Za-z._+-]*$ ]]; then
        error "Invalid $label: $component"
        error "$label must be one direct child name of at most 128 characters"
        return 1
    fi
}

# Create one installer-owned root without following a final symlink or
# accepting a non-directory object at that ownership boundary.
prepare_managed_directory_root() {
    local directory="$1"
    local label="${2:-managed directory}"

    if [ -L "$directory" ] ||
            { [ -e "$directory" ] && [ ! -d "$directory" ]; }; then
        error "$label is not an ordinary directory: $directory"
        return 1
    fi
    if ! mkdir -p "$directory"; then
        error "Failed to create $label: $directory"
        return 1
    fi
    if [ ! -d "$directory" ] || [ -L "$directory" ]; then
        error "$label is not an ordinary directory: $directory"
        return 1
    fi
}

# Allocate one exact, child-bound staging root in the namespace owned by the
# durable directory publisher. Recovery may recursively remove only roots with
# this shape, so ordinary hidden state under a tool directory is never
# classified as disposable transaction state.
create_managed_staging_directory() {
    local parent="$1"
    local child_name="$2"

    validate_managed_child_component "$child_name" "managed child" || return 1
    if [ ! -d "$parent" ] || [ -L "$parent" ]; then
        error "Managed staging parent is not an ordinary directory: $parent"
        return 1
    fi
    python3 - "$parent" "$child_name" << 'PY'
import os
import sys
import uuid
from pathlib import Path

parent = Path(sys.argv[1]).absolute()
child = sys.argv[2]
for _attempt in range(16):
    staging = parent / f".dotfiles-stage-{child}.{uuid.uuid4().hex}"
    try:
        staging.mkdir(mode=0o700)
    except FileExistsError:
        continue
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    print(staging)
    break
else:
    raise SystemExit(f"could not allocate managed staging below {parent}")
PY
}

# Hold a kernel-released per-child guard across download, extraction, and
# publication. File descriptors 8 and 9 are reserved for the only supported
# nesting shape: a top-level installer may call the pinned-checkout installer.
acquire_managed_installation_guard() {
    local parent="$1"
    local child_name="$2"
    local guard_descriptor="${3:-9}"
    local guard_path

    validate_managed_child_component "$child_name" "managed child" || return 1
    if [ ! -d "$parent" ] || [ -L "$parent" ]; then
        error "Managed installation parent is not an ordinary directory: $parent"
        return 1
    fi
    # Publication and preparation share one physical exclusion domain. Derive
    # it only from the managed parent and child; an ambient cache/state root
    # would let two callers select different guards for the same installation.
    guard_path="$parent/.publish-$child_name.guard"
    if [ -L "$guard_path" ] ||
            { [ -e "$guard_path" ] && [ ! -f "$guard_path" ]; }; then
        error "Managed installation guard is not an ordinary file: $guard_path"
        return 1
    fi
    case "$guard_descriptor" in
        8)
            exec 8<>"$guard_path"
            DOTFILES_MANAGED_INSTALL_GUARD_PATH_8="$guard_path"
            ;;
        9)
            exec 9<>"$guard_path"
            DOTFILES_MANAGED_INSTALL_GUARD_PATH_9="$guard_path"
            ;;
        *)
            error "Unsupported managed installation guard descriptor: $guard_descriptor"
            return 1
            ;;
    esac
    if ! python3 - "$guard_path" "$guard_descriptor" << 'PY'
import fcntl
import os
import stat
import sys

path = sys.argv[1]
descriptor = int(sys.argv[2])
path_metadata = os.lstat(path)
descriptor_metadata = os.fstat(descriptor)
if (
    not stat.S_ISREG(path_metadata.st_mode)
    or (path_metadata.st_dev, path_metadata.st_ino)
    != (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
):
    raise SystemExit(f"managed installation guard identity changed: {path}")
try:
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError as exc:
    raise SystemExit(f"another installer owns: {path}") from exc
path_metadata = os.lstat(path)
descriptor_metadata = os.fstat(descriptor)
if (
    not stat.S_ISREG(path_metadata.st_mode)
    or (path_metadata.st_dev, path_metadata.st_ino)
    != (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
):
    raise SystemExit(f"managed installation guard identity changed: {path}")
PY
    then
        case "$guard_descriptor" in
            8)
                exec 8>&-
                unset DOTFILES_MANAGED_INSTALL_GUARD_PATH_8
                ;;
            9)
                exec 9>&-
                unset DOTFILES_MANAGED_INSTALL_GUARD_PATH_9
                ;;
        esac
        return 1
    fi
}

release_managed_installation_guard() {
    case "${1:-9}" in
        8)
            exec 8>&-
            unset DOTFILES_MANAGED_INSTALL_GUARD_PATH_8
            ;;
        9)
            exec 9>&-
            unset DOTFILES_MANAGED_INSTALL_GUARD_PATH_9
            ;;
        *)
            error "Unsupported managed installation guard descriptor: $1"
            return 1
            ;;
    esac
}

# Recover a journaled rename and remove pre-journal staging left by a dead
# producer. The inherited guard descriptor proves no live installer can still
# own any child-bound staging root selected for cleanup.
recover_managed_installation() {
    local parent="$1"
    local child_name="$2"
    local guard_descriptor="${3:-9}"
    local guard_path

    case "$guard_descriptor" in
        8) guard_path="${DOTFILES_MANAGED_INSTALL_GUARD_PATH_8:-}" ;;
        9) guard_path="${DOTFILES_MANAGED_INSTALL_GUARD_PATH_9:-}" ;;
        *)
            error "Unsupported managed installation guard descriptor: $guard_descriptor"
            return 1
            ;;
    esac
    if [ -z "$guard_path" ]; then
        error "Managed installation guard descriptor is not owned: $guard_descriptor"
        return 1
    fi

    validate_managed_child_component "$child_name" "managed child" || return 1
    python3 "$MANAGED_DIRECTORY_PUBLISHER" \
        --parent "$parent" \
        --child "$child_name" \
        --recover-only \
        --cleanup-orphan-staging \
        --installer-guard-fd "$guard_descriptor" \
        --installer-guard-path "$guard_path"
}

# Test-only process-death boundary used by offline production fixtures. Normal
# environments leave the selector empty.
managed_installer_test_fault() {
    local fault_name="$1"

    if [ "${DOTFILES_INSTALLER_TEST_FAULT:-}" = \
            "process-crash-$fault_name" ]; then
        kill -KILL "$$"
    fi
}

# Resolve one GitHub release asset's server-recorded SHA-256. The asset name
# and release tag are exact inputs; a missing or duplicate asset fails closed.
github_release_asset_sha256() {
    local repository="$1"
    local release_tag="$2"
    local asset_name="$3"
    local release_json
    local digest

    if [[ ! "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
        error "Invalid GitHub repository identity: $repository"
        return 1
    fi
    validate_version_component "$release_tag" "release tag" || return 1
    if [[ ! "$asset_name" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ ]]; then
        error "Invalid GitHub release asset name: $asset_name"
        return 1
    fi

    release_json=$(curl -fsSL \
        "https://api.github.com/repos/$repository/releases/tags/$release_tag") ||
        return 1
    digest=$(printf '%s' "$release_json" | python3 -c '
import json
import sys

asset_name = sys.argv[1]
release = json.load(sys.stdin)
matches = [asset for asset in release.get("assets", [])
           if asset.get("name") == asset_name]
if len(matches) != 1:
    raise SystemExit(
        f"expected one release asset named {asset_name!r}, found {len(matches)}"
    )
digest = matches[0].get("digest") or ""
if not digest.startswith("sha256:"):
    raise SystemExit(f"release asset {asset_name!r} has no SHA-256 digest")
print(digest.removeprefix("sha256:"))
' "$asset_name") || return 1
    if [ "${#digest}" -ne 64 ]; then
        error "GitHub returned an invalid SHA-256 for $asset_name"
        return 1
    fi
    case "$digest" in
        *[!0-9a-f]*)
            error "GitHub returned an invalid SHA-256 for $asset_name"
            return 1
            ;;
    esac
    printf '%s\n' "$digest"
}

# Select the first exact candidate name present in a GitHub release and print
# its name and SHA-256 on separate lines. This supports upstreams that changed
# an asset naming convention without accepting arbitrary names from metadata.
github_release_asset_selection() {
    local repository="$1"
    local release_tag="$2"
    shift 2
    local release_json

    if [ $# -eq 0 ]; then
        error "No GitHub release asset candidates were provided"
        return 1
    fi
    if [[ ! "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
        error "Invalid GitHub repository identity: $repository"
        return 1
    fi
    validate_version_component "$release_tag" "release tag" || return 1
    local candidate_name
    for candidate_name in "$@"; do
        if [[ ! "$candidate_name" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ ]]; then
            error "Invalid GitHub release asset name: $candidate_name"
            return 1
        fi
    done
    release_json=$(curl -fsSL \
        "https://api.github.com/repos/$repository/releases/tags/$release_tag") ||
        return 1
    printf '%s' "$release_json" | python3 -c '
import json
import sys

candidate_names = sys.argv[1:]
release = json.load(sys.stdin)
assets = release.get("assets", [])
for candidate_name in candidate_names:
    matches = [asset for asset in assets if asset.get("name") == candidate_name]
    if len(matches) > 1:
        raise SystemExit(f"duplicate release asset named {candidate_name!r}")
    if not matches:
        continue
    digest = matches[0].get("digest") or ""
    if not digest.startswith("sha256:"):
        raise SystemExit(f"release asset {candidate_name!r} has no SHA-256 digest")
    print(candidate_name)
    print(digest.removeprefix("sha256:"))
    raise SystemExit(0)
raise SystemExit(
    "none of the accepted release asset names exist: " + ", ".join(candidate_names)
)
' "$@"
}

# Extract one exact ordinary file from a verified tar archive. Other archive
# members are never materialized, so their paths and link semantics are inert.
extract_regular_tar_member() {
    local archive_path="$1"
    local member_name="$2"
    local destination="$3"

    python3 - "$archive_path" "$member_name" "$destination" << 'PY'
import shutil
import sys
import tarfile

archive_path, member_name, destination = sys.argv[1:]
with tarfile.open(archive_path) as archive:
    matches = [member for member in archive.getmembers()
               if member.name == member_name]
    if len(matches) != 1:
        raise SystemExit(
            f"expected one archive member named {member_name!r}, found {len(matches)}"
        )
    member = matches[0]
    if not member.isfile():
        raise SystemExit(f"archive member {member_name!r} is not an ordinary file")
    source = archive.extractfile(member)
    if source is None:
        raise SystemExit(f"could not read archive member {member_name!r}")
    with source, open(destination, "xb") as output:
        shutil.copyfileobj(source, output)
PY
}

# Validate a tar archive before extracting it into an empty staging directory.
# The one expected top-level root will be renamed to the version payload, so
# symlink targets are checked in that post-rename namespace as well.
validate_single_root_tar_archive() {
    local archive_path="$1"
    local expected_root="$2"

    validate_version_component "$expected_root" "archive root" || return 1
    python3 - "$archive_path" "$expected_root" << 'PY'
import posixpath
import sys
import tarfile

archive_path, expected_root = sys.argv[1:]
seen = set()
member_count = 0

with tarfile.open(archive_path) as archive:
    for member in archive.getmembers():
        member_count += 1
        normalized = posixpath.normpath(member.name)
        if (
            member.name.startswith("/")
            or normalized == ".."
            or normalized.startswith("../")
            or normalized != member.name.rstrip("/")
        ):
            raise SystemExit(f"unsafe archive member: {member.name}")
        if normalized != expected_root and not normalized.startswith(
            expected_root + "/"
        ):
            raise SystemExit(f"archive member is outside {expected_root}: {member.name}")
        if normalized in seen:
            raise SystemExit(f"duplicate archive member: {member.name}")
        seen.add(normalized)
        if not (
            member.isfile()
            or member.isdir()
            or member.issym()
            or member.islnk()
        ):
            raise SystemExit(f"unsupported archive member type: {member.name}")

        if member.issym():
            relative_name = normalized[len(expected_root):].lstrip("/")
            relative_parent = posixpath.dirname(relative_name)
            target = posixpath.normpath(
                posixpath.join(relative_parent, member.linkname)
            )
            if (
                member.linkname.startswith("/")
                or target == ".."
                or target.startswith("../")
            ):
                raise SystemExit(
                    f"archive symlink escapes the published payload: {member.name}"
                )
        elif member.islnk():
            target = posixpath.normpath(member.linkname)
            if (
                member.linkname.startswith("/")
                or (
                    target != expected_root
                    and not target.startswith(expected_root + "/")
                )
            ):
                raise SystemExit(f"unsafe archive hard link: {member.name}")

if member_count == 0:
    raise SystemExit("archive is empty")
PY
}

# Remove one exact child tree without following symlinks or crossing into a
# mounted filesystem. This is for installer-owned staging and version roots.
remove_managed_tree() {
    local parent="$1"
    local path="$2"
    local child

    case "$path" in
        "$parent"/*) child="${path#"$parent"/}" ;;
        *)
            error "Refusing to remove path outside $parent: $path"
            return 1
            ;;
    esac
    case "$child" in
        ""|*/*)
            error "Refusing to remove non-child path below $parent: $path"
            return 1
            ;;
    esac
    [ ! -e "$path" ] && [ ! -L "$path" ] && return 0
    if ! python3 - "$parent" "$path" << 'PY'
import os
import sys

parent, path = sys.argv[1:]
if os.path.islink(path):
    raise SystemExit(0)
if os.path.ismount(path):
    raise SystemExit(f"refusing to remove mounted tree: {path}")
if os.stat(parent).st_dev != os.stat(path).st_dev:
    raise SystemExit(f"refusing cross-device managed tree: {path}")
PY
    then
        error "Managed tree is a mount or crosses the parent filesystem: $path"
        return 1
    fi
    find "$path" -xdev -depth -delete
}

# Replace one managed child only after its complete payload exists beside the
# destination. The durable journal classifies the two rename commit points
# after signals or hard process death; a kernel-held guard excludes concurrent
# recovery without leaving a stale ownership lock.
_publish_staged_child_directory() {
    local tool_dir="$1"
    local child_name="$2"
    local payload_dir="$3"
    local guard_descriptor="${4:-9}"
    local guard_path=""
    local publication_status=0
    local -a guard_arguments=()

    validate_managed_child_component "$child_name" "managed child" || return 1
    case "$guard_descriptor" in
        8) guard_path="${DOTFILES_MANAGED_INSTALL_GUARD_PATH_8:-}" ;;
        9) guard_path="${DOTFILES_MANAGED_INSTALL_GUARD_PATH_9:-}" ;;
        *)
            error "Unsupported managed installation guard descriptor: $guard_descriptor"
            return 1
            ;;
    esac
    if [ -n "$guard_path" ]; then
        guard_arguments=(
            --installer-guard-fd "$guard_descriptor"
            --installer-guard-path "$guard_path"
        )
    fi
    python3 "$MANAGED_DIRECTORY_PUBLISHER" \
        --parent "$tool_dir" \
        --child "$child_name" \
        --payload "$payload_dir" \
        "${guard_arguments[@]}" ||
        publication_status=$?
    if [ "$publication_status" -eq 0 ]; then
        return 0
    fi

    # The Python worker normally recovers its own exceptions. If it dies
    # outright while the caller shell survives, replay here before the
    # installer's EXIT trap can discard the staged payload named by its
    # journal. A killed caller cannot reach this path; the next invocation's
    # early recovery owns that case.
    if ! python3 "$MANAGED_DIRECTORY_PUBLISHER" \
            --parent "$tool_dir" \
            --child "$child_name" \
            --recover-only \
            "${guard_arguments[@]}"; then
        error "Publication failed and its durable state could not be recovered"
        return 1
    fi
    return "$publication_status"
}

# Replay an interrupted publication before an installer inspects or rebuilds
# the selected child. Ambiguous or foreign transaction state remains intact.
recover_staged_child_directory_publication() {
    local parent="$1"
    local child_name="$2"

    validate_managed_child_component "$child_name" "managed child" || return 1
    python3 "$MANAGED_DIRECTORY_PUBLISHER" \
        --parent "$parent" \
        --child "$child_name" \
        --recover-only
}

recover_staged_directory_publication() {
    local tool_dir="$1"
    local version="$2"

    validate_version_component "$version" "version" || return 1
    recover_staged_child_directory_publication "$tool_dir" "$version"
}

# Publish one versioned tool payload.
publish_staged_directory() {
    local tool_dir="$1"
    local version="$2"
    local payload_dir="$3"

    validate_version_component "$version" "version" || return 1
    _publish_staged_child_directory "$tool_dir" "$version" "$payload_dir"
}

# Publish one non-versioned managed child such as a pinned Git checkout.
publish_staged_child_directory() {
    local parent="$1"
    local child_name="$2"
    local payload_dir="$3"
    local guard_descriptor="${4:-9}"

    validate_managed_child_component "$child_name" "managed child" || return 1
    _publish_staged_child_directory \
        "$parent" "$child_name" "$payload_dir" "$guard_descriptor"
}

# Verify that a checkout is an ordinary, clean, detached checkout of one exact
# commit from one exact origin. Callers use this both as a reuse gate and after
# publication.
pinned_git_checkout_valid() {
    local checkout_dir="$1"
    local expected_origin="$2"
    local expected_commit="$3"
    local actual_origin
    local actual_commit
    local checkout_status

    [ -d "$checkout_dir" ] &&
        [ ! -L "$checkout_dir" ] &&
        [ -d "$checkout_dir/.git" ] &&
        [ ! -L "$checkout_dir/.git" ] ||
        return 1
    actual_origin=$(git -C "$checkout_dir" remote get-url --all origin) ||
        return 1
    [ "$actual_origin" = "$expected_origin" ] || return 1
    actual_commit=$(
        git -C "$checkout_dir" rev-parse --verify 'HEAD^{commit}' 2>/dev/null
    ) || return 1
    [ "$actual_commit" = "$expected_commit" ] || return 1
    if git -C "$checkout_dir" symbolic-ref --quiet HEAD >/dev/null 2>&1; then
        return 1
    fi
    checkout_status=$(
        git -c core.fsmonitor=false -c core.hooksPath=/dev/null \
            -C "$checkout_dir" status --porcelain=v1 \
            --untracked-files=all --ignore-submodules=none
    ) || return 1
    [ -z "$checkout_status" ]
}

# Install a reviewed Git commit without running an upstream installer or
# mutating an existing checkout in place. Existing state must first prove the
# expected origin and cleanliness; a different clean revision is replaced as
# one directory transaction.
install_pinned_git_checkout() (
    set -e
    local label="$1"
    local origin="$2"
    local commit="$3"
    local checkout_dir="$4"
    local checkout_parent
    local checkout_name
    local staging_root=""
    local payload_dir
    local actual_origin
    local checkout_status
    local fetched_commit

    # shellcheck disable=SC2317,SC2329  # Invoked by the EXIT trap below.
    cleanup_pinned_checkout_staging() {
        local final_status=$?
        trap - EXIT HUP INT TERM
        if [ -n "$staging_root" ] &&
                { [ -e "$staging_root" ] || [ -L "$staging_root" ]; }; then
            if ! remove_managed_tree "$checkout_parent" "$staging_root"; then
                error "$label staging requires inspection: $staging_root"
                final_status=1
            fi
        fi
        exit "$final_status"
    }
    trap cleanup_pinned_checkout_staging EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    if [[ ! "$commit" =~ ^[0-9a-f]{40}$ ]]; then
        error "Invalid pinned commit for $label: $commit"
        return 1
    fi
    if [[ ! "$origin" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git$ ]] &&
            [[ ! "$origin" =~ ^file://.+$ ]]; then
        error "Invalid pinned origin for $label: $origin"
        return 1
    fi

    checkout_parent=$(dirname "$checkout_dir")
    checkout_name=$(basename "$checkout_dir")
    validate_managed_child_component "$checkout_name" "$label directory" ||
        return 1
    if [ ! -d "$checkout_parent" ]; then
        mkdir -p "$checkout_parent"
    fi
    if [ ! -d "$checkout_parent" ] || [ -L "$checkout_parent" ]; then
        error "$label parent is not an ordinary directory: $checkout_parent"
        return 1
    fi
    acquire_managed_installation_guard \
        "$checkout_parent" "$checkout_name" 8 || return 1
    recover_managed_installation \
        "$checkout_parent" "$checkout_name" 8 || return 1

    if [ -e "$checkout_dir" ] || [ -L "$checkout_dir" ]; then
        if [ ! -d "$checkout_dir" ] || [ -L "$checkout_dir" ] ||
                [ ! -d "$checkout_dir/.git" ] ||
                [ -L "$checkout_dir/.git" ]; then
            error "$label path is not an ordinary Git checkout: $checkout_dir"
            return 1
        fi
        actual_origin=$(git -C "$checkout_dir" remote get-url --all origin) ||
            return 1
        if [ "$actual_origin" != "$origin" ]; then
            error "$label origin mismatch: $checkout_dir"
            error "Expected: $origin"
            error "Actual:   $actual_origin"
            return 1
        fi
        checkout_status=$(
            git -c core.fsmonitor=false -c core.hooksPath=/dev/null \
                -C "$checkout_dir" status --porcelain=v1 \
                --untracked-files=all --ignore-submodules=none
        ) || {
            error "Could not inspect $label checkout: $checkout_dir"
            return 1
        }
        if [ -n "$checkout_status" ]; then
            error "$label checkout has local changes: $checkout_dir"
            return 1
        fi
        if pinned_git_checkout_valid "$checkout_dir" "$origin" "$commit"; then
            info "$label is already pinned at $commit"
            return 0
        fi
    fi

    staging_root=$(
        create_managed_staging_directory "$checkout_parent" "$checkout_name"
    )
    payload_dir="$staging_root/payload"
    git init --quiet "$payload_dir"
    git -C "$payload_dir" remote add origin "$origin"
    git -c core.hooksPath=/dev/null -C "$payload_dir" \
        fetch --quiet --no-tags --depth=1 origin "$commit"
    fetched_commit=$(
        git -C "$payload_dir" rev-parse --verify 'FETCH_HEAD^{commit}'
    )
    if [ "$fetched_commit" != "$commit" ]; then
        error "$label fetch resolved to unexpected commit: $fetched_commit"
        return 1
    fi
    git -c advice.detachedHead=false -c core.hooksPath=/dev/null \
        -C "$payload_dir" checkout --quiet --detach "$commit"
    if ! pinned_git_checkout_valid "$payload_dir" "$origin" "$commit"; then
        error "$label staged checkout failed identity validation"
        return 1
    fi

    publish_staged_child_directory \
        "$checkout_parent" "$checkout_name" "$payload_dir" 8
    if ! pinned_git_checkout_valid "$checkout_dir" "$origin" "$commit"; then
        error "$label published checkout failed identity validation"
        return 1
    fi
    remove_managed_tree "$checkout_parent" "$staging_root"
    staging_root=""
    info "$label pinned at $commit"
)

# Create or update latest through a temporary symlink and atomic os.replace.
update_latest() (
    local tool_dir="$1"
    local version="$2"
    local install_dir="$tool_dir/$version"
    local latest_path="$tool_dir/latest"
    local latest_temporary_link

    # shellcheck disable=SC2317,SC2329  # Invoked by the EXIT trap below.
    cleanup_latest_temporary_link() {
        if [ -n "${latest_temporary_link:-}" ] &&
                { [ -e "$latest_temporary_link" ] ||
                    [ -L "$latest_temporary_link" ]; }; then
            unlink "$latest_temporary_link"
        fi
    }
    trap cleanup_latest_temporary_link EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    validate_version_component "$version" "version" || return 1
    if [ ! -d "$install_dir" ] || [ -L "$install_dir" ]; then
        error "Cannot publish latest for a non-directory install: $install_dir"
        return 1
    fi
    if { [ -e "$latest_path" ] || [ -L "$latest_path" ]; } &&
            [ ! -L "$latest_path" ]; then
        error "Refusing to replace non-symlink latest path: $latest_path"
        return 1
    fi

    latest_temporary_link=$(mktemp "$tool_dir/.latest.XXXXXX")
    unlink "$latest_temporary_link"
    ln -s "$version" "$latest_temporary_link"
    if ! python3 - "$latest_temporary_link" "$latest_path" << 'PY'
import os
import sys

os.replace(sys.argv[1], sys.argv[2])
PY
    then
        return 1
    fi
    latest_temporary_link=""
    info "Updated latest -> $version"
)

# Resolve a command to one ordinary executable, then publish its alias without
# overwriting an independently owned regular file or following a directory
# destination. Resolving once accepts package-managed command links without
# carrying their mutable link chain into the user-owned alias.
update_command_symlink() (
    local target="$1"
    local destination="$2"
    local resolved_target
    local destination_directory
    local command_temporary_link

    # shellcheck disable=SC2317,SC2329  # Invoked by the EXIT trap below.
    cleanup_command_temporary_link() {
        if [ -n "${command_temporary_link:-}" ] &&
                { [ -e "$command_temporary_link" ] ||
                    [ -L "$command_temporary_link" ]; }; then
            unlink "$command_temporary_link"
        fi
    }
    trap cleanup_command_temporary_link EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    case "$target" in
        /*) ;;
        *)
            error "Cannot link a non-absolute executable: $target"
            return 1
            ;;
    esac
    resolved_target=$(python3 - "$target" << 'PY'
import os
import sys

print(os.path.realpath(sys.argv[1]))
PY
    ) || return 1
    if [ ! -f "$resolved_target" ] ||
            [ -L "$resolved_target" ] ||
            [ ! -x "$resolved_target" ]; then
        error "Cannot link a non-regular executable: $target"
        return 1
    fi
    destination_directory=$(dirname "$destination")
    if [ ! -d "$destination_directory" ] || [ -L "$destination_directory" ]; then
        error "Command-link directory is not an ordinary directory: $destination_directory"
        return 1
    fi
    if { [ -e "$destination" ] || [ -L "$destination" ]; } &&
            [ ! -L "$destination" ]; then
        error "Refusing to replace non-symlink command path: $destination"
        return 1
    fi

    command_temporary_link=$(mktemp \
        "$destination_directory/.command-link.XXXXXX")
    unlink "$command_temporary_link"
    ln -s "$resolved_target" "$command_temporary_link"
    if ! python3 - "$command_temporary_link" "$destination" << 'PY'
import os
import sys

os.replace(sys.argv[1], sys.argv[2])
PY
    then
        return 1
    fi
    command_temporary_link=""
)

# Force replacement is authorization granted only by this invocation's parsed
# command line. An ambient environment variable must never authorize deletion.
# shellcheck disable=SC2034  # Parsed by installers after sourcing this library.
FORCE=false

# Show usage for a tool installer.
show_install_usage() {
    local tool="$1"
    local version_help="${2:-VERSION}"
    cat << EOF
Usage: $tool/install.sh [$version_help]

Install $tool to ~/tools/$tool/<version>/

If no version specified, installs the latest.
EOF
}
