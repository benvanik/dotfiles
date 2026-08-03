#!/bin/bash
# Integration coverage for the project worktree lifecycle commands.

set -euo pipefail

DOTFILES="${DOTFILES:-$HOME/.dotfiles}"
BASH_EXECUTABLE="${BASH:-/bin/bash}"
TEST_PARENT="${TMPDIR:-/tmp}"
TEST_ROOT=$(mktemp -d "$TEST_PARENT/dotfiles-worktree-test.XXXXXX")
cleanup_test_root() {
    case "$TEST_ROOT" in
        "$TEST_PARENT"/dotfiles-worktree-test.*) ;;
        *)
            echo "refusing unexpected test cleanup path: $TEST_ROOT" >&2
            return 1
            ;;
    esac
    [ ! -e "$TEST_ROOT" ] || find "$TEST_ROOT" -xdev -depth -delete
}
trap cleanup_test_root EXIT

# Pre-commit hooks export repository-local Git paths. They must not leak into
# the independent repositories created below.
while IFS= read -r git_environment_variable; do
    unset "$git_environment_variable"
done < <(git rev-parse --local-env-vars)

fail() {
    echo "project worktree test: $1" >&2
    exit 1
}

REMOTE="$TEST_ROOT/remote.git"
SEED="$TEST_ROOT/seed"
PROJECT_ROOT="$TEST_ROOT/path with spaces/example"
MAIN_WORKTREE="$PROJECT_ROOT/main"

git init -q --bare "$REMOTE"
git init -q -b main "$SEED"
git -C "$SEED" config user.name "Dotfiles Test"
git -C "$SEED" config user.email "dotfiles-test@example.com"
printf 'test repository\n' > "$SEED/README.md"
git -C "$SEED" add README.md
git -C "$SEED" commit -q -m "Initial commit"
git -C "$SEED" remote add origin "$REMOTE"
git -C "$SEED" push -q origin main
git -C "$SEED" switch -q -c users/test/remote
printf 'remote branch\n' > "$SEED/remote.txt"
git -C "$SEED" add remote.txt
git -C "$SEED" commit -q -m "Add remote branch"
git -C "$SEED" push -q origin users/test/remote
git -C "$SEED" switch -q main
git clone -q --branch main "$REMOTE" "$MAIN_WORKTREE"

# The primary directory is the stable repository anchor, not a promise that it
# continuously checks out the main branch.
git -C "$MAIN_WORKTREE" switch -q -c integration/primary-work
printf 'shared instructions\n' > "$MAIN_WORKTREE/AGENTS.override.md"
printf 'shared bazel configuration\n' > "$MAIN_WORKTREE/.bazelrc.local"
(
    cd "$MAIN_WORKTREE"
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-worktree-init" \
        users/test/feature feature >/dev/null
)

FEATURE_WORKTREE="$PROJECT_ROOT/feature"
[ -L "$FEATURE_WORKTREE/AGENTS.override.md" ] || fail "override link was not created"
[ "$(readlink "$FEATURE_WORKTREE/AGENTS.override.md")" = "../main/AGENTS.override.md" ] || \
    fail "override link does not target the main worktree"
[ "$(cat "$FEATURE_WORKTREE/AGENTS.override.md")" = "shared instructions" ] || \
    fail "override link does not resolve to shared contents"
[ -L "$FEATURE_WORKTREE/.bazelrc.local" ] || \
    fail "bazel configuration link was not created"
[ "$(readlink "$FEATURE_WORKTREE/.bazelrc.local")" = "../main/.bazelrc.local" ] || \
    fail "bazel configuration link does not target the main worktree"
[ "$(cat "$FEATURE_WORKTREE/.bazelrc.local")" = "shared bazel configuration" ] || \
    fail "bazel configuration link does not resolve to shared contents"
if [ -n "$(git -C "$FEATURE_WORKTREE" \
        -c core.excludesFile="$DOTFILES/git/ignore_global" \
        status --porcelain)" ]; then
    fail "managed shared links made a new worktree appear dirty"
