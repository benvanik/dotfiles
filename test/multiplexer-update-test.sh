#!/bin/bash
# Offline end-to-end fixture for the multiplexer build/update command.
set -euo pipefail

DOTFILES="${DOTFILES:-$HOME/.dotfiles}"
UPDATE_COMMAND="$DOTFILES/bin/update-multiplexer"
TEST_PARENT="${TMPDIR:-/tmp}"
TEST_ROOT=$(mktemp -d "$TEST_PARENT/dotfiles-multiplexer-update.XXXXXX")
cleanup_test_root() {
    case "$TEST_ROOT" in
        "$TEST_PARENT"/dotfiles-multiplexer-update.*) ;;
        *)
            echo "refusing unexpected test cleanup path: $TEST_ROOT" >&2
            return 1
            ;;
    esac
    [ ! -e "$TEST_ROOT" ] || find "$TEST_ROOT" -xdev -depth -delete
}
trap cleanup_test_root EXIT

fail() {
    echo "multiplexer update test: $1" >&2
    exit 1
}

file_sha256() {
    local output
    if command -v sha256sum >/dev/null 2>&1; then
        output=$(sha256sum "$1")
    else
        output=$(shasum -a 256 "$1")
    fi
    printf '%s\n' "${output%% *}"
}

git_fixture() {
    local directory="$1"

    mkdir -p "$directory"
    git -C "$directory" init --quiet
    git -C "$directory" config user.name fixture
    git -C "$directory" config user.email fixture@example.invalid
}

FIXTURE_DOTFILES="$TEST_ROOT/dotfiles"
FIXTURE_BIN="$TEST_ROOT/bin"
FIXTURE_HOME="$TEST_ROOT/home"
FIXTURE_PREFIX="$FIXTURE_HOME/.local"
FIXTURE_STATE="$TEST_ROOT/state"
FIXTURE_PROCESS_ROOT="$TEST_ROOT/process-root"
FIXTURE_PLUGIN_ROOT="$FIXTURE_HOME/.tmux/plugins"
mkdir -p \
    "$FIXTURE_DOTFILES/lib" \
    "$FIXTURE_DOTFILES/tools" \
    "$FIXTURE_BIN" \
    "$FIXTURE_HOME" \
    "$FIXTURE_PROCESS_ROOT"
cp "$DOTFILES/tools/install-utils.sh" "$FIXTURE_DOTFILES/tools/install-utils.sh"
cp "$DOTFILES/lib/managed-directory-publication.py" \
    "$FIXTURE_DOTFILES/lib/managed-directory-publication.py"
cp "$DOTFILES/lib/multiplexer-publication.py" \
    "$FIXTURE_DOTFILES/lib/multiplexer-publication.py"
cp "$DOTFILES/lib/multiplexer-plugins.sh" \
    "$FIXTURE_DOTFILES/lib/multiplexer-plugins.sh"
cp "$DOTFILES/tmux.conf" "$FIXTURE_DOTFILES/tmux.conf"

TMUX_FIXTURE_VERSION="9.8a"
BYOBU_FIXTURE_VERSION="7.65"

TMUX_SOURCE_ROOT="$TEST_ROOT/tmux-source/tmux-$TMUX_FIXTURE_VERSION"
mkdir -p "$TMUX_SOURCE_ROOT"
cat > "$TMUX_SOURCE_ROOT/tmux" << EOF
#!/bin/bash
for argument in "\$@"; do
    case "\$argument" in
        -V)
            printf 'tmux %s\\n' '$TMUX_FIXTURE_VERSION'
            exit 0
            ;;
        display-message)
            printf '%s\\n' '$TMUX_FIXTURE_VERSION'
            exit 0
            ;;
    esac
done
exit 0
EOF
cat > "$TMUX_SOURCE_ROOT/configure" << 'EOF'
#!/bin/bash
set -e
prefix=""
for argument in "$@"; do
    case "$argument" in
        --prefix=*) prefix="${argument#--prefix=}" ;;
    esac
done
[ -n "$prefix" ]
{
    printf 'all:\n\t@true\n'
    printf 'install:\n'
    printf '\tinstall -d "$(DESTDIR)%s/bin"\n' "$prefix"
    printf '\tinstall -m 0755 tmux "$(DESTDIR)%s/bin/tmux"\n' "$prefix"
} > Makefile
EOF
chmod 0755 "$TMUX_SOURCE_ROOT/tmux" "$TMUX_SOURCE_ROOT/configure"
TMUX_ARCHIVE="$TEST_ROOT/tmux-$TMUX_FIXTURE_VERSION.tar.gz"
tar czf "$TMUX_ARCHIVE" \
    -C "$TEST_ROOT/tmux-source" "tmux-$TMUX_FIXTURE_VERSION"
