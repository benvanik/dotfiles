# Per-directory shell history using Oh My Zsh plugin.
#
# This plugin saves command history separately for each directory, allowing
# project-specific history that doesn't pollute other projects.
#
# Features:
#   - Ctrl-G toggles between per-directory and global history
#   - Commands are saved to BOTH histories (never lose commands)
#   - HISTORY_BASE can be set via direnv for project-wide history
#
# Configuration via direnv (.envrc):
#   use_project_history          # Creates .history/ in current dir
#   use_project_history "../"    # Share history across worktrees
#
# See: https://github.com/ohmyzsh/ohmyzsh/tree/master/plugins/per-directory-history

# Only load if Oh My Zsh is installed.
_omz_pdh_plugin="$HOME/.oh-my-zsh/plugins/per-directory-history/per-directory-history.plugin.zsh"

if [[ -f "$_omz_pdh_plugin" ]]; then
    # Set default history base if not already set by direnv.
    : ${HISTORY_BASE:=$HOME/.directory_history}

    # Ensure the directory exists.
    [[ -d "$HISTORY_BASE" ]] || mkdir -p "$HISTORY_BASE"

    # Source the plugin.
    source "$_omz_pdh_plugin"
fi

unset _omz_pdh_plugin
