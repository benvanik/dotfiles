#!/bin/bash
# Managed agent-contract publication and drift detection.
# shellcheck disable=SC2030,SC2031,SC2317

set -euo pipefail

export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1

DOTFILES="${DOTFILES:-$HOME/.dotfiles}"
TEST_PARENT="${TMPDIR:-/tmp}"
TEST_ROOT=$(mktemp -d "$TEST_PARENT/dotfiles-agent-contract-test.XXXXXX")
BACKGROUND_PIDS=()
BACKGROUND_RELEASES=()
cleanup_test_root() {
    local release_path=""
    local background_pid=""
    for release_path in "${BACKGROUND_RELEASES[@]}"; do
        [ -z "$release_path" ] || : > "$release_path"
    done
    for background_pid in "${BACKGROUND_PIDS[@]}"; do
        [ -n "$background_pid" ] || continue
        if kill -0 "$background_pid" 2>/dev/null; then
            kill -TERM "$background_pid" 2>/dev/null || true
        fi
        wait "$background_pid" 2>/dev/null || true
    done
    case "$TEST_ROOT" in
        "$TEST_PARENT"/dotfiles-agent-contract-test.*) ;;
        *)
            echo "refusing unexpected test cleanup path: $TEST_ROOT" >&2
            return 1
            ;;
    esac
    [ ! -e "$TEST_ROOT" ] || find "$TEST_ROOT" -xdev -depth -delete
}
trap cleanup_test_root EXIT

fail() {
    echo "agent contract test: $1" >&2
    exit 1
}

wait_for_marker() {
    local marker_path="$1"
    local process_id="$2"
    local description="$3"
    local completion_path="${4:-}"

    while [ ! -e "$marker_path" ]; do
        if [ -n "$completion_path" ] && [ -e "$completion_path" ]; then
            fail "$description exited before reaching its readiness marker"
        fi
        if ! kill -0 "$process_id" 2>/dev/null; then
            fail "$description exited before reaching its readiness marker"
        fi
        sleep 0.01
    done
}

REMOTE="$TEST_ROOT/remote.git"
SEED="$TEST_ROOT/seed"
TEST_HOME="$TEST_ROOT/home"
CHECKOUT="$TEST_HOME/.dotfiles"
FAKE_BIN="$TEST_ROOT/fake-bin"
PUBLICATION_MODULES=(
    agent-contract-publication.sh
    home-publication-transaction.sh
    local-config-publication.sh
    managed-copy-publication.sh
    managed-link-publication.sh
)

git init -q --bare "$REMOTE"
git --git-dir="$REMOTE" symbolic-ref HEAD refs/heads/main
git init -q -b main "$SEED"
git -C "$SEED" config user.name "Dotfiles Test"
git -C "$SEED" config user.email "dotfiles-test@example.invalid"
git -C "$SEED" config commit.gpgsign false
mkdir -p "$SEED/agents" "$SEED/bin" "$SEED/lib"
cp "$DOTFILES/bin/dotfiles" "$SEED/bin/dotfiles"
for publication_module in "${PUBLICATION_MODULES[@]}"; do
    cp \
        "$DOTFILES/lib/$publication_module" \
        "$SEED/lib/$publication_module"
done
printf '%s\n' "contract-v1" > "$SEED/agents/WORKING_CONTRACT.md"
git -C "$SEED" add \
    agents/WORKING_CONTRACT.md \
    bin/dotfiles \
    lib
git -C "$SEED" commit -q -m "seed managed contract"
git -C "$SEED" remote add origin "$REMOTE"
git -C "$SEED" push -q -u origin main

mkdir -p "$TEST_HOME"
git clone -q "$REMOTE" "$CHECKOUT"
mkdir -p "$TEST_HOME/.claude" "$TEST_HOME/.codex"
mkdir -p "$FAKE_BIN"

# A command symlink resolves its implementation modules from the physical
# checkout while DOTFILES continues to select the managed content root.
SYMLINK_BIN="$TEST_ROOT/symlink-bin"
mkdir "$SYMLINK_BIN"
ln -s "../home/.dotfiles/bin/dotfiles" "$SYMLINK_BIN/dotfiles"
HOME="$TEST_HOME" DOTFILES="$CHECKOUT" \
    "$SYMLINK_BIN/dotfiles" help >/dev/null

system_date=$(command -v date)
cat > "$FAKE_BIN/date" << EOF
#!/bin/sh
if [ "\$#" -eq 1 ] && [ "\$1" = "+%Y%m%d-%H%M%S" ]; then
    printf '%s\n' '20000101-000000'
    exit 0
fi
exec '$system_date' "\$@"
EOF
chmod +x "$FAKE_BIN/date"
cp \
    "$CHECKOUT/agents/WORKING_CONTRACT.md" \
    "$TEST_HOME/.claude/CLAUDE.md"
cp \
    "$CHECKOUT/agents/WORKING_CONTRACT.md" \
    "$TEST_HOME/.codex/AGENTS.md"

printf '%s\n' "contract-v2" > "$SEED/agents/WORKING_CONTRACT.md"
git -C "$SEED" add agents/WORKING_CONTRACT.md
git -C "$SEED" commit -q -m "update managed contract"
git -C "$SEED" push -q

HOME="$TEST_HOME" DOTFILES="$CHECKOUT" PATH="$FAKE_BIN:$PATH" \
    "$CHECKOUT/bin/dotfiles" update >/dev/null

for managed_contract in \
        "$TEST_HOME/.claude/CLAUDE.md" \
        "$TEST_HOME/.codex/AGENTS.md"; do
    if [ ! -f "$managed_contract" ] || [ -L "$managed_contract" ]; then
        fail "update did not publish a regular file: $managed_contract"
    fi
    cmp -s "$CHECKOUT/agents/WORKING_CONTRACT.md" "$managed_contract" || \
        fail "update left a stale managed contract: $managed_contract"
done

# Publishing is useful even on a deliberately local-only branch.
git -C "$CHECKOUT" branch --unset-upstream
printf '%s\n' "local-only drift" > "$TEST_HOME/.claude/CLAUDE.md"
HOME="$TEST_HOME" DOTFILES="$CHECKOUT" PATH="$FAKE_BIN:$PATH" \
    "$CHECKOUT/bin/dotfiles" update >/dev/null
cmp -s \
    "$CHECKOUT/agents/WORKING_CONTRACT.md" \
    "$TEST_HOME/.claude/CLAUDE.md" ||
    fail "update without an upstream did not publish the local contract"
grep -RqxF \
    "contract-v1" "$TEST_HOME/.local/share/dotfiles/backups" ||
    fail "first managed contract backup was not preserved"
grep -RqxF \
    "local-only drift" "$TEST_HOME/.local/share/dotfiles/backups" ||
    fail "same-second managed contract backup was not preserved"

