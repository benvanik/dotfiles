#!/bin/bash
# Install or repair Determinate Nix from one pinned native installer.
# Usage: nix/install.sh [--force]
set -euo pipefail

TOOL_NAME="nix"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../install-utils.sh
source "$SCRIPT_DIR/../install-utils.sh"

# Updating the installer is a reviewed source change: change the version and all
# platform digests together from the corresponding GitHub release.
NIX_INSTALLER_VERSION="3.21.9"
NIX_INSTALLER_URL_ROOT="https://install.determinate.systems/nix/tag/v$NIX_INSTALLER_VERSION"
NIX_ROOT="/nix"
NIX_STAGING_PARENT=""
NIX_STAGING_DIR=""
FORCE=false

show_help() {
    cat << EOF
Usage: nix/install.sh [options]

Install Determinate Nix system-wide using the pinned, checksummed Determinate
Nix Installer v$NIX_INSTALLER_VERSION.

Options:
  -f, --force  Repair an existing verified Determinate-managed installation
               with its attested installed installer. This does not reinstall
               Nix or rewrite its installation receipt.
  -h, --help   Show this help.

The current pin applies to fresh installs. Installations created by a prior
reviewed pin remain recognized. Foreign, unidentified, and partial /nix
installations are left untouched.
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
                if [ $# -ne 0 ]; then
                    error "nix/install.sh accepts no positional arguments"
                    exit 1
                fi
                ;;
            -*)
                error "Unknown option: $1"
                exit 1
                ;;
            *)
                error "Unknown argument: $1"
                exit 1
                ;;
        esac
    done
}

# Ownership survives a source-controlled pin advance only when the installed
# receipt and installer binary match this explicit reviewed lineage. When
# advancing NIX_INSTALLER_VERSION, retain prior entries and add the new release
# digests; never replace the table with only the new current pin.
reviewed_nix_installer_sha256() {
    local version="$1"

    case "$version:$PLATFORM/$ARCH" in
        3.21.9:linux/x86_64)
            printf '%s\n' \
                58cf15422853e95187405d66b0cdb306e66f602218ee0032386c46b1b776a6d1
            ;;
        3.21.9:linux/aarch64)
            printf '%s\n' \
                3e4f83cc87025c2890293cd2a8b6889ad2a0f7c5394f87ba8ad4fc958cf2aaea
            ;;
        3.21.9:darwin/aarch64)
            printf '%s\n' \
                f6a266434f08606a023fd5bd33a77b868016256265ba5668ad0748d71d1625b0
            ;;
        *) return 1 ;;
    esac
}

select_installer() {
    case "$PLATFORM/$ARCH" in
        linux/x86_64)
            INSTALLER_ASSET="nix-installer-x86_64-linux"
            ;;
        linux/aarch64)
            INSTALLER_ASSET="nix-installer-aarch64-linux"
            ;;
        darwin/aarch64)
            INSTALLER_ASSET="nix-installer-aarch64-darwin"
            ;;
        darwin/x86_64)
            error "Determinate Nix Installer v$NIX_INSTALLER_VERSION does not publish an Intel macOS binary"
            return 1
            ;;
        *)
            error "Unsupported platform: $PLATFORM/$ARCH"
            return 1
            ;;
    esac
    INSTALLER_SHA256=$(
        reviewed_nix_installer_sha256 "$NIX_INSTALLER_VERSION"
    ) || {
        error "No reviewed installer digest for Nix Installer v$NIX_INSTALLER_VERSION on $PLATFORM/$ARCH"
        return 1
    }
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

nix_receipt_version() {
    local receipt="$1"

    python3 - "$receipt" "$PLATFORM" << 'PY'
import json
import re
import sys

receipt_path, platform = sys.argv[1:]
with open(receipt_path, encoding="utf-8") as receipt_file:
    receipt = json.load(receipt_file)

if not isinstance(receipt, dict):
    raise SystemExit("Nix installer receipt is not an object")
version = receipt.get("version")
if not isinstance(version, str) or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
    raise SystemExit("Nix installer receipt has no canonical version")

actions = receipt.get("actions")
if (
    not isinstance(actions, list)
    or not actions
    or any(not isinstance(action, dict) for action in actions)
):
    raise SystemExit("Nix installer receipt has no action plan")

planner = receipt.get("planner")
if not isinstance(planner, dict):
    raise SystemExit("Nix installer receipt has no planner")
planner_name = planner.get("planner")
accepted_planners = {
    "linux": {"linux", "ostree", "steam-deck"},
    "darwin": {"macos"},
}
if planner_name not in accepted_planners.get(platform, set()):
    raise SystemExit("Nix installer receipt planner does not match this platform")
print(version)
PY
}

managed_nix_installation_version() {
    local installed_binary="$NIX_ROOT/var/nix/profiles/default/bin/nix"
    local installed_installer="$NIX_ROOT/nix-installer"
    local receipt="$NIX_ROOT/receipt.json"
    local receipt_version
    local expected_installer_sha256

    [ -d "$NIX_ROOT" ] && [ ! -L "$NIX_ROOT" ] || return 1
    managed_executable_valid "$NIX_ROOT" "$installed_binary" || return 1
    [ -f "$installed_installer" ] &&
        [ ! -L "$installed_installer" ] &&
        [ -x "$installed_installer" ] || return 1
    [ -f "$receipt" ] && [ ! -L "$receipt" ] || return 1
    receipt_version=$(nix_receipt_version "$receipt" 2>/dev/null) || return 1
    expected_installer_sha256=$(
        reviewed_nix_installer_sha256 "$receipt_version"
    ) || return 1
    verify_sha256 "$installed_installer" "$expected_installer_sha256" \
        >/dev/null 2>&1 || return 1
    "$installed_binary" --version >/dev/null || return 1
    printf '%s\n' "$receipt_version"
}

