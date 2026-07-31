# Optimized direnv hook for zsh.
# The common shrc deliberately installs no shell-specific hook.
(( $+commands[direnv] )) && eval "$(direnv hook zsh)"
