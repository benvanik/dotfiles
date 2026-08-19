#!/bin/bash
# Offline behavior tests for pinned shell-component bootstrap paths.
set -euo pipefail

DOTFILES="${DOTFILES:-$HOME/.dotfiles}"
TEST_PARENT="${TMPDIR:-/tmp}"
TEST_ROOT=$(mktemp -d "$TEST_PARENT/dotfiles-bootstrap-installer.XXXXXX")

cleanup_test_root() {
    case "$TEST_ROOT" in
        "$TEST_PARENT"/dotfiles-bootstrap-installer.*) ;;
        *)
            echo "refusing unexpected bootstrap test root: $TEST_ROOT" >&2
            return 1
            ;;
    esac
    [ ! -e "$TEST_ROOT" ] ||
        find "$TEST_ROOT" -xdev -depth -delete
}
trap cleanup_test_root EXIT

fail() {
    echo "bootstrap installer test: $1" >&2
    exit 1
}

git_fixture() {
    local repository="$1"
    mkdir -p "$repository"
    git init --quiet "$repository"
    git -C "$repository" config user.email fixtures@example.com
    git -C "$repository" config user.name Fixtures
}

export HOME="$TEST_ROOT/home"
mkdir -p "$HOME"
TOOL_NAME="bootstrap-test"
# shellcheck source=../tools/install-utils.sh
source "$DOTFILES/tools/install-utils.sh"

# A pinned checkout is fetched into private staging and published detached.
SOURCE_REPOSITORY="$TEST_ROOT/source"
git_fixture "$SOURCE_REPOSITORY"
printf 'one\n' > "$SOURCE_REPOSITORY/payload"
git -C "$SOURCE_REPOSITORY" add payload
git -C "$SOURCE_REPOSITORY" commit --quiet -m one
FIRST_COMMIT=$(git -C "$SOURCE_REPOSITORY" rev-parse HEAD)
printf 'two\n' > "$SOURCE_REPOSITORY/payload"
git -C "$SOURCE_REPOSITORY" commit --quiet -am two
SECOND_COMMIT=$(git -C "$SOURCE_REPOSITORY" rev-parse HEAD)
SOURCE_ORIGIN="file://$SOURCE_REPOSITORY"
MANAGED_CHECKOUT="$TEST_ROOT/managed/component"

install_pinned_git_checkout \
    Component "$SOURCE_ORIGIN" "$FIRST_COMMIT" "$MANAGED_CHECKOUT"
pinned_git_checkout_valid \
    "$MANAGED_CHECKOUT" "$SOURCE_ORIGIN" "$FIRST_COMMIT" ||
    fail "new checkout did not retain its exact detached identity"

# Reuse of an exact checkout never contacts its now-unavailable origin.
mv "$SOURCE_REPOSITORY" "$TEST_ROOT/source-offline"
install_pinned_git_checkout \
    Component "$SOURCE_ORIGIN" "$FIRST_COMMIT" "$MANAGED_CHECKOUT"
mv "$TEST_ROOT/source-offline" "$SOURCE_REPOSITORY"

# A clean revision change publishes a complete new generation.
install_pinned_git_checkout \
    Component "$SOURCE_ORIGIN" "$SECOND_COMMIT" "$MANAGED_CHECKOUT"
pinned_git_checkout_valid \
    "$MANAGED_CHECKOUT" "$SOURCE_ORIGIN" "$SECOND_COMMIT" ||
    fail "clean checkout did not move to the reviewed commit"
grep -qx two "$MANAGED_CHECKOUT/payload" ||
    fail "checkout replacement mixed the old and new generations"
if find "$TEST_ROOT/managed" -maxdepth 1 \
        \( -name '.replace-*' -o -name '.publish-*.lock' \) \
        -print -quit | grep -q .; then
    fail "checkout replacement retained transaction state"
fi

# Local content and foreign origins are ownership failures, not update inputs.
printf 'local\n' >> "$MANAGED_CHECKOUT/payload"
if install_pinned_git_checkout \
        Component "$SOURCE_ORIGIN" "$FIRST_COMMIT" "$MANAGED_CHECKOUT" \
        >/dev/null 2>&1; then
    fail "dirty checkout was replaced"