TMUX_ARCHIVE_SHA256=$(file_sha256 "$TMUX_ARCHIVE")

BYOBU_SOURCE="$TEST_ROOT/byobu-source"
git_fixture "$BYOBU_SOURCE"
mkdir -p \
    "$BYOBU_SOURCE/bin" \
    "$BYOBU_SOURCE/etc/byobu" \
    "$BYOBU_SOURCE/lib/byobu" \
    "$BYOBU_SOURCE/share/byobu" \
    "$BYOBU_SOURCE/share/doc/byobu"
cat > "$BYOBU_SOURCE/bin/byobu" << EOF
#!/bin/bash
if [ "\${1:-}" = "--version" ]; then
    printf 'byobu version %s\\n' '$BYOBU_FIXTURE_VERSION'
fi
exit 0
EOF
printf '#!/bin/sh\nexit 0\n' > "$BYOBU_SOURCE/bin/byobu-helper"
printf 'fixture\n' > "$BYOBU_SOURCE/etc/byobu/state"
printf 'fixture\n' > "$BYOBU_SOURCE/lib/byobu/state"
printf 'fixture\n' > "$BYOBU_SOURCE/share/byobu/state"
printf 'fixture\n' > "$BYOBU_SOURCE/share/doc/byobu/state"
cat > "$BYOBU_SOURCE/autogen.sh" << 'EOF'
#!/bin/sh
exit 0
EOF
cat > "$BYOBU_SOURCE/configure" << 'EOF'
#!/bin/bash
set -e
prefix=""
for argument in "$@"; do
    case "$argument" in
        --prefix=*) prefix="${argument#--prefix=}" ;;
    esac
done
[ -n "$prefix" ]
{
    printf 'all:\n\t@true\n'
    printf 'install:\n'
    printf '\tinstall -d "$(DESTDIR)%s/bin"\n' "$prefix"
    printf '\tinstall -m 0755 bin/byobu bin/byobu-helper "$(DESTDIR)%s/bin/"\n' \
        "$prefix"
    for path in etc/byobu lib/byobu share/byobu share/doc/byobu; do
        printf '\tinstall -d "$(DESTDIR)%s/%s"\n' "$prefix" "$path"
        printf '\tcp -a %s/. "$(DESTDIR)%s/%s/"\n' \
            "$path" "$prefix" "$path"
    done
} > Makefile
EOF
chmod 0755 \
    "$BYOBU_SOURCE/bin/byobu" \
    "$BYOBU_SOURCE/bin/byobu-helper" \
    "$BYOBU_SOURCE/autogen.sh" \
    "$BYOBU_SOURCE/configure"
git -C "$BYOBU_SOURCE" add .
git -C "$BYOBU_SOURCE" commit --quiet -m byobu
BYOBU_COMMIT=$(git -C "$BYOBU_SOURCE" rev-parse HEAD)

TPM_SOURCE="$TEST_ROOT/tpm-source"
git_fixture "$TPM_SOURCE"
mkdir -p "$TPM_SOURCE/bin"
printf '#!/bin/sh\nexit 0\n' > "$TPM_SOURCE/bin/install_plugins"
printf '#!/bin/sh\nexit 0\n' > "$TPM_SOURCE/bin/update_plugins"
chmod 0755 "$TPM_SOURCE/bin/install_plugins" "$TPM_SOURCE/bin/update_plugins"
git -C "$TPM_SOURCE" add .
git -C "$TPM_SOURCE" commit --quiet -m tpm
TPM_COMMIT=$(git -C "$TPM_SOURCE" rev-parse HEAD)

cat > "$FIXTURE_DOTFILES/lib/bootstrap-pins.sh" << EOF
#!/bin/bash
DOTFILES_TPM_ORIGIN="file://$TPM_SOURCE"
DOTFILES_TPM_COMMIT="$TPM_COMMIT"
DOTFILES_TMUX_VERSION="$TMUX_FIXTURE_VERSION"
DOTFILES_TMUX_ARCHIVE_SHA256="$TMUX_ARCHIVE_SHA256"
DOTFILES_BYOBU_ORIGIN="file://$BYOBU_SOURCE"
DOTFILES_BYOBU_VERSION="$BYOBU_FIXTURE_VERSION"
DOTFILES_BYOBU_COMMIT="$BYOBU_COMMIT"
EOF

