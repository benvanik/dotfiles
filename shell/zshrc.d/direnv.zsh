# Optimized direnv hook for zsh.
# This overrides the bash hook in shrc with the native zsh hook.
(( $+commands[direnv] )) && eval "$(direnv hook zsh)"
