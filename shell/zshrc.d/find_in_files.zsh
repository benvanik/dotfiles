# ~/.zshrc.d/find_in_files.zsh - Interactive find-in-files using ripgrep + fzf
#
# Provides:
#   - Ctrl-G: Interactive find-in-files using ripgrep + fzf
#
# Requirements: fzf, rg (ripgrep)
#
# Usage examples:
#   nano <Ctrl-G>     → Search and insert file:line, nano opens at that line with: nano +LINE file
#   code <Ctrl-G>     → Search and insert file:line, VSCode opens at that line with: code -g file:line

# ============================================================================
# Interactive Ripgrep Find-in-Files (Ctrl-G)
# ============================================================================
# Search file contents interactively with rg + fzf.
# Usage: Press Ctrl-G, type search pattern, browse results, press Enter to
# insert file:line into command line for easy editing with vim/nvim.

fzf-rg-widget() {
    # Check if rg is available.
    if ! command -v rg >/dev/null 2>&1; then
        echo "ripgrep (rg) not found. Install with: apt install ripgrep"
        zle reset-prompt
        return 1
    fi

    # Initial query from any text already on command line.
    local initial_query="${LBUFFER}"

    # Run rg with fzf for interactive search.
    # Format: filename:line:column:content
    local selected=$(
        rg --color=always --line-number --no-heading --smart-case "${initial_query:-.*}" 2>/dev/null |
        fzf --ansi \
            --disabled \
            --bind "change:reload:rg --color=always --line-number --no-heading --smart-case {q} || true" \
            --bind "enter:become(echo {1}:{2})" \
            --delimiter : \
            --preview 'bat --color=always --style=numbers --highlight-line {2} {1} 2>/dev/null || cat {1} 2>/dev/null | head -500' \
            --preview-window 'up,60%,border-bottom,+{2}+3/3,~3' \
            --prompt='rg> ' \
            --header='Ctrl-G: Find in files | Enter: Insert file:line into prompt'
    )

    # Insert the file:line result into the command line.
    if [[ -n "$selected" ]]; then
        # Extract just filename:line for easy editing/viewing.
        local file_line="${selected%%:*}:${selected#*:}"
        file_line="${file_line%%:*}:${file_line##*:}"

        # Append to current line with a space separator (don't replace).
        if [[ -n "$LBUFFER" ]]; then
            LBUFFER="${LBUFFER} ${file_line}"
        else
            LBUFFER="${file_line}"
        fi
        RBUFFER=""
    fi

    zle reset-prompt
}

# Register the widget and bind to Ctrl-G.
zle -N fzf-rg-widget
bindkey '^G' fzf-rg-widget

# ============================================================================
# Helper Functions for Opening Files at Specific Lines
# ============================================================================

# Wrapper for nano to handle file:line syntax.
# Usage: n file:line  OR  n file
n() {
    if [[ $1 =~ ^(.+):([0-9]+)$ ]]; then
        nano "+${BASH_REMATCH[2]}" "${BASH_REMATCH[1]}"
    else
        nano "$@"
    fi
}

# Wrapper for VSCode Remote to handle file:line syntax.
# Usage: c file:line  OR  c file
c() {
    if command -v code >/dev/null 2>&1; then
        # VSCode supports -g for goto: code -g file:line:column
        code -g "$@"
    else
        echo "VSCode 'code' command not found. Make sure VSCode Remote SSH is connected."
        return 1
    fi
}

# End of ~/.zshrc.d/find_in_files.zsh