fi
git -C "$MANAGED_CHECKOUT" show HEAD:payload > "$TEST_ROOT/clean-payload"
cp "$TEST_ROOT/clean-payload" "$MANAGED_CHECKOUT/payload"
git -C "$MANAGED_CHECKOUT" remote set-url origin \
    file://"$TEST_ROOT"/foreign
if install_pinned_git_checkout \
        Component "$SOURCE_ORIGIN" "$SECOND_COMMIT" "$MANAGED_CHECKOUT" \
        >/dev/null 2>&1; then
    fail "foreign checkout origin was accepted"
fi
git -C "$MANAGED_CHECKOUT" remote set-url origin "$SOURCE_ORIGIN"

# Source install-deps without executing package-manager work.
# shellcheck source=../install-deps.sh
source "$DOTFILES/install-deps.sh"

[ "$DOTFILES_OMZ_COMMIT" = \
    "beadd56dd75e8a40fe0a7d4a5d63ed5bf9efcd48" ] ||
    fail "Oh My Zsh pin drifted"
[ "$DOTFILES_P10K_COMMIT" = \
    "36f3045d69d1ba402db09d09eb12b42eebe0fa3b" ] ||
    fail "Powerlevel10k pin drifted"
[ "$DOTFILES_TPM_COMMIT" = \
    "e261deb1b47614eed3400089ce7197dc68acc4eb" ] ||
    fail "TPM pin drifted"
[ "$DOTFILES_NODE_VERSION" = "24.11.1" ] ||
    fail "Node bootstrap is not exact"

# Every apt caller must re-establish noninteractive mode after sudo clears the
# caller's environment. The fake sudo enforces the production argv boundary,
# then runs all six transaction paths through a recording apt-get fixture.
APT_FIXTURE_ROOT="$TEST_ROOT/apt"
APT_CALL_LOG="$APT_FIXTURE_ROOT/calls"
APT_FIXTURE="$APT_FIXTURE_ROOT/apt-get"
mkdir -p "$APT_FIXTURE_ROOT"
export APT_CALL_LOG
cat > "$APT_FIXTURE" << 'EOF'
#!/bin/sh
set -eu
printf '%s|' "${DEBIAN_FRONTEND-unset}" >> "$APT_CALL_LOG"
printf '<%s>' "$@" >> "$APT_CALL_LOG"
printf '\n' >> "$APT_CALL_LOG"
EOF
chmod 0755 "$APT_FIXTURE"
(
    # shellcheck disable=SC2317  # Invoked by each apt transaction.
    sudo() {
        local env_path="${1:-}"
        local environment_assignment="${2:-}"
        local apt_get_path="${3:-}"
        shift 3
        [ "$env_path" = "/usr/bin/env" ] ||
            fail "apt transaction used an untrusted env path"
        [ "$environment_assignment" = "DEBIAN_FRONTEND=noninteractive" ] ||
            fail "apt transaction omitted noninteractive mode"
        [ "$apt_get_path" = "/usr/bin/apt-get" ] ||
            fail "apt transaction used an untrusted apt-get path"
        unset DEBIAN_FRONTEND
        /usr/bin/env "$environment_assignment" "$APT_FIXTURE" "$@"
    }
    # shellcheck disable=SC2317  # Replaced to isolate package transactions.
    setup_debian_symlinks() {
        :
    }
    # shellcheck disable=SC2317  # Replaced to isolate package transactions.
    install_tpm() {
        :
    }
    # shellcheck disable=SC2317  # Forces both multiplexer package branches.
    command() {
        if [ "${1:-}" = "-v" ]; then
            case "${2:-}" in
                tmux|byobu) return 1 ;;
            esac
        fi
        builtin command "$@"
    }

    install_apt
    PKG_MGR=apt
    install_tmux_byobu
)
[ "$(wc -l < "$APT_CALL_LOG")" -eq 6 ] ||
    fail "not every apt transaction crossed the noninteractive wrapper"
if grep -v '^noninteractive|' "$APT_CALL_LOG" >/dev/null; then
    fail "an apt transaction lost noninteractive mode after sudo"
fi
sed -n '1p' "$APT_CALL_LOG" |
    grep -qxF 'noninteractive|<update>' ||
    fail "apt update did not use the noninteractive wrapper"
