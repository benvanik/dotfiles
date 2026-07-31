#!/bin/bash
# ~/.dotfiles/install-deps.sh - Install dependencies for dotfiles
# Supports apt (Debian/Ubuntu), dnf (Fedora), pacman (Arch), and brew (macOS).
set -euo pipefail

DOTFILES="${DOTFILES:-$HOME/.dotfiles}"
TOOL_NAME="deps"

# Package names and manager-specific mappings.
# shellcheck source=lib/packages.sh
source "$DOTFILES/lib/packages.sh"
# Reviewed Git, font, and Node identities.
# shellcheck source=lib/bootstrap-pins.sh
source "$DOTFILES/lib/bootstrap-pins.sh"
# Transactional publication, digest, and checkout helpers.
# shellcheck source=tools/install-utils.sh
source "$DOTFILES/tools/install-utils.sh"

PKG_MGR=""
NVM_INSTALLER="$DOTFILES/tools/nvm/install.sh"

FONT_NAMES=(
    "MesloLGS NF Regular.ttf"
    "MesloLGS NF Bold.ttf"
    "MesloLGS NF Italic.ttf"
    "MesloLGS NF Bold Italic.ttf"
)
FONT_SHA256=(
    "d97946186e97f8d7c0139e8983abf40a1d2d086924f2c5dbf1c29bd8f2c6e57d"
    "b6c0199cf7c7483c8343ea020658925e6de0aeb318b89908152fcb4d19226003"
    "6f357bcbe2597704e157a915625928bca38364a89c22a4ac36e7a116dcd392ef"
    "56b4131adecec052c4b324efb818dd326d586dbc316fc68f98f1cae2eb8d1220"
)

# ============================================================================
# Detect Package Manager
# ============================================================================
detect_package_manager() {
    _pkg_detect_pm && return
    if [[ "$OSTYPE" == "darwin"* ]]; then
        error "Homebrew not found. Install from https://brew.sh"
    else
        error "Unsupported package manager. Install packages manually."
    fi
    exit 1
}

# ============================================================================
# Package Installation Functions
# ============================================================================

run_apt_noninteractive() {
    sudo /usr/bin/env \
        DEBIAN_FRONTEND=noninteractive \
        /usr/bin/apt-get "$@"
}

install_apt() {
    local required_packages=()
    local recommended_packages=()
    read -r -a required_packages <<< "$(_pkg_get_install_list apt required)"
    read -r -a recommended_packages <<< "$(_pkg_get_install_list apt recommended)"

    info "Updating package lists..."
    run_apt_noninteractive update

    info "Installing required packages..."
    run_apt_noninteractive install -y "${required_packages[@]}"

    info "Installing recommended packages..."
    # Some packages may not exist on older systems.
    run_apt_noninteractive install -y \
        "${recommended_packages[@]}" \
        zsh-autosuggestions 2>/dev/null || \
        warn "Some optional packages not available"

    info "Installing build tools..."
    run_apt_noninteractive install -y patchelf

    # Debian/Ubuntu use different binary names.
    setup_debian_symlinks
}

install_dnf() {
    local required_packages=()
    local recommended_packages=()
    read -r -a required_packages <<< "$(_pkg_get_install_list dnf required)"
    read -r -a recommended_packages <<< "$(_pkg_get_install_list dnf recommended)"

    info "Installing required packages..."
    sudo dnf install -y "${required_packages[@]}"

    info "Installing recommended packages..."
    sudo dnf install -y \
        "${recommended_packages[@]}" \
        zsh-autosuggestions 2>/dev/null || \
        warn "Some optional packages not available"
}

install_pacman() {
    local required_packages=()
    local recommended_packages=()
    read -r -a required_packages <<< "$(_pkg_get_install_list pacman required)"
    read -r -a recommended_packages <<< "$(_pkg_get_install_list pacman recommended)"

    info "Installing required packages..."
    sudo pacman -S --needed --noconfirm "${required_packages[@]}"

    info "Installing recommended packages..."
    sudo pacman -S --needed --noconfirm \
        "${recommended_packages[@]}" \
        zsh-autosuggestions || warn "Some optional packages not available"
}

