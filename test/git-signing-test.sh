#!/bin/bash
# Behavioral coverage for repeatable machine-local SSH signing setup.

set -euo pipefail

DOTFILES="${DOTFILES:-$HOME/.dotfiles}"
TEST_PARENT="${TMPDIR:-/tmp}"
TEST_ROOT=$(mktemp -d "$TEST_PARENT/dotfiles-git-signing-test.XXXXXX")
cleanup_test_root() {
    case "$TEST_ROOT" in
        "$TEST_PARENT"/dotfiles-git-signing-test.*) ;;
        *)
            echo "refusing unexpected test cleanup path: $TEST_ROOT" >&2
            return 1
            ;;
    esac
    [ ! -e "$TEST_ROOT" ] || find "$TEST_ROOT" -xdev -depth -delete
}
trap cleanup_test_root EXIT

fail() {
    echo "git signing test: $1" >&2
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

TEST_HOME="$TEST_ROOT/home"
SSH_DIRECTORY="$TEST_HOME/.ssh"
GLOBAL_CONFIG="$TEST_HOME/.gitconfig"
LOCAL_CONFIG="$TEST_HOME/.gitconfig.local"
ALLOWED_SIGNERS="$SSH_DIRECTORY/allowed_signers"
PORTABLE_HOME_PREFIX='~'
mkdir -p "$SSH_DIRECTORY"

EMAIL="developer@example.com"
ssh-keygen -q -t ed25519 -N '' -C old-key \
    -f "$SSH_DIRECTORY/id_rsa"
ssh-keygen -q -t ed25519 -N '' -C selected-key \
    -f "$SSH_DIRECTORY/id_ed25519"
RSA_KEY=$(cat "$SSH_DIRECTORY/id_rsa.pub")
ED25519_KEY=$(cat "$SSH_DIRECTORY/id_ed25519.pub")

initialize_signing_home() {
    local signing_home="$1"

    mkdir -p "$signing_home/.ssh"
    cp "$SSH_DIRECTORY/id_ed25519" \
        "$signing_home/.ssh/id_ed25519"
    cp "$SSH_DIRECTORY/id_ed25519.pub" \
        "$signing_home/.ssh/id_ed25519.pub"
    chmod 600 "$signing_home/.ssh/id_ed25519"
    chmod 644 "$signing_home/.ssh/id_ed25519.pub"
    git config --file "$signing_home/.gitconfig" \
        include.path "$PORTABLE_HOME_PREFIX/.gitconfig.local"
    git config --file "$signing_home/.gitconfig" user.email "$EMAIL"
    git config --file "$signing_home/.gitconfig" gpg.format ssh
    git config --file "$signing_home/.gitconfig" commit.gpgsign true
}

git config --file "$GLOBAL_CONFIG" \
    include.path "$PORTABLE_HOME_PREFIX/.gitconfig.local"
git config --file "$GLOBAL_CONFIG" user.email "$EMAIL"
git config --file "$GLOBAL_CONFIG" gpg.format ssh
git config --file "$GLOBAL_CONFIG" commit.gpgsign true
printf '%s %s\n' "$EMAIL" "$RSA_KEY" > "$ALLOWED_SIGNERS"

HOME="$TEST_HOME" "$DOTFILES/bin/git-setup-signing" >/dev/null
[ "$(git config --file "$LOCAL_CONFIG" user.signingkey)" = \
    "$PORTABLE_HOME_PREFIX/.ssh/id_ed25519.pub" ] ||
    fail "preferred Ed25519 key was not selected"
grep -Fqx -- "$EMAIL $RSA_KEY" "$ALLOWED_SIGNERS" ||
    fail "existing signer key was discarded"
grep -Fqx -- "$EMAIL $ED25519_KEY" "$ALLOWED_SIGNERS" ||
    fail "selected signer key was not added"

# Repeating setup is idempotent and preserves an explicit machine-local key.
HOME="$TEST_HOME" "$DOTFILES/bin/git-setup-signing" >/dev/null
[ "$(grep -Fxc -- "$EMAIL $ED25519_KEY" "$ALLOWED_SIGNERS")" -eq 1 ] ||
    fail "repeated setup duplicated the selected signer"
git config --file "$LOCAL_CONFIG" \
    user.signingkey "$PORTABLE_HOME_PREFIX/.ssh/id_rsa.pub"
HOME="$TEST_HOME" "$DOTFILES/bin/git-setup-signing" >/dev/null
[ "$(git config --file "$LOCAL_CONFIG" user.signingkey)" = \
    "$PORTABLE_HOME_PREFIX/.ssh/id_rsa.pub" ] ||
    fail "explicit machine-local signing key was overwritten"

# An explicit key outside ~/.ssh is machine-local and must not be rewritten to
# a nonexistent portable basename.
EXTERNAL_PRIVATE_KEY="$TEST_ROOT/external-signing-key"
EXTERNAL_KEY="$EXTERNAL_PRIVATE_KEY.pub"
ssh-keygen -q -t ed25519 -N '' -C external-key \
    -f "$EXTERNAL_PRIVATE_KEY"
EXTERNAL_KEY_CONTENT=$(cat "$EXTERNAL_KEY")
git config --file "$LOCAL_CONFIG" user.signingkey "$EXTERNAL_KEY"
HOME="$TEST_HOME" "$DOTFILES/bin/git-setup-signing" >/dev/null
[ "$(git config --file "$LOCAL_CONFIG" user.signingkey)" = "$EXTERNAL_KEY" ] ||
    fail "external machine-local signing key path was rewritten"
grep -Fqx -- "$EMAIL $EXTERNAL_KEY_CONTENT" "$ALLOWED_SIGNERS" ||
    fail "external machine-local signer was not authorized"

# Git may legitimately sign through a private-key path. Keep that configured
# path while authorizing only the corresponding public key.
CONFIGURED_PRIVATE_KEY="$SSH_DIRECTORY/configured-private"
ssh-keygen -q -t ed25519 -N '' -C configured-private \
    -f "$CONFIGURED_PRIVATE_KEY"
CONFIGURED_PRIVATE_PUBLIC=$(cat "$CONFIGURED_PRIVATE_KEY.pub")
git config --file "$LOCAL_CONFIG" \
    user.signingkey "$PORTABLE_HOME_PREFIX/.ssh/configured-private"
HOME="$TEST_HOME" "$DOTFILES/bin/git-setup-signing" >/dev/null
[ "$(git config --file "$LOCAL_CONFIG" user.signingkey)" = \
    "$PORTABLE_HOME_PREFIX/.ssh/configured-private" ] ||
    fail "configured private signing-key path was rewritten"
grep -Fqx -- "$EMAIL $CONFIGURED_PRIVATE_PUBLIC" "$ALLOWED_SIGNERS" ||
    fail "configured private key did not authorize its public counterpart"
if grep -q 'PRIVATE KEY' "$ALLOWED_SIGNERS"; then
    fail "private signing-key material leaked into allowed signers"
fi

# A public-key path must contain exactly one valid key. Reject extra records
# rather than appending multiline data under one principal.
MALFORMED_PUBLIC_KEY="$TEST_ROOT/multiline-signing-key.pub"
printf '%s\n%s\n' "$ED25519_KEY" "$EXTERNAL_KEY_CONTENT" \
    > "$MALFORMED_PUBLIC_KEY"
cp "$ALLOWED_SIGNERS" "$TEST_ROOT/allowed-signers-before-malformed"
git config --file "$LOCAL_CONFIG" user.signingkey "$MALFORMED_PUBLIC_KEY"
if HOME="$TEST_HOME" "$DOTFILES/bin/git-setup-signing" \
        >/dev/null 2>&1; then
    fail "multiline configured public key was accepted"
fi
cmp -s "$TEST_ROOT/allowed-signers-before-malformed" "$ALLOWED_SIGNERS" ||
    fail "rejected configured public key changed allowed signers"

if HOME="$TEST_HOME" "$DOTFILES/bin/git-setup-signing" unexpected \
        >/dev/null 2>&1; then
    fail "unexpected positional argument was accepted"
fi

# Two first-time invocations share the HOME transaction lock. Both complete,
# while the one selected signer and one complete config generation are
# published exactly once.
CONCURRENT_HOME="$TEST_ROOT/concurrent-home"
initialize_signing_home "$CONCURRENT_HOME"
HOME="$CONCURRENT_HOME" "$DOTFILES/bin/git-setup-signing" \
    >"$TEST_ROOT/concurrent-first.out" 2>&1 &
first_pid=$!
HOME="$CONCURRENT_HOME" "$DOTFILES/bin/git-setup-signing" \
    >"$TEST_ROOT/concurrent-second.out" 2>&1 &
second_pid=$!
if ! wait "$first_pid"; then
    fail "first concurrent signing setup failed"
fi
if ! wait "$second_pid"; then
    fail "second concurrent signing setup failed"
fi
[ "$(grep -Fxc -- \
        "$EMAIL $ED25519_KEY" \
        "$CONCURRENT_HOME/.ssh/allowed_signers")" -eq 1 ] ||
    fail "concurrent signing setup duplicated the selected signer"
[ "$(git config --file "$CONCURRENT_HOME/.gitconfig.local" \
        user.signingkey)" = \
        "$PORTABLE_HOME_PREFIX/.ssh/id_ed25519.pub" ] ||
    fail "concurrent signing setup published an incomplete config"

# Appending after a legacy record without a final newline inserts the missing
# record boundary. Existing bytes and modes survive both file publications.
NO_NEWLINE_HOME="$TEST_ROOT/no-newline-home"
initialize_signing_home "$NO_NEWLINE_HOME"
printf '[alias]\n    signing-sentinel = status\n' \
    > "$NO_NEWLINE_HOME/.gitconfig.local"
chmod 640 "$NO_NEWLINE_HOME/.gitconfig.local"
printf '%s' "legacy@example.invalid $RSA_KEY" \
    > "$NO_NEWLINE_HOME/.ssh/allowed_signers"
chmod 600 "$NO_NEWLINE_HOME/.ssh/allowed_signers"
HOME="$NO_NEWLINE_HOME" "$DOTFILES/bin/git-setup-signing" >/dev/null
printf '%s\n%s\n' \
    "legacy@example.invalid $RSA_KEY" \
    "$EMAIL $ED25519_KEY" \
    > "$TEST_ROOT/expected-no-newline-signers"
cmp -s \
    "$TEST_ROOT/expected-no-newline-signers" \
    "$NO_NEWLINE_HOME/.ssh/allowed_signers" ||
    fail "missing final newline concatenated two signer records"
[ "$(file_mode "$NO_NEWLINE_HOME/.ssh/allowed_signers")" = 600 ] ||
    fail "allowed-signers publication changed its existing mode"
[ "$(file_mode "$NO_NEWLINE_HOME/.gitconfig.local")" = 640 ] ||
    fail "Git-config publication changed its existing mode"
[ "$(git config --file "$NO_NEWLINE_HOME/.gitconfig.local" \
        alias.signing-sentinel)" = status ] ||
    fail "Git-config publication discarded existing configuration"

# The signer is committed and synced before the config may select it. A fault
# at that boundary therefore leaves the prior config byte-exact and can be
# resumed without duplicating the already-authorized key.
FAULT_HOME="$TEST_ROOT/fault-home"
initialize_signing_home "$FAULT_HOME"
printf '[user]\n    signingkey = /missing/prior-signing-key\n' \
    > "$FAULT_HOME/.gitconfig.local"
printf '[alias]\n    retained = status\n' \
    >> "$FAULT_HOME/.gitconfig.local"
chmod 640 "$FAULT_HOME/.gitconfig.local"
cp "$FAULT_HOME/.gitconfig.local" "$TEST_ROOT/fault-config-before"
if HOME="$FAULT_HOME" \
        DOTFILES_GIT_SIGNING_TEST_FAULT=after-signer-publication \
        "$DOTFILES/bin/git-setup-signing" >/dev/null 2>&1; then
    fail "post-signer publication fault returned success"
fi
cmp -s \
    "$TEST_ROOT/fault-config-before" \
    "$FAULT_HOME/.gitconfig.local" ||
    fail "post-signer publication fault changed Git config"
grep -Fqx -- \
    "$EMAIL $ED25519_KEY" \
    "$FAULT_HOME/.ssh/allowed_signers" ||
    fail "post-signer publication fault lost the authorized signer"
HOME="$FAULT_HOME" "$DOTFILES/bin/git-setup-signing" >/dev/null
[ "$(grep -Fxc -- \
        "$EMAIL $ED25519_KEY" \
        "$FAULT_HOME/.ssh/allowed_signers")" -eq 1 ] ||
    fail "post-fault replay duplicated the selected signer"
[ "$(file_mode "$FAULT_HOME/.gitconfig.local")" = 640 ] ||
    fail "post-fault replay changed the Git-config mode"

# Refuse a redirecting allowed-signers path instead of appending through it.
unlink "$ALLOWED_SIGNERS"
SIGNER_TARGET="$TEST_ROOT/external-signers"
printf '%s\n' "external sentinel" > "$SIGNER_TARGET"
ln -s "$SIGNER_TARGET" "$ALLOWED_SIGNERS"
git config --file "$LOCAL_CONFIG" user.signingkey "$EXTERNAL_KEY"
if HOME="$TEST_HOME" "$DOTFILES/bin/git-setup-signing" >/dev/null 2>&1; then
    fail "symlinked allowed-signers path was accepted"
fi
[ "$(cat "$SIGNER_TARGET")" = "external sentinel" ] ||
    fail "symlinked allowed-signers target was modified"

# Both signing destinations reject live and dangling symlinks before either
# file can change.
LIVE_CONFIG_HOME="$TEST_ROOT/live-config-symlink-home"
initialize_signing_home "$LIVE_CONFIG_HOME"
LIVE_CONFIG_TARGET="$TEST_ROOT/live-config-target"
printf '%s\n' "config sentinel" > "$LIVE_CONFIG_TARGET"
ln -s "$LIVE_CONFIG_TARGET" "$LIVE_CONFIG_HOME/.gitconfig.local"
if HOME="$LIVE_CONFIG_HOME" "$DOTFILES/bin/git-setup-signing" \
        >/dev/null 2>&1; then
    fail "live symlinked Git config was accepted"
fi
grep -qxF "config sentinel" "$LIVE_CONFIG_TARGET" ||
    fail "live symlinked Git-config target was modified"
[ ! -e "$LIVE_CONFIG_HOME/.ssh/allowed_signers" ] ||
    fail "Git-config symlink refusal changed allowed signers"

DANGLING_CONFIG_HOME="$TEST_ROOT/dangling-config-symlink-home"
initialize_signing_home "$DANGLING_CONFIG_HOME"
DANGLING_CONFIG_TARGET="$TEST_ROOT/missing-config-target"
ln -s "$DANGLING_CONFIG_TARGET" \
    "$DANGLING_CONFIG_HOME/.gitconfig.local"
if HOME="$DANGLING_CONFIG_HOME" "$DOTFILES/bin/git-setup-signing" \
        >/dev/null 2>&1; then
    fail "dangling symlinked Git config was accepted"
fi
[ ! -e "$DANGLING_CONFIG_TARGET" ] ||
    fail "dangling Git-config symlink was followed"
[ ! -e "$DANGLING_CONFIG_HOME/.ssh/allowed_signers" ] ||
    fail "dangling Git-config refusal changed allowed signers"

LIVE_SIGNERS_HOME="$TEST_ROOT/live-signers-symlink-home"
initialize_signing_home "$LIVE_SIGNERS_HOME"
LIVE_SIGNERS_TARGET="$TEST_ROOT/live-signers-target"
printf '%s\n' "signers sentinel" > "$LIVE_SIGNERS_TARGET"
ln -s "$LIVE_SIGNERS_TARGET" \
    "$LIVE_SIGNERS_HOME/.ssh/allowed_signers"
if HOME="$LIVE_SIGNERS_HOME" "$DOTFILES/bin/git-setup-signing" \
        >/dev/null 2>&1; then
    fail "live symlinked allowed signers was accepted"
fi
grep -qxF "signers sentinel" "$LIVE_SIGNERS_TARGET" ||
    fail "live symlinked allowed-signers target was modified"
[ ! -e "$LIVE_SIGNERS_HOME/.gitconfig.local" ] ||
    fail "allowed-signers symlink refusal changed Git config"

DANGLING_SIGNERS_HOME="$TEST_ROOT/dangling-signers-symlink-home"
initialize_signing_home "$DANGLING_SIGNERS_HOME"
DANGLING_SIGNERS_TARGET="$TEST_ROOT/missing-signers-target"
ln -s "$DANGLING_SIGNERS_TARGET" \
    "$DANGLING_SIGNERS_HOME/.ssh/allowed_signers"
if HOME="$DANGLING_SIGNERS_HOME" "$DOTFILES/bin/git-setup-signing" \
        >/dev/null 2>&1; then
    fail "dangling symlinked allowed signers was accepted"
fi
[ ! -e "$DANGLING_SIGNERS_TARGET" ] ||
    fail "dangling allowed-signers symlink was followed"
[ ! -e "$DANGLING_SIGNERS_HOME/.gitconfig.local" ] ||
    fail "dangling allowed-signers refusal changed Git config"

# The install dispatcher must not skip a configured private key merely because
# no public-key files are discoverable. The signing helper can safely derive
# an unencrypted key for allowed_signers.
PRIVATE_ONLY_HOME="$TEST_ROOT/private-only-home"
mkdir -p "$PRIVATE_ONLY_HOME/.ssh"
PRIVATE_ONLY_KEY="$PRIVATE_ONLY_HOME/.ssh/signing-key"
ssh-keygen -q -t ed25519 -N '' -C private-only \
    -f "$PRIVATE_ONLY_KEY"
PRIVATE_ONLY_PUBLIC=$(cat "$PRIVATE_ONLY_KEY.pub")
unlink "$PRIVATE_ONLY_KEY.pub"
git config --file "$PRIVATE_ONLY_HOME/.gitconfig" \
    include.path "$PORTABLE_HOME_PREFIX/.gitconfig.local"
git config --file "$PRIVATE_ONLY_HOME/.gitconfig" user.email "$EMAIL"
git config --file "$PRIVATE_ONLY_HOME/.gitconfig.local" \
    user.signingkey "$PORTABLE_HOME_PREFIX/.ssh/signing-key"
(
    export HOME="$PRIVATE_ONLY_HOME"
    set -- help
    # shellcheck disable=SC1091  # Runtime-selected repository under test.
    . "$DOTFILES/bin/dotfiles" >/dev/null
    _setup_git_signing >/dev/null
)
grep -Fqx -- \
    "$EMAIL $PRIVATE_ONLY_PUBLIC" \
    "$PRIVATE_ONLY_HOME/.ssh/allowed_signers" ||
    fail "install path skipped the configured private signing key"

echo "git signing setup passed"
