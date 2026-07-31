#!/bin/bash
# Atomic, create-once publication of machine-local HOME configuration.
set -euo pipefail

DOTFILES="${DOTFILES:-$HOME/.dotfiles}"
TEST_PARENT="${TMPDIR:-/tmp}"
TEST_ROOT=$(mktemp -d "$TEST_PARENT/dotfiles-local-config-test.XXXXXX")

cleanup_test_root() {
    case "$TEST_ROOT" in
        "$TEST_PARENT"/dotfiles-local-config-test.*) ;;
        *)
            printf 'refusing unexpected test cleanup path: %s\n' \
                "$TEST_ROOT" >&2
            return 1
            ;;
    esac
    [ ! -e "$TEST_ROOT" ] ||
        find "$TEST_ROOT" -xdev -depth -delete
}
trap cleanup_test_root EXIT

fail() {
    printf 'local config publication test: %s\n' "$1" >&2
    exit 1
}

file_mode() {
    python3 -c '
import os
import stat
import sys
print(format(stat.S_IMODE(os.lstat(sys.argv[1]).st_mode), "o"))
' "$1"
}

# Source the command once to exercise its real Bash 3.2-compatible functions.
# "help" prevents an install while leaving the function definitions available.
BOOTSTRAP_HOME="$TEST_ROOT/bootstrap-home"
mkdir -p "$BOOTSTRAP_HOME"
HOME="$BOOTSTRAP_HOME"
export HOME DOTFILES
set -- help
# shellcheck source=/dev/null
. "$DOTFILES/bin/dotfiles" >/dev/null

IDENTITY_HOME="$TEST_ROOT/identity-home"
mkdir -p "$IDENTITY_HOME"
HOME="$IDENTITY_HOME" _ensure_shrc_local >/dev/null
if [ ! -f "$IDENTITY_HOME/.shrc.local" ] ||
        [ -L "$IDENTITY_HOME/.shrc.local" ]; then
    fail "shrc template is not a real regular file"
fi
[ "$(file_mode "$IDENTITY_HOME/.shrc.local")" = 644 ] ||
    fail "shrc template has the wrong mode"

SPECIAL_NAME='Ada O'"'"'Neil / & "Compiler"'
SPECIAL_EMAIL='ada+compiler@example.invalid'
SPECIAL_GITHUB='ada-o-neil'
HOME="$IDENTITY_HOME" _with_home_transaction_lock \
    _update_shrc_local_identity_locked \
    "$SPECIAL_NAME" "$SPECIAL_EMAIL" "$SPECIAL_GITHUB" >/dev/null

actual_name=$(bash --noprofile --norc -c \
    '. "$1"; printf "%s" "$USER_NAME"' \
    local-config-test "$IDENTITY_HOME/.shrc.local")
actual_email=$(bash --noprofile --norc -c \
    '. "$1"; printf "%s" "$USER_EMAIL"' \
    local-config-test "$IDENTITY_HOME/.shrc.local")
actual_github=$(bash --noprofile --norc -c \
    '. "$1"; printf "%s" "$USER_GITHUB_ID"' \
    local-config-test "$IDENTITY_HOME/.shrc.local")
[ "$actual_name" = "$SPECIAL_NAME" ] ||
    fail "identity name was not shell-serialized exactly"
[ "$actual_email" = "$SPECIAL_EMAIL" ] ||
    fail "identity email was not shell-serialized exactly"
[ "$actual_github" = "$SPECIAL_GITHUB" ] ||
    fail "identity GitHub ID was not shell-serialized exactly"
[ ! -e "$IDENTITY_HOME/.shrc.local.bak" ] ||
    fail "identity update left an in-place editor backup"

HOME="$IDENTITY_HOME" _generate_gitconfig_local >/dev/null
if [ ! -f "$IDENTITY_HOME/.gitconfig.local" ] ||
        [ -L "$IDENTITY_HOME/.gitconfig.local" ]; then
    fail "git config is not a real regular file"
fi
[ "$(git config --file "$IDENTITY_HOME/.gitconfig.local" --get user.name)" = \
        "$SPECIAL_NAME" ] ||
    fail "git config did not preserve the exact user name"
[ "$(git config --file "$IDENTITY_HOME/.gitconfig.local" --get user.email)" = \
        "$SPECIAL_EMAIL" ] ||
    fail "git config did not preserve the exact user email"

HOME="$IDENTITY_HOME" _ensure_secrets >/dev/null
if [ ! -f "$IDENTITY_HOME/.secrets" ] ||
        [ -L "$IDENTITY_HOME/.secrets" ]; then
    fail "secrets template is not a real regular file"
fi
[ "$(file_mode "$IDENTITY_HOME/.secrets")" = 600 ] ||
    fail "secrets template is not private"
