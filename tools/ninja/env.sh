# Ninja environment.
# Sourced by tools.sh and direnvrc after NINJA_ROOT is selected.
if [ -n "${NINJA_ROOT:-}" ]; then
    if [ ! -x "$NINJA_ROOT/bin/ninja" ]; then
        printf 'Ninja root is incomplete: %s\n' "$NINJA_ROOT" >&2
        return 1
    fi
    _replace_managed_path_entry \
        PATH "$NINJA_ROOT/bin" DOTFILES_NINJA_PATH_ENTRY || return 1
fi
