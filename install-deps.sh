#!/bin/bash
# ~/.dotfiles/install-deps.sh - Install dependencies for dotfiles
# Supports apt (Debian/Ubuntu), dnf (Fedora), pacman (Arch), and brew (macOS).
set -e

DOTFILES="${DOTFILES:-$HOME/.dotfiles}"

GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m'

info() { printf "${GREEN}[deps]${NC} %s\n" "$1"; }
warn() { printf "${YELLOW}[deps]${NC} %s\n" "$1"; }
error() { printf "${RED}[deps]${NC} %s\n" "$1" >&2; }

# ============================================================================
# Verify Mode (--verify)
# ============================================================================
# Check if dependencies are installed without installing anything.
# Used by: dotfiles install (to check before prompting for sudo).
if [[ "${1:-}" == "--verify" ]]; then
    # Source package definitions.
    source "$DOTFILES/lib/packages.sh"
    _pkg_verify_required
    exit $?
fi

# ============================================================================
# Detect Package Manager
# ============================================================================
detect_package_manager() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        if command -v brew &>/dev/null; then
            echo "brew"
        else
            error "Homebrew not found. Install from https://brew.sh"
            exit 1
        fi
    elif command -v apt &>/dev/null; then
        echo "apt"
    elif command -v dnf &>/dev/null; then
        echo "dnf"
    elif command -v pacman &>/dev/null; then
        echo "pacman"
    else
        error "Unsupported package manager. Install packages manually."
        exit 1
    fi
}

PKG_MGR=$(detect_package_manager)
info "Detected package manager: $PKG_MGR"

# ============================================================================
# Package Installation Functions
# ============================================================================

install_apt() {
    info "Updating package lists..."
    sudo apt update

    info "Installing core packages..."
    sudo apt install -y zsh git curl

    info "Installing CLI tools..."
    sudo apt install -y fzf ripgrep bat jq shellcheck ccache direnv

    info "Installing recommended packages..."
    # Some packages may not exist on older systems.
    sudo apt install -y fd-find eza zsh-autosuggestions 2>/dev/null || \
        warn "Some optional packages not available"

    info "Installing build tools..."
    sudo apt install -y patchelf

    # Debian/Ubuntu use different binary names.
    setup_debian_symlinks
}

install_dnf() {
    info "Installing core packages..."
    sudo dnf install -y zsh git curl

    info "Installing CLI tools..."
    sudo dnf install -y fzf ripgrep bat jq ShellCheck ccache direnv

    info "Installing recommended packages..."
    sudo dnf install -y fd-find eza zsh-autosuggestions 2>/dev/null || \
        warn "Some optional packages not available"
}

install_pacman() {
    info "Installing packages..."
    sudo pacman -S --needed --noconfirm \
        zsh git curl fzf ripgrep jq shellcheck ccache direnv fd bat eza zsh-autosuggestions
}

install_brew() {
    local dotfiles_dir="${DOTFILES:-$HOME/.dotfiles}"
    local brewfile="$dotfiles_dir/Brewfile"

    # Use Brewfile if it exists (preferred).
    if [ -f "$brewfile" ]; then
        info "Installing packages from Brewfile..."
        brew bundle --file="$brewfile"
    else
        # Fallback to individual installs.
        info "Installing core packages..."
        brew install zsh git curl

        info "Installing CLI tools..."
        brew install fzf ripgrep bat jq shellcheck direnv ccache

        info "Installing recommended packages..."
        brew install fd eza zsh-autosuggestions zsh-syntax-highlighting
    fi

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
        ln -sf "$(which fdfind)" "$HOME/.local/bin/fd"
    fi

    # batcat -> bat.
    if command -v batcat &>/dev/null && ! command -v bat &>/dev/null; then
        info "Creating bat symlink (batcat -> bat)..."
        ln -sf "$(which batcat)" "$HOME/.local/bin/bat"
    fi
}

# ============================================================================
# Install Powerlevel10k
# ============================================================================
install_p10k() {
    P10K_DIR="$HOME/.local/p10k"

    if [ -d "$P10K_DIR" ]; then
        info "Powerlevel10k already installed at $P10K_DIR"
        info "Updating Powerlevel10k..."
        git -C "$P10K_DIR" pull --quiet
    else
        info "Installing Powerlevel10k..."
        git clone --depth=1 https://github.com/romkatv/powerlevel10k "$P10K_DIR"
    fi
}

