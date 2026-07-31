#!/bin/bash
# Offline production-path tests for atomic multiplexer activation.
set -euo pipefail

DOTFILES="${DOTFILES:-$HOME/.dotfiles}"
TEST_PARENT="${TMPDIR:-/tmp}"
TEST_ROOT=$(mktemp -d "$TEST_PARENT/dotfiles-multiplexer-publication.XXXXXX")
cleanup_test_root() {
    case "$TEST_ROOT" in
        "$TEST_PARENT"/dotfiles-multiplexer-publication.*) ;;
        *)
            echo "refusing unexpected test cleanup path: $TEST_ROOT" >&2
            return 1
            ;;
    esac
    [ ! -e "$TEST_ROOT" ] || find "$TEST_ROOT" -xdev -depth -delete
}
trap cleanup_test_root EXIT

fail() {
    echo "multiplexer publication test: $1" >&2
    exit 1
}

PUBLICATION="$DOTFILES/lib/multiplexer-publication.py"

make_generation() {
    local state_root="$1"
    local generation="$2"
    local marker="$3"
    local root="$state_root/generations/$generation"

    mkdir -p \
        "$root/bin" \
        "$root/etc/byobu" \
        "$root/lib/byobu" \
        "$root/share/byobu" \
        "$root/share/doc/byobu"
    printf '#!/bin/sh\nprintf "%s\\n"\n' "$marker-tmux" > "$root/bin/tmux"
    printf '#!/bin/sh\nprintf "%s\\n"\n' "$marker-byobu" > "$root/bin/byobu"
    printf '#!/bin/sh\nprintf "%s\\n"\n' "$marker-helper" \
        > "$root/bin/byobu-helper"
    chmod 0755 "$root/bin/tmux" "$root/bin/byobu" "$root/bin/byobu-helper"
    printf '%s\n' "$marker-resource" > "$root/etc/byobu/state"
}

activate() {
    local state_root="$1"
    local prefix="$2"
    local generation="$3"
    shift 3

    PYTHONDONTWRITEBYTECODE=1 python3 "$PUBLICATION" \
        --state-root "$state_root" \
        --install-prefix "$prefix" \
        --generation "$generation" \
        "$@"
}

STATE_ROOT="$TEST_ROOT/state"
PREFIX="$TEST_ROOT/prefix"
mkdir -p "$STATE_ROOT/generations" "$PREFIX"
make_generation "$STATE_ROOT" generation-1 first
activate "$STATE_ROOT" "$PREFIX" generation-1
[ "$(readlink "$STATE_ROOT/current")" = "generations/generation-1" ] ||
    fail "first activation did not publish its selector"
[ "$(readlink "$PREFIX/bin/tmux")" = "$STATE_ROOT/current/bin/tmux" ] ||
    fail "first activation did not publish a stable command projection"
[ "$("$PREFIX/bin/tmux")" = first-tmux ] ||
    fail "first command projection did not resolve through current"
[ "$(cat "$PREFIX/etc/byobu/state")" = first-resource ] ||
    fail "first resource projection did not resolve through current"

make_generation "$STATE_ROOT" generation-2 second
activate "$STATE_ROOT" "$PREFIX" generation-2
[ "$(readlink "$STATE_ROOT/current")" = "generations/generation-2" ] ||
    fail "upgrade did not atomically switch the selector"
[ "$("$PREFIX/bin/tmux")" = second-tmux ] ||
    fail "stable command projection did not follow the upgraded selector"

# A legacy in-place stack is foreign until the caller explicitly authorizes a
# preserved migration.
MIGRATION_STATE="$TEST_ROOT/migration-state"
MIGRATION_PREFIX="$TEST_ROOT/migration-prefix"
mkdir -p "$MIGRATION_STATE/generations" "$MIGRATION_PREFIX/bin"
make_generation "$MIGRATION_STATE" migration-1 managed
printf 'legacy tmux\n' > "$MIGRATION_PREFIX/bin/tmux"
printf 'legacy byobu\n' > "$MIGRATION_PREFIX/bin/byobu"
if activate "$MIGRATION_STATE" "$MIGRATION_PREFIX" migration-1 \
        >/dev/null 2>&1; then
    fail "legacy prefix paths were replaced without --force"
fi
grep -qx 'legacy tmux' "$MIGRATION_PREFIX/bin/tmux" ||
    fail "refused migration changed legacy tmux"
grep -qx 'legacy byobu' "$MIGRATION_PREFIX/bin/byobu" ||
    fail "refused migration changed legacy Byobu"

