# ~/.zshrc.d/p10k.zsh - Powerlevel10k theme configuration
#
# Powerlevel10k is a fast, customizable zsh theme with great-looking prompts.
# Homepage: https://github.com/romkatv/powerlevel10k
#
# To customize your prompt, run: p10k configure

# ============================================================================
# Powerlevel10k Instant Prompt
# ============================================================================
# Instant prompt is DISABLED for simplicity with terminal-specific configs.
#
# Why disabled:
# - Config changes take effect immediately with just: source ~/.zshrc
# - No cache management needed
# - No stale prompts shown
# - Startup delay is negligible (~50-200ms)
#
# To re-enable instant prompt, uncomment the lines below:
# if [[ -r "${XDG_CACHE_HOME:-$HOME/.cache}/p10k-instant-prompt-${(%):-%n}.zsh" ]]; then
#   source "${XDG_CACHE_HOME:-$HOME/.cache}/p10k-instant-prompt-${(%):-%n}.zsh"
# fi

# ============================================================================
# Powerlevel10k Theme
# ============================================================================
# Try to load the theme from common installation locations.

# Check if powerlevel10k is installed and source it.
if [[ -r ${ZSH_CUSTOM:-$HOME/.local}/p10k/powerlevel10k.zsh-theme ]]; then
    source ${ZSH_CUSTOM:-$HOME/.local}/p10k/powerlevel10k.zsh-theme
elif [[ -r /usr/share/powerlevel10k/powerlevel10k.zsh-theme ]]; then
    # System-wide installation (e.g., apt install zsh-powerlevel10k)
    source /usr/share/powerlevel10k/powerlevel10k.zsh-theme
fi

# ============================================================================
# Powerlevel10k User Configuration
# ============================================================================
# Load user customizations from terminal-specific p10k config files.
# This allows different icons, colors, and styles per terminal emulator.

# Detect terminal type and set config file path.
typeset -g P10K_CONFIG=""

# Check for VSCode integrated terminal.
if [[ -n "${VSCODE_INJECTION:-}" ]] || [[ "${TERM_PROGRAM:-}" == "vscode" ]]; then
    P10K_CONFIG="$HOME/.p10k-vscode.zsh"

# Check for Tabby terminal.
elif [[ -n "${TABBY_CONFIG_DIRECTORY:-}" ]] || [[ "${TERM_PROGRAM:-}" == "Tabby" ]]; then
    P10K_CONFIG="$HOME/.p10k-tabby.zsh"

# Check for GNOME Terminal (old).
elif [[ -n "${GNOME_TERMINAL_SCREEN:-}" ]] || [[ -n "${GNOME_TERMINAL_SERVICE:-}" ]]; then
    P10K_CONFIG="$HOME/.p10k-gnome.zsh"

# Check for GNOME Console (kgx - new GNOME terminal).
elif [[ "${TERM_PROGRAM:-}" == "kgx" ]]; then
    P10K_CONFIG="$HOME/.p10k-kgx.zsh"

# Check for Windows Terminal.
elif [[ -n "${WT_SESSION:-}" ]] || [[ -n "${WT_PROFILE_ID:-}" ]]; then
    P10K_CONFIG="$HOME/.p10k-wt.zsh"

# Fallback: check parent process name for common terminals.
else
    # Get parent process name (works on Linux).
    local parent_cmd
    if [[ -f /proc/$PPID/comm ]]; then
        parent_cmd=$(cat /proc/$PPID/comm 2>/dev/null)
        case "$parent_cmd" in
            Code|code)
                P10K_CONFIG="$HOME/.p10k-vscode.zsh"
                ;;
            tabby*)
                P10K_CONFIG="$HOME/.p10k-tabby.zsh"
                ;;
            WindowsTerminal.exe|wt.exe)
                P10K_CONFIG="$HOME/.p10k-wt.zsh"
                ;;
        esac
    fi
fi

# Load terminal-specific config, or fall back to default.
if [[ -n "$P10K_CONFIG" ]] && [[ -f "$P10K_CONFIG" ]]; then
    source "$P10K_CONFIG"
elif [[ -f ~/.p10k.zsh ]]; then
    # Fallback to default config.
    source ~/.p10k.zsh
fi

# End of ~/.zshrc.d/p10k.zsh
