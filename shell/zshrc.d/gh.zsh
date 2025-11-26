# ~/.zshrc.d/gh.zsh - GitHub CLI (gh) completions
#
# Generates and caches zsh completions for the gh command.
# Completions are regenerated in the background on each shell start.
#
# Source: oh-my-zsh/plugins/gh (adapted for modular zsh setup)

# Skip if gh is not installed.
if (( ! $+commands[gh] )); then
    return
fi

# Use standard zsh cache location.
: ${XDG_CACHE_HOME:=$HOME/.cache}
local _gh_completion_file="$XDG_CACHE_HOME/zsh/completions/_gh"

# Ensure completions directory exists.
[[ -d "${_gh_completion_file:h}" ]] || mkdir -p "${_gh_completion_file:h}"

# If the completion file doesn't exist yet, we need to autoload it and
# bind it to `gh`. Otherwise, compinit will have already done that.
if [[ ! -f "$_gh_completion_file" ]]; then
    typeset -g -A _comps
    autoload -Uz _gh
    _comps[gh]=_gh
fi

# Regenerate completions in the background (handles gh version upgrades).
gh completion --shell zsh >| "$_gh_completion_file" &|

# End of ~/.zshrc.d/gh.zsh