fi

# Ignored state is still local data. Git's own worktree removal check omits it,
# so the wrapper must detect it before Git recursively deletes the worktree.
EXCLUDE_FILE=$(git -C "$FEATURE_WORKTREE" rev-parse --git-path info/exclude)
printf '.worktree-local/\n' >> "$EXCLUDE_FILE"
mkdir -p "$FEATURE_WORKTREE/.worktree-local"
printf 'ignored state\n' > "$FEATURE_WORKTREE/.worktree-local/state"
git -C "$FEATURE_WORKTREE" check-ignore -q .worktree-local/state || \
    fail "ignored-state fixture is not ignored"

# shellcheck source=../lib/project-worktrees.sh
. "$DOTFILES/lib/project-worktrees.sh"
[ "$(project_main_worktree "$FEATURE_WORKTREE")" = "$MAIN_WORKTREE" ] ||
    fail "primary worktree was not found while it checked out another branch"
PROJECT_SESSION_DIGEST=$(project_path_digest "$PROJECT_ROOT")
FEATURE_SESSION="example-$PROJECT_SESSION_DIGEST-feature"
MAIN_SESSION="example-$PROJECT_SESSION_DIGEST-main"
[ "$(project_default_session_name "$FEATURE_WORKTREE")" = "$FEATURE_SESSION" ] || \
    fail "worktree session name is not repository-qualified"
[ "$(project_default_session_name "$MAIN_WORKTREE")" = "$MAIN_SESSION" ] || \
    fail "main session name is not repository-qualified"

# Session identity is independent of the caller's Git object format.
SHA1_CALLER="$TEST_ROOT/sha1-caller"
SHA256_CALLER="$TEST_ROOT/sha256-caller"
git init -q --object-format=sha1 "$SHA1_CALLER"
SHA1_CONTEXT_DIGEST=$(
    cd "$SHA1_CALLER"
    project_path_digest "$PROJECT_ROOT"
)
[ "$SHA1_CONTEXT_DIGEST" = "$PROJECT_SESSION_DIGEST" ] ||
    fail "SHA-1 caller changed the physical project identity"
if git init -q --object-format=sha256 "$SHA256_CALLER" 2>/dev/null; then
    SHA256_CONTEXT_DIGEST=$(
        cd "$SHA256_CALLER"
        project_path_digest "$PROJECT_ROOT"
    )
    [ "$SHA256_CONTEXT_DIGEST" = "$PROJECT_SESSION_DIGEST" ] ||
        fail "SHA-256 caller changed the physical project identity"
fi

# Human-readable basenames are not global identities. Two repositories with
# the same layout and basename must still receive different default sessions.
SECOND_PROJECT_PARENT="$TEST_ROOT/"$'another\nparent'
SECOND_PROJECT_ROOT="$SECOND_PROJECT_PARENT/example"
SECOND_MAIN_WORKTREE="$SECOND_PROJECT_ROOT/main"
git clone -q --branch main "$REMOTE" "$SECOND_MAIN_WORKTREE"
SECOND_MAIN_SESSION=$(project_default_session_name "$SECOND_MAIN_WORKTREE")
[ "$SECOND_MAIN_SESSION" != "$MAIN_SESSION" ] ||
    fail "same-basename repositories shared a default tmux session"
case "$SECOND_MAIN_SESSION" in
    example-*-main) ;;
    *) fail "same-basename repository lost its readable session prefix" ;;
esac

# Default names sanitize tmux target delimiters in human path components.
UNSAFE_DIRECTORY="$TEST_ROOT/unsafe:directory"
mkdir -p "$UNSAFE_DIRECTORY"
UNSAFE_SESSION=$(project_default_session_name "$UNSAFE_DIRECTORY")
case "$UNSAFE_SESSION" in
    *:*) fail "default session retained a tmux target delimiter" ;;
    unsafe_directory-*) ;;
    *) fail "default session lost its sanitized human prefix" ;;