EXPECTED_APT_BUILD_CALL='noninteractive|<install><-y><autoconf><automake><bison>'\
'<build-essential><coreutils><pkg-config><tar><libevent-dev>'\
'<libncurses-dev>'
sed -n '4p' "$APT_CALL_LOG" |
    grep -qxF "$EXPECTED_APT_BUILD_CALL" ||
    fail "apt build-tool transaction bypassed the wrapper"
sed -n '5p' "$APT_CALL_LOG" |
    grep -qxF 'noninteractive|<install><-y><tmux>' ||
    fail "tmux apt transaction bypassed the wrapper"
sed -n '6p' "$APT_CALL_LOG" |
    grep -qxF 'noninteractive|<install><-y><byobu>' ||
    fail "byobu apt transaction bypassed the wrapper"
if rg -n 'sudo[[:space:]]+apt(-get)?([[:space:]]|$)' \
        "$DOTFILES/install-deps.sh" >/dev/null; then
    fail "bootstrap retained an apt call outside the noninteractive wrapper"
fi

# Debian may package a renamed command as a symlink. Publish the fully resolved
# executable so the user alias does not inherit a mutable symlink chain.
DEBIAN_COMMAND_ROOT="$TEST_ROOT/debian-command"
DEBIAN_COMMAND_BIN="$DEBIAN_COMMAND_ROOT/bin"
DEBIAN_COMMAND_TARGET="$DEBIAN_COMMAND_ROOT/lib/fd"
SYSTEM_BASH=$(command -v bash)
mkdir -p "$DEBIAN_COMMAND_BIN" "$(dirname "$DEBIAN_COMMAND_TARGET")"
for command_name in dirname ln mkdir mktemp python3 uname unlink; do
    ln -s "$(command -v "$command_name")" \
        "$DEBIAN_COMMAND_BIN/$command_name"
done
cat > "$DEBIAN_COMMAND_TARGET" << 'EOF'
#!/bin/sh
printf 'fixture fd\n'
EOF
chmod 0755 "$DEBIAN_COMMAND_TARGET"
ln -s ../lib/fd "$DEBIAN_COMMAND_BIN/fdfind"
mkdir -p "$DEBIAN_COMMAND_ROOT/home"
# shellcheck disable=SC2016  # DOTFILES expands in the isolated child shell.
/usr/bin/env \
    PATH="$DEBIAN_COMMAND_BIN" \
    HOME="$DEBIAN_COMMAND_ROOT/home" \
    DOTFILES="$DOTFILES" \
    "$SYSTEM_BASH" -c \
        'source "$DOTFILES/install-deps.sh"; setup_debian_symlinks'
[ "$(readlink "$DEBIAN_COMMAND_ROOT/home/.local/bin/fd")" = \
    "$DEBIAN_COMMAND_TARGET" ] ||
    fail "Debian command alias did not resolve its packaged symlink"
[ "$("$DEBIAN_COMMAND_ROOT/home/.local/bin/fd")" = "fixture fd" ] ||
    fail "Debian command alias does not execute its resolved target"
DEBIAN_COMMAND_NEW_TARGET="$DEBIAN_COMMAND_ROOT/lib/fd-new"
cat > "$DEBIAN_COMMAND_NEW_TARGET" << 'EOF'
#!/bin/sh
printf 'retargeted fd\n'
EOF
chmod 0755 "$DEBIAN_COMMAND_NEW_TARGET"
unlink "$DEBIAN_COMMAND_BIN/fdfind"
ln -s ../lib/fd-new "$DEBIAN_COMMAND_BIN/fdfind"
if [ "$(readlink "$DEBIAN_COMMAND_ROOT/home/.local/bin/fd")" != \
        "$DEBIAN_COMMAND_TARGET" ] ||
        [ "$("$DEBIAN_COMMAND_ROOT/home/.local/bin/fd")" != "fixture fd" ]; then
    fail "published command alias inherited a later source retarget"
fi

ln -s missing "$DEBIAN_COMMAND_BIN/dangling"
if update_command_symlink \
        "$DEBIAN_COMMAND_BIN/dangling" \
        "$DEBIAN_COMMAND_ROOT/home/.local/bin/dangling-rejected" \
        >/dev/null 2>&1; then
    fail "command alias accepted a dangling source link"
