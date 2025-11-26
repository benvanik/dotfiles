# ~/.zshrc.d/keybindings.zsh - Custom keybindings for zsh
#
# VSCode/Windows-compatible keybindings for comfortable terminal use.
# These match common keyboard shortcuts from GUI editors and Windows terminals.

# Use Emacs-style keybindings as base (Ctrl-A, Ctrl-E, etc.).
bindkey -e

# ============================================================================
# Navigation Keys
# ============================================================================

# Home / End keys.
bindkey '\e[H'  beginning-of-line    # Home
bindkey '\e[F'  end-of-line          # End

# Some terminals send different sequences for Home/End.
bindkey '\e[1~' beginning-of-line    # Home (alternative)
bindkey '\e[4~' end-of-line          # End (alternative)

# Delete key.
bindkey '\e[3~' delete-char          # Delete

# ============================================================================
# Word Navigation (Ctrl+Arrow Keys)
# ============================================================================

# Ctrl+Left / Ctrl+Right - jump between words.
bindkey '\e[1;5D' backward-word      # Ctrl+Left (most terminals)
bindkey '\e[1;5C' forward-word       # Ctrl+Right (most terminals)

# Some terminals use these sequences instead.
bindkey '\e[5D' backward-word        # Ctrl+Left (alternative)
bindkey '\e[5C' forward-word         # Ctrl+Right (alternative)

# ============================================================================
# Word Deletion
# ============================================================================

# Ctrl+Backspace - delete word backwards.
bindkey '^H'    backward-kill-word   # Ctrl+Backspace

# Ctrl+Delete - delete word forwards.
bindkey '\e[3;5~' kill-word          # Ctrl+Delete

# Alt+Backspace - delete word backwards (common on Linux).
bindkey '^[^?' backward-kill-word    # Alt+Backspace

# ============================================================================
# Line Editing
# ============================================================================

# Ctrl+J - insert literal newline (for multi-line commands).
# By default, Ctrl+J executes the command (like Enter).
# This override lets you write multi-line commands in the shell.
insert-newline() {
    LBUFFER+=$'\n'
    zle -R
}
zle -N insert-newline
bindkey -r '^J' 2>/dev/null          # remove default binding
bindkey '^J' insert-newline          # insert newline without executing

# ============================================================================
# Custom Keybindings
# ============================================================================
# Add your personal keybindings below this line.
# Examples:
#   bindkey '^P' up-line-or-history     # Ctrl+P: previous command
#   bindkey '^N' down-line-or-history   # Ctrl+N: next command
#   bindkey '^F' forward-char           # Ctrl+F: move forward one char

# End of ~/.zshrc.d/keybindings.zsh