# Destination validation is a two-client preflight: either known-invalid path
# must stop publication before the other client's current contract changes.
unlink "$TEST_HOME/.claude/CLAUDE.md"
mkdir "$TEST_HOME/.claude/CLAUDE.md"
printf '%s\n' "Codex before failed publication" > "$TEST_HOME/.codex/AGENTS.md"
if HOME="$TEST_HOME" DOTFILES="$CHECKOUT" PATH="$FAKE_BIN:$PATH" \
        "$CHECKOUT/bin/dotfiles" update >/dev/null 2>&1; then
    fail "update accepted a non-file Claude destination"
fi
grep -qxF \
    "Codex before failed publication" "$TEST_HOME/.codex/AGENTS.md" ||
    fail "failed Claude preflight partially updated Codex"
rmdir "$TEST_HOME/.claude/CLAUDE.md"
cp \
    "$CHECKOUT/agents/WORKING_CONTRACT.md" \
    "$TEST_HOME/.claude/CLAUDE.md"

unlink "$TEST_HOME/.codex/AGENTS.md"
mkdir "$TEST_HOME/.codex/AGENTS.md"
printf '%s\n' "Claude before failed publication" > "$TEST_HOME/.claude/CLAUDE.md"
if HOME="$TEST_HOME" DOTFILES="$CHECKOUT" PATH="$FAKE_BIN:$PATH" \
        "$CHECKOUT/bin/dotfiles" update >/dev/null 2>&1; then
    fail "update accepted a non-file Codex destination"
fi
grep -qxF \
    "Claude before failed publication" "$TEST_HOME/.claude/CLAUDE.md" ||
    fail "failed Codex preflight partially updated Claude"
rmdir "$TEST_HOME/.codex/AGENTS.md"
cp \
    "$CHECKOUT/agents/WORKING_CONTRACT.md" \
    "$TEST_HOME/.codex/AGENTS.md"

# Contract parents follow the same HOME ownership boundary as managed links.
# An intermediate symlink outside HOME cannot redirect either publication, and
# the other client remains byte-for-byte unchanged.
PARENT_ESCAPE_HOME="$TEST_ROOT/parent-escape-home"
PARENT_ESCAPE_ROOT="$TEST_ROOT/parent-escape-root"
mkdir -p "$PARENT_ESCAPE_HOME/.codex" "$PARENT_ESCAPE_ROOT"
ln -s "$PARENT_ESCAPE_ROOT" "$PARENT_ESCAPE_HOME/.claude"
printf '%s\n' "external Claude sentinel" \
    > "$PARENT_ESCAPE_ROOT/CLAUDE.md"
printf '%s\n' "internal Codex sentinel" \
    > "$PARENT_ESCAPE_HOME/.codex/AGENTS.md"
(
    export HOME="$PARENT_ESCAPE_HOME"
    export DOTFILES="$CHECKOUT"
    export PATH="$FAKE_BIN:$PATH"
    set -- help
    # shellcheck source=/dev/null
    . "$CHECKOUT/bin/dotfiles" >/dev/null
    if _publish_agent_contracts >/dev/null 2>&1; then
        fail "contract publication accepted an external parent symlink"
    fi
)
grep -qxF \
    "external Claude sentinel" "$PARENT_ESCAPE_ROOT/CLAUDE.md" ||
    fail "contract publication changed the external Claude target"
grep -qxF \
    "internal Codex sentinel" "$PARENT_ESCAPE_HOME/.codex/AGENTS.md" ||
    fail "failed parent validation partially changed Codex"

# A valid custom contract symlink is replaced by the canonical regular copy,
# but its exact symlink identity remains in the permanent backup store.
CUSTOM_LINK_HOME="$TEST_ROOT/custom-link-home"
mkdir -p "$CUSTOM_LINK_HOME/.claude" "$CUSTOM_LINK_HOME/.codex"
printf '%s\n' "custom contract target" \
    > "$CUSTOM_LINK_HOME/custom-contract"
ln -s \
    "$CUSTOM_LINK_HOME/custom-contract" \
    "$CUSTOM_LINK_HOME/.claude/CLAUDE.md"
printf '%s\n' "prior Codex contract" \
    > "$CUSTOM_LINK_HOME/.codex/AGENTS.md"
(
    export HOME="$CUSTOM_LINK_HOME"
    export DOTFILES="$CHECKOUT"
    export PATH="$FAKE_BIN:$PATH"
    set -- help
    # shellcheck source=/dev/null
    . "$CHECKOUT/bin/dotfiles" >/dev/null
    _publish_agent_contracts >/dev/null
)
if [ ! -f "$CUSTOM_LINK_HOME/.claude/CLAUDE.md" ] ||
        [ -L "$CUSTOM_LINK_HOME/.claude/CLAUDE.md" ]; then
    fail "custom contract symlink was not replaced by a regular copy"
fi
custom_link_backup=""
while IFS= read -r candidate; do
    if [ "$(readlink "$candidate")" = \
            "$CUSTOM_LINK_HOME/custom-contract" ]; then
        custom_link_backup="$candidate"
        break
    fi
done < <(
    find "$CUSTOM_LINK_HOME/.local/share/dotfiles/backups" \
        -type l -name CLAUDE.md -print
)
[ -n "$custom_link_backup" ] ||
    fail "custom contract symlink was not preserved in permanent backup"

# A successful first publication is not a commit. Fail the second atomic
# replace and require both exact pre-transaction states, including a symlink,
# to be restored.
ROLLBACK_HOME="$TEST_ROOT/rollback-home"
mkdir -p "$ROLLBACK_HOME/.claude" "$ROLLBACK_HOME/.codex"
printf '%s\n' "original Claude target" > "$ROLLBACK_HOME/claude-target"
ln -s \
    "$ROLLBACK_HOME/claude-target" \
    "$ROLLBACK_HOME/.claude/CLAUDE.md"
printf '%s\n' \
    "Codex before second publication failure" \
    > "$ROLLBACK_HOME/.codex/AGENTS.md"
SECOND_PUBLICATION_FAILED="$TEST_ROOT/second-publication-failed"
(
    export HOME="$ROLLBACK_HOME"
    export DOTFILES="$CHECKOUT"
    export PATH="$FAKE_BIN:$PATH"
    set -- help
    # shellcheck source=/dev/null
    . "$CHECKOUT/bin/dotfiles" >/dev/null

    real_python3=$(command -v python3)
    python3() {
        case "${2:-}" in
            *"os.replace"*)
                if [ "${4:-}" = "$HOME/.codex/AGENTS.md" ] &&
                        [ ! -e "$SECOND_PUBLICATION_FAILED" ]; then
                    : > "$SECOND_PUBLICATION_FAILED"
                    return 73
                fi
                ;;
        esac
        "$real_python3" "$@"
    }

    if _publish_agent_contracts >/dev/null 2>&1; then
        fail "agent contract transaction accepted a failed second publication"
    fi
)
[ -L "$ROLLBACK_HOME/.claude/CLAUDE.md" ] ||
    fail "failed transaction did not restore the Claude symlink"