install_brew() {
    local required_packages=()
    local recommended_packages=()
    read -r -a required_packages <<< "$(_pkg_get_install_list brew required)"
    read -r -a recommended_packages <<< "$(_pkg_get_install_list brew recommended)"

    info "Installing required packages..."
    brew install "${required_packages[@]}"

    info "Installing recommended packages..."
    brew install \
        "${recommended_packages[@]}" \
        zsh-autosuggestions \
        zsh-syntax-highlighting

    # Set up fzf key bindings.
    if [ -f "$(brew --prefix)/opt/fzf/install" ]; then
        info "Setting up fzf key bindings..."
        "$(brew --prefix)/opt/fzf/install" --key-bindings --completion --no-update-rc
    fi
}

# Create symlinks for Debian/Ubuntu renamed binaries.
setup_debian_symlinks() {
    mkdir -p "$HOME/.local/bin"

    # fd-find -> fd.
    if command -v fdfind &>/dev/null && ! command -v fd &>/dev/null; then
        info "Creating fd symlink (fdfind -> fd)..."
        update_command_symlink \
            "$(command -v fdfind)" "$HOME/.local/bin/fd"
    fi

    # batcat -> bat.
    if command -v batcat &>/dev/null && ! command -v bat &>/dev/null; then
        info "Creating bat symlink (batcat -> bat)..."
        update_command_symlink \
            "$(command -v batcat)" "$HOME/.local/bin/bat"
    fi
}

# ============================================================================
# Install Powerlevel10k
# ============================================================================
install_p10k() {
    install_pinned_git_checkout \
        "Powerlevel10k" \
        "$DOTFILES_P10K_ORIGIN" \
        "$DOTFILES_P10K_COMMIT" \
        "$HOME/.local/p10k"
}

# ============================================================================
# Install Nerd Fonts (MesloLGS NF for p10k)
# ============================================================================
install_fonts() (
    set -e
    local font_dir
    local font_index
    local font_name
    local font_path
    local encoded_name
    local staging_root=""
    local staged_font
    local missing_font="false"

    # shellcheck disable=SC2317  # Invoked by the EXIT trap below.
    cleanup_font_staging() {
        local final_status=$?
        trap - EXIT HUP INT TERM
        if [ -n "$staging_root" ] &&
                { [ -e "$staging_root" ] || [ -L "$staging_root" ]; }; then
            if ! remove_managed_tree "$font_dir" "$staging_root"; then
                error "Font staging requires inspection: $staging_root"
                final_status=1
            fi
        fi
        exit "$final_status"
    }
    trap cleanup_font_staging EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    if [[ "$OSTYPE" == "darwin"* ]]; then
        font_dir="$HOME/Library/Fonts"
    else
        font_dir="$HOME/.local/share/fonts"
    fi

    mkdir -p "$font_dir"
    if [ ! -d "$font_dir" ] || [ -L "$font_dir" ]; then
        error "Font root is not an ordinary directory: $font_dir"
        return 1
    fi

    for font_index in "${!FONT_NAMES[@]}"; do
        font_name="${FONT_NAMES[$font_index]}"
        font_path="$font_dir/$font_name"
        if [ -e "$font_path" ] || [ -L "$font_path" ]; then
            if ! verify_sha256 \
                    "$font_path" "${FONT_SHA256[$font_index]}" \
                    >/dev/null 2>&1; then
                error "Refusing unverified existing font: $font_path"
                return 1
            fi
        else
            missing_font="true"
        fi
    done

    if [ "$missing_font" = "false" ]; then
        info "MesloLGS NF fonts are already verified"
        return 0
    fi

    info "Installing pinned MesloLGS NF fonts for Powerlevel10k..."
    staging_root=$(mktemp -d "$font_dir/.dotfiles-meslo.XXXXXX")
    for font_index in "${!FONT_NAMES[@]}"; do
        font_name="${FONT_NAMES[$font_index]}"
        font_path="$font_dir/$font_name"
        if [ -e "$font_path" ] || [ -L "$font_path" ]; then
            continue
        fi
        encoded_name="${font_name// /%20}"
        staged_font="$staging_root/$font_name"
        download \
            "https://raw.githubusercontent.com/romkatv/powerlevel10k-media/$DOTFILES_FONT_MEDIA_COMMIT/$encoded_name" \
            "$staged_font"
        verify_sha256 "$staged_font" "${FONT_SHA256[$font_index]}"
        chmod 0644 "$staged_font"
    done

    # Hard-link publication fails rather than replacing a path that appeared
    # concurrently. Removing staging later leaves each published font intact.
    for font_index in "${!FONT_NAMES[@]}"; do
        font_name="${FONT_NAMES[$font_index]}"
        staged_font="$staging_root/$font_name"
        [ -f "$staged_font" ] || continue
        font_path="$font_dir/$font_name"
        python3 - "$staged_font" "$font_path" << 'PY'
import os
import sys

source, destination = sys.argv[1:]
os.link(source, destination, follow_symlinks=False)
PY
    done
    remove_managed_tree "$font_dir" "$staging_root"
    staging_root=""

    # Refresh font cache (Linux only).
    if [[ "$OSTYPE" != "darwin"* ]] && command -v fc-cache &>/dev/null; then
        fc-cache -f "$font_dir"
    fi

    info "Fonts installed! Set your terminal font to 'MesloLGS NF'"
)