path_nix_binary() {
    local binary

    binary=$(type -P nix 2>/dev/null || true)
    [ -n "$binary" ] || return 1
    printf '%s\n' "$binary"
}

# The installer exposes every setting through NIX_INSTALLER_* environment
# variables. Clear that ambient control plane so the reviewed command below is
# the complete install/repair plan.
run_installer_with_clean_settings() (
    local environment_entry
    local variable_name

    while IFS= read -r environment_entry; do
        variable_name="${environment_entry%%=*}"
        case "$variable_name" in
            NIX_INSTALLER_*) unset "$variable_name" ;;
        esac
    done < <(env)
    "$@"
)

verify_after_operation() {
    local expected_managed_version="$1"
    local installed_binary="$NIX_ROOT/var/nix/profiles/default/bin/nix"
    local daemon_profile="$NIX_ROOT/var/nix/profiles/default/etc/profile.d/nix-daemon.sh"
    local managed_version
    local installed_version

    managed_version=$(managed_nix_installation_version) || {
        error "Determinate Nix ownership or executable validation failed"
        return 1
    }
    if [ "$managed_version" != "$expected_managed_version" ]; then
        error "Determinate Nix receipt changed from reviewed v$expected_managed_version to v$managed_version"
        return 1
    fi
    if [ -f "$daemon_profile" ] && [ ! -L "$daemon_profile" ]; then
        # shellcheck source=/dev/null
        . "$daemon_profile"
    fi
    installed_version=$("$installed_binary" --version)
    info "Installed: $installed_version"
}

main() {
    local existing_path_nix=""
    local installer_path
    local managed_version=""

    parse_arguments "$@"
    select_installer

    managed_version=$(managed_nix_installation_version || true)
    if [ -n "$managed_version" ]; then
        if [ "$FORCE" != "true" ]; then
            info "Already installed: $("$NIX_ROOT/var/nix/profiles/default/bin/nix" --version)"
            if [ "$managed_version" != "$NIX_INSTALLER_VERSION" ]; then
                info "Recognized reviewed installer v$managed_version; v$NIX_INSTALLER_VERSION is the fresh-install pin"
            fi
            info "Use --force to repair with the attested installed installer"
            exit 0
        fi

        info "Repairing the verified Determinate Nix installation..."
        run_installer_with_clean_settings \
            "$NIX_ROOT/nix-installer" repair --no-confirm
        verify_after_operation "$managed_version"
        info "Determinate Nix repair completed successfully"
        exit 0
    else
        existing_path_nix=$(path_nix_binary || true)
        if [ -n "$existing_path_nix" ] ||
                [ -e "$NIX_ROOT" ] || [ -L "$NIX_ROOT" ]; then
            error "Refusing to modify an unidentified or partial Nix installation"
            [ -z "$existing_path_nix" ] ||
                error "PATH resolves nix to: $existing_path_nix"
            if [ -e "$NIX_ROOT" ] || [ -L "$NIX_ROOT" ]; then
                error "Unmanaged Nix root exists at: $NIX_ROOT"
            fi
            exit 1
        fi
        if [ "$FORCE" = "true" ]; then
            error "--force repairs an existing managed installation; none was found"
            exit 1
        fi
    fi

    NIX_STAGING_PARENT="${TMPDIR:-/tmp}"
    if [ ! -d "$NIX_STAGING_PARENT" ] || [ -L "$NIX_STAGING_PARENT" ]; then
        error "Temporary directory is not an ordinary directory: $NIX_STAGING_PARENT"
        exit 1
    fi
    NIX_STAGING_DIR=$(
        mktemp -d "$NIX_STAGING_PARENT/dotfiles-nix-installer.XXXXXX"
    )
    cleanup_staging() {
        local final_status=$?

        trap - EXIT HUP INT TERM
        case "$NIX_STAGING_DIR" in
            "$NIX_STAGING_PARENT"/dotfiles-nix-installer.*)
                if [ -e "$NIX_STAGING_DIR" ] &&
                        ! find "$NIX_STAGING_DIR" -xdev -depth -delete; then
                    error "Nix staging directory requires inspection: $NIX_STAGING_DIR"
                    final_status=1
                fi
                ;;
            *)
                error "Refusing unexpected Nix staging cleanup: $NIX_STAGING_DIR"
                final_status=1
                ;;
        esac
        exit "$final_status"
    }
    trap cleanup_staging EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    installer_path="$NIX_STAGING_DIR/$INSTALLER_ASSET"

    download "$NIX_INSTALLER_URL_ROOT/$INSTALLER_ASSET" "$installer_path"
    verify_sha256 "$installer_path" "$INSTALLER_SHA256"
    chmod 0700 "$installer_path"
    "$installer_path" --version >/dev/null || {
        error "Pinned Determinate Nix installer is not executable on this host"
        exit 1
    }

    info "Installing Determinate Nix..."
    run_installer_with_clean_settings \
        "$installer_path" install --no-confirm
    verify_after_operation "$NIX_INSTALLER_VERSION"

    info "Determinate Nix operation completed successfully"
    info "Restart your shell to load the daemon profile"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
