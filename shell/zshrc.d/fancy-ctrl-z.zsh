# ~/.zshrc.d/fancy-ctrl-z.zsh - Toggle background jobs with Ctrl+Z
#
# Behavior:
#   - Empty command line: pressing Ctrl+Z runs 'fg' to resume last job
#   - Non-empty command line: pushes current input and clears screen
#
# Source: oh-my-zsh/plugins/fancy-ctrl-z

fancy-ctrl-z() {
    if [[ $#BUFFER -eq 0 ]]; then
        BUFFER="fg"
        zle accept-line -w
    else
        zle push-input -w
        zle clear-screen -w
    fi
}
zle -N fancy-ctrl-z
bindkey '^Z' fancy-ctrl-z

# End of ~/.zshrc.d/fancy-ctrl-z.zsh