[ "$(readlink "$ROLLBACK_HOME/.claude/CLAUDE.md")" = \
        "$ROLLBACK_HOME/claude-target" ] ||
    fail "failed transaction restored the wrong Claude symlink"
grep -qxF \
    "Codex before second publication failure" \
    "$ROLLBACK_HOME/.codex/AGENTS.md" ||
    fail "failed transaction did not restore the Codex file"

# A writer that ignores the HOME advisory lock can still land before the final
# destination check. Preserve those bytes, fail publication, and roll back only
# the first path that still contains this transaction's managed payload.
DESTINATION_RACE_HOME="$TEST_ROOT/destination-race-home"
DESTINATION_RACE_REACHED="$TEST_ROOT/destination-race-reached"
mkdir -p \
    "$DESTINATION_RACE_HOME/.claude" \
    "$DESTINATION_RACE_HOME/.codex"
printf '%s\n' \
    "Claude before destination race" \
    > "$DESTINATION_RACE_HOME/.claude/CLAUDE.md"
printf '%s\n' \
    "Codex before destination race" \
    > "$DESTINATION_RACE_HOME/.codex/AGENTS.md"
(
    export HOME="$DESTINATION_RACE_HOME"
    export DOTFILES="$CHECKOUT"
    export PATH="$FAKE_BIN:$PATH"
    set -- help
    # shellcheck source=/dev/null
    . "$CHECKOUT/bin/dotfiles" >/dev/null

    eval "$(
        declare -f _fsync_transaction_paths |
            sed '1s/_fsync_transaction_paths/_real_fsync_transaction_paths/'
    )"
    _fsync_transaction_paths() {
        local sync_path=""
        _real_fsync_transaction_paths "$@" || return
        for sync_path in "$@"; do
            case "$sync_path" in
                "$HOME"/.codex/.dotfiles-copy.*)
                    if [ ! -e "$DESTINATION_RACE_REACHED" ]; then
                        printf '%s\n' \
                            "external Codex edit during publication" \
                            > "$HOME/.codex/AGENTS.md"
                        : > "$DESTINATION_RACE_REACHED"
                    fi
                    ;;
            esac
        done
    }

    if _publish_agent_contracts >/dev/null 2>&1; then
        fail "agent contract publisher overwrote a raced destination"
    fi
)
[ -e "$DESTINATION_RACE_REACHED" ] ||
    fail "destination-race fixture did not reach managed staging"
grep -qxF \
    "Claude before destination race" \
    "$DESTINATION_RACE_HOME/.claude/CLAUDE.md" ||
    fail "destination-race failure did not roll back the owned Claude payload"
grep -qxF \
    "external Codex edit during publication" \
    "$DESTINATION_RACE_HOME/.codex/AGENTS.md" ||
    fail "destination-race rollback overwrote the external Codex edit"
if find "$DESTINATION_RACE_HOME/.local/state/dotfiles" \
        -xdev -maxdepth 1 \
        -name 'agent-contract-transaction.*' -print -quit |
        grep -q .; then
    fail "resolved destination-race transaction retained its journal"
fi

# TERM after the first destination replace follows the same rollback contract.
TERM_HOME="$TEST_ROOT/term-home"
mkdir -p "$TERM_HOME/.claude" "$TERM_HOME/.codex"
printf '%s\n' \
    "Claude before terminated publication" \
    > "$TERM_HOME/.claude/CLAUDE.md"
printf '%s\n' \
    "Codex before terminated publication" \
    > "$TERM_HOME/.codex/AGENTS.md"
FIRST_PUBLICATION_TERMINATED="$TEST_ROOT/first-publication-terminated"
(
    export HOME="$TERM_HOME"
    export DOTFILES="$CHECKOUT"
    export PATH="$FAKE_BIN:$PATH"
    set -- help
    # shellcheck source=/dev/null
    . "$CHECKOUT/bin/dotfiles" >/dev/null

    real_python3=$(command -v python3)
    python3() {
        case "${2:-}" in
            *"os.replace"*)
                if [ "${4:-}" = "$HOME/.claude/CLAUDE.md" ] &&
                        [ ! -e "$FIRST_PUBLICATION_TERMINATED" ]; then
                    : > "$FIRST_PUBLICATION_TERMINATED"
                    "$real_python3" "$@" || return
                    "$real_python3" -c \
                        'import os, signal; os.kill(os.getppid(), signal.SIGTERM)'
                    return
                fi
                ;;
        esac
        "$real_python3" "$@"
    }

    if _publish_agent_contracts >/dev/null 2>&1; then
        fail "agent contract transaction succeeded after injected TERM"
    fi
)
grep -qxF \
    "Claude before terminated publication" \
    "$TERM_HOME/.claude/CLAUDE.md" ||
    fail "terminated transaction did not restore the Claude file"
grep -qxF \
    "Codex before terminated publication" \
    "$TERM_HOME/.codex/AGENTS.md" ||
    fail "terminated transaction did not restore the Codex file"

# SIGKILL cannot run shell traps. The ready journal must survive the killed
# publisher, and the next invocation must replay it before validating or
# publishing a new source generation.
CRASH_HOME="$TEST_ROOT/crash-home"
CRASH_DOTFILES="$TEST_ROOT/crash-dotfiles"
CRASH_KILLED="$TEST_ROOT/crash-publisher-killed"
mkdir -p \
    "$CRASH_HOME/.claude" \
    "$CRASH_HOME/.codex" \
    "$CRASH_DOTFILES/agents"
printf '%s\n' \
    "contract published before SIGKILL" \
    > "$CRASH_DOTFILES/agents/WORKING_CONTRACT.md"
printf '%s\n' \
    "Claude before SIGKILL" \
    > "$CRASH_HOME/.claude/CLAUDE.md"
printf '%s\n' \
    "Codex before SIGKILL" \
    > "$CRASH_HOME/.codex/AGENTS.md"
(
    export HOME="$CRASH_HOME"
    export DOTFILES="$CRASH_DOTFILES"
    export PATH="$FAKE_BIN:$PATH"
    set -- help
    # shellcheck source=/dev/null
    . "$CHECKOUT/bin/dotfiles" >/dev/null

    real_python3=$(command -v python3)
    python3() {
        case "${2:-}" in
            *"os.replace"*)
                if [ "${4:-}" = "$HOME/.claude/CLAUDE.md" ] &&
                        [ ! -e "$CRASH_KILLED" ]; then
                    "$real_python3" "$@" || return
                    : > "$CRASH_KILLED"
                    "$real_python3" -c '
import os
import signal
import subprocess

copy_process = os.getppid()
publisher_process = int(subprocess.check_output(
    ("ps", "-o", "ppid=", "-p", str(copy_process)),
    text=True,
).strip())
os.kill(publisher_process, signal.SIGKILL)
'
                    return
                fi
                ;;
        esac
        "$real_python3" "$@"
    }

    if _publish_agent_contracts >/dev/null 2>&1; then
        fail "agent contract transaction survived injected SIGKILL"
    fi
)
[ -e "$CRASH_KILLED" ] ||
    fail "agent contract SIGKILL fixture did not reach first publication"