cmp -s "$DOTFILES/secrets.template" "$IDENTITY_HOME/.secrets" ||
    fail "secrets template payload changed during publication"

# Existing real files are create-once state and remain byte-for-byte untouched.
printf '%s\n' 'locally managed secret' > "$IDENTITY_HOME/.secrets"
HOME="$IDENTITY_HOME" _ensure_secrets >/dev/null
grep -qxF 'locally managed secret' "$IDENTITY_HOME/.secrets" ||
    fail "create-once publication overwrote an existing local file"

# A renderer failure can leave only an off-path staging file, never a partial
# live destination.
FAILURE_HOME="$TEST_ROOT/failure-home"
mkdir -p "$FAILURE_HOME"
render_then_fail() {
    printf '%s\n' partial > "$1"
    return 73
}
if HOME="$FAILURE_HOME" _with_home_transaction_lock \
        _create_local_file_locked \
        "$FAILURE_HOME/.shrc.local" 644 render_then_fail \
        >/dev/null 2>&1; then
    fail "failed renderer published a local configuration"
fi
if [ -e "$FAILURE_HOME/.shrc.local" ] ||
        [ -L "$FAILURE_HOME/.shrc.local" ]; then
    fail "failed renderer left a partial live destination"
fi

# Two installers serialize through the HOME lock. Both succeed, but exactly one
# complete template becomes visible.
CONCURRENT_HOME="$TEST_ROOT/concurrent-home"
mkdir -p "$CONCURRENT_HOME"
HOME="$CONCURRENT_HOME" _ensure_shrc_local >/dev/null &
first_pid=$!
HOME="$CONCURRENT_HOME" _ensure_shrc_local >/dev/null &
second_pid=$!
wait "$first_pid"
wait "$second_pid"
if [ ! -f "$CONCURRENT_HOME/.shrc.local" ] ||
        [ -L "$CONCURRENT_HOME/.shrc.local" ]; then
    fail "concurrent publication did not produce one regular file"
fi
grep -qxF 'export USER_NAME="Your Name"' \
    "$CONCURRENT_HOME/.shrc.local" ||
    fail "concurrent publication exposed an incomplete template"

# Create-once paths reject symlinks, including dangling links, rather than
# following them or treating them as absence.
SYMLINK_HOME="$TEST_ROOT/symlink-home"
mkdir -p "$SYMLINK_HOME"
ln -s "$TEST_ROOT/missing-shrc-target" "$SYMLINK_HOME/.shrc.local"
if HOME="$SYMLINK_HOME" _ensure_shrc_local >/dev/null 2>&1; then
    fail "shrc publication accepted a dangling destination symlink"
fi
[ ! -e "$TEST_ROOT/missing-shrc-target" ] ||
    fail "shrc publication followed a dangling destination symlink"

SECRETS_SYMLINK_HOME="$TEST_ROOT/secrets-symlink-home"
mkdir -p "$SECRETS_SYMLINK_HOME"
printf '%s\n' sentinel > "$TEST_ROOT/secrets-target"
ln -s "$TEST_ROOT/secrets-target" "$SECRETS_SYMLINK_HOME/.secrets"
if HOME="$SECRETS_SYMLINK_HOME" _ensure_secrets >/dev/null 2>&1; then
    fail "secrets publication accepted a destination symlink"
fi
grep -qxF sentinel "$TEST_ROOT/secrets-target" ||
    fail "secrets publication overwrote a symlink target"

GIT_SYMLINK_HOME="$TEST_ROOT/git-symlink-home"
mkdir -p "$GIT_SYMLINK_HOME"
ln -s "$TEST_ROOT/missing-git-target" \
    "$GIT_SYMLINK_HOME/.gitconfig.local"
if HOME="$GIT_SYMLINK_HOME" _generate_gitconfig_local \
        >/dev/null 2>&1; then
    fail "git config publication accepted a dangling destination symlink"
fi
[ ! -e "$TEST_ROOT/missing-git-target" ] ||
    fail "git config publication followed a dangling destination symlink"

DIRECTORY_HOME="$TEST_ROOT/directory-home"
mkdir -p "$DIRECTORY_HOME/.secrets"
if HOME="$DIRECTORY_HOME" _ensure_secrets >/dev/null 2>&1; then
    fail "secrets publication accepted a directory destination"
fi

# Fixup publishes the local append before reverting the exact tracked diff.
# It refuses a symlinked local file and leaves the repository change intact.
FIXUP_REPOSITORY="$TEST_ROOT/fixup-repository"
FIXUP_HOME="$TEST_ROOT/fixup-home"
mkdir -p "$FIXUP_REPOSITORY/shell" "$FIXUP_HOME"
git init -q -b main "$FIXUP_REPOSITORY"
git -C "$FIXUP_REPOSITORY" config user.name "Local Config Test"
git -C "$FIXUP_REPOSITORY" config user.email \
    "local-config-test@example.invalid"
