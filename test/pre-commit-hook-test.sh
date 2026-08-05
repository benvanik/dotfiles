#!/bin/bash
# Integration coverage for pre-commit worktree and Git environment isolation.

set -euo pipefail

export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1

DOTFILES="${DOTFILES:-$HOME/.dotfiles}"
TEST_PARENT="${TMPDIR:-/tmp}"
TEST_ROOT=$(mktemp -d "$TEST_PARENT/dotfiles-pre-commit-hook-test.XXXXXX")
cleanup_test_root() {
    case "$TEST_ROOT" in
        "$TEST_PARENT"/dotfiles-pre-commit-hook-test.*) ;;
        *)
            echo "refusing unexpected test cleanup path: $TEST_ROOT" >&2
            return 1
            ;;
    esac
    [ ! -e "$TEST_ROOT" ] || find "$TEST_ROOT" -xdev -depth -delete
}
trap cleanup_test_root EXIT

fail() {
    echo "pre-commit hook test: $1" >&2
    exit 1
}

REPOSITORY="$TEST_ROOT/repository"
git init -q -b main "$REPOSITORY"
git -C "$REPOSITORY" config user.name "Pre-commit Hook Test"
git -C "$REPOSITORY" config user.email \
    "pre-commit-hook-test@example.invalid"
git -C "$REPOSITORY" config commit.gpgsign false
printf '%s\n' "initial contents" > "$REPOSITORY/README.md"
git -C "$REPOSITORY" add README.md
git -C "$REPOSITORY" commit -q -m "Initialize hook fixture"

mkdir -p "$REPOSITORY/bin"
# shellcheck disable=SC2016  # Expanded by the generated fixture command.
printf '%s\n' \
    '#!/bin/sh' \
    'set -eu' \
    '[ "$#" -eq 1 ] && [ "$1" = test ] || exit 20' \
    'expected_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)' \
    '[ "${DOTFILES-}" = "$expected_root" ] || exit 21' \
    'for variable in $(git rev-parse --local-env-vars); do' \
    '    if printenv "$variable" >/dev/null 2>&1; then' \
    '        echo "inherited Git environment variable: $variable" >&2' \
    '        exit 22' \
    '    fi' \
    'done' \
    ': > "$DOTFILES/hook-ran"' \
    > "$REPOSITORY/bin/dotfiles"
chmod 755 "$REPOSITORY/bin/dotfiles"
ln -s "$DOTFILES/git/hooks/pre-commit" \
    "$REPOSITORY/.git/hooks/pre-commit"

printf '%s\n' "committed contents" > "$REPOSITORY/README.md"
git -C "$REPOSITORY" add README.md bin/dotfiles
(
    cd "$REPOSITORY"
    export GIT_DIR="$REPOSITORY/.git"
    export GIT_WORK_TREE="$REPOSITORY"
    export GIT_INDEX_FILE="$REPOSITORY/.git/index"
    git commit -q -m "Exercise managed hook"
)

[ -f "$REPOSITORY/hook-ran" ] ||
    fail "managed hook did not validate through the active worktree"
[ "$(git -C "$REPOSITORY" show HEAD:README.md)" = "committed contents" ] ||
    fail "commit did not preserve the staged fixture"
git -C "$REPOSITORY" diff --quiet ||
    fail "hook changed the committed fixture"

echo "pre-commit hook test passed"
