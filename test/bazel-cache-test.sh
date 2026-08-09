#!/bin/bash
# Behavioral coverage for machine-local Bazel placement and HOME protection.

set -euo pipefail

DOTFILES="${DOTFILES:-$HOME/.dotfiles}"
TEST_PARENT="${TMPDIR:-/tmp}"
TEST_ROOT=$(mktemp -d "$TEST_PARENT/dotfiles-bazel-cache-test.XXXXXX")
cleanup_test_root() {
    case "$TEST_ROOT" in
        "$TEST_PARENT"/dotfiles-bazel-cache-test.*) ;;
        *)
            echo "refusing unexpected test cleanup path: $TEST_ROOT" >&2
            return 1
            ;;
    esac
    [ ! -e "$TEST_ROOT" ] || find "$TEST_ROOT" -xdev -type d -exec chmod u+w {} +
    [ ! -e "$TEST_ROOT" ] || find "$TEST_ROOT" -xdev -depth -delete
}
trap cleanup_test_root EXIT

fail() {
    echo "bazel cache test: $1" >&2
    exit 1
}

info() { :; }
warn() { :; }
error() { printf '%s\n' "$1" >&2; }

# shellcheck source=../lib/home-publication-transaction.sh
. "$DOTFILES/lib/home-publication-transaction.sh"
# shellcheck source=../lib/local-config-publication.sh
. "$DOTFILES/lib/local-config-publication.sh"
# shellcheck source=../lib/bazel-cache.sh
. "$DOTFILES/lib/bazel-cache.sh"

TEST_HOME="$TEST_ROOT/home"
CACHE_ROOT="$TEST_ROOT/storage/bazel"
SECOND_CACHE_ROOT="$TEST_ROOT/second-storage/bazel"
mkdir -p "$TEST_HOME" "$CACHE_ROOT" "$SECOND_CACHE_ROOT"

HOME="$TEST_HOME"
USER=tester
XDG_CACHE_HOME="$TEST_HOME/.cache"
export HOME USER XDG_CACHE_HOME

printf 'export BAZEL_CACHE_ROOT=%q\n' "$CACHE_ROOT" > "$TEST_HOME/.shrc.local"
[ "$(bazel_cache_configured_root)" = "$CACHE_ROOT" ] ||
    fail "cache root was not read from machine configuration"

bazel_cache_configure "$CACHE_ROOT"
EXPECTED_OUTPUT_ROOT="$CACHE_ROOT/_bazel_tester"
DEFAULT_OUTPUT_ROOT="$TEST_HOME/.cache/bazel/_bazel_tester"
MARKER="$DEFAULT_OUTPUT_ROOT/$BAZEL_CACHE_GUARD_MARKER"

[ "$(cat "$TEST_HOME/.bazelrc")" = \
    "$(bazel_cache_print_home_bazelrc "$CACHE_ROOT")" ] ||
    fail "machine configuration does not contain the configured cache root"
grep -qxF 'try-import %workspace%/.bazelrc.cache' "$TEST_HOME/.bazelrc" ||
    fail "machine configuration does not reapply managed cache placement"
if grep -qF '.bazelrc.local' "$TEST_HOME/.bazelrc"; then
    fail "machine configuration reimports general workspace policy"
fi
[ -d "$EXPECTED_OUTPUT_ROOT/cache/disk" ] ||
    fail "configured disk-cache directory was not created"
[ ! -w "$DEFAULT_OUTPUT_ROOT" ] ||
    fail "default HOME output root remained writable"
[ "$(cat "$MARKER")" = "$(bazel_cache_print_guard_marker "$CACHE_ROOT")" ] ||
    fail "default-root marker does not explain the configured cache root"
[ "$(bazel_cache_default_root_state_count "$DEFAULT_OUTPUT_ROOT")" -eq 0 ] ||
    fail "guard marker was counted as stale Bazel state"
if mkdir "$DEFAULT_OUTPUT_ROOT/0123456789abcdef0123456789abcdef" 2>/dev/null; then
    fail "protected default root accepted a new output base"
fi

# Doctor-facing state accounting includes old shared caches and install bases,
# not only workspace-hash directories.
chmod 0755 "$DEFAULT_OUTPUT_ROOT"
mkdir "$DEFAULT_OUTPUT_ROOT/install"
chmod 0555 "$DEFAULT_OUTPUT_ROOT"
[ "$(bazel_cache_default_root_state_count "$DEFAULT_OUTPUT_ROOT")" -eq 1 ] ||
    fail "old install state was not detected under the guarded root"
chmod 0755 "$DEFAULT_OUTPUT_ROOT"
rmdir "$DEFAULT_OUTPUT_ROOT/install"
chmod 0555 "$DEFAULT_OUTPUT_ROOT"

# Reconfiguration is idempotent and can deliberately move the machine cache.
bazel_cache_configure "$CACHE_ROOT"
bazel_cache_configure "$SECOND_CACHE_ROOT"
[ "$(cat "$TEST_HOME/.bazelrc")" = \
    "$(bazel_cache_print_home_bazelrc "$SECOND_CACHE_ROOT")" ] ||
    fail "managed fallback did not follow a machine cache-root change"
[ "$(cat "$MARKER")" = \
    "$(bazel_cache_print_guard_marker "$SECOND_CACHE_ROOT")" ] ||
    fail "guard marker did not follow a machine cache-root change"

# A user-owned HOME rc is never overwritten merely to make placement pass.
FOREIGN_HOME="$TEST_ROOT/foreign-home"
FOREIGN_CACHE_ROOT="$TEST_ROOT/foreign-storage/bazel"
mkdir "$FOREIGN_HOME"
printf '%s\n' 'build --color=no' > "$FOREIGN_HOME/.bazelrc"
if HOME="$FOREIGN_HOME" XDG_CACHE_HOME="$FOREIGN_HOME/.cache" \
        bazel_cache_configure "$FOREIGN_CACHE_ROOT" >/dev/null 2>&1; then
    fail "foreign machine Bazel configuration was overwritten"
fi
grep -qxF 'build --color=no' "$FOREIGN_HOME/.bazelrc" ||
    fail "foreign machine Bazel configuration changed after refusal"
[ ! -e "$FOREIGN_CACHE_ROOT" ] ||
    fail "refused machine configuration still created cache state"

if bazel_cache_validate_root "$TEST_HOME/cache" >/dev/null 2>&1; then
    fail "cache placement inside HOME was accepted"
fi
if bazel_cache_validate_root relative/cache >/dev/null 2>&1; then
    fail "relative cache placement was accepted"
fi

echo "bazel cache placement passed"
