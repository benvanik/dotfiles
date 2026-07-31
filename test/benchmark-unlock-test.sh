#!/bin/bash
# Focused coverage for restoring the historical root-owned lock state.

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

wait_for_path() {
    local path="$1"
    local process_id="$2"
    local description="$3"

    while [ ! -e "$path" ]; do
        if ! kill -0 "$process_id" 2>/dev/null; then
            fail "$description exited before publishing $path"
        fi
        sleep 0.01
    done
}

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

# Concurrent lockers share one guard across the complete snapshot and mutation
# window. Hold the first process in its first privileged write, then prove the
# second has opened the same guard but cannot pass it until the committed state
# makes its re-entry fail.
CONCURRENT_STATE_DIR="$TEST_ROOT/concurrent-state"
CONCURRENT_GUARD="$TEST_ROOT/concurrent.guard"
FIRST_WRITE_READY="$TEST_ROOT/first-write-ready"
FIRST_WRITE_RELEASE="$TEST_ROOT/first-write-release"
cat > "$FAKE_BIN/sudo" << EOF
#!/bin/bash
if [ "\${1:-}" = "-v" ]; then
    exit 0
fi
if [ "\${1:-}" = "tee" ]; then
    IFS= read -r value
    if [ ! -e "$FIRST_WRITE_READY" ]; then
        : > "$FIRST_WRITE_READY"
        while [ ! -e "$FIRST_WRITE_RELEASE" ]; do
            sleep 0.01
        done
    fi
    printf '%s\\n' "\$value"
    exit 0
fi
exec "\$@"
EOF
chmod +x "$FAKE_BIN/sudo"

PATH="$FAKE_BIN:$PATH" \
BENCHMARK_LOCK_STATE_DIR="$CONCURRENT_STATE_DIR" \
BENCHMARK_LOCK_GUARD_FILE="$CONCURRENT_GUARD" \
    "$DOTFILES/bin/benchmark-lock" >/dev/null 2>&1 &
FIRST_PID=$!
wait_for_path "$FIRST_WRITE_READY" "$FIRST_PID" "first benchmark lock"

PATH="$FAKE_BIN:$PATH" \
BENCHMARK_LOCK_STATE_DIR="$CONCURRENT_STATE_DIR" \
BENCHMARK_LOCK_GUARD_FILE="$CONCURRENT_GUARD" \
    "$DOTFILES/bin/benchmark-lock" >/dev/null 2>&1 &
SECOND_PID=$!

# File descriptor 8 is the shared held/waiting guard in both commands. Seeing
# the second descriptor proves it reached the forced overlap before release.
wait_for_path "/proc/$SECOND_PID/fd/8" "$SECOND_PID" "second benchmark lock"
[ "$(readlink "/proc/$SECOND_PID/fd/8")" = "$CONCURRENT_GUARD" ] ||
    fail "second benchmark lock opened a different guard"
[ -d "/proc/$SECOND_PID" ] ||
    fail "second benchmark lock passed the held transaction guard"

: > "$FIRST_WRITE_RELEASE"
FIRST_EXIT=0
SECOND_EXIT=0
wait "$FIRST_PID" || FIRST_EXIT=$?
wait "$SECOND_PID" || SECOND_EXIT=$?
[ "$FIRST_EXIT" -eq 0 ] ||
    fail "first benchmark lock failed"
[ "$SECOND_EXIT" -ne 0 ] ||
    fail "concurrent benchmark lock overwrote the active baseline"
[ -s "$CONCURRENT_STATE_DIR/state.tsv" ] ||
    fail "serialized benchmark lock did not retain its baseline"

echo "benchmark transaction recovery passed"
