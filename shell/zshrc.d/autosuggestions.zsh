# ~/.zshrc.d/autosuggestions.zsh - Fish-style command suggestions
#
# As you type, suggests commands from your history in gray text.
# Press Right Arrow (→) to accept the suggestion.
# Press Ctrl+→ to accept one word at a time.
#
# Package: zsh-autosuggestions (apt install zsh-autosuggestions)

# Load zsh-autosuggestions if available.
if [[ -f /usr/share/zsh-autosuggestions/zsh-autosuggestions.zsh ]]; then
    source /usr/share/zsh-autosuggestions/zsh-autosuggestions.zsh
fi

# Configuration options.
# Suggestion color (dim gray - subtle but visible).
ZSH_AUTOSUGGEST_HIGHLIGHT_STYLE='fg=240'

# Suggestion strategy: history first, then completion.
ZSH_AUTOSUGGEST_STRATEGY=(history completion)

# Keybindings for accepting suggestions.
# Right Arrow - accept entire suggestion (default).
# Ctrl+Right Arrow - accept one word at a time (already works with forward-word).

# End of ~/.zshrc.d/autosuggestions.zsh
