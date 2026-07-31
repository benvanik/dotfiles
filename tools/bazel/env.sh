# Bazel tools environment.
# Sourced by tools.sh and direnvrc after BAZEL_ROOT is selected.
if [ -n "${BAZEL_ROOT:-}" ]; then
    if [ ! -x "$BAZEL_ROOT/bin/bazel" ]; then
        printf 'Bazel root is incomplete: %s\n' "$BAZEL_ROOT" >&2
        return 1
    fi
    _replace_managed_path_entry \
        PATH "$BAZEL_ROOT/bin" DOTFILES_BAZEL_PATH_ENTRY || return 1
fi
