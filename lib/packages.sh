#!/bin/bash
# ~/.dotfiles/lib/packages.sh - Package definitions and verification.
# Single source of truth for all system dependencies.
# Requires bash 4+ for associative arrays.

# ============================================================================
# Package Categories
# ============================================================================

# Required packages - dotfiles install fails without these.
REQUIRED_PACKAGES=(zsh git curl fzf rg jq direnv)

# Recommended packages - warnings only, install proceeds.
RECOMMENDED_PACKAGES=(fd bat eza shellcheck ccache)

# ============================================================================
# Package Manager Name Mappings
# ============================================================================
# Format: PKG_NAMES[pm:canonical]=actual_package_name
# Only specify if different from canonical name.

declare -A PKG_NAMES=(
    # apt uses different names.
    [apt:rg]="ripgrep"
    [apt:fd]="fd-find"

    # dnf differences.
    [dnf:rg]="ripgrep"
    [dnf:fd]="fd-find"
    [dnf:shellcheck]="ShellCheck"

    # pacman differences.
    [pacman:rg]="ripgrep"

    # brew differences.
    [brew:rg]="ripgrep"
)

# ============================================================================
# Binary Name Mappings
# ============================================================================
# Some packages install binaries with different names.
# Format: BIN_NAMES[pm:canonical]=actual_binary

declare -A BIN_NAMES=(
    [apt:fd]="fdfind"
    [apt:bat]="batcat"
)

# ============================================================================
# Package Manager Detection
# ============================================================================

# Detect the system package manager.
# Returns: apt|dnf|pacman|brew or empty string.
_pkg_detect_pm() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        command -v brew &>/dev/null && echo "brew" && return
        return 1
    elif command -v apt &>/dev/null; then
        echo "apt"
    elif command -v dnf &>/dev/null; then
        echo "dnf"
    elif command -v pacman &>/dev/null; then
        echo "pacman"
    else
        return 1
    fi
}

# ============================================================================
# Package Resolution
# ============================================================================

# Get package manager-specific name for a canonical package.
# Args: pm, canonical_name
# Returns: actual package name for that manager.
_pkg_resolve_name() {
    local pm="$1" canonical="$2"
    local key="${pm}:${canonical}"

    # Check for manager-specific override.
    if [[ -v "PKG_NAMES[$key]" ]]; then
        echo "${PKG_NAMES[$key]}"
    else
        # Default to canonical name.
        echo "$canonical"
    fi
}

# Get the binary name to check for a package.
# Args: canonical_name, pm
# Returns: binary name to look for in PATH.
_pkg_resolve_bin() {
    local canonical="$1" pm="$2"
    local key="${pm}:${canonical}"

    # Check for manager-specific binary name.
    if [[ -v "BIN_NAMES[$key]" ]]; then
        echo "${BIN_NAMES[$key]}"
    else
        # Binary name same as canonical name.
        echo "$canonical"
    fi
}

# ============================================================================
# Verification Functions
# ============================================================================

# Check if a single package/binary is available.
# Args: canonical_name
# Returns: 0 if found, 1 if missing.
_pkg_check() {
    local canonical="$1"
    local pm
    pm=$(_pkg_detect_pm) || pm=""
    local bin
    bin=$(_pkg_resolve_bin "$canonical" "$pm")

    command -v "$bin" &>/dev/null
}

# Verify all required packages are installed.
# Returns: 0 if all found, 1 if any missing.
# Side effect: Prints missing packages to stderr.
_pkg_verify_required() {
    local missing=()
    local pm
    pm=$(_pkg_detect_pm) || pm=""

    for pkg in "${REQUIRED_PACKAGES[@]}"; do
        if ! _pkg_check "$pkg"; then
            missing+=("$pkg")
        fi
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "Missing required packages:" >&2
        for pkg in "${missing[@]}"; do
            local actual
            actual=$(_pkg_resolve_name "$pm" "$pkg")
            echo "  - $pkg (package: $actual)" >&2
        done
        return 1
    fi
    return 0
}

# Verify recommended packages (warnings only).
# Returns: 0 always.
# Side effect: Prints missing packages as warnings.
_pkg_verify_recommended() {
    local missing=()
    local pm
    pm=$(_pkg_detect_pm) || pm=""

    for pkg in "${RECOMMENDED_PACKAGES[@]}"; do
        if ! _pkg_check "$pkg"; then
            missing+=("$pkg")
        fi
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "Missing recommended packages (optional):" >&2
        for pkg in "${missing[@]}"; do
            local actual
            actual=$(_pkg_resolve_name "$pm" "$pkg")
            echo "  - $pkg (package: $actual)" >&2
        done
    fi
    return 0
}

# Full verification for doctor command.
# Args: Optional color codes (GREEN, YELLOW, RED, NC, BOLD).
# Returns: 0 if all required found, 1 otherwise.
# Output: Formatted for human consumption.
_pkg_verify_all() {
    local GREEN="${1:-$'\033[0;32m'}"
    local YELLOW="${2:-$'\033[0;33m'}"
    local RED="${3:-$'\033[0;31m'}"
    local NC="${4:-$'\033[0m'}"
    local BOLD="${5:-$'\033[1m'}"

    local pm
    pm=$(_pkg_detect_pm) || pm=""
    local all_ok=true

    printf "%bRequired packages:%b\n" "$BOLD" "$NC"
    for pkg in "${REQUIRED_PACKAGES[@]}"; do
        local bin
        bin=$(_pkg_resolve_bin "$pkg" "$pm")
        if command -v "$bin" &>/dev/null; then
            printf "  %b[OK]%b %s\n" "$GREEN" "$NC" "$pkg"
        else
            printf "  %b[MISSING]%b %s\n" "$RED" "$NC" "$pkg"
            all_ok=false
        fi
    done

    echo ""
    printf "%bRecommended packages:%b\n" "$BOLD" "$NC"
    for pkg in "${RECOMMENDED_PACKAGES[@]}"; do
        local bin
        bin=$(_pkg_resolve_bin "$pkg" "$pm")
        if command -v "$bin" &>/dev/null; then
            printf "  %b[OK]%b %s\n" "$GREEN" "$NC" "$pkg"
        else
            printf "  %b[MISSING]%b %s (optional)\n" "$YELLOW" "$NC" "$pkg"
        fi
    done

    $all_ok
}

# ============================================================================
# Package List Generation
# ============================================================================

# Get list of packages to install for a package manager.
# Args: pm, category (required|recommended|all)
# Returns: Space-separated list of package names.
_pkg_get_install_list() {
    local pm="$1" category="${2:-all}"
    local packages=()

    case "$category" in
        required)
            for pkg in "${REQUIRED_PACKAGES[@]}"; do
                packages+=("$(_pkg_resolve_name "$pm" "$pkg")")
            done
            ;;
        recommended)
            for pkg in "${RECOMMENDED_PACKAGES[@]}"; do
                packages+=("$(_pkg_resolve_name "$pm" "$pkg")")
            done
            ;;
        all|*)
            for pkg in "${REQUIRED_PACKAGES[@]}" "${RECOMMENDED_PACKAGES[@]}"; do
                packages+=("$(_pkg_resolve_name "$pm" "$pkg")")
            done
            ;;
    esac

    echo "${packages[*]}"
}
