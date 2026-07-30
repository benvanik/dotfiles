#!/bin/bash
# Behavioral coverage for portable package-name resolution.

set -euo pipefail

DOTFILES="${DOTFILES:-$HOME/.dotfiles}"

fail() {
    echo "packages test: $1" >&2
    exit 1
}

# shellcheck source=../lib/packages.sh
. "$DOTFILES/lib/packages.sh"

[ "$(_pkg_resolve_name apt rg)" = "ripgrep" ] || fail "apt rg mapping"
[ "$(_pkg_resolve_name apt fd)" = "fd-find" ] || fail "apt fd mapping"
[ "$(_pkg_resolve_name dnf shellcheck)" = "ShellCheck" ] || \
    fail "dnf shellcheck mapping"
[ "$(_pkg_resolve_name brew rg)" = "ripgrep" ] || fail "brew rg mapping"
[ "$(_pkg_resolve_name brew jq)" = "jq" ] || fail "default package mapping"
[ "$(_pkg_resolve_bin fd apt)" = "fdfind" ] || fail "apt fd binary mapping"
[ "$(_pkg_resolve_bin bat apt)" = "batcat" ] || fail "apt bat binary mapping"
[ "$(_pkg_resolve_bin fd brew)" = "fd" ] || fail "default binary mapping"

case " $(_pkg_get_install_list apt all) " in
    *" ccache "*) fail "ccache remains in the package install list" ;;
esac

echo "package resolution passed"