esac

if (
    cd "$MAIN_WORKTREE"
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-worktree-init" \
        users/test/attic attic
) >/dev/null 2>&1; then
    fail "reserved attic worktree name was accepted"
fi
[ ! -e "$PROJECT_ROOT/attic" ] ||
    fail "reserved attic worktree path was created"
if git -C "$MAIN_WORKTREE" show-ref --verify --quiet \
        refs/heads/users/test/attic; then
    fail "reserved attic request created a branch"
fi

# A post-checkout failure must remove earlier managed links and must not leave a
# registered worktree or the branch created for it. Stub the second link only.
LINK_FAILURE_BIN="$TEST_ROOT/link-failure-bin"
mkdir -p "$LINK_FAILURE_BIN"
cat > "$LINK_FAILURE_BIN/ln" << 'EOF'
#!/bin/bash
case "${3:-}" in
    */.bazelrc.local) exit 71 ;;
esac
exec /bin/ln "$@"
EOF
chmod +x "$LINK_FAILURE_BIN/ln"
if (
    cd "$MAIN_WORKTREE"
    PATH="$LINK_FAILURE_BIN:$PATH" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-worktree-init" \
        users/test/link-failure link-failure
) >/dev/null 2>&1; then
    fail "managed-override link failure unexpectedly succeeded"
fi
[ ! -e "$PROJECT_ROOT/link-failure" ] ||
    fail "managed-override link failure left a worktree directory"
if git -C "$MAIN_WORKTREE" worktree list --porcelain |
        grep -Fqx "worktree $PROJECT_ROOT/link-failure"; then
    fail "managed-override link failure left a registered worktree"
fi
if git -C "$MAIN_WORKTREE" show-ref --verify --quiet \
        refs/heads/users/test/link-failure; then
    fail "managed-override link failure left its newly created branch"
fi

printf 'dirty\n' > "$FEATURE_WORKTREE/untracked.txt"
if (
    cd "$MAIN_WORKTREE"
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-worktree-deinit" feature
) >/dev/null 2>&1; then
    fail "dirty worktree removal unexpectedly succeeded"
fi
[ -d "$FEATURE_WORKTREE" ] || fail "dirty worktree was removed"
[ -L "$FEATURE_WORKTREE/AGENTS.override.md" ] || \
    fail "managed override was not restored after failed removal"
[ -L "$FEATURE_WORKTREE/.bazelrc.local" ] || \
    fail "managed bazel configuration was not restored after failed removal"

unlink "$FEATURE_WORKTREE/untracked.txt"
if (
    cd "$MAIN_WORKTREE"
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-worktree-deinit" feature
) >/dev/null 2>&1; then
    fail "ignored worktree state was silently removed"
fi
[ -d "$FEATURE_WORKTREE/.worktree-local" ] || \
    fail "ignored worktree state was removed"
[ -L "$FEATURE_WORKTREE/AGENTS.override.md" ] || \
    fail "managed override was not restored after ignored-state refusal"
[ -L "$FEATURE_WORKTREE/.bazelrc.local" ] || \
    fail "managed bazel configuration was not restored after ignored-state refusal"

find "$FEATURE_WORKTREE/.worktree-local" -depth -delete
(
    cd "$MAIN_WORKTREE"
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-worktree-deinit" feature >/dev/null
)
[ ! -e "$FEATURE_WORKTREE" ] || fail "clean worktree was not removed"

# The first removal leaves a local branch; creating it again exercises the
# existing-local-branch path.
(
    cd "$MAIN_WORKTREE"
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-worktree-init" \
        users/test/feature local >/dev/null
)
LOCAL_WORKTREE="$PROJECT_ROOT/local"
[ "$(git -C "$LOCAL_WORKTREE" branch --show-current)" = "users/test/feature" ] || \
    fail "existing local branch was not checked out"
