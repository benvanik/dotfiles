# NVM (Node Version Manager) integration.
# Loads nvm and sets up completions for zsh.

export NVM_DIR="$HOME/.nvm"

# Load nvm if installed.
if [[ -s "$NVM_DIR/nvm.sh" ]]; then
    source "$NVM_DIR/nvm.sh"
fi