fi
DEBIAN_COMMAND_NONEXECUTABLE="$DEBIAN_COMMAND_ROOT/lib/nonexecutable"
printf 'not executable\n' > "$DEBIAN_COMMAND_NONEXECUTABLE"
if update_command_symlink \
        "$DEBIAN_COMMAND_NONEXECUTABLE" \
        "$DEBIAN_COMMAND_ROOT/home/.local/bin/nonexec-rejected" \
        >/dev/null 2>&1; then
    fail "command alias accepted a non-executable source"
fi
if update_command_symlink \
        relative-command \
        "$DEBIAN_COMMAND_ROOT/home/.local/bin/relative-rejected" \
        >/dev/null 2>&1; then
    fail "command alias accepted a relative source"
fi
for rejected_alias in \
        dangling-rejected nonexec-rejected relative-rejected; do
    if [ -e "$DEBIAN_COMMAND_ROOT/home/.local/bin/$rejected_alias" ] ||
            [ -L "$DEBIAN_COMMAND_ROOT/home/.local/bin/$rejected_alias" ]; then
        fail "rejected command alias left a destination behind: $rejected_alias"
    fi
done

# The three bootstrap functions pass the reviewed identities to the one
# transactional checkout implementation.
CAPTURE_ROOT="$TEST_ROOT/captures"
mkdir "$CAPTURE_ROOT"
(
    install_pinned_git_checkout() {
        printf '%s\n' "$@" > "$CAPTURE_ROOT/omz"
    }
    install_omz
)
sed -n '2p' "$CAPTURE_ROOT/omz" |
    grep -qxF "$DOTFILES_OMZ_ORIGIN" ||
    fail "Oh My Zsh wiring changed its origin"
sed -n '3p' "$CAPTURE_ROOT/omz" |
    grep -qxF "$DOTFILES_OMZ_COMMIT" ||
    fail "Oh My Zsh wiring changed its commit"
(
    install_pinned_git_checkout() {
        printf '%s\n' "$@" > "$CAPTURE_ROOT/p10k"
    }
    install_p10k
)
sed -n '3p' "$CAPTURE_ROOT/p10k" |
    grep -qxF "$DOTFILES_P10K_COMMIT" ||
    fail "Powerlevel10k wiring changed its commit"
(
    install_pinned_git_checkout() {
        printf '%s\n' "$@" > "$CAPTURE_ROOT/tpm"
    }
    install_tpm
)
sed -n '3p' "$CAPTURE_ROOT/tpm" |
    grep -qxF "$DOTFILES_TPM_COMMIT" ||
    fail "TPM bootstrap wiring changed its commit"

# Font publication verifies every byte, refuses an existing foreign file, and
# does not redownload an already verified set.
FONT_FIXTURES="$TEST_ROOT/font-fixtures"
FONT_DOWNLOAD_LOG="$TEST_ROOT/font-downloads"
mkdir "$FONT_FIXTURES"
printf 'font-a\n' > "$FONT_FIXTURES/Test Regular.ttf"
printf 'font-b\n' > "$FONT_FIXTURES/Test Bold.ttf"
FONT_NAMES=("Test Regular.ttf" "Test Bold.ttf")
FONT_SHA256=(
    "$(sha256sum "$FONT_FIXTURES/Test Regular.ttf" | awk '{print $1}')"
    "$(sha256sum "$FONT_FIXTURES/Test Bold.ttf" | awk '{print $1}')"
)
DOTFILES_FONT_MEDIA_COMMIT="fixture-font-commit"
download() {
    local url="$1"
    local output="$2"
    case "$url" in
        *"/$DOTFILES_FONT_MEDIA_COMMIT/"*) ;;
        *) fail "font download omitted its reviewed media commit" ;;
    esac
    printf '%s\n' "$url" >> "$FONT_DOWNLOAD_LOG"
    cp "$FONT_FIXTURES/$(basename "$output")" "$output"
}
FAKE_COMMANDS="$TEST_ROOT/fake-commands"
mkdir "$FAKE_COMMANDS"
printf '%s\n' '#!/bin/bash' 'exit 0' > "$FAKE_COMMANDS/fc-cache"
chmod 0755 "$FAKE_COMMANDS/fc-cache"
PATH="$FAKE_COMMANDS:$PATH" install_fonts
[ "$(wc -l < "$FONT_DOWNLOAD_LOG")" -eq 2 ] ||
    fail "font installer did not fetch the exact missing set"