(
    cd "$MAIN_WORKTREE"
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-worktree-deinit" local >/dev/null
)

# The clone has the branch only as origin/users/test/remote.
(
    cd "$MAIN_WORKTREE"
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-worktree-init" \
        users/test/remote remote >/dev/null
)
REMOTE_WORKTREE="$PROJECT_ROOT/remote"
[ "$(git -C "$REMOTE_WORKTREE" branch --show-current)" = "users/test/remote" ] || \
    fail "remote branch was not checked out as a tracking branch"
[ "$(git -C "$REMOTE_WORKTREE" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}')" = \
    "origin/users/test/remote" ] || fail "remote branch upstream is incorrect"
[ -f "$REMOTE_WORKTREE/remote.txt" ] || fail "remote branch contents are missing"
(
    cd "$MAIN_WORKTREE"
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-worktree-deinit" remote >/dev/null
)

# Names that differ only by the old prefix special case must remain distinct.
(
    cd "$MAIN_WORKTREE"
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-worktree-init" \
        users/test/prefixed prefixed >/dev/null
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-worktree-init" \
        users/test/example-prefixed example-prefixed >/dev/null
)
PLAIN_PREFIX_WORKTREE="$PROJECT_ROOT/prefixed"
PREFIXED_WORKTREE="$PROJECT_ROOT/example-prefixed"
PLAIN_PREFIX_SESSION=$(project_default_session_name "$PLAIN_PREFIX_WORKTREE")
PREFIXED_SESSION=$(project_default_session_name "$PREFIXED_WORKTREE")
[ "$PLAIN_PREFIX_SESSION" != "$PREFIXED_SESSION" ] ||
    fail "distinct sibling worktrees shared a default tmux session"
[ "$PREFIXED_SESSION" = \
    "example-$PROJECT_SESSION_DIGEST-example-prefixed" ] ||
    fail "prefixed worktree session name lost its complete identity"

# Stopping the session is part of the removal transaction. If quiescence
# produces any final local state, the second full inspection must preserve it.
TMUX_STUB_DIR="$TEST_ROOT/tmux-stub"
TMUX_LOG="$TEST_ROOT/tmux.log"
mkdir -p "$TMUX_STUB_DIR"
cat > "$TMUX_STUB_DIR/tmux" << 'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$TMUX_LOG"
case "$1" in
    has-session)
        [ "${TMUX_HAS_SESSION:-true}" = "true" ]
        exit
        ;;
    kill-session)
        if [ "${TMUX_KILL_FAIL:-false}" = "true" ]; then
            exit 72
        fi
        if [ -n "${TMUX_QUIESCE_WRITE_PATH:-}" ]; then
            printf '%s\n' "state written during session shutdown" \
                > "$TMUX_QUIESCE_WRITE_PATH"
        fi
        ;;
esac
EOF
chmod +x "$TMUX_STUB_DIR/tmux"
ln -s tmux "$TMUX_STUB_DIR/byobu"

if (
    cd "$MAIN_WORKTREE"
    PATH="$TMUX_STUB_DIR:$PATH" \
    TMUX_LOG="$TMUX_LOG" \
    TMUX_KILL_FAIL=true \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-worktree-deinit" \
        prefixed >/dev/null
) >/dev/null 2>&1; then
    fail "worktree removal ignored a failed session shutdown"
fi
[ -d "$PLAIN_PREFIX_WORKTREE" ] ||
    fail "failed session shutdown removed the worktree"
[ -L "$PLAIN_PREFIX_WORKTREE/AGENTS.override.md" ] ||
    fail "failed session shutdown did not restore the managed override"
[ -L "$PLAIN_PREFIX_WORKTREE/.bazelrc.local" ] ||
    fail "failed session shutdown did not restore the managed bazel configuration"

