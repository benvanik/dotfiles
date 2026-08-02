#!/bin/sh
# Establish a clean tool boundary before the target project's shell and direnv.

set -eu

if [ "$#" -ne 1 ]; then
    printf 'project shell: expected one shell executable\n' >&2
    exit 64
fi

project_shell="$1"
case "$project_shell" in
    /*) ;;
    *)
        printf 'project shell: shell path is not absolute: %s\n' \
            "$project_shell" >&2
        exit 64
        ;;
esac
if [ ! -x "$project_shell" ]; then
    printf 'project shell: shell is not executable: %s\n' "$project_shell" >&2
    exit 69
fi

# The target directory, not the source terminal or tmux server, owns direnv and
# project-local Python/history state.
unset \
    DIRENV_DIFF \
    DIRENV_DIR \
    DIRENV_FILE \
    DIRENV_IN_ENVRC \
    DIRENV_LAYOUT_DIR \
    DIRENV_WATCHES \
    HISTORY_BASE

reset_environment="$HOME/.dotfiles/tools/reset-environment.sh"
# shellcheck source=../tools/reset-environment.sh
if [ ! -f "$reset_environment" ] || ! . "$reset_environment"; then
    printf 'project shell: could not reset inherited tool environment\n' >&2
    exit 1
fi
unset reset_environment

export SHELL="$project_shell"
exec "$project_shell"
