# shellcheck shell=bash
# mold linker environment. This file is sourced only by explicit use_mold.
if [ -n "${MOLD_ROOT:-}" ]; then
    if [ ! -x "$MOLD_ROOT/bin/mold" ]; then
        printf 'mold root is incomplete: %s\n' "$MOLD_ROOT" >&2
        return 1
    fi
    _replace_managed_path_entry \
        PATH "$MOLD_ROOT/bin" DOTFILES_MOLD_PATH_ENTRY || return 1
    case " ${LDFLAGS:-} " in
        *" -fuse-ld=mold "*) ;;
        *) export LDFLAGS="-fuse-ld=mold${LDFLAGS:+ $LDFLAGS}" ;;
    esac
fi