QUIESCED_STATE="$PREFIXED_WORKTREE/quiesced-state"
if (
    cd "$MAIN_WORKTREE"
    PATH="$TMUX_STUB_DIR:$PATH" \
    TMUX_LOG="$TMUX_LOG" \
    TMUX_QUIESCE_WRITE_PATH="$QUIESCED_STATE" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-worktree-deinit" \
        example-prefixed >/dev/null
) >/dev/null 2>&1; then
    fail "worktree removal ignored state written while quiescing its session"
fi
[ -f "$QUIESCED_STATE" ] ||
    fail "tmux quiescence fixture did not write worktree state"
[ -L "$PREFIXED_WORKTREE/AGENTS.override.md" ] ||
    fail "managed override was not restored after the quiescence recheck"
[ -L "$PREFIXED_WORKTREE/.bazelrc.local" ] ||
    fail "managed bazel configuration was not restored after the quiescence recheck"
grep -Fqx "has-session -t =$PREFIXED_SESSION" "$TMUX_LOG" ||
    fail "deinitializer did not look up the exact tmux session"
grep -Fqx "kill-session -t =$PREFIXED_SESSION" "$TMUX_LOG" ||
    fail "deinitializer did not kill the exact tmux session"

unlink "$QUIESCED_STATE"
(
    cd "$MAIN_WORKTREE"
    PATH="$TMUX_STUB_DIR:$PATH" TMUX_LOG="$TMUX_LOG" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-worktree-deinit" \
        example-prefixed >/dev/null
)
[ ! -e "$PREFIXED_WORKTREE" ] ||
    fail "quiesced clean worktree was not removed"
(
    cd "$MAIN_WORKTREE"
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-worktree-deinit" \
        prefixed >/dev/null
)

: > "$TMUX_LOG"
TMUX="" PATH="$TMUX_STUB_DIR:$PATH" TMUX_LOG="$TMUX_LOG" \
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-dev" \
    literal.session "$MAIN_WORKTREE" >/dev/null
grep -Fqx "has-session -t =literal.session" "$TMUX_LOG" ||
    fail "project-dev did not look up the exact tmux session"
grep -Fqx "attach-session -t =literal.session" "$TMUX_LOG" ||
    fail "project-dev did not attach to the exact tmux session"

: > "$TMUX_LOG"
TMUX="" SHELL=/bin/bash TMUX_HAS_SESSION=false \
PATH="$TMUX_STUB_DIR:$PATH" TMUX_LOG="$TMUX_LOG" \
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-dev" \
    fresh.session "$MAIN_WORKTREE" >/dev/null
grep -Fqx "new-session -s fresh.session -c $MAIN_WORKTREE $DOTFILES/lib/project-shell.sh /bin/bash" \
    "$TMUX_LOG" ||
    fail "project-dev did not launch a clean target shell"

if TMUX="" PATH="$TMUX_STUB_DIR:$PATH" TMUX_LOG="$TMUX_LOG" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-dev" \
        "bad:session" "$MAIN_WORKTREE" >/dev/null 2>&1; then
    fail "project-dev accepted a tmux target delimiter in a session name"
fi
if PATH="$TMUX_STUB_DIR:$PATH" TMUX_LOG="$TMUX_LOG" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-deinit" \
        --session "bad:session" --tmux-only --yes \
        "$MAIN_WORKTREE" >/dev/null 2>&1; then
    fail "project-deinit accepted a tmux target delimiter in a session name"
fi

TMUX_ONLY_PROJECT="$TEST_ROOT/tmux-only-project"
TMUX_ONLY_HISTORY="$TEST_ROOT/tmux-only-history"
mkdir -p "$TMUX_ONLY_PROJECT" "$TMUX_ONLY_HISTORY"
ln -s "$TMUX_ONLY_HISTORY" "$TMUX_ONLY_PROJECT/.history"
PATH="$TMUX_STUB_DIR:$PATH" TMUX_LOG="$TMUX_LOG" \
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-deinit" \
    --session tmux-only.session --tmux-only --yes \
    "$TMUX_ONLY_PROJECT" >/dev/null