# A hard crash after a legacy path moves is replayed before the next request.
if DOTFILES_MULTIPLEXER_TEST_FAULT=hard-crash-after-collision-move \
        activate "$MIGRATION_STATE" "$MIGRATION_PREFIX" migration-1 --force \
        >/dev/null 2>&1; then
    fail "hard-crash fixture returned success"
fi
[ -f "$MIGRATION_STATE/activation-transaction.json" ] ||
    fail "hard-crash fixture did not retain a durable migration journal"
if activate "$MIGRATION_STATE" "$MIGRATION_PREFIX" migration-1 \
        >/dev/null 2>&1; then
    fail "recovered legacy paths stopped requiring explicit migration"
fi
[ ! -e "$MIGRATION_STATE/activation-transaction.json" ] ||
    fail "legacy rollback retained its migration journal"
grep -qx 'legacy tmux' "$MIGRATION_PREFIX/bin/tmux" ||
    fail "hard-crash rollback did not restore legacy tmux"
grep -qx 'legacy byobu' "$MIGRATION_PREFIX/bin/byobu" ||
    fail "hard-crash rollback did not restore legacy Byobu"

MIGRATION_OUTPUT="$TEST_ROOT/migration-output"
activate "$MIGRATION_STATE" "$MIGRATION_PREFIX" migration-1 --force \
    > "$MIGRATION_OUTPUT"
grep -q '^preserved legacy multiplexer paths at ' "$MIGRATION_OUTPUT" ||
    fail "authorized migration did not report its preserved backup"
[ "$("$MIGRATION_PREFIX/bin/tmux")" = managed-tmux ] ||
    fail "authorized migration did not activate managed tmux"
find "$MIGRATION_STATE/backups" -path '*/bin/tmux' -type f -print -quit |
    grep -q . ||
    fail "authorized migration did not preserve legacy tmux"
find "$MIGRATION_STATE/backups" -path '*/bin/byobu' -type f -print -quit |
    grep -q . ||
    fail "authorized migration did not preserve legacy Byobu"

# The selector rename is the commit point. A crash immediately afterward must
# finish the committed generation, not roll it back.
make_generation "$MIGRATION_STATE" migration-2 committed
if DOTFILES_MULTIPLEXER_TEST_FAULT=hard-crash-after-selector \
        activate "$MIGRATION_STATE" "$MIGRATION_PREFIX" migration-2 \
        >/dev/null 2>&1; then
    fail "post-selector hard-crash fixture returned success"
fi
[ "$(readlink "$MIGRATION_STATE/current")" = "generations/migration-2" ] ||
    fail "post-selector crash did not reach the commit point"
[ -f "$MIGRATION_STATE/activation-transaction.json" ] ||
    fail "post-selector crash did not retain its recovery journal"
activate "$MIGRATION_STATE" "$MIGRATION_PREFIX" migration-2
[ ! -e "$MIGRATION_STATE/activation-transaction.json" ] ||
    fail "committed replay retained its recovery journal"
[ "$("$MIGRATION_PREFIX/bin/tmux")" = committed-tmux ] ||
    fail "committed replay did not retain the new generation"

# A projection changed behind the ownership manifest fails closed.
unlink "$MIGRATION_PREFIX/bin/tmux"
printf 'foreign\n' > "$MIGRATION_PREFIX/bin/tmux"
if activate "$MIGRATION_STATE" "$MIGRATION_PREFIX" migration-2 \
        >/dev/null 2>&1; then
    fail "changed managed projection was silently replaced"
fi
grep -qx foreign "$MIGRATION_PREFIX/bin/tmux" ||
    fail "projection refusal changed the foreign path"

# Generation members may not resolve outside their immutable generation.
ESCAPE_STATE="$TEST_ROOT/escape-state"
ESCAPE_PREFIX="$TEST_ROOT/escape-prefix"
mkdir -p "$ESCAPE_STATE/generations/escape/bin" "$ESCAPE_PREFIX"
ln -s /bin/true "$ESCAPE_STATE/generations/escape/bin/tmux"
printf '#!/bin/sh\nexit 0\n' > "$ESCAPE_STATE/generations/escape/bin/byobu"
chmod 0755 "$ESCAPE_STATE/generations/escape/bin/byobu"
if activate "$ESCAPE_STATE" "$ESCAPE_PREFIX" escape >/dev/null 2>&1; then
    fail "generation accepted an executable symlink outside its root"
fi

echo "multiplexer publication safety passed"
