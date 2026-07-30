#!/bin/bash
# Integration coverage for the project worktree lifecycle commands.

set -euo pipefail

DOTFILES="${DOTFILES:-$HOME/.dotfiles}"
BASH_EXECUTABLE="${BASH:-/bin/bash}"
TEST_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/dotfiles-worktree-test.XXXXXX")
trap 'rm -rf -- "$TEST_ROOT"' EXIT

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

printf 'shared instructions\n' > "$MAIN_WORKTREE/AGENTS.override.md"
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

# shellcheck source=../lib/project-worktrees.sh
. "$DOTFILES/lib/project-worktrees.sh"
[ "$(project_default_session_name "$FEATURE_WORKTREE")" = "example-feature" ] || \
    fail "worktree session name is not repository-qualified"
[ "$(project_default_session_name "$MAIN_WORKTREE")" = "example-main" ] || \
    fail "main session name is not repository-qualified"

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

rm -f "$FEATURE_WORKTREE/untracked.txt"
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

# A worktree name that already includes the repository prefix must use the same
# tmux session name in project-dev and project-worktree-deinit.
(
    cd "$MAIN_WORKTREE"
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-worktree-init" \
        users/test/prefixed example-prefixed >/dev/null
)
PREFIXED_WORKTREE="$PROJECT_ROOT/example-prefixed"
[ "$(project_default_session_name "$PREFIXED_WORKTREE")" = "example-prefixed" ] || \
    fail "prefixed worktree session name was duplicated"
TMUX_STUB_DIR="$TEST_ROOT/tmux-stub"
TMUX_LOG="$TEST_ROOT/tmux.log"
mkdir -p "$TMUX_STUB_DIR"
cat > "$TMUX_STUB_DIR/tmux" << 'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$TMUX_LOG"
case "$1" in
    has-session) exit 0 ;;
esac
EOF
chmod +x "$TMUX_STUB_DIR/tmux"
(
    cd "$MAIN_WORKTREE"
    PATH="$TMUX_STUB_DIR:$PATH" TMUX_LOG="$TMUX_LOG" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-worktree-deinit" \
        example-prefixed >/dev/null
)
grep -Fqx "kill-session -t example-prefixed" "$TMUX_LOG" || \
    fail "deinitializer targeted the wrong tmux session"

rm -f "$MAIN_WORKTREE/AGENTS.override.md"
(
    cd "$MAIN_WORKTREE"
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-worktree-init" \
        users/test/plain plain >/dev/null
)
PLAIN_WORKTREE="$PROJECT_ROOT/plain"
[ ! -e "$PLAIN_WORKTREE/AGENTS.override.md" ] || \
    fail "override link was created without a main override"
(
    cd "$MAIN_WORKTREE"
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-worktree-deinit" plain >/dev/null
)

echo "project worktree lifecycle passed"