[ -L "$TMUX_ONLY_PROJECT/.history" ] ||
    fail "tmux-only cleanup inspected or changed project-local paths"
grep -Fqx "kill-session -t =tmux-only.session" "$TMUX_LOG" ||
    fail "tmux-only cleanup did not kill the exact requested session"

unlink "$MAIN_WORKTREE/AGENTS.override.md"
unlink "$MAIN_WORKTREE/.bazelrc.local"
(
    cd "$MAIN_WORKTREE"
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-worktree-init" \
        users/test/plain plain >/dev/null
)
PLAIN_WORKTREE="$PROJECT_ROOT/plain"
[ ! -e "$PLAIN_WORKTREE/AGENTS.override.md" ] || \
    fail "override link was created without a main override"
[ ! -e "$PLAIN_WORKTREE/.bazelrc.local" ] || \
    fail "bazel configuration link was created without a main configuration"
(
    cd "$MAIN_WORKTREE"
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-worktree-deinit" plain >/dev/null
)

# The editor wrapper has one directory operand and must report path failures
# instead of relying on set -e to exit without a mechanism.
CODE_STUB_DIRECTORY="$TEST_ROOT/code-stub"
CODE_LOG="$TEST_ROOT/code.log"
CODE_PROJECT="$TEST_ROOT/code project"
mkdir -p "$CODE_STUB_DIRECTORY" "$CODE_PROJECT"
cat > "$CODE_STUB_DIRECTORY/code" << 'EOF'
#!/bin/bash
printf '%s\n' "$@" > "$CODE_LOG"
EOF
chmod +x "$CODE_STUB_DIRECTORY/code"
printf '{}\n' > "$CODE_PROJECT/code project.code-workspace"
HOME="$TEST_ROOT/code-home" \
PATH="$CODE_STUB_DIRECTORY:$PATH" \
CODE_LOG="$CODE_LOG" \
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-code" \
    "$CODE_PROJECT" -- --reuse-window
[ "$(sed -n '1p' "$CODE_LOG")" = \
    "$CODE_PROJECT/code project.code-workspace" ] ||
    fail "project-code did not select the directory-named workspace"
[ "$(sed -n '2p' "$CODE_LOG")" = "--reuse-window" ] ||
    fail "project-code did not preserve editor arguments after --"
if HOME="$TEST_ROOT/code-home" \
        PATH="$CODE_STUB_DIRECTORY:$PATH" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-code" \
        "$TEST_ROOT/missing-project" >"$TEST_ROOT/code-error" 2>&1; then
    fail "project-code accepted a missing directory"
fi
grep -Fq "directory does not exist" "$TEST_ROOT/code-error" ||
    fail "project-code hid the missing-directory mechanism"
if HOME="$TEST_ROOT/code-home" \
        PATH="$CODE_STUB_DIRECTORY:$PATH" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-code" \
        "$CODE_PROJECT" "$MAIN_WORKTREE" \
        >"$TEST_ROOT/code-error" 2>&1; then
    fail "project-code accepted multiple directory operands"
fi
grep -Fq "unexpected extra directory" "$TEST_ROOT/code-error" ||
    fail "project-code hid the extra-directory mechanism"

if "$BASH_EXECUTABLE" "$DOTFILES/bin/project-deinit" --session \
        >"$TEST_ROOT/deinit-error" 2>&1; then
    fail "project-deinit accepted a missing session name"
fi
grep -Fq "requires a non-empty name" "$TEST_ROOT/deinit-error" ||
    fail "project-deinit hid the missing session-name mechanism"
if "$BASH_EXECUTABLE" "$DOTFILES/bin/project-deinit" \
        "$MAIN_WORKTREE" "$CODE_PROJECT" \
        >"$TEST_ROOT/deinit-error" 2>&1; then
    fail "project-deinit accepted multiple directory operands"
