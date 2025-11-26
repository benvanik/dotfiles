# ~/.zshrc.d/completion.zsh - Enhanced tab completion configuration
#
# Provides:
#   - Enhanced completion behavior (case-insensitive, menu selection, etc.)
#   - Smart flag completion for binaries (parses --help output)
#   - Path completion enhancements
#
# Works with zsh's built-in completion system initialized in ~/.zshrc.

# ============================================================================
# Enhanced Completion Settings
# ============================================================================
# Make zsh completion more powerful and flexible.

# Enable completion in the middle of a line.
setopt COMPLETE_IN_WORD

# Move cursor to end after completion.
setopt ALWAYS_TO_END

# Automatically list choices on ambiguous completion.
setopt AUTO_LIST

# Automatically use menu completion after second Tab press.
setopt AUTO_MENU

# Show completion menu on successive tab press.
unsetopt MENU_COMPLETE

# Don't beep on ambiguous completions.
unsetopt LIST_BEEP

# Completion menu configuration.
zstyle ':completion:*' menu select
zstyle ':completion:*' use-cache on
zstyle ':completion:*' cache-path "$XDG_CACHE_HOME/zsh/completion-cache"

# Case-insensitive completion (smart: case-sensitive if uppercase present).
zstyle ':completion:*' matcher-list 'm:{a-zA-Z}={A-Za-z}' 'r:|=*' 'l:|=* r:|=*'

# Colorful completion listings.
zstyle ':completion:*' list-colors "${(s.:.)LS_COLORS}"

# Group completions by category.
zstyle ':completion:*' group-name ''
zstyle ':completion:*:descriptions' format '%F{yellow}-- %d --%f'
zstyle ':completion:*:messages' format '%F{purple}-- %d --%f'
zstyle ':completion:*:warnings' format '%F{red}-- no matches found --%f'

# ============================================================================
# Smart Flag Completion for Binaries
# ============================================================================
# Parse --help output to provide tab completion for binary flags.
# Works for any binary that supports --help.

# Function to extract flags from --help output.
_generic_flags_completion() {
    local -a flags
    local binary=$words[1]

    # Skip if binary doesn't exist or isn't executable.
    [[ -x "$binary" ]] || return 1

    # Try to get help output (no head limit - some tools have thousands of flags).
    local help_output
    help_output=$("$binary" --help 2>&1)
    [[ -z "$help_output" ]] && return 1

    # Extract flags more robustly.
    # Matches: -f, --flag, --flag=VALUE, --flag[=VALUE]
    # This handles LLVM-style flags like --mlir-print-ir-after-all
    if command -v rg >/dev/null 2>&1; then
        # Use ripgrep if available (faster).
        flags=(${(f)"$(echo "$help_output" | rg -o '(?:^|\s)(-[a-zA-Z0-9]|--[a-zA-Z0-9][a-zA-Z0-9_-]*)' -r '$1' | sort -u)"})
    else
        # Fallback to grep.
        flags=(${(f)"$(echo "$help_output" | grep -oE '(^|[[:space:]])--?[a-zA-Z0-9][a-zA-Z0-9_-]*' | sed 's/^[[:space:]]*//' | sort -u)"})
    fi

    [[ ${#flags} -eq 0 ]] && return 1

    # Offer completions.
    _describe 'flags' flags
}

# Enable generic flag completion for common patterns.
# This will fallback to flag parsing when no specific completion exists.
# Add your custom binaries here:
compdef _generic_flags_completion iree-opt
compdef _generic_flags_completion iree-compile
compdef _generic_flags_completion iree-run-module

# Note: For system commands, zsh already has good completions.
# This is mainly for custom/project binaries in your build directories.
#
# Performance note: The first tab completion will parse --help output, which
# may take a moment for tools with thousands of flags (like IREE tools).
# Subsequent completions in the same shell session are cached by zsh.

# ============================================================================
# Path Completion Enhancement
# ============================================================================
# Allow .. to expand to parent directories easily.

# Enable .. expansion: type .. and it becomes ../, ... becomes ../../, etc.
alias -g ...='../..'
alias -g ....='../../..'
alias -g .....='../../../..'

# Enable automatic directory expansion (just type directory name to cd).
setopt AUTO_CD

# Push directory onto stack when cd-ing (use "dirs -v" to see stack).
setopt AUTO_PUSHD
setopt PUSHD_IGNORE_DUPS
setopt PUSHD_SILENT

# End of ~/.zshrc.d/completion.zsh