for font_index in "${!FONT_NAMES[@]}"; do
    verify_sha256 \
        "$HOME/.local/share/fonts/${FONT_NAMES[$font_index]}" \
        "${FONT_SHA256[$font_index]}" ||
        fail "published font failed digest verification"
done
download() {
    fail "verified font set attempted network access"
}
PATH="$FAKE_COMMANDS:$PATH" install_fonts
printf 'foreign\n' > "$HOME/.local/share/fonts/Test Bold.ttf"
if PATH="$FAKE_COMMANDS:$PATH" install_fonts >/dev/null 2>&1; then
    fail "font installer accepted an existing unverified font"
fi
grep -qx foreign "$HOME/.local/share/fonts/Test Bold.ttf" ||
    fail "font refusal overwrote the foreign file"

# The bootstrap delegates NVM migration to its hardened installer and selects
# an exact Node version for install, alias, use, and final runtime validation.
NVM_INSTALLER_LOG="$TEST_ROOT/nvm-installer-log"
NVM_CALL_LOG="$TEST_ROOT/nvm-call-log"
export NVM_INSTALLER_LOG NVM_CALL_LOG
FAKE_NVM_INSTALLER="$TEST_ROOT/fake-nvm-installer"
# shellcheck disable=SC2016  # The generated script expands this at runtime.
printf '%s\n' \
    '#!/bin/bash' \
    'printf "%s\n" "$*" > "$NVM_INSTALLER_LOG"' \
    > "$FAKE_NVM_INSTALLER"
mkdir -p "$HOME/.nvm"
cat > "$HOME/.nvm/nvm.sh" << 'EOF'
nvm() {
    printf '%s\n' "$*" >> "$NVM_CALL_LOG"
}
node() {
    printf 'v%s\n' "$DOTFILES_NODE_VERSION"
}
npm() {
    printf '11.6.4\n'
}
EOF
(
    NVM_INSTALLER="$FAKE_NVM_INSTALLER"
    install_nvm
)
grep -qx -- '--migrate' "$NVM_INSTALLER_LOG" ||
    fail "NVM bootstrap did not request safe legacy migration"
grep -qxF "install $DOTFILES_NODE_VERSION" "$NVM_CALL_LOG" ||
    fail "NVM bootstrap used a floating Node install"
grep -qxF "alias default $DOTFILES_NODE_VERSION" "$NVM_CALL_LOG" ||
    fail "NVM bootstrap used a floating default alias"
grep -qxF "use --silent $DOTFILES_NODE_VERSION" "$NVM_CALL_LOG" ||
    fail "NVM bootstrap did not activate the exact Node version"

# The Tier 1 smoketest establishes the managed default itself. It must not
# inherit node, npm, NVM_DIR, or shell startup side effects from its caller.
NVM_SMOKETEST_OUTPUT="$TEST_ROOT/nvm-smoketest-output"
/usr/bin/env -i \
    HOME="$HOME" \
    PATH="/no-ambient-node" \
    NVM_CALL_LOG="$NVM_CALL_LOG" \
    DOTFILES_NODE_VERSION="$DOTFILES_NODE_VERSION" \
    /bin/bash "$DOTFILES/tools/nvm/smoketest.sh" \
    > "$NVM_SMOKETEST_OUTPUT" ||
    fail "NVM smoketest depended on ambient shell initialization"
grep -qxF 'use --silent default' "$NVM_CALL_LOG" ||
    fail "NVM smoketest did not activate the managed default"
grep -qxF "  node: v$DOTFILES_NODE_VERSION" "$NVM_SMOKETEST_OUTPUT" ||
    fail "NVM smoketest did not report the managed Node runtime"
grep -qxF '  npm: v11.6.4' "$NVM_SMOKETEST_OUTPUT" ||
    fail "NVM smoketest did not report the managed npm runtime"