# ============================================================================
# Install Oh My Zsh
# ============================================================================
install_omz() {
    install_pinned_git_checkout \
        "Oh My Zsh" \
        "$DOTFILES_OMZ_ORIGIN" \
        "$DOTFILES_OMZ_COMMIT" \
        "$HOME/.oh-my-zsh"
}

install_tpm() {
    install_pinned_git_checkout \
        "TPM" \
        "$DOTFILES_TPM_ORIGIN" \
        "$DOTFILES_TPM_COMMIT" \
        "$HOME/.tmux/plugins/tpm"
}

# ============================================================================
# Install tmux, byobu, and TPM
# ============================================================================
install_tmux_byobu() {
    # Install tmux via package manager.
    if ! command -v tmux &>/dev/null; then
        info "Installing tmux..."
        case "$PKG_MGR" in
            apt)    run_apt_noninteractive install -y tmux ;;
            dnf)    sudo dnf install -y tmux ;;
            pacman) sudo pacman -S --needed --noconfirm tmux ;;
            brew)   brew install tmux ;;
        esac
    else
        info "tmux already installed: $(tmux -V)"
    fi

    # Install byobu (tmux wrapper with better defaults).
    if ! command -v byobu &>/dev/null; then
        info "Installing byobu..."
        case "$PKG_MGR" in
            apt)    run_apt_noninteractive install -y byobu ;;
            dnf)    sudo dnf install -y byobu ;;
            pacman) sudo pacman -S --needed --noconfirm byobu ;;
            brew)   brew install byobu ;;
        esac
    else
        info "byobu already installed"
    fi

    install_tpm
}

# ============================================================================
# Install NVM and Node.js
# ============================================================================
install_nvm() {
    export NVM_DIR="$HOME/.nvm"
    if [ ! -f "$NVM_INSTALLER" ] || [ -L "$NVM_INSTALLER" ]; then
        error "Pinned NVM installer is unavailable: $NVM_INSTALLER"
        return 1
    fi
    bash "$NVM_INSTALLER" --migrate

    # Load nvm for this session.
    # shellcheck disable=SC1091
    . "$NVM_DIR/nvm.sh"

    info "Installing Node.js $DOTFILES_NODE_VERSION..."
    nvm install "$DOTFILES_NODE_VERSION"
    nvm alias default "$DOTFILES_NODE_VERSION"
    nvm use --silent "$DOTFILES_NODE_VERSION"
    if [ "$(node --version)" != "v$DOTFILES_NODE_VERSION" ]; then
        error "Node version did not resolve to v$DOTFILES_NODE_VERSION"
        return 1
    fi
}

# ============================================================================
# Main
# ============================================================================
main() {
    if [ $# -eq 1 ] && [ "$1" = "--verify" ]; then
        _pkg_verify_required
        return
    fi
    if [ $# -ne 0 ]; then
        error "Usage: install-deps.sh [--verify]"
        return 1
    fi

    PKG_MGR=$(detect_package_manager)
    info "Detected package manager: $PKG_MGR"

    case "$PKG_MGR" in
        apt)    install_apt ;;
        dnf)    install_dnf ;;
        pacman) install_pacman ;;
        brew)   install_brew ;;
    esac

    install_omz
    install_tmux_byobu
    install_p10k
    install_fonts
    install_nvm

    echo ""
    info "====================================="
    info "Dependencies installed!"
    info "====================================="
    echo ""
    info "Next step:"
    info "  $DOTFILES/bin/dotfiles install"
    echo ""

    # Remind about changing shell.
    if [ "${SHELL:-}" != "$(command -v zsh)" ]; then
        warn "Your default shell is not zsh."
        warn "To change: chsh -s \$(which zsh)"
        echo ""
    fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