# ============================================================================
# Install Nerd Fonts (MesloLGS NF for p10k)
# ============================================================================
install_fonts() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        FONT_DIR="$HOME/Library/Fonts"
    else
        FONT_DIR="$HOME/.local/share/fonts"
    fi

    mkdir -p "$FONT_DIR"

    # Check if already installed.
    if ls "$FONT_DIR"/MesloLGS*.ttf &>/dev/null; then
        info "MesloLGS NF fonts already installed"
        return
    fi

    info "Installing MesloLGS NF fonts for Powerlevel10k..."
    local base_url="https://github.com/romkatv/powerlevel10k-media/raw/master"
    for font in "MesloLGS%20NF%20Regular.ttf" "MesloLGS%20NF%20Bold.ttf" \
                "MesloLGS%20NF%20Italic.ttf" "MesloLGS%20NF%20Bold%20Italic.ttf"; do
        curl -fsSL "$base_url/$font" -o "$FONT_DIR/${font//%20/ }"
    done

    # Refresh font cache (Linux only).
    if [[ "$OSTYPE" != "darwin"* ]] && command -v fc-cache &>/dev/null; then
        fc-cache -f "$FONT_DIR"
    fi

    info "Fonts installed! Set your terminal font to 'MesloLGS NF'"
}

# ============================================================================
# Install Oh My Zsh
# ============================================================================
install_omz() {
    OMZ_DIR="$HOME/.oh-my-zsh"

    if [ -d "$OMZ_DIR" ]; then
        info "Oh My Zsh already installed at $OMZ_DIR"
        info "Updating Oh My Zsh..."
        git -C "$OMZ_DIR" pull --quiet 2>/dev/null || true
    else
        info "Installing Oh My Zsh..."
        # Use --unattended to avoid interactive prompts and shell switching.
        sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" "" --unattended --keep-zshrc
    fi
}

# ============================================================================
# Install tmux, byobu, and TPM
# ============================================================================
install_tmux_byobu() {
    # Install tmux via package manager.
    if ! command -v tmux &>/dev/null; then
        info "Installing tmux..."
        case "$PKG_MGR" in
            apt)    sudo apt install -y tmux ;;
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
            apt)    sudo apt install -y byobu ;;
            dnf)    sudo dnf install -y byobu ;;
            pacman) sudo pacman -S --needed --noconfirm byobu ;;
            brew)   brew install byobu ;;
        esac
    else
        info "byobu already installed"
    fi

    # Install TPM (Tmux Plugin Manager).
    TPM_DIR="$HOME/.tmux/plugins/tpm"
    if [ -d "$TPM_DIR" ]; then
        info "TPM already installed at $TPM_DIR"
        info "Updating TPM..."
        git -C "$TPM_DIR" pull --quiet
    else
        info "Installing TPM (Tmux Plugin Manager)..."
        git clone --depth=1 https://github.com/tmux-plugins/tpm "$TPM_DIR"
    fi
}

# ============================================================================
# Install NVM and Node.js
# ============================================================================
install_nvm() {
    NVM_DIR="$HOME/.nvm"

    if [ -d "$NVM_DIR" ]; then
        info "nvm already installed at $NVM_DIR"
        info "Updating nvm..."
        git -C "$NVM_DIR" fetch --tags origin
        git -C "$NVM_DIR" checkout "$(git -C "$NVM_DIR" describe --tags --abbrev=0)"
    else
        info "Installing nvm..."
        git clone https://github.com/nvm-sh/nvm.git "$NVM_DIR"
        git -C "$NVM_DIR" checkout "$(git -C "$NVM_DIR" describe --tags --abbrev=0)"
    fi

    # Load nvm for this session.
    export NVM_DIR
    # shellcheck disable=SC1091
    . "$NVM_DIR/nvm.sh"

    # Install Node.js LTS if not present.
    if ! command -v node &>/dev/null; then
        info "Installing Node.js 24 (LTS)..."
        nvm install 24
        nvm alias default 24
    else
        info "Node.js already installed: $(node --version)"
    fi
}

# ============================================================================
# Main
# ============================================================================
main() {
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
    if [ "$SHELL" != "$(which zsh)" ]; then
        warn "Your default shell is not zsh."
        warn "To change: chsh -s \$(which zsh)"
        echo ""
    fi
}

main "$@"
