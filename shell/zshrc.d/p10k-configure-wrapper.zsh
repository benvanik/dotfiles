# ~/.zshrc.d/p10k-configure-wrapper.zsh - Terminal-aware p10k configuration
#
# This wrapper ensures p10k configure updates the correct terminal-specific config.

# Wrapper function for p10k configure.
function p10k-configure() {
    # Detect which terminal we're in (same logic as p10k.zsh).
    local target_config=""
    local terminal_name=""

    # Check for VSCode integrated terminal.
    if [[ -n "${VSCODE_INJECTION:-}" ]] || [[ "${TERM_PROGRAM:-}" == "vscode" ]]; then
        target_config="$HOME/.p10k-vscode.zsh"
        terminal_name="VSCode"

    # Check for Tabby terminal.
    elif [[ -n "${TABBY_CONFIG_DIRECTORY:-}" ]] || [[ "${TERM_PROGRAM:-}" == "Tabby" ]]; then
        target_config="$HOME/.p10k-tabby.zsh"
        terminal_name="Tabby"

    # Check for GNOME Terminal (old).
    elif [[ -n "${GNOME_TERMINAL_SCREEN:-}" ]] || [[ -n "${GNOME_TERMINAL_SERVICE:-}" ]]; then
        target_config="$HOME/.p10k-gnome.zsh"
        terminal_name="GNOME Terminal"

    # Check for GNOME Console (kgx - new GNOME terminal).
    elif [[ "${TERM_PROGRAM:-}" == "kgx" ]]; then
        target_config="$HOME/.p10k-kgx.zsh"
        terminal_name="GNOME Console"

    # Check for Windows Terminal.
    elif [[ -n "${WT_SESSION:-}" ]] || [[ -n "${WT_PROFILE_ID:-}" ]]; then
        target_config="$HOME/.p10k-wt.zsh"
        terminal_name="Windows Terminal"

    # Fallback: check parent process name.
    else
        local parent_cmd
        if [[ -f /proc/$PPID/comm ]]; then
            parent_cmd=$(cat /proc/$PPID/comm 2>/dev/null)
            case "$parent_cmd" in
                Code|code)
                    target_config="$HOME/.p10k-vscode.zsh"
                    terminal_name="VSCode"
                    ;;
                tabby*)
                    target_config="$HOME/.p10k-tabby.zsh"
                    terminal_name="Tabby"
                    ;;
                WindowsTerminal.exe|wt.exe)
                    target_config="$HOME/.p10k-wt.zsh"
                    terminal_name="Windows Terminal"
                    ;;
            esac
        fi
    fi

    # If we detected a terminal, inform the user.
    if [[ -n "$target_config" ]]; then
        echo "Detected terminal: $terminal_name"
        echo "Will update: $target_config"
        echo ""
    else
        echo "Using default config: ~/.p10k.zsh"
        echo ""
    fi

    # Backup current config if it exists.
    if [[ -n "$target_config" ]] && [[ -f "$target_config" ]]; then
        local backup="${target_config}.backup-$(date +%Y%m%d-%H%M%S)"
        cp "$target_config" "$backup"
        echo "Backed up existing config to: $backup"
        echo ""
    fi

    # Run the actual p10k configure wizard.
    # Note: p10k configure may write to the current config file if it detects one.
    p10k configure

    # If p10k wrote to ~/.p10k.zsh and we want a different target, copy it.
    if [[ -n "$target_config" ]] && [[ "$target_config" != "$HOME/.p10k.zsh" ]]; then
        if [[ -f ~/.p10k.zsh ]] && [[ ~/.p10k.zsh -nt "$target_config" ]]; then
            # ~/.p10k.zsh is newer, so p10k wrote there. Copy to terminal-specific config.
            cp ~/.p10k.zsh "$target_config"
            echo ""
            echo "Configuration saved to: $target_config"
        elif [[ -f "$target_config" ]]; then
            # Target config was updated directly by p10k (it detected it).
            echo ""
            echo "Configuration saved to: $target_config"
        fi
        echo "Reload with: source ~/.zshrc"
    fi
}

# End of ~/.zshrc.d/p10k-configure-wrapper.zsh