TMUX_FIXTURE_ARCHIVE="$TMUX_ARCHIVE"
export TMUX_FIXTURE_ARCHIVE TMUX_FIXTURE_VERSION
cat > "$FIXTURE_BIN/curl" << 'EOF'
#!/bin/bash
set -e
output=""
url=""
while [ $# -gt 0 ]; do
    case "$1" in
        -o|--output)
            shift
            output="$1"
            ;;
        -*)
            ;;
        *)
            url="$1"
            ;;
    esac
    shift
done
if [ "${MULTIPLEXER_FIXTURE_NETWORK_FORBIDDEN:-false}" = "true" ]; then
    echo "fixture network access was forbidden: $url" >&2
    exit 72
fi
case "$url" in
    */tmux-"$TMUX_FIXTURE_VERSION".tar.gz)
        cp "$TMUX_FIXTURE_ARCHIVE" "$output"
        ;;
    *)
        echo "unexpected multiplexer fixture URL: $url" >&2
        exit 73
        ;;
esac
EOF
cat > "$FIXTURE_BIN/pkg-config" << 'EOF'
#!/bin/bash
if [ "${1:-}" != "--exists" ]; then
    exit 2
fi
case "${2:-}" in
    libevent|ncurses) exit 0 ;;
    *) exit 1 ;;
esac
EOF
chmod 0755 "$FIXTURE_BIN/curl" "$FIXTURE_BIN/pkg-config"

run_update() {
    PATH="$FIXTURE_BIN:$PATH" \
        HOME="$FIXTURE_HOME" \
        DOTFILES="$FIXTURE_DOTFILES" \
        MULTIPLEXER_INSTALL_PREFIX="$FIXTURE_PREFIX" \
        MULTIPLEXER_STATE_ROOT="$FIXTURE_STATE" \
        TMUX_PLUGIN_MANAGER_PATH="$FIXTURE_PLUGIN_ROOT" \
        DOTFILES_MULTIPLEXER_TEST_PROCESS_ROOT="$FIXTURE_PROCESS_ROOT" \
        MULTIPLEXER_FIXTURE_NETWORK_FORBIDDEN="${MULTIPLEXER_FIXTURE_NETWORK_FORBIDDEN:-false}" \
        bash "$UPDATE_COMMAND" "$@"
}

OUTPUT="$TEST_ROOT/output"
run_update > "$OUTPUT" 2>&1 ||
    {
        cat "$OUTPUT" >&2
        fail "offline multiplexer stack did not build and activate"
    }
[ "$("$FIXTURE_PREFIX/bin/tmux" -V)" = "tmux $TMUX_FIXTURE_VERSION" ] ||
    fail "end-to-end update did not project the built tmux"
case "$(readlink "$FIXTURE_STATE/current")" in
    generations/*) ;;
    *) fail "end-to-end update did not publish a generation selector" ;;
esac
[ -L "$FIXTURE_PREFIX/etc/byobu" ] ||
    fail "end-to-end update did not project Byobu resources"
[ -d "$FIXTURE_PLUGIN_ROOT/tpm/.git" ] ||
    fail "end-to-end update did not publish pinned TPM"
GENERATION_DIRECTORY=$(
    readlink -f "$FIXTURE_STATE/current"
)
grep -qx "tmux_archive_sha256=$TMUX_ARCHIVE_SHA256" \
    "$GENERATION_DIRECTORY/.dotfiles-install-identity" ||
    fail "generation identity did not retain the tmux attestation"
grep -qx "byobu_commit=$BYOBU_COMMIT" \
    "$GENERATION_DIRECTORY/.dotfiles-install-identity" ||
    fail "generation identity did not retain the Byobu commit"

MULTIPLEXER_FIXTURE_NETWORK_FORBIDDEN=true run_update > "$OUTPUT" 2>&1 ||
    fail "verified multiplexer generation was not reusable offline"

printf 'tampered\n' > "$GENERATION_DIRECTORY/bin/tmux"
if MULTIPLEXER_FIXTURE_NETWORK_FORBIDDEN=true run_update \
        > "$OUTPUT" 2>&1; then
    fail "tampered generation was silently reused"
fi
grep -q 'no valid recorded identity' "$OUTPUT" ||
    fail "tampered generation failed after attempting a rebuild"

run_update --force > "$OUTPUT" 2>&1 ||
    {
        cat "$OUTPUT" >&2
        fail "forced managed-generation rebuild failed"
    }
[ "$("$FIXTURE_PREFIX/bin/tmux" -V)" = "tmux $TMUX_FIXTURE_VERSION" ] ||
    fail "forced rebuild did not restore the attested stack"

echo "multiplexer update production path passed"