git -C "$FIXUP_REPOSITORY" config commit.gpgsign false
printf '%s\n' 'tracked shell line' > "$FIXUP_REPOSITORY/shell/shrc"
git -C "$FIXUP_REPOSITORY" add shell/shrc
git -C "$FIXUP_REPOSITORY" commit -q -m "seed shell configuration"
printf 'installer addition\n+leading-plus option\n\n\n' \
    >> "$FIXUP_REPOSITORY/shell/shrc"
(
    cd "$FIXUP_REPOSITORY"
    HOME="$FIXUP_HOME" _fixup_move_to_shrc_local shell/shrc >/dev/null
)
git -C "$FIXUP_REPOSITORY" diff --quiet -- shell/shrc ||
    fail "fixup did not restore the published tracked generation"
grep -qxF 'installer addition' "$FIXUP_HOME/.shrc.local" ||
    fail "fixup did not publish the installer addition"
python3 - "$FIXUP_HOME/.shrc.local" << 'PY' ||
import sys

if not open(sys.argv[1], "rb").read().endswith(
        b"installer addition\n+leading-plus option\n\n\n"):
    raise SystemExit(1)
PY
    fail "fixup did not preserve the exact appended byte suffix"

SYMLINK_FIXUP_HOME="$TEST_ROOT/symlink-fixup-home"
mkdir -p "$SYMLINK_FIXUP_HOME"
printf '%s\n' 'local target sentinel' > "$TEST_ROOT/fixup-local-target"
ln -s "$TEST_ROOT/fixup-local-target" \
    "$SYMLINK_FIXUP_HOME/.shrc.local"
printf '%s\n' 'second installer addition' \
    >> "$FIXUP_REPOSITORY/shell/shrc"
if (
    cd "$FIXUP_REPOSITORY"
    HOME="$SYMLINK_FIXUP_HOME" \
        _fixup_move_to_shrc_local shell/shrc >/dev/null 2>&1
); then
    fail "fixup accepted a symlinked local shell configuration"
fi
git -C "$FIXUP_REPOSITORY" diff --quiet -- shell/shrc &&
    fail "failed fixup discarded the tracked shell change"
grep -qxF 'local target sentinel' "$TEST_ROOT/fixup-local-target" ||
    fail "failed fixup followed the local shell symlink"

# Mixed edits are not equivalent to installer append pollution. Refuse before
# publishing anything so a later full-file restore cannot discard deletions.
git -C "$FIXUP_REPOSITORY" checkout -- shell/shrc
printf '%s\n' \
    'replacement tracked line' \
    'mixed installer addition' \
    > "$FIXUP_REPOSITORY/shell/shrc"
cp "$FIXUP_HOME/.shrc.local" "$TEST_ROOT/shrc-local-before-mixed-fixup"
if (
    cd "$FIXUP_REPOSITORY"
    HOME="$FIXUP_HOME" \
        _fixup_move_to_shrc_local shell/shrc >/dev/null 2>&1
); then
    fail "fixup accepted a mixed add/delete diff"
fi
git -C "$FIXUP_REPOSITORY" diff --quiet -- shell/shrc &&
    fail "mixed-diff refusal discarded the tracked change"
cmp -s \
    "$TEST_ROOT/shrc-local-before-mixed-fixup" \
    "$FIXUP_HOME/.shrc.local" ||
    fail "mixed-diff refusal changed the local shell configuration"

# A pure insertion has no deleted lines, but it is not an appended suffix and
# moving it to the end can change shell evaluation order.
git -C "$FIXUP_REPOSITORY" checkout -- shell/shrc
printf '%s\n' \
    'inserted before tracked state' \
    'tracked shell line' \
    > "$FIXUP_REPOSITORY/shell/shrc"
cp "$FIXUP_HOME/.shrc.local" "$TEST_ROOT/shrc-local-before-insert-fixup"
if (
    cd "$FIXUP_REPOSITORY"
    HOME="$FIXUP_HOME" \
        _fixup_move_to_shrc_local shell/shrc >/dev/null 2>&1
); then
    fail "fixup accepted a mid-file insertion"
fi
git -C "$FIXUP_REPOSITORY" diff --quiet -- shell/shrc &&
    fail "insertion refusal discarded the tracked change"
cmp -s \
    "$TEST_ROOT/shrc-local-before-insert-fixup" \
    "$FIXUP_HOME/.shrc.local" ||
    fail "insertion refusal changed the local shell configuration"

printf 'local config publication test: PASS\n'