grep -qxF \
    "contract published before SIGKILL" \
    "$CRASH_HOME/.claude/CLAUDE.md" ||
    fail "SIGKILL fixture did not publish the first destination"
grep -qxF \
    "Codex before SIGKILL" \
    "$CRASH_HOME/.codex/AGENTS.md" ||
    fail "SIGKILL fixture unexpectedly published the second destination"

# Make the new source invalid. Recovery precedes source validation, so this
# invocation must fail only after restoring the exact prior generation.
unlink "$CRASH_DOTFILES/agents/WORKING_CONTRACT.md"
mkdir "$CRASH_DOTFILES/agents/WORKING_CONTRACT.md"
(
    export HOME="$CRASH_HOME"
    export DOTFILES="$CRASH_DOTFILES"
    export PATH="$FAKE_BIN:$PATH"
    set -- help
    # shellcheck source=/dev/null
    . "$CHECKOUT/bin/dotfiles" >/dev/null
    if _publish_agent_contracts >/dev/null 2>&1; then
        fail "agent contract publisher accepted an invalid post-crash source"
    fi
)
grep -qxF \
    "Claude before SIGKILL" \
    "$CRASH_HOME/.claude/CLAUDE.md" ||
    fail "next invocation did not recover Claude after SIGKILL"
grep -qxF \
    "Codex before SIGKILL" \
    "$CRASH_HOME/.codex/AGENTS.md" ||
    fail "next invocation did not recover Codex after SIGKILL"
if find "$CRASH_HOME/.local/state/dotfiles" -xdev -maxdepth 1 \
        -name 'agent-contract-transaction.*' -print -quit |
        grep -q .; then
    fail "recovered agent contract journal was not retired"
fi

# Both replacements are still recoverable while rollback readiness exists.
# Once successful cleanup clears that marker, a kill may leave journal debris
# but the next invocation must preserve the committed generation.
COMMIT_HOME="$TEST_ROOT/commit-home"
COMMIT_DOTFILES="$TEST_ROOT/commit-dotfiles"
COMMIT_CLEANUP_KILLED="$TEST_ROOT/commit-cleanup-killed"
mkdir -p \
    "$COMMIT_HOME/.claude" \
    "$COMMIT_HOME/.codex" \
    "$COMMIT_DOTFILES/agents"
printf '%s\n' \
    "contract committed before cleanup SIGKILL" \
    > "$COMMIT_DOTFILES/agents/WORKING_CONTRACT.md"
printf '%s\n' \
    "Claude before committed publication" \
    > "$COMMIT_HOME/.claude/CLAUDE.md"
printf '%s\n' \
    "Codex before committed publication" \
    > "$COMMIT_HOME/.codex/AGENTS.md"
(
    export HOME="$COMMIT_HOME"
    export DOTFILES="$COMMIT_DOTFILES"
    export PATH="$FAKE_BIN:$PATH"
    set -- help
    # shellcheck source=/dev/null
    . "$CHECKOUT/bin/dotfiles" >/dev/null

    real_unlink=$(command -v unlink)
    real_python3=$(command -v python3)
    unlink() {
        case "${1:-}" in
            */agent-contract-transaction.*/rollback-ready)
                if [ ! -e "$COMMIT_CLEANUP_KILLED" ]; then
                    "$real_unlink" "$@" || return
                    : > "$COMMIT_CLEANUP_KILLED"
                    "$real_python3" -c \
                        'import os, signal; os.kill(os.getppid(), signal.SIGKILL)'
                    return
                fi
                ;;
        esac
        "$real_unlink" "$@"
    }

    if _publish_agent_contracts >/dev/null 2>&1; then
        fail "agent contract publisher survived cleanup SIGKILL"
    fi
)
[ -e "$COMMIT_CLEANUP_KILLED" ] ||
    fail "agent contract cleanup SIGKILL fixture did not clear readiness"
grep -qxF \
    "contract committed before cleanup SIGKILL" \
    "$COMMIT_HOME/.claude/CLAUDE.md" ||
    fail "cleanup SIGKILL lost the committed Claude contract"
grep -qxF \
    "contract committed before cleanup SIGKILL" \
    "$COMMIT_HOME/.codex/AGENTS.md" ||
    fail "cleanup SIGKILL lost the committed Codex contract"

unlink "$COMMIT_DOTFILES/agents/WORKING_CONTRACT.md"
mkdir "$COMMIT_DOTFILES/agents/WORKING_CONTRACT.md"
(
    export HOME="$COMMIT_HOME"
    export DOTFILES="$COMMIT_DOTFILES"
    export PATH="$FAKE_BIN:$PATH"
    set -- help
    # shellcheck source=/dev/null
    . "$CHECKOUT/bin/dotfiles" >/dev/null
    if _publish_agent_contracts >/dev/null 2>&1; then
        fail "agent contract publisher accepted an invalid source after commit recovery"
    fi
)
grep -qxF \
    "contract committed before cleanup SIGKILL" \
    "$COMMIT_HOME/.claude/CLAUDE.md" ||
    fail "commit recovery rolled back the Claude contract"
grep -qxF \
    "contract committed before cleanup SIGKILL" \
    "$COMMIT_HOME/.codex/AGENTS.md" ||
    fail "commit recovery rolled back the Codex contract"
if find "$COMMIT_HOME/.local/state/dotfiles" -xdev -maxdepth 1 \
        -name 'agent-contract-transaction.*' -print -quit |
        grep -q .; then
    fail "committed agent contract cleanup journal was not retired"
fi

# Both destinations must come from one immutable source generation even if the
# checkout changes after the first destination is published.
SNAPSHOT_HOME="$TEST_ROOT/snapshot-home"
SNAPSHOT_DOTFILES="$TEST_ROOT/snapshot-dotfiles"
SNAPSHOT_ENTERED="$TEST_ROOT/snapshot-entered"
SNAPSHOT_RELEASE="$TEST_ROOT/snapshot-release"
SNAPSHOT_DONE="$TEST_ROOT/snapshot-done"
mkdir -p \
    "$SNAPSHOT_HOME/.claude" \
    "$SNAPSHOT_HOME/.codex" \
    "$SNAPSHOT_DOTFILES/agents"
printf '%s\n' \
    "snapshot-generation-a" \
    > "$SNAPSHOT_DOTFILES/agents/WORKING_CONTRACT.md"