fi
grep -Fq "Unexpected extra directory" "$TEST_ROOT/deinit-error" ||
    fail "project-deinit hid the extra-directory mechanism"
if "$BASH_EXECUTABLE" "$DOTFILES/bin/project-dev" \
        session "$MAIN_WORKTREE" extra \
        >"$TEST_ROOT/dev-error" 2>&1; then
    fail "project-dev silently ignored an extra operand"
fi
grep -Fq "expected at most" "$TEST_ROOT/dev-error" ||
    fail "project-dev hid the extra-operand mechanism"

# Ordinary local state follows the documented cleanup/archive transaction.
CLEANUP_PARENT="$TEST_ROOT/cleanup-parent"
CLEANUP_PROJECT="$CLEANUP_PARENT/project"
mkdir -p "$CLEANUP_PROJECT/.beads" "$CLEANUP_PROJECT/.history"
printf '%s\n' "issue state" > "$CLEANUP_PROJECT/.beads/state"
printf '%s\n' "history state" > "$CLEANUP_PROJECT/.history/state"
printf '%s\n' "environment" > "$CLEANUP_PROJECT/.envrc"
printf '%s\n' "local environment" > "$CLEANUP_PROJECT/.envrc.local"
"$BASH_EXECUTABLE" "$DOTFILES/bin/project-deinit" \
    --yes "$CLEANUP_PROJECT" >/dev/null
[ ! -e "$CLEANUP_PROJECT/.envrc" ] ||
    fail "project-deinit retained the generated environment"
[ ! -e "$CLEANUP_PROJECT/.envrc.local" ] ||
    fail "project-deinit retained the local environment"
[ ! -e "$CLEANUP_PROJECT/.beads" ] ||
    fail "project-deinit retained archived issue state"
[ ! -e "$CLEANUP_PROJECT/.history" ] ||
    fail "project-deinit retained project history"
grep -qxF "issue state" \
    "$CLEANUP_PARENT/attic/project/.beads/state" ||
    fail "project-deinit did not archive issue state"

# Archival must move an owned directory, never follow or relocate symlinks.
SYMLINK_BEADS_PROJECT="$TEST_ROOT/symlink-beads-project"
SYMLINK_BEADS_TARGET="$TEST_ROOT/symlink-beads-target"
mkdir -p "$SYMLINK_BEADS_PROJECT" "$SYMLINK_BEADS_TARGET"
printf '%s\n' "external issue state" > "$SYMLINK_BEADS_TARGET/sentinel"
printf '%s\n' "project environment" > "$SYMLINK_BEADS_PROJECT/.envrc"
ln -s "$SYMLINK_BEADS_TARGET" "$SYMLINK_BEADS_PROJECT/.beads"
if "$BASH_EXECUTABLE" "$DOTFILES/bin/project-deinit" \
        --yes "$SYMLINK_BEADS_PROJECT" >/dev/null 2>&1; then
    fail "project-deinit accepted a symlinked .beads source"
fi
[ -L "$SYMLINK_BEADS_PROJECT/.beads" ] ||
    fail "project-deinit moved the symlinked .beads source"
grep -qxF "external issue state" "$SYMLINK_BEADS_TARGET/sentinel" ||
    fail "project-deinit changed the symlinked .beads target"
grep -qxF "project environment" "$SYMLINK_BEADS_PROJECT/.envrc" ||
    fail "archive preflight failure partially removed project state"

ARCHIVE_PARENT="$TEST_ROOT/archive-parent"
ARCHIVE_PROJECT="$ARCHIVE_PARENT/project"
ARCHIVE_REDIRECT="$TEST_ROOT/archive-redirect"
mkdir -p "$ARCHIVE_PROJECT/.beads" "$ARCHIVE_REDIRECT"
printf '%s\n' "owned issue state" > "$ARCHIVE_PROJECT/.beads/state"
printf '%s\n' "redirect sentinel" > "$ARCHIVE_REDIRECT/sentinel"
printf '%s\n' "project environment" > "$ARCHIVE_PROJECT/.envrc"
ln -s "$ARCHIVE_REDIRECT" "$ARCHIVE_PARENT/attic"
if "$BASH_EXECUTABLE" "$DOTFILES/bin/project-deinit" \
        --yes "$ARCHIVE_PROJECT" >/dev/null 2>&1; then
    fail "project-deinit accepted a symlinked attic root"
