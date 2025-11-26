# ~/.zshrc.d/fzf.zsh - fzf fuzzy finder integration for zsh
#
# Provides:
#   - Ctrl-R: Search command history
#   - Ctrl-T: Find files in current directory
#   - Alt-C:  Change directory (fuzzy search)
#   - **<Tab>: Trigger completion for paths, hostnames, processes, etc.
#
# For more info: https://github.com/junegunn/fzf

# Check if fzf is available.
if ! command -v fzf >/dev/null 2>&1; then
    return
fi

# Modern fzf (0.48.0+) supports --zsh flag to generate all integration code.
# This provides keybindings and completion without needing separate script files.
if fzf --zsh >/dev/null 2>&1; then
    eval "$(fzf --zsh)"
else
    # Fallback for older fzf versions: try to source integration files manually.
    # (Unlikely to be needed with your fzf 0.62.0, but kept for completeness.)
    for f in \
        /usr/share/doc/fzf/examples/key-bindings.zsh \
        /usr/share/fzf/key-bindings.zsh \
        ~/.fzf/shell/key-bindings.zsh
    do
        [[ -r $f ]] && source "$f" && break
    done

    for f in \
        /usr/share/doc/fzf/examples/completion.zsh \
        /usr/share/fzf/completion.zsh \
        ~/.fzf/shell/completion.zsh
    do
        [[ -r $f ]] && source "$f" && break
    done
fi

# ============================================================================
# fzf Configuration
# ============================================================================

# Default options for fzf appearance and behavior.
export FZF_DEFAULT_OPTS='
  --height=40%
  --layout=reverse
  --border
  --info=inline
  --prompt="❯ "
  --pointer="▶"
  --marker="✓"
  --color=fg:#d0d0d0,bg:#121212,hl:#5f87af
  --color=fg+:#d0d0d0,bg+:#262626,hl+:#5fd7ff
  --color=info:#afaf87,prompt:#d7005f,pointer:#af5fff
  --color=marker:#87ff00,spinner:#af5fff,header:#87afaf
'

# Helper script to delete history entry.
# This needs to be a separate script because we can't use zsh builtins in fzf's execute.
_fzf_delete_history_entry_script() {
    cat > /tmp/fzf-delete-history.sh << 'SCRIPT'
#!/bin/sh
# Extract the command text (remove leading spaces and line number).
entry=$(echo "$1" | sed 's/^ *[0-9]\{1,\} \{1,\}//')

histfile="${HISTFILE:-$HOME/.zsh_history}"

# Create backup.
cp "$histfile" "$histfile.bak" 2>/dev/null

# Delete lines matching this command.
# Handles both formats:
#   Extended: ": timestamp:duration;command"
#   Simple: "command"
awk -v cmd="$entry" '
{
    line = $0
    # Check if extended format (starts with ": ").
    if (index($0, ": ") == 1 && index($0, ";") > 0) {
        # Extended format - extract command after semicolon.
        cmd_part = substr($0, index($0, ";") + 1)
        if (cmd_part != cmd) print line
    } else {
        # Simple format - match entire line.
        if ($0 != cmd) print line
    }
}
' "$histfile" > "$histfile.tmp"

# Replace if successful.
if [ -s "$histfile.tmp" ]; then
    mv "$histfile.tmp" "$histfile"
else
    rm -f "$histfile.tmp"
fi
SCRIPT
    chmod +x /tmp/fzf-delete-history.sh
}

# Create the helper script on first load.
_fzf_delete_history_entry_script

# Ctrl-R history search options with delete keybinding.
# Press Ctrl-D to delete from file (permanent).
# Note: Deleted entries remain visible in current shell's Ctrl-R until shell restart,
# but new shells won't see them. This is the most reliable approach with SHARE_HISTORY.
export FZF_CTRL_R_OPTS="
  --header='Ctrl-R: History | Ctrl-D: Delete (takes effect in new shells)'
  --bind 'ctrl-d:execute-silent(/tmp/fzf-delete-history.sh {})+abort'
"

# Use fd for file search if available (faster than find, respects .gitignore).
if command -v fd >/dev/null 2>&1; then
    export FZF_DEFAULT_COMMAND='fd --type f --hidden --follow --exclude .git'
    export FZF_CTRL_T_COMMAND="$FZF_DEFAULT_COMMAND"
fi

# Use bat for file preview if available (syntax highlighting).
if command -v bat >/dev/null 2>&1; then
    export FZF_CTRL_T_OPTS="--preview 'bat --color=always --style=numbers --line-range=:500 {}'"
fi

# Use tree for directory preview if available.
if command -v tree >/dev/null 2>&1; then
    export FZF_ALT_C_OPTS="--preview 'tree -C {} | head -200'"
fi

# End of ~/.zshrc.d/fzf.zsh