# The multiplexer path installs pinned TPM, updates only sibling plugins, and
# refuses dirty plugin state before invoking any updater.
# shellcheck source=../lib/multiplexer-plugins.sh
source "$DOTFILES/lib/multiplexer-plugins.sh"
TPM_SOURCE="$TEST_ROOT/tpm-source"
git_fixture "$TPM_SOURCE"
mkdir -p "$TPM_SOURCE/bin"
cat > "$TPM_SOURCE/bin/install_plugins" << 'EOF'
#!/bin/bash
printf 'install|%s\n' "$TMUX_PLUGIN_MANAGER_PATH" >> "$MULTIPLEXER_PLUGIN_LOG"
EOF
cat > "$TPM_SOURCE/bin/update_plugins" << 'EOF'
#!/bin/bash
printf 'update|%s|%s\n' \
    "$TMUX_PLUGIN_MANAGER_PATH" "$*" >> "$MULTIPLEXER_PLUGIN_LOG"
case " $* " in
    *" tpm "*) exit 91 ;;
esac
EOF
chmod 0755 \
    "$TPM_SOURCE/bin/install_plugins" \
    "$TPM_SOURCE/bin/update_plugins"
git -C "$TPM_SOURCE" add bin
git -C "$TPM_SOURCE" commit --quiet -m tpm
TPM_FIXTURE_COMMIT=$(git -C "$TPM_SOURCE" rev-parse HEAD)
PLUGIN_ROOT="$TEST_ROOT/plugins"
ALPHA_PLUGIN="$PLUGIN_ROOT/alpha"
git_fixture "$ALPHA_PLUGIN"
printf 'alpha\n' > "$ALPHA_PLUGIN/plugin"
git -C "$ALPHA_PLUGIN" add plugin
git -C "$ALPHA_PLUGIN" commit --quiet -m alpha
MULTIPLEXER_PLUGIN_LOG="$TEST_ROOT/plugin-log"
export MULTIPLEXER_PLUGIN_LOG
update_multiplexer_plugins \
    "$PLUGIN_ROOT" "file://$TPM_SOURCE" "$TPM_FIXTURE_COMMIT"
pinned_git_checkout_valid \
    "$PLUGIN_ROOT/tpm" "file://$TPM_SOURCE" "$TPM_FIXTURE_COMMIT" ||
    fail "multiplexer updater did not retain pinned TPM"
STAGED_PLUGIN_ROOT=$(
    sed -n 's/^install|\(.*\)$/\1/p' "$MULTIPLEXER_PLUGIN_LOG"
)
case "$STAGED_PLUGIN_ROOT" in
    "$TEST_ROOT"/.dotfiles-stage-plugins.*/payload/) ;;
    *) fail "multiplexer updater did not stage TPM installation off-path" ;;
esac
grep -qxF "update|$STAGED_PLUGIN_ROOT|alpha" "$MULTIPLEXER_PLUGIN_LOG" ||
    fail "multiplexer updater did not update the staged sibling plugin"
if grep -q 'update|.*|.*tpm' "$MULTIPLEXER_PLUGIN_LOG"; then
    fail "multiplexer updater allowed TPM to update itself"
fi
PLUGIN_LOG_LINES=$(wc -l < "$MULTIPLEXER_PLUGIN_LOG")
printf 'dirty\n' >> "$ALPHA_PLUGIN/plugin"
if update_multiplexer_plugins \
        "$PLUGIN_ROOT" "file://$TPM_SOURCE" "$TPM_FIXTURE_COMMIT" \
        >/dev/null 2>&1; then
    fail "multiplexer updater accepted dirty plugin state"
fi
[ "$(wc -l < "$MULTIPLEXER_PLUGIN_LOG")" -eq "$PLUGIN_LOG_LINES" ] ||
    fail "dirty-plugin refusal executed an updater"

if bash "$DOTFILES/install-deps.sh" unexpected >/dev/null 2>&1; then
    fail "install-deps accepted an unknown argument"
fi
if rg -n 'git[[:space:]].*pull|/master/|update_plugins[[:space:]]+all' \
        "$DOTFILES/install-deps.sh" \
        "$DOTFILES/bin/update-multiplexer" \
        "$DOTFILES/lib/multiplexer-plugins.sh" >/dev/null; then
    fail "bootstrap retained a mutable Git or TPM-self-update path"
fi

echo "pinned shell bootstrap passed"
