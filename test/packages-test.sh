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
[ "$(_pkg_resolve_name brew python3)" = "python" ] || \
    fail "brew python mapping"
[ "$(_pkg_resolve_name pacman python3)" = "python" ] || \
    fail "pacman python mapping"
[ "$(_pkg_resolve_name brew jq)" = "jq" ] || fail "default package mapping"
[ "$(_pkg_resolve_bin fd apt)" = "fdfind" ] || fail "apt fd binary mapping"
[ "$(_pkg_resolve_bin bat apt)" = "batcat" ] || fail "apt bat binary mapping"
[ "$(_pkg_resolve_bin fd brew)" = "fd" ] || fail "default binary mapping"

# Detection names the noninteractive transaction executable, not apt's
# interactive command-line frontend.
(
    PACKAGE_MANAGER_AVAILABLE="apt-get"
    OSTYPE="linux-fixture"
    # shellcheck disable=SC2317  # Invoked by _pkg_detect_pm.
    command() {
        if [ "${1:-}" = "-v" ]; then
            [ "${2:-}" = "$PACKAGE_MANAGER_AVAILABLE" ]
            return
        fi
        builtin command "$@"
    }
    [ "$(_pkg_detect_pm)" = "apt" ] ||
        fail "apt-get-only host was not detected as apt"
    PACKAGE_MANAGER_AVAILABLE="apt"
    if _pkg_detect_pm >/dev/null; then
        fail "interactive apt frontend selected the apt transaction path"
    fi
)

if _pkg_get_install_list unknown all >/dev/null 2>&1; then
    fail "package resolver accepted an unknown package manager"
fi
if _pkg_get_install_list apt unknown >/dev/null 2>&1; then
    fail "package resolver accepted an unknown package category"
fi

case " $(_pkg_get_install_list apt all) " in
    *" ccache "*) fail "ccache remains in the package install list" ;;
esac

[ "$(_pkg_get_install_list apt build)" = \
    "autoconf automake bison build-essential coreutils pkg-config tar libevent-dev libncurses-dev" ] ||
    fail "apt multiplexer build plan"
[ "$(_pkg_get_install_list dnf build)" = \
    "autoconf automake bison gcc make coreutils pkgconf-pkg-config tar libevent-devel ncurses-devel" ] ||
    fail "dnf multiplexer build plan"
[ "$(_pkg_get_install_list pacman build)" = \
    "base-devel coreutils tar libevent ncurses" ] ||
    fail "pacman multiplexer build plan"
[ -z "$(_pkg_get_install_list brew build)" ] ||
    fail "brew received a Linux-only multiplexer build plan"

# Every manager plan must retain the required Python runtime and omit retired
# cache tooling.
for package_manager in apt dnf pacman brew; do
    package_plan=" $(_pkg_get_install_list "$package_manager" all) "
    python_package=$(_pkg_resolve_name "$package_manager" python3)
    case "$package_plan" in
        *" $python_package "*) ;;
        *) fail "$package_manager plan omitted Python" ;;
    esac
    case "$package_plan" in
        *" ccache "*) fail "$package_manager plan retained ccache" ;;
    esac
done

# The updater and bootstrap verifier consume one exact external-interface
# contract rather than drifting copies of command and library requirements.
# shellcheck disable=SC2016  # Match the literal production source expression.
grep -qF 'source "$DOTFILES/lib/packages.sh"' \
    "$DOTFILES/bin/update-multiplexer" ||
    fail "multiplexer updater does not load the package contract"
# shellcheck disable=SC2016  # Match the literal production array expansion.
grep -qF '"${MULTIPLEXER_BUILD_COMMANDS[@]}"' \
    "$DOTFILES/bin/update-multiplexer" ||
    fail "multiplexer updater duplicated its required command plan"
# shellcheck disable=SC2016  # Match the literal production array expansion.
grep -qF '"${MULTIPLEXER_BUILD_PKG_CONFIG_MODULES[@]}"' \
    "$DOTFILES/bin/update-multiplexer" ||
    fail "multiplexer updater duplicated its required module plan"

# The manual Brew bundle must cover the same canonical core plan as the
# installer. Additional macOS conveniences are allowed.
BREWFILE_PACKAGES=$(
    sed -n 's/^brew "\([^"]*\)".*/\1/p' "$DOTFILES/Brewfile"
)
for package in $(_pkg_get_install_list brew all); do
    printf '%s\n' "$BREWFILE_PACKAGES" | grep -qxF "$package" ||
        fail "Brewfile omitted $package"
done
printf '%s\n' "$BREWFILE_PACKAGES" | grep -qxF "ccache" &&
    fail "Brewfile retained ccache"

# Actual installation paths must consume the canonical resolver instead of
# carrying another hard-coded copy of each manager's core list.
for package_manager in apt dnf pacman brew; do
    grep -qF "_pkg_get_install_list $package_manager required" \
        "$DOTFILES/install-deps.sh" ||
        fail "install-deps does not use the $package_manager required plan"
    grep -qF "_pkg_get_install_list $package_manager recommended" \
        "$DOTFILES/install-deps.sh" ||
        fail "install-deps does not use the $package_manager recommended plan"
done
for package_manager in apt dnf pacman; do
    grep -qF "_pkg_get_install_list $package_manager build" \
        "$DOTFILES/install-deps.sh" ||
        fail "install-deps does not use the $package_manager build plan"
done

echo "package resolution passed"