printf '%s\n' "Claude before snapshot test" > "$SNAPSHOT_HOME/.claude/CLAUDE.md"
printf '%s\n' "Codex before snapshot test" > "$SNAPSHOT_HOME/.codex/AGENTS.md"
(
    trap ': > "$SNAPSHOT_DONE"' EXIT
    export HOME="$SNAPSHOT_HOME"
    export DOTFILES="$SNAPSHOT_DOTFILES"
    export PATH="$FAKE_BIN:$PATH"
    set -- help
    # shellcheck source=/dev/null
    . "$CHECKOUT/bin/dotfiles" >/dev/null

    real_python3=$(command -v python3)
    python3() {
        case "${2:-}" in
            *"os.replace"*)
                if [ "${4:-}" = "$HOME/.claude/CLAUDE.md" ] &&
                        [ ! -e "$SNAPSHOT_ENTERED" ]; then
                    "$real_python3" "$@" || return
                    : > "$SNAPSHOT_ENTERED"
                    while [ ! -e "$SNAPSHOT_RELEASE" ]; do
                        sleep 0.01
                    done
                    return 0
                fi
                ;;
        esac
        "$real_python3" "$@"
    }

    _publish_agent_contracts >"$TEST_ROOT/snapshot-publish.log" 2>&1
) &
snapshot_process=$!
BACKGROUND_PIDS+=("$snapshot_process")
snapshot_process_index=$((${#BACKGROUND_PIDS[@]} - 1))
BACKGROUND_RELEASES+=("$SNAPSHOT_RELEASE")
wait_for_marker \
    "$SNAPSHOT_ENTERED" "$snapshot_process" "source snapshot publisher" \
    "$SNAPSHOT_DONE"
printf '%s\n' \
    "snapshot-generation-b" \
    > "$SNAPSHOT_DOTFILES/agents/WORKING_CONTRACT.md"
: > "$SNAPSHOT_RELEASE"
if wait "$snapshot_process"; then
    snapshot_status=0
else
    snapshot_status=$?
fi
BACKGROUND_PIDS[snapshot_process_index]=""
if [ "$snapshot_status" -ne 0 ]; then
    fail "source snapshot publisher failed"
fi
grep -qxF \
    "snapshot-generation-a" "$SNAPSHOT_HOME/.claude/CLAUDE.md" ||
    fail "Claude did not receive the immutable source snapshot"
grep -qxF \
    "snapshot-generation-a" "$SNAPSHOT_HOME/.codex/AGENTS.md" ||
    fail "Codex did not receive the immutable source snapshot"

# Two dotfiles generations targeting one HOME serialize through the retained
# advisory lock. Hold generation A after its first publication, prove B
# observes contention, then require B to publish one complete final generation.
CONCURRENT_HOME="$TEST_ROOT/concurrent-home"
GENERATION_A="$TEST_ROOT/generation-a"
GENERATION_B="$TEST_ROOT/generation-b"
CONCURRENT_A_ENTERED="$TEST_ROOT/concurrent-a-entered"
CONCURRENT_A_RELEASE="$TEST_ROOT/concurrent-a-release"
CONCURRENT_A_DONE="$TEST_ROOT/concurrent-a-done"
CONCURRENT_B_ATTEMPTED="$TEST_ROOT/concurrent-b-attempted"
CONCURRENT_B_BLOCKED="$TEST_ROOT/concurrent-b-blocked"
CONCURRENT_B_ACQUIRED="$TEST_ROOT/concurrent-b-acquired"
CONCURRENT_B_DONE="$TEST_ROOT/concurrent-b-done"
mkdir -p \
    "$CONCURRENT_HOME/.claude" \
    "$CONCURRENT_HOME/.codex" \
    "$GENERATION_A/agents" \
    "$GENERATION_B/agents"
printf '%s\n' \
    "concurrent-generation-a" \
    > "$GENERATION_A/agents/WORKING_CONTRACT.md"
printf '%s\n' \
    "concurrent-generation-b" \
    > "$GENERATION_B/agents/WORKING_CONTRACT.md"
printf '%s\n' \
    "Claude before concurrency test" \
    > "$CONCURRENT_HOME/.claude/CLAUDE.md"
printf '%s\n' \
    "Codex before concurrency test" \
    > "$CONCURRENT_HOME/.codex/AGENTS.md"
(
    trap ': > "$CONCURRENT_A_DONE"' EXIT
    export HOME="$CONCURRENT_HOME"
    export DOTFILES="$GENERATION_A"
    export XDG_STATE_HOME="$TEST_ROOT/concurrent-state-a"
    export PATH="$FAKE_BIN:$PATH"
    set -- help
    # shellcheck source=/dev/null
    . "$CHECKOUT/bin/dotfiles" >/dev/null

    real_python3=$(command -v python3)
    python3() {
        case "${2:-}" in
            *"os.replace"*)
                if [ "${4:-}" = "$HOME/.claude/CLAUDE.md" ] &&
                        [ ! -e "$CONCURRENT_A_ENTERED" ]; then
                    "$real_python3" "$@" || return
                    : > "$CONCURRENT_A_ENTERED"
                    while [ ! -e "$CONCURRENT_A_RELEASE" ]; do
                        sleep 0.01
                    done
                    return 0
                fi
                ;;
        esac
        "$real_python3" "$@"
    }

    _publish_agent_contracts >"$TEST_ROOT/concurrent-a.log" 2>&1
) &
concurrent_a_process=$!
BACKGROUND_PIDS+=("$concurrent_a_process")
concurrent_a_process_index=$((${#BACKGROUND_PIDS[@]} - 1))
BACKGROUND_RELEASES+=("$CONCURRENT_A_RELEASE")
wait_for_marker \
    "$CONCURRENT_A_ENTERED" "$concurrent_a_process" \
    "generation A publisher" "$CONCURRENT_A_DONE"

(
    trap ': > "$CONCURRENT_B_DONE"' EXIT
    export HOME="$CONCURRENT_HOME"
    export DOTFILES="$GENERATION_B"
    export XDG_STATE_HOME="$TEST_ROOT/concurrent-state-b"
    export PATH="$FAKE_BIN:$PATH"
    set -- help
    # shellcheck source=/dev/null
    . "$CHECKOUT/bin/dotfiles" >/dev/null

    real_python3=$(command -v python3)
    python3() {
        local python_status=0
        case "${2:-}" in
            *"fcntl.flock"*)
                : > "$CONCURRENT_B_ATTEMPTED"
                if "$real_python3" "$@"; then
                    : > "$CONCURRENT_B_ACQUIRED"
                    return 0
                else
                    python_status=$?
                    if [ "$python_status" -eq 75 ]; then
                        : > "$CONCURRENT_B_BLOCKED"
                    fi
                    return "$python_status"
                fi
                ;;
        esac
        "$real_python3" "$@"
    }

    _publish_agent_contracts >"$TEST_ROOT/concurrent-b.log" 2>&1
) &
concurrent_b_process=$!
BACKGROUND_PIDS+=("$concurrent_b_process")
concurrent_b_process_index=$((${#BACKGROUND_PIDS[@]} - 1))
wait_for_marker \
    "$CONCURRENT_B_ATTEMPTED" "$concurrent_b_process" \
    "generation B publisher" "$CONCURRENT_B_DONE"
wait_for_marker \
    "$CONCURRENT_B_BLOCKED" "$concurrent_b_process" \
    "generation B lock contention" "$CONCURRENT_B_DONE"
[ ! -e "$CONCURRENT_B_ACQUIRED" ] ||
    fail "generation B acquired the lock while generation A still held it"
: > "$CONCURRENT_A_RELEASE"
if wait "$concurrent_a_process"; then
    concurrent_a_status=0
else
    concurrent_a_status=$?
fi
BACKGROUND_PIDS[concurrent_a_process_index]=""
if [ "$concurrent_a_status" -ne 0 ]; then
    fail "generation A publisher failed"
fi
if wait "$concurrent_b_process"; then
    concurrent_b_status=0
else
    concurrent_b_status=$?
fi
BACKGROUND_PIDS[concurrent_b_process_index]=""
if [ "$concurrent_b_status" -ne 0 ]; then
    fail "generation B publisher failed"
fi
[ -e "$CONCURRENT_B_ACQUIRED" ] ||
    fail "generation B never acquired the released transaction lock"
grep -qxF \
    "concurrent-generation-b" "$CONCURRENT_HOME/.claude/CLAUDE.md" ||
    fail "concurrent publication left Claude on a split generation"
grep -qxF \
    "concurrent-generation-b" "$CONCURRENT_HOME/.codex/AGENTS.md" ||
    fail "concurrent publication left Codex on a split generation"

# Doctor has broader machine-health checks, but it must independently name a
# contract drift rather than allowing it to disappear among those findings.
printf '%s\n' "locally drifted contract" > "$TEST_HOME/.codex/AGENTS.md"
doctor_output="$TEST_ROOT/doctor-output"
dotfiles_command="$DOTFILES/bin/dotfiles"
if env HOME="$TEST_HOME" DOTFILES="$DOTFILES" \
        "$dotfiles_command" doctor >"$doctor_output" 2>&1; then
    fail "doctor accepted a drifted managed contract"
fi
grep -Fq \
    "$TEST_HOME/.codex/AGENTS.md differs from the canonical contract" \
    "$doctor_output" ||
    fail "doctor did not identify the drifted managed contract"

# A process-wide timestamp is not a unique backup identity. Exercise the
# symlink publisher twice under the fixed fake clock and require both displaced
# payloads to survive.
LINK_HOME="$TEST_ROOT/link-home"
mkdir -p "$LINK_HOME"
printf '%s\n' "first link destination" > "$LINK_HOME/.profile"
(
    export HOME="$LINK_HOME"
    export DOTFILES="$CHECKOUT"
    export PATH="$FAKE_BIN:$PATH"
    set -- help
    # shellcheck source=/dev/null
    . "$CHECKOUT/bin/dotfiles" >/dev/null
    _link agents/WORKING_CONTRACT.md .profile >/dev/null
    unlink "$LINK_HOME/.profile"
    printf '%s\n' "second link destination" > "$LINK_HOME/.profile"
    _link agents/WORKING_CONTRACT.md .profile >/dev/null

    # The staged replacement is not the commit point. Inject failure into the
    # second atomic rename and require exact rollback for every accepted
    # destination type.
    real_mkdir=$(command -v mkdir)
    real_python3=$(command -v python3)
    real_unlink=$(command -v unlink)
    path_identity() {
        "$real_python3" -c \
            'import os, sys; value = os.lstat(sys.argv[1]); print(value.st_dev, value.st_ino)' \
            "$1"
    }
    mkdir_mode=normal
    rename_mode=fail_publication
    unlink_mode=normal
    python3() {
        case "${2:-}" in
            *"os.rename"*)
                rename_call_count=$((rename_call_count + 1))
                if [ "$rename_mode" = fail_publication ] &&
                        [ "$rename_call_count" -eq 2 ]; then
                    return 73
                fi
                if [ "$rename_mode" = term_after_displacement ] &&
                        [ "$rename_call_count" -eq 1 ]; then
                    "$real_python3" "$@" || return
                    "$real_python3" -c \
                        'import os, signal; os.kill(os.getppid(), signal.SIGTERM)'
                    return
                fi
                if [ "$rename_mode" = sigkill_after_displacement ] &&
                        [ "$rename_call_count" -eq 1 ]; then
                    "$real_python3" "$@" || return
                    : > "$LINK_SIGKILL_MARKER"
                    "$real_python3" -c \
                        'import os, signal; os.kill(os.getppid(), signal.SIGKILL)'
                    return
                fi
                ;;
        esac
        "$real_python3" "$@"
    }
    mkdir() {
        local destination_argument=""
        for destination_argument in "$@"; do :; done
        case "$destination_argument" in
            "$LINK_HOME"/.dotfiles-link.*)
                if [ "$mkdir_mode" = sigkill_after_staging_create ] &&
                        [ ! -e "$LINK_PRE_READY_SIGKILL_MARKER" ]; then
                    "$real_mkdir" "$@" || return
                    : > "$LINK_PRE_READY_SIGKILL_MARKER"
                    "$real_python3" -c \
                        'import os, signal; os.kill(os.getppid(), signal.SIGKILL)'
                    return
                fi
                ;;
        esac
        "$real_mkdir" "$@"
    }
    unlink() {
        case "${1:-}" in
            */link-transaction.*/ready)
                if [ "$unlink_mode" = sigkill_after_ready_cleanup ] &&
                        [ ! -e "$LINK_CLEANUP_SIGKILL_MARKER" ]; then
                    "$real_unlink" "$@" || return
                    : > "$LINK_CLEANUP_SIGKILL_MARKER"
                    "$real_python3" -c \
                        'import os, signal; os.kill(os.getppid(), signal.SIGKILL)'
                    return
                fi
                ;;
        esac
        "$real_unlink" "$@"
    }

    printf '%s\n' "file before failed link" > "$LINK_HOME/file-destination"
    file_identity=$(path_identity "$LINK_HOME/file-destination")
    rename_call_count=0
    if _link \
            agents/WORKING_CONTRACT.md file-destination >/dev/null 2>&1; then
        fail "_link accepted an injected publication failure for a file"
    fi
    grep -qxF \
        "file before failed link" "$LINK_HOME/file-destination" ||
        fail "_link did not restore a file after publication failure"
    [ ! -L "$LINK_HOME/file-destination" ] ||
        fail "_link restored the file destination as a symlink"
    [ "$(path_identity "$LINK_HOME/file-destination")" = "$file_identity" ] ||
        fail "_link did not restore the exact displaced file"

    mkdir "$LINK_HOME/directory-destination"
    printf '%s\n' \
        "directory before failed link" \
        > "$LINK_HOME/directory-destination/payload"
    directory_identity=$(path_identity "$LINK_HOME/directory-destination")
    rename_call_count=0
    if _link \
            agents/WORKING_CONTRACT.md directory-destination \
            >/dev/null 2>&1; then
        fail "_link accepted an injected publication failure for a directory"
    fi
    grep -qxF \
        "directory before failed link" \
        "$LINK_HOME/directory-destination/payload" ||
        fail "_link did not restore a directory after publication failure"
    [ "$(path_identity "$LINK_HOME/directory-destination")" = \
            "$directory_identity" ] ||
        fail "_link did not restore the exact displaced directory"

    printf '%s\n' "symlink target" > "$LINK_HOME/original-link-target"
    ln -s \
        "$LINK_HOME/original-link-target" \
        "$LINK_HOME/symlink-destination"
    symlink_identity=$(path_identity "$LINK_HOME/symlink-destination")
    rename_call_count=0
    if _link \
            agents/WORKING_CONTRACT.md symlink-destination \
            >/dev/null 2>&1; then
        fail "_link accepted an injected publication failure for a symlink"
    fi
    [ -L "$LINK_HOME/symlink-destination" ] ||
        fail "_link did not restore a symlink after publication failure"
    [ "$(readlink "$LINK_HOME/symlink-destination")" = \
            "$LINK_HOME/original-link-target" ] ||
        fail "_link restored the wrong symlink target"
    [ "$(path_identity "$LINK_HOME/symlink-destination")" = \
            "$symlink_identity" ] ||
        fail "_link did not restore the exact displaced symlink"

    # A termination immediately after displacement takes the same rollback
    # path rather than leaving the live destination absent.
    printf '%s\n' "file before terminated link" > "$LINK_HOME/term-destination"
    term_identity=$(path_identity "$LINK_HOME/term-destination")
    rename_mode=term_after_displacement
    rename_call_count=0
    if _link \
            agents/WORKING_CONTRACT.md term-destination >/dev/null 2>&1; then
        fail "_link succeeded after injected TERM"
    fi
    grep -qxF \
        "file before terminated link" "$LINK_HOME/term-destination" ||
        fail "_link did not restore its destination after TERM"
    [ ! -L "$LINK_HOME/term-destination" ] ||
        fail "_link restored the terminated destination as a symlink"
    [ "$(path_identity "$LINK_HOME/term-destination")" = "$term_identity" ] ||
        fail "_link did not restore the exact TERM-displaced file"

    # Off-path identities are journaled before their directories exist. A hard
    # crash immediately after staging creation is therefore discoverable and
    # the next invocation retires both the journal and exact staging root.
    LINK_PRE_READY_SIGKILL_MARKER="$TEST_ROOT/link-pre-ready-sigkill-reached"
    mkdir_mode=sigkill_after_staging_create
    rename_mode=normal
    rename_call_count=0
    if _link \
            agents/WORKING_CONTRACT.md pre-ready-sigkill-destination \
            >/dev/null 2>&1; then
        fail "_link survived a SIGKILL after staging creation"
    fi
    [ -e "$LINK_PRE_READY_SIGKILL_MARKER" ] ||
        fail "_link pre-ready SIGKILL fixture did not create staging"

    mkdir_mode=normal
    if _link \
            agents/DOES_NOT_EXIST.md pre-ready-sigkill-destination \
            >/dev/null 2>&1; then
        fail "_link accepted an invalid source after pre-ready recovery"
    fi
    if find "$LINK_HOME" -xdev -maxdepth 1 \
            -name '.dotfiles-link.*' -print -quit |
            grep -q .; then
        fail "pre-ready recovery retained managed symlink staging"
    fi
    if find "$LINK_HOME/.local/state/dotfiles" -xdev -maxdepth 1 \
            -name 'link-transaction.*' -print -quit |
            grep -q .; then
        fail "pre-ready recovery retained its transaction journal"
    fi

    # SIGKILL bypasses the EXIT trap after displacement. The next _link call
    # must replay the durable journal before it validates its own source.
    LINK_SIGKILL_MARKER="$TEST_ROOT/link-sigkill-reached"
    printf '%s\n' \
        "file before killed link" \
        > "$LINK_HOME/sigkill-destination"
    sigkill_identity=$(path_identity "$LINK_HOME/sigkill-destination")
    rename_mode=sigkill_after_displacement
    rename_call_count=0
    if _link \
            agents/WORKING_CONTRACT.md sigkill-destination \
            >/dev/null 2>&1; then
        fail "_link survived injected SIGKILL"
    fi
    [ -e "$LINK_SIGKILL_MARKER" ] ||
        fail "_link SIGKILL fixture did not reach displacement"
    if [ -e "$LINK_HOME/sigkill-destination" ] ||
            [ -L "$LINK_HOME/sigkill-destination" ]; then
        fail "_link SIGKILL fixture did not expose the recovery window"
    fi

    rename_mode=normal
    rename_call_count=0
    if _link \
            agents/DOES_NOT_EXIST.md sigkill-destination \
            >/dev/null 2>&1; then
        fail "_link accepted an invalid source after crash recovery"
    fi
    grep -qxF \
        "file before killed link" "$LINK_HOME/sigkill-destination" ||
        fail "next _link invocation did not recover the killed transaction"
    [ "$(path_identity "$LINK_HOME/sigkill-destination")" = \
            "$sigkill_identity" ] ||
        fail "_link crash recovery did not restore the exact displaced file"
    if find "$LINK_HOME/.local/state/dotfiles" -xdev -maxdepth 1 \
            -name 'link-transaction.*' -print -quit |
            grep -q .; then
        fail "recovered link transaction journal was not retired"
    fi

    # Journal retirement clears readiness only after the live state is fully
    # resolved. A kill immediately after that unlink leaves an unready cleanup
    # journal and a legitimate permanent backup; recovery must retire the
    # journal without rolling back the committed link or rejecting the backup.
    LINK_CLEANUP_SIGKILL_MARKER="$TEST_ROOT/link-cleanup-sigkill-reached"
    printf '%s\n' \
        "file before cleanup-boundary kill" \
        > "$LINK_HOME/cleanup-sigkill-destination"
    rename_mode=normal
    rename_call_count=0
    unlink_mode=sigkill_after_ready_cleanup
    if _link \
            agents/WORKING_CONTRACT.md cleanup-sigkill-destination \
            >/dev/null 2>&1; then
        fail "_link survived a SIGKILL while retiring its journal"
    fi
    [ -e "$LINK_CLEANUP_SIGKILL_MARKER" ] ||
        fail "_link cleanup SIGKILL fixture did not clear journal readiness"
    [ -L "$LINK_HOME/cleanup-sigkill-destination" ] ||
        fail "_link cleanup SIGKILL fixture lost the committed link"
    [ "$(readlink "$LINK_HOME/cleanup-sigkill-destination")" = \
            "$CHECKOUT/agents/WORKING_CONTRACT.md" ] ||
        fail "_link cleanup SIGKILL fixture published the wrong target"

    unlink_mode=normal
    if _link \
            agents/DOES_NOT_EXIST.md cleanup-sigkill-destination \
            >/dev/null 2>&1; then
        fail "_link accepted an invalid source after cleanup recovery"
    fi
    [ -L "$LINK_HOME/cleanup-sigkill-destination" ] ||
        fail "cleanup recovery rolled back a committed managed link"
    [ "$(readlink "$LINK_HOME/cleanup-sigkill-destination")" = \
            "$CHECKOUT/agents/WORKING_CONTRACT.md" ] ||
        fail "cleanup recovery changed the committed managed link"
    grep -RqxF \
        "file before cleanup-boundary kill" \
        "$LINK_HOME/.local/share/dotfiles/backups" ||
        fail "cleanup recovery lost the committed link's permanent backup"
    if find "$LINK_HOME/.local/state/dotfiles" -xdev -maxdepth 1 \
            -name 'link-transaction.*' -print -quit |
            grep -q .; then
        fail "unready link cleanup journal was not retired"
    fi

    # Destination ownership follows the resolved parent, not just the textual
    # "$HOME/" prefix. Reject both lexical traversal and an intermediate
    # symlink that would publish outside HOME.
    LINK_ESCAPE_ROOT="$TEST_ROOT/link-escape-root"
    mkdir "$LINK_ESCAPE_ROOT"
    ln -s "$LINK_ESCAPE_ROOT" "$LINK_HOME/escape-parent"
    if _link \
            agents/WORKING_CONTRACT.md ../escaped-link \
            >/dev/null 2>&1; then
        fail "_link accepted a destination that traverses outside HOME"
    fi
    if _link \
            agents/WORKING_CONTRACT.md escape-parent/escaped-link \
            >/dev/null 2>&1; then
        fail "_link accepted a parent symlink that escapes HOME"
    fi
    if [ -e "$TEST_ROOT/escaped-link" ] ||
            [ -L "$TEST_ROOT/escaped-link" ]; then
        fail "_link published through lexical HOME traversal"
    fi
    if [ -e "$LINK_ESCAPE_ROOT/escaped-link" ] ||
            [ -L "$LINK_ESCAPE_ROOT/escaped-link" ]; then
        fail "_link published through a parent symlink outside HOME"
    fi

    # Repository hook installation is also a preserving managed link. An
    # existing user hook survives in the backup store instead of being
    # overwritten by a force-link operation.
    HOOK_REPOSITORY="$LINK_HOME/.dotfiles"
    mkdir -p \
        "$HOOK_REPOSITORY/.git/hooks" \
        "$HOOK_REPOSITORY/git/hooks"
    printf '%s\n' '#!/bin/sh' 'exit 0' \
        > "$HOOK_REPOSITORY/git/hooks/pre-commit"
    printf '%s\n' 'existing user hook' \
        > "$HOOK_REPOSITORY/.git/hooks/pre-commit"
    DOTFILES="$HOOK_REPOSITORY"
    _install_pre_commit_hook >/dev/null
    [ -L "$HOOK_REPOSITORY/.git/hooks/pre-commit" ] ||
        fail "pre-commit hook was not installed as a managed link"
    [ "$(readlink "$HOOK_REPOSITORY/.git/hooks/pre-commit")" = \
            "$HOOK_REPOSITORY/git/hooks/pre-commit" ] ||
        fail "pre-commit hook points at the wrong managed source"
    grep -RqxF \
        'existing user hook' \
        "$LINK_HOME/.local/share/dotfiles/backups" ||
        fail "pre-commit hook installation lost the existing user hook"
    DOTFILES="$CHECKOUT"

    # A journal is data, not authority. Even a structurally plausible backup
    # suffix cannot move an external path into HOME; recovery binds the path
    # to the configured XDG backup root and fails without touching either end.
    FORGED_DESTINATION="$LINK_HOME/forged-destination"
    FORGED_STAGING="$LINK_HOME/.dotfiles-link.FORGED"
    FORGED_JOURNAL="$LINK_HOME/.local/state/dotfiles/link-transaction.FORGED"
    FORGED_BACKUP_PATH="$TEST_ROOT/external/dotfiles/backups/20000101-000000/forged-destination.ABCDEF/forged-destination"
    mkdir -p \
        "$FORGED_STAGING" \
        "$FORGED_JOURNAL" \
        "$(dirname "$FORGED_BACKUP_PATH")"
    ln -s \
        "$CHECKOUT/agents/WORKING_CONTRACT.md" \
        "$FORGED_STAGING/replacement"
    ln -s "$FORGED_DESTINATION" "$FORGED_JOURNAL/destination"
    ln -s \
        "$CHECKOUT/agents/WORKING_CONTRACT.md" \
        "$FORGED_JOURNAL/source"
    ln -s "$FORGED_STAGING" "$FORGED_JOURNAL/staging"
    ln -s "$FORGED_BACKUP_PATH" "$FORGED_JOURNAL/backup"
    : > "$FORGED_JOURNAL/ready"
    printf '%s\n' "external forged backup payload" > "$FORGED_BACKUP_PATH"
    forged_backup_identity=$(path_identity "$FORGED_BACKUP_PATH")

    if _link \
            agents/WORKING_CONTRACT.md forged-destination \
            >/dev/null 2>&1; then
        fail "_link accepted an external backup path from a forged journal"
    fi
    grep -qxF \
        "external forged backup payload" "$FORGED_BACKUP_PATH" ||
        fail "forged journal recovery moved the external backup payload"
    [ "$(path_identity "$FORGED_BACKUP_PATH")" = \
            "$forged_backup_identity" ] ||
        fail "forged journal recovery changed the external backup identity"
    if [ -e "$FORGED_DESTINATION" ] ||
            [ -L "$FORGED_DESTINATION" ]; then
        fail "forged journal recovery populated the HOME destination"
    fi
    [ -e "$FORGED_JOURNAL/ready" ] ||
        fail "failed forged-journal recovery discarded its evidence"
)
grep -RqxF \
    "first link destination" "$LINK_HOME/.local/share/dotfiles/backups" ||
    fail "first same-second symlink backup was not preserved"
grep -RqxF \
    "second link destination" "$LINK_HOME/.local/share/dotfiles/backups" ||
    fail "second same-second symlink backup was not preserved"

echo "agent contract lifecycle passed"
