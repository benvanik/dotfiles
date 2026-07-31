#!/bin/bash
# Focused coverage for restoring historical /tmp lock state.

set -euo pipefail

DOTFILES="${DOTFILES:-$HOME/.dotfiles}"
TEST_PARENT="${TMPDIR:-/tmp}"
TEST_ROOT=$(mktemp -d "$TEST_PARENT/dotfiles-benchmark-unlock-test.XXXXXX")
cleanup_test_root() {
    case "$TEST_ROOT" in
        "$TEST_PARENT"/dotfiles-benchmark-unlock-test.*) ;;
        *)
            echo "refusing unexpected test cleanup path: $TEST_ROOT" >&2
            return 1
            ;;
    esac
    [ ! -e "$TEST_ROOT" ] || find "$TEST_ROOT" -xdev -depth -delete
}
trap cleanup_test_root EXIT

fail() {
    echo "benchmark unlock test: $1" >&2
    exit 1
}

if [ ! -f "$DOTFILES/bin/benchmark-lock" ] ||
        [ ! -x "$DOTFILES/bin/benchmark-lock" ]; then
    fail "canonical repository benchmark-lock is not executable"
fi

FAKE_BIN="$TEST_ROOT/fake-bin"
CURRENT_STATE_DIR="$TEST_ROOT/current-state"
LEGACY_STATE_DIR="$TEST_ROOT/legacy-root-state"
KNOB="$TEST_ROOT/benchmark-knob"
mkdir -p "$FAKE_BIN" "$LEGACY_STATE_DIR"

cat > "$FAKE_BIN/sudo" << 'EOF'
#!/bin/bash
if [ "${1:-}" = "-v" ]; then
    exit 0
fi
if [ "${1:-}" = "tee" ] &&
        [ "${2:-}" = "/proc/sys/kernel/randomize_va_space" ]; then
    IFS= read -r value
    printf '%s\n' "$value" > "$BENCHMARK_TEST_KNOB"
    printf '%s\n' "$value"
    exit 0
fi
exec "$@"
EOF
chmod +x "$FAKE_BIN/sudo"

printf 'locked\n' > "$KNOB"
printf 'aslr\t/proc/sys/kernel/randomize_va_space\t1\n' \
    > "$LEGACY_STATE_DIR/state.tsv"

PATH="$FAKE_BIN:$PATH" \
BENCHMARK_TEST_KNOB="$KNOB" \
BENCHMARK_LOCK_STATE_DIR="$CURRENT_STATE_DIR" \
BENCHMARK_LOCK_LEGACY_STATE_DIR="$LEGACY_STATE_DIR" \
    "$DOTFILES/bin/benchmark-unlock" >/dev/null

[ "$(cat "$KNOB")" = "1" ] ||
    fail "legacy state did not restore the saved value"
[ ! -e "$LEGACY_STATE_DIR/state.tsv" ] ||
    fail "restored legacy state file was retained"
[ ! -e "$LEGACY_STATE_DIR" ] ||
    fail "empty legacy state directory was retained"
[ ! -e "$CURRENT_STATE_DIR" ] ||
    fail "unlock created or retained the absent current state directory"

# User-writable state cannot grant an arbitrary root write. Validation covers
# the complete file before the first sudo tee and retains rejected evidence.
MALICIOUS_STATE_DIR="$TEST_ROOT/malicious-state"
mkdir -p "$MALICIOUS_STATE_DIR"
printf 'aslr\t%s\tcompromised\n' "$KNOB" \
    > "$MALICIOUS_STATE_DIR/state.tsv"
printf 'untouched\n' > "$KNOB"
if PATH="$FAKE_BIN:$PATH" \
        BENCHMARK_TEST_KNOB="$KNOB" \
        BENCHMARK_LOCK_STATE_DIR="$MALICIOUS_STATE_DIR" \
        BENCHMARK_LOCK_LEGACY_STATE_DIR="$TEST_ROOT/no-legacy-state" \
        "$DOTFILES/bin/benchmark-unlock" >/dev/null 2>&1; then
    fail "benchmark unlock accepted an arbitrary privileged write path"
fi
[ "$(cat "$KNOB")" = "untouched" ] ||
    fail "rejected benchmark state changed its target"
[ -f "$MALICIOUS_STATE_DIR/state.tsv" ] ||
    fail "rejected benchmark state was discarded"

# Validation is a one-time authority boundary. Replace the user-owned state
# file during the first privileged write and prove later restores still use the
# already-parsed records rather than reopening the pathname.
SWAP_STATE_DIR="$TEST_ROOT/swap-state"
SWAP_STATE_FILE="$SWAP_STATE_DIR/state.tsv"
SWAP_MARKER="$TEST_ROOT/swap-performed"
ARBITRARY_TARGET="$TEST_ROOT/arbitrary-root-target"
mkdir -p "$SWAP_STATE_DIR"
printf 'aslr\t/proc/sys/kernel/randomize_va_space\t1\n' \
    > "$SWAP_STATE_FILE"
printf 'aslr\t/proc/sys/kernel/randomize_va_space\t2\n' \
    >> "$SWAP_STATE_FILE"
printf 'untouched\n' > "$ARBITRARY_TARGET"
cat > "$FAKE_BIN/sudo" << 'EOF'
#!/bin/bash
if [ "${1:-}" = "-v" ]; then
    exit 0
fi
if [ "${1:-}" = "tee" ]; then
    IFS= read -r value
    if [ ! -e "$BENCHMARK_SWAP_MARKER" ]; then
        printf 'aslr\t/proc/sys/kernel/randomize_va_space\t1\n' \
            > "$BENCHMARK_SWAP_STATE_FILE"
        printf 'aslr\t%s\tcompromised\n' "$BENCHMARK_ARBITRARY_TARGET" \
            >> "$BENCHMARK_SWAP_STATE_FILE"
        : > "$BENCHMARK_SWAP_MARKER"
    fi
    if [ "${2:-}" = "/proc/sys/kernel/randomize_va_space" ]; then
        printf '%s\n' "$value" > "$BENCHMARK_TEST_KNOB"
    elif [ "${2:-}" = "$BENCHMARK_ARBITRARY_TARGET" ]; then
        printf '%s\n' "$value" > "$BENCHMARK_ARBITRARY_TARGET"
    fi
    printf '%s\n' "$value"
    exit 0
fi
exec "$@"
EOF
chmod +x "$FAKE_BIN/sudo"
printf 'locked\n' > "$KNOB"
PATH="$FAKE_BIN:$PATH" \
BENCHMARK_TEST_KNOB="$KNOB" \
BENCHMARK_SWAP_MARKER="$SWAP_MARKER" \
BENCHMARK_SWAP_STATE_FILE="$SWAP_STATE_FILE" \
BENCHMARK_ARBITRARY_TARGET="$ARBITRARY_TARGET" \
BENCHMARK_LOCK_STATE_DIR="$SWAP_STATE_DIR" \
BENCHMARK_LOCK_LEGACY_STATE_DIR="$TEST_ROOT/no-legacy-state" \
    "$DOTFILES/bin/benchmark-unlock" >/dev/null
[ -e "$SWAP_MARKER" ] ||
    fail "state-swap fixture did not replace the validated pathname"
[ "$(cat "$KNOB")" = 2 ] ||
    fail "state replacement changed the already-parsed restore records"
[ "$(cat "$ARBITRARY_TARGET")" = untouched ] ||
    fail "unlock reopened swapped state as privileged write authority"

echo "benchmark legacy-state recovery passed"