fi
[ -f "$ARCHIVE_PROJECT/.beads/state" ] ||
    fail "project-deinit moved issue state through a symlinked attic root"
grep -qxF "redirect sentinel" "$ARCHIVE_REDIRECT/sentinel" ||
    fail "project-deinit changed the symlinked attic target"
grep -qxF "project environment" "$ARCHIVE_PROJECT/.envrc" ||
    fail "attic preflight failure partially removed project state"

ARCHIVE_BEADS_PARENT="$TEST_ROOT/archive-beads-parent"
ARCHIVE_BEADS_PROJECT="$ARCHIVE_BEADS_PARENT/project"
ARCHIVE_BEADS_REDIRECT="$TEST_ROOT/archive-beads-redirect"
mkdir -p \
    "$ARCHIVE_BEADS_PROJECT/.beads" \
    "$ARCHIVE_BEADS_PARENT/attic/project" \
    "$ARCHIVE_BEADS_REDIRECT"
printf '%s\n' "new issue state" > "$ARCHIVE_BEADS_PROJECT/.beads/state"
printf '%s\n' "old archive sentinel" > "$ARCHIVE_BEADS_REDIRECT/sentinel"
printf '%s\n' "project environment" > "$ARCHIVE_BEADS_PROJECT/.envrc"
ln -s "$ARCHIVE_BEADS_REDIRECT" \
    "$ARCHIVE_BEADS_PARENT/attic/project/.beads"
if "$BASH_EXECUTABLE" "$DOTFILES/bin/project-deinit" \
        --yes "$ARCHIVE_BEADS_PROJECT" >/dev/null 2>&1; then
    fail "project-deinit accepted a symlinked archive .beads component"
fi
[ -f "$ARCHIVE_BEADS_PROJECT/.beads/state" ] ||
    fail "project-deinit moved state past a symlinked archive component"
grep -qxF "old archive sentinel" "$ARCHIVE_BEADS_REDIRECT/sentinel" ||
    fail "project-deinit changed the archive .beads symlink target"
grep -qxF "project environment" "$ARCHIVE_BEADS_PROJECT/.envrc" ||
    fail "archive-component preflight partially removed project state"

SYMLINK_HISTORY_PROJECT="$TEST_ROOT/symlink-history-project"
SYMLINK_HISTORY_TARGET="$TEST_ROOT/symlink-history-target"
mkdir -p "$SYMLINK_HISTORY_PROJECT" "$SYMLINK_HISTORY_TARGET"
printf '%s\n' "external history" > "$SYMLINK_HISTORY_TARGET/sentinel"
printf '%s\n' "project environment" > "$SYMLINK_HISTORY_PROJECT/.envrc"
ln -s "$SYMLINK_HISTORY_TARGET" "$SYMLINK_HISTORY_PROJECT/.history"
if "$BASH_EXECUTABLE" "$DOTFILES/bin/project-deinit" \
        --yes "$SYMLINK_HISTORY_PROJECT" >/dev/null 2>&1; then
    fail "project-deinit accepted a symlinked history path"
fi
[ -L "$SYMLINK_HISTORY_PROJECT/.history" ] ||
    fail "project-deinit removed the symlinked history path"
grep -qxF "external history" "$SYMLINK_HISTORY_TARGET/sentinel" ||
    fail "project-deinit changed the symlinked history target"
grep -qxF "project environment" "$SYMLINK_HISTORY_PROJECT/.envrc" ||
    fail "history preflight failure partially removed project state"

echo "project worktree lifecycle passed"
