#!/bin/bash
# Behavioral coverage for project-init's multi-artifact idempotency.

set -euo pipefail

DOTFILES="${DOTFILES:-$HOME/.dotfiles}"
BASH_EXECUTABLE="${BASH:-/bin/bash}"
TEST_PARENT="${TMPDIR:-/tmp}"
TEST_ROOT=$(mktemp -d "$TEST_PARENT/dotfiles-project-init-test.XXXXXX")
cleanup_test_root() {
    case "$TEST_ROOT" in
        "$TEST_PARENT"/dotfiles-project-init-test.*) ;;
        *)
            echo "refusing unexpected test cleanup path: $TEST_ROOT" >&2
            return 1
            ;;
    esac
    [ ! -e "$TEST_ROOT" ] || find "$TEST_ROOT" -xdev -depth -delete
}
trap cleanup_test_root EXIT

fail() {
    echo "project init test: $1" >&2
    exit 1
}

assert_envrc_tools() {
    local envrc="$1"
    local expected="$2"
    local actual=""

    actual=$(awk '
        /^use_[a-z_]+([[:space:]]|$)/ {
            tool = $1
            sub(/^use_/, "", tool)
            actual = actual separator tool
            separator = " "
        }
        END { print actual }
    ' "$envrc")
    [ "$actual" = "$expected" ] ||
        fail "$(basename "$(dirname "$envrc")") selected '$actual', expected '$expected'"
}

file_identity() {
    python3 - "$1" << 'PY'
import os
import sys

status = os.stat(sys.argv[1], follow_symlinks=False)
print(f"{status.st_dev}:{status.st_ino}")
PY
}

TEST_HOME="$TEST_ROOT/home"
PROJECT_DIRECTORY="$TEST_ROOT/project"
mkdir -p "$TEST_HOME" "$PROJECT_DIRECTORY"
ln -s "$DOTFILES" "$TEST_HOME/.dotfiles"

# The generated shared environment is a repository input. Only its local
# override and history payload belong in the global excludes file.
IGNORE_PROJECT="$TEST_ROOT/ignore-project"
mkdir -p "$IGNORE_PROJECT/.history"
git init -q "$IGNORE_PROJECT"
printf '%s\n' "unrelated-local-output" > "$IGNORE_PROJECT/.gitignore"
printf '%s\n' "source_local_envrc" > "$IGNORE_PROJECT/.envrc"
printf '%s\n' "machine-only" > "$IGNORE_PROJECT/.envrc.local"
printf '%s\n' "history" > "$IGNORE_PROJECT/.history/state"
if git -C "$IGNORE_PROJECT" \
        -c core.excludesFile="$DOTFILES/git/ignore_global" \
        check-ignore -q .envrc; then
    fail "global or project-local ignore rules hid the committable .envrc"
fi
if git -C "$DOTFILES" check-ignore --no-index -q .envrc; then
    fail "dotfiles project-local ignore rules hid the committable .envrc"
fi
git -C "$IGNORE_PROJECT" \
    -c core.excludesFile="$DOTFILES/git/ignore_global" \
    check-ignore -q .envrc.local ||
    fail "global ignore stopped covering .envrc.local"
git -C "$IGNORE_PROJECT" \
    -c core.excludesFile="$DOTFILES/git/ignore_global" \
    check-ignore -q .history/state ||
    fail "global ignore stopped covering project history"

# Installer transaction state alone is not an installed tool. A failed or
# killed publication must not make a later project request an unusable
# environment.
GUARD_ONLY_HOME="$TEST_ROOT/guard-only-home"
GUARD_ONLY_PROJECT="$TEST_ROOT/guard-only-project"
mkdir -p \
    "$GUARD_ONLY_HOME/tools/cmake/.dotfiles-stage-4.0.0.0123456789abcdef0123456789abcdef" \
    "$GUARD_ONLY_PROJECT"
HOME="$GUARD_ONLY_HOME" \
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
    --build --yes --no-history "$GUARD_ONLY_PROJECT" >/dev/null
if grep -q '^use_cmake' "$GUARD_ONLY_PROJECT/.envrc"; then
    fail "installer transaction state was mistaken for an installed tool"
fi

# Host support comes from the same exact activation contract used by direnv.
# Populate every tool, including stale-but-resolvable selectors, so omissions
# can only come from the host matrix rather than fixture availability.
PLATFORM_MATRIX_HOME="$TEST_ROOT/platform-matrix-home"
PLATFORM_MATRIX_BIN="$TEST_ROOT/platform-matrix-bin"
mkdir -p "$PLATFORM_MATRIX_HOME/tools" "$PLATFORM_MATRIX_BIN"
for tool_version in \
        cmake/4.0.0 \
        ninja/1.13.2 \
        llvm/21.1.6 \
        bazel/8.4.2 \
        mold/2.41.0 \
        cuda/13.0.0 \
        rocm/7.0.0 \
        vulkan/1.4.328.1; do
    mkdir -p "$PLATFORM_MATRIX_HOME/tools/$tool_version"
done
for tool_version in \
        ninja/1.13.2 \
        bazel/8.4.2 \
        mold/2.41.0 \
        cuda/13.0.0 \
        vulkan/1.4.328.1; do
    tool="${tool_version%%/*}"
    version="${tool_version#*/}"
    ln -s "$version" "$PLATFORM_MATRIX_HOME/tools/$tool/latest"
done

cat > "$PLATFORM_MATRIX_BIN/uname" << 'EOF'
#!/bin/sh
case "${1:-}" in
    -s) printf '%s\n' "${PROJECT_INIT_TEST_UNAME_SYSTEM:?}" ;;
    -m) printf '%s\n' "${PROJECT_INIT_TEST_UNAME_ARCHITECTURE:?}" ;;
    *) exit 64 ;;
esac
EOF
SYSTEM_GREP=$(command -v grep)
cat > "$PLATFORM_MATRIX_BIN/grep" << EOF
#!/bin/sh
if [ "\${1:-}" = "-qi" ] &&
        [ "\${2:-}" = "microsoft" ] &&
        [ "\${3:-}" = "/proc/version" ]; then
    [ "\${PROJECT_INIT_TEST_WSL:-false}" = "true" ]
    exit
fi
exec "$SYSTEM_GREP" "\$@"
EOF
cat > "$PLATFORM_MATRIX_BIN/direnv" << 'EOF'
#!/bin/sh
exit 0
EOF
chmod +x \
    "$PLATFORM_MATRIX_BIN/uname" \
    "$PLATFORM_MATRIX_BIN/grep" \
    "$PLATFORM_MATRIX_BIN/direnv"

run_project_init_on_host() {
    local system="$1"
    local architecture="$2"
    local wsl="$3"
    local home="$4"
    local project="$5"
    shift 5

    HOME="$home" \
    PATH="$PLATFORM_MATRIX_BIN:$PATH" \
    PROJECT_INIT_TEST_UNAME_SYSTEM="$system" \
    PROJECT_INIT_TEST_UNAME_ARCHITECTURE="$architecture" \
    PROJECT_INIT_TEST_WSL="$wsl" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
        "$@" "$project" >/dev/null
}

for host_case in \
        "linux-x86:Linux:x86_64:false:cmake ninja llvm bazel cuda rocm vulkan" \
        "linux-arm:Linux:aarch64:false:cmake ninja llvm bazel cuda" \
        "darwin-x86:Darwin:x86_64:false:cmake ninja bazel" \
        "darwin-arm:Darwin:arm64:false:cmake ninja llvm bazel" \
        "wsl-x86:Linux:x86_64:true:cmake ninja llvm bazel vulkan" \
        "unknown:FreeBSD:riscv64:false:"; do
    host_name="${host_case%%:*}"
    host_rest="${host_case#*:}"
    host_system="${host_rest%%:*}"
    host_rest="${host_rest#*:}"
    host_architecture="${host_rest%%:*}"
    host_rest="${host_rest#*:}"
    host_wsl="${host_rest%%:*}"
    expected_tools="${host_rest#*:}"
    host_project="$TEST_ROOT/platform-$host_name"
    mkdir -p "$host_project"
    run_project_init_on_host \
        "$host_system" "$host_architecture" "$host_wsl" \
        "$PLATFORM_MATRIX_HOME" "$host_project" \
        --all --yes --no-history
    assert_envrc_tools "$host_project/.envrc" "$expected_tools"
done

UNSUPPORTED_MOLD_PROJECT="$TEST_ROOT/unsupported-mold-project"
mkdir -p "$UNSUPPORTED_MOLD_PROJECT"
if run_project_init_on_host \
        Darwin x86_64 false \
        "$PLATFORM_MATRIX_HOME" "$UNSUPPORTED_MOLD_PROJECT" \
        --build --mold --yes --no-history >/dev/null 2>&1; then
    fail "Intel macOS accepted an unsupported explicit mold selection"
fi
[ ! -e "$UNSUPPORTED_MOLD_PROJECT/.envrc" ] ||
    fail "unsupported explicit mold selection created an environment"

UNSUPPORTED_LLVM_WORKSPACE_PROJECT="$TEST_ROOT/unsupported-llvm-workspace-project"
mkdir -p "$UNSUPPORTED_LLVM_WORKSPACE_PROJECT"
if run_project_init_on_host \
        Darwin x86_64 false \
        "$PLATFORM_MATRIX_HOME" "$UNSUPPORTED_LLVM_WORKSPACE_PROJECT" \
        --no-env --vscode --llvm >/dev/null 2>&1; then
    fail "Intel macOS used a stale Linux LLVM root in a workspace"
fi
[ ! -e "$UNSUPPORTED_LLVM_WORKSPACE_PROJECT/unsupported-llvm-workspace-project.code-workspace" ] ||
    fail "unsupported LLVM workspace integration created output"

# A copied or dangling latest selector is corrupt publication state, not
# permission to select an arbitrary numeric sibling.
OBSTRUCTED_HOME="$TEST_ROOT/obstructed-latest-home"
mkdir -p \
    "$OBSTRUCTED_HOME/tools/ninja/1.13.2" \
    "$OBSTRUCTED_HOME/tools/ninja/latest"
for selector_state in copied dangling; do
    obstructed_project="$TEST_ROOT/obstructed-$selector_state-project"
    mkdir -p "$obstructed_project"
    run_project_init_on_host \
        Linux x86_64 false \
        "$OBSTRUCTED_HOME" "$obstructed_project" \
        --build --yes --no-history
    assert_envrc_tools "$obstructed_project/.envrc" ""
    if [ "$selector_state" = "copied" ]; then
        rmdir "$OBSTRUCTED_HOME/tools/ninja/latest"
        ln -s missing "$OBSTRUCTED_HOME/tools/ninja/latest"
    fi
done
unlink "$OBSTRUCTED_HOME/tools/ninja/latest"
NUMERIC_FALLBACK_PROJECT="$TEST_ROOT/numeric-fallback-project"
mkdir -p "$NUMERIC_FALLBACK_PROJECT"
run_project_init_on_host \
    Linux x86_64 false \
    "$OBSTRUCTED_HOME" "$NUMERIC_FALLBACK_PROJECT" \
    --build --yes --no-history
assert_envrc_tools "$NUMERIC_FALLBACK_PROJECT/.envrc" "ninja"

run_project_init() {
    HOME="$TEST_HOME" \
    XDG_CACHE_HOME="$TEST_HOME/.cache" \
    XDG_CONFIG_HOME="$TEST_HOME/.config" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
        --all --yes --no-history --vscode --claude \
        "$PROJECT_DIRECTORY" >/dev/null
}

run_project_init
WORKSPACE="$PROJECT_DIRECTORY/project.code-workspace"
CLAUDE_SETTINGS="$PROJECT_DIRECTORY/.claude/settings.local.json"
LOCAL_ENVRC="$PROJECT_DIRECTORY/.envrc.local"
[ -f "$PROJECT_DIRECTORY/.envrc" ] || fail "environment file was not created"
[ -f "$LOCAL_ENVRC" ] || fail "local environment file was not created"
[ -f "$WORKSPACE" ] || fail "workspace was not created"
[ -f "$CLAUDE_SETTINGS" ] || fail "Claude settings were not created"

# An unchanged .envrc must not suppress explicitly requested missing outputs.
LOCAL_ENVRC_CONTENT="user-owned local environment"
printf '%s\n' "$LOCAL_ENVRC_CONTENT" > "$LOCAL_ENVRC"
unlink "$WORKSPACE"
unlink "$CLAUDE_SETTINGS"
run_project_init
[ -f "$WORKSPACE" ] || fail "missing workspace was not regenerated"
[ -f "$CLAUDE_SETTINGS" ] || fail "missing Claude settings were not regenerated"
[ "$(cat "$LOCAL_ENVRC")" = "$LOCAL_ENVRC_CONTENT" ] ||
    fail "user-owned local environment file was overwritten"

# Missing ancillary state must not be hidden by an otherwise-idempotent run.
unlink "$LOCAL_ENVRC"
run_project_init
[ -f "$LOCAL_ENVRC" ] ||
    fail "missing local environment file was not regenerated"

HISTORY_PROJECT_DIRECTORY="$TEST_ROOT/history-project"
mkdir -p "$HISTORY_PROJECT_DIRECTORY"
HOME="$TEST_HOME" \
XDG_CACHE_HOME="$TEST_HOME/.cache" \
XDG_CONFIG_HOME="$TEST_HOME/.config" \
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
    --all --yes "$HISTORY_PROJECT_DIRECTORY" >/dev/null
[ -d "$HISTORY_PROJECT_DIRECTORY/.history" ] ||
    fail "history directory was not created"
rmdir "$HISTORY_PROJECT_DIRECTORY/.history"
HOME="$TEST_HOME" \
XDG_CACHE_HOME="$TEST_HOME/.cache" \
XDG_CONFIG_HOME="$TEST_HOME/.config" \
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
    --all --yes "$HISTORY_PROJECT_DIRECTORY" >/dev/null
[ -d "$HISTORY_PROJECT_DIRECTORY/.history" ] ||
    fail "missing history directory was not regenerated"

# A retained custom history directive owns an opaque shell-escaped path. An
# ordinary rerun preserves it and must not materialize the unrelated default.
CUSTOM_HISTORY_PROJECT="$TEST_ROOT/custom-history-project"
mkdir -p "$CUSTOM_HISTORY_PROJECT"
HOME="$TEST_HOME" \
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
    --all --yes --history-path shared-history \
    "$CUSTOM_HISTORY_PROJECT" >/dev/null
rmdir "$CUSTOM_HISTORY_PROJECT/shared-history"
HOME="$TEST_HOME" \
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
    --all --yes "$CUSTOM_HISTORY_PROJECT" >/dev/null
grep -q '^use_project_history shared-history$' \
    "$CUSTOM_HISTORY_PROJECT/.envrc" ||
    fail "custom history directive did not survive convergence"
[ ! -e "$CUSTOM_HISTORY_PROJECT/.history" ] ||
    fail "retained custom history directive created the default history path"

# Project-init owns the generated structure but preserves supported directive
# arguments exactly. Mold is explicit-only: --all never adds it, while an
# existing opt-in survives later build-preset convergence.
ROUNDTRIP_HOME="$TEST_ROOT/roundtrip-home"
ROUNDTRIP_PROJECT="$TEST_ROOT/roundtrip-project"
ROUNDTRIP_ALL_PROJECT="$TEST_ROOT/roundtrip-all-project"
mkdir -p \
    "$ROUNDTRIP_HOME/tools/llvm/21.1.6" \
    "$ROUNDTRIP_HOME/tools/mold/2.41.0" \
    "$ROUNDTRIP_PROJECT" \
    "$ROUNDTRIP_ALL_PROJECT"
run_project_init_on_host \
    Linux x86_64 false \
    "$ROUNDTRIP_HOME" "$ROUNDTRIP_PROJECT" \
    --build --mold --yes --no-history
grep -qxF 'use_mold' "$ROUNDTRIP_PROJECT/.envrc" ||
    fail "explicit mold selection was not generated"
sed 's/use_llvm ">=21.0.0"/use_llvm "21.1.6"/' \
    "$ROUNDTRIP_PROJECT/.envrc" \
    > "$ROUNDTRIP_PROJECT/.envrc.edited"
mv "$ROUNDTRIP_PROJECT/.envrc.edited" "$ROUNDTRIP_PROJECT/.envrc"
HOME="$ROUNDTRIP_HOME" \
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
    --build --yes --no-history "$ROUNDTRIP_PROJECT" >/dev/null
grep -qxF 'use_llvm "21.1.6"' "$ROUNDTRIP_PROJECT/.envrc" ||
    fail "project convergence reset an edited tool requirement"
grep -qxF 'use_mold' "$ROUNDTRIP_PROJECT/.envrc" ||
    fail "project convergence discarded the explicit mold opt-in"

HOME="$ROUNDTRIP_HOME" \
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
    --all --yes --no-history "$ROUNDTRIP_ALL_PROJECT" >/dev/null
if grep -q '^use_mold' "$ROUNDTRIP_ALL_PROJECT/.envrc"; then
    fail "--all selected the explicit-only mold environment"
fi

# A canceled selector is not an empty selection. Preserve the prior file and
# require the explicit --none route before clearing every tool, including
# durable explicit-only choices.
FZF_PROJECT="$TEST_ROOT/fzf-project"
FZF_BIN="$TEST_ROOT/fzf-bin"
mkdir -p "$FZF_PROJECT" "$FZF_BIN"
run_project_init_on_host \
    Linux x86_64 false \
    "$ROUNDTRIP_HOME" "$FZF_PROJECT" \
    --build --mold --yes --no-history
cp "$FZF_PROJECT/.envrc" "$FZF_PROJECT/.envrc.before-cancel"
cat > "$FZF_BIN/fzf" << 'EOF'
#!/bin/bash
exit 130
EOF
chmod +x "$FZF_BIN/fzf"
if HOME="$ROUNDTRIP_HOME" \
        PATH="$FZF_BIN:$PATH" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
        --yes "$FZF_PROJECT" >/dev/null 2>&1; then
    fail "canceled fzf selection was treated as success"
fi
cmp -s "$FZF_PROJECT/.envrc.before-cancel" "$FZF_PROJECT/.envrc" ||
    fail "canceled fzf selection changed the project environment"

HOME="$ROUNDTRIP_HOME" \
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
    --none --yes --no-history "$FZF_PROJECT" >/dev/null
if grep -q '^use_' "$FZF_PROJECT/.envrc"; then
    fail "--none retained a tool directive"
fi
cp "$FZF_PROJECT/.envrc" "$FZF_PROJECT/.envrc.none"
if HOME="$ROUNDTRIP_HOME" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
        --none --build --yes --no-history "$FZF_PROJECT" \
        >/dev/null 2>&1; then
    fail "--none accepted a competing tool-selection preset"
fi
cmp -s "$FZF_PROJECT/.envrc.none" "$FZF_PROJECT/.envrc" ||
    fail "rejected --none combination changed the environment"

printf '%s\n' 'export PROJECT_SHARED_VALUE=kept' \
    >> "$ROUNDTRIP_PROJECT/.envrc"
cp "$ROUNDTRIP_PROJECT/.envrc" "$ROUNDTRIP_PROJECT/.envrc.with-unmanaged"
if HOME="$ROUNDTRIP_HOME" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
        --build --yes --no-history "$ROUNDTRIP_PROJECT" \
        >/dev/null 2>&1; then
    fail "project convergence accepted unmanaged content it cannot retain"
fi
cmp -s \
    "$ROUNDTRIP_PROJECT/.envrc.with-unmanaged" \
    "$ROUNDTRIP_PROJECT/.envrc" ||
    fail "rejected project convergence changed unmanaged content"

# Files generated by either merge parent had no strict-import line and could
# explicitly retain a project virtualenv. Converge that exact old shape once
# without restoring automatic Python selection.
LEGACY_ENVRC_PROJECT="$TEST_ROOT/legacy-envrc-project"
mkdir -p "$LEGACY_ENVRC_PROJECT"
cat > "$LEGACY_ENVRC_PROJECT/.envrc" << 'EOF'
# Project environment - managed by direnv.
# Edit tool versions as needed. Run 'direnv allow' after changes.

use_venv ".venv"

# Load machine-specific overrides if present.
source_local_envrc
EOF
HOME="$TEST_HOME" \
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
    --build --yes --no-history "$LEGACY_ENVRC_PROJECT" >/dev/null
grep -qxF 'set -o errexit -o pipefail' \
    "$LEGACY_ENVRC_PROJECT/.envrc" ||
    fail "prior generated environment was not upgraded to strict imports"
grep -qxF 'use_venv ".venv"' "$LEGACY_ENVRC_PROJECT/.envrc" ||
    fail "prior explicit virtualenv directive was discarded"

# Explicit directory-valued options must not compete with the project path.
CUSTOM_PROJECT_DIRECTORY="$TEST_ROOT/custom-project"
CUSTOM_BUILD_DIRECTORY="$TEST_ROOT/custom-build-one"
CUSTOM_CLAUDE_DIRECTORY="$TEST_ROOT/custom-claude-one"
mkdir -p "$CUSTOM_PROJECT_DIRECTORY"
HOME="$TEST_HOME" \
XDG_CACHE_HOME="$TEST_HOME/.cache" \
XDG_CONFIG_HOME="$TEST_HOME/.config" \
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
    --all --yes --no-history --vscode \
    --cmake-build-dir "$CUSTOM_BUILD_DIRECTORY" \
    --claude-build-dir "$CUSTOM_CLAUDE_DIRECTORY" \
    "$CUSTOM_PROJECT_DIRECTORY" >/dev/null
CUSTOM_WORKSPACE="$CUSTOM_PROJECT_DIRECTORY/custom-project.code-workspace"
CUSTOM_CLAUDE_SETTINGS="$CUSTOM_PROJECT_DIRECTORY/.claude/settings.local.json"
grep -qF "$CUSTOM_BUILD_DIRECTORY" "$CUSTOM_WORKSPACE" ||
    fail "explicit CMake build directory was not retained"
grep -qF "Edit($CUSTOM_CLAUDE_DIRECTORY)" "$CUSTOM_CLAUDE_SETTINGS" ||
    fail "explicit Claude build directory was not retained"

# Requested generated outputs converge when their directory options change.
UPDATED_BUILD_DIRECTORY="$TEST_ROOT/custom-build-two"
UPDATED_CLAUDE_DIRECTORY="$TEST_ROOT/custom-claude-two"
HOME="$TEST_HOME" \
XDG_CACHE_HOME="$TEST_HOME/.cache" \
XDG_CONFIG_HOME="$TEST_HOME/.config" \
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
    --all --yes --no-history --vscode \
    --cmake-build-dir "$UPDATED_BUILD_DIRECTORY" \
    --claude-build-dir "$UPDATED_CLAUDE_DIRECTORY" \
    "$CUSTOM_PROJECT_DIRECTORY" >/dev/null
grep -qF "$UPDATED_BUILD_DIRECTORY" "$CUSTOM_WORKSPACE" ||
    fail "changed CMake build directory did not update the workspace"
if grep -qF "$CUSTOM_BUILD_DIRECTORY" "$CUSTOM_WORKSPACE"; then
    fail "workspace retained the superseded CMake build directory"
fi
grep -qF "Edit($UPDATED_CLAUDE_DIRECTORY)" "$CUSTOM_CLAUDE_SETTINGS" ||
    fail "changed Claude build directory did not update settings"
if grep -qF "$CUSTOM_CLAUDE_DIRECTORY" "$CUSTOM_CLAUDE_SETTINGS"; then
    fail "Claude settings retained the superseded build directory"
fi

# Generated shell and JSON content must preserve arbitrary path bytes without
# turning them into shell syntax or sed replacement syntax.
QUOTED_HISTORY_PROJECT="$TEST_ROOT/quoted-history-project"
QUOTED_HISTORY_DIRECTORY="history \$(touch injected-marker)"
mkdir -p "$QUOTED_HISTORY_PROJECT"
HOME="$TEST_HOME" \
XDG_CACHE_HOME="$TEST_HOME/.cache" \
XDG_CONFIG_HOME="$TEST_HOME/.config" \
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
    --build --yes --history-path "$QUOTED_HISTORY_DIRECTORY" \
    "$QUOTED_HISTORY_PROJECT" >/dev/null
[ -d "$QUOTED_HISTORY_PROJECT/$QUOTED_HISTORY_DIRECTORY" ] ||
    fail "custom history directory was not created literally"
[ ! -e "$QUOTED_HISTORY_PROJECT/.history" ] ||
    fail "custom history path also created the unused default directory"
(
    cd "$QUOTED_HISTORY_PROJECT"
    # shellcheck disable=SC2317  # Invoked by the generated .envrc.
    use_project_history() {
        printf '%s\n' "$1" > "$TEST_ROOT/captured-history-path"
    }
    # shellcheck disable=SC2317  # Invoked by the generated .envrc.
    source_local_envrc() { :; }
    # shellcheck source=/dev/null
    . ./.envrc
)
[ ! -e "$QUOTED_HISTORY_PROJECT/injected-marker" ] ||
    fail "history path executed generated shell syntax"
[ "$(cat "$TEST_ROOT/captured-history-path")" = \
    "$QUOTED_HISTORY_DIRECTORY" ] ||
    fail "generated history path did not round-trip"

# Generated activation must preserve the first failing helper instead of
# allowing a later successful line to mask it.
# An external Bash keeps its errexit behavior independent of the parent test's
# conditional context.
# shellcheck disable=SC2016  # Expanded by the child shell.
if "$BASH_EXECUTABLE" -c '
    cd -- "$1"
    use_project_history() { return 42; }
    source_local_envrc() { :; }
    . ./.envrc
' sh "$QUOTED_HISTORY_PROJECT"; then
    fail "generated environment masked a history activation failure"
fi
# shellcheck disable=SC2016  # Expanded by the child shell.
if "$BASH_EXECUTABLE" -c '
    cd -- "$1"
    use_project_history() { :; }
    source_local_envrc() { return 43; }
    . ./.envrc
' sh "$QUOTED_HISTORY_PROJECT"; then
    fail "generated environment masked a local override failure"
fi

# The direnv helper resolves a relative history directory once, after creating
# it successfully, so later directory changes do not retarget history.
(
    # shellcheck disable=SC2317  # Invoked while sourcing direnvrc.
    source_env() {
        # shellcheck source=/dev/null
        . "$1"
    }
    # shellcheck disable=SC2317  # Invoked while sourcing direnvrc.
    watch_file() { :; }
    # shellcheck disable=SC2317  # Invoked by direnvrc on source failure.
    log_error() {
        printf '%s\n' "$*" >&2
    }
    # shellcheck source=/dev/null
    . "$DOTFILES/tools/direnvrc"
    RELATIVE_HISTORY_ROOT="$TEST_ROOT/relative-history"
    mkdir -p "$RELATIVE_HISTORY_ROOT/project/child"
    cd "$RELATIVE_HISTORY_ROOT/project"
    use_project_history shared-history
    expected_history="$RELATIVE_HISTORY_ROOT/project/shared-history"
    [ "$HISTORY_BASE" = "$expected_history" ] ||
        fail "relative history path was not resolved physically"
    cd child
    [ -d "$HISTORY_BASE" ] ||
        fail "relative history path changed meaning after cd"
    printf '%s\n' "history obstruction" > blocked-history
    prior_history_base="$HISTORY_BASE"
    if use_project_history blocked-history 2>/dev/null; then
        fail "history helper accepted a regular-file destination"
    fi
    [ "$HISTORY_BASE" = "$prior_history_base" ] ||
        fail "failed history activation exported a bad destination"
)

# Exercise the real direnv evaluator: source_env performs successful cleanup
# after sourcing and can otherwise hide a rejected imported environment.
DIRENV_PROJECT="$TEST_ROOT/direnv-project"
DIRENV_CMAKE_ROOT="$TEST_HOME/tools/cmake/4.0.0"
DIRENV_LOG="$TEST_ROOT/direnv.log"
mkdir -p "$DIRENV_PROJECT" "$DIRENV_CMAKE_ROOT/bin"
ln -s "$DOTFILES/tools/direnvrc" "$TEST_HOME/.direnvrc"
cat > "$DIRENV_CMAKE_ROOT/bin/cmake" << 'EOF'
#!/bin/sh
exit 0
EOF
chmod +x "$DIRENV_CMAKE_ROOT/bin/cmake"
ln -s "4.0.0" "$TEST_HOME/tools/cmake/latest"
HOME="$TEST_HOME" \
XDG_CACHE_HOME="$TEST_HOME/.cache" \
XDG_CONFIG_HOME="$TEST_HOME/.config" \
XDG_DATA_HOME="$TEST_HOME/.local/share" \
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
    --build --yes --no-history "$DIRENV_PROJECT" >/dev/null
HOME="$TEST_HOME" \
XDG_CACHE_HOME="$TEST_HOME/.cache" \
XDG_CONFIG_HOME="$TEST_HOME/.config" \
XDG_DATA_HOME="$TEST_HOME/.local/share" \
    direnv allow "$DIRENV_PROJECT"
# shellcheck disable=SC2016  # Expanded by the child shell under direnv.
HOME="$TEST_HOME" \
XDG_CACHE_HOME="$TEST_HOME/.cache" \
XDG_CONFIG_HOME="$TEST_HOME/.config" \
XDG_DATA_HOME="$TEST_HOME/.local/share" \
    direnv exec "$DIRENV_PROJECT" /bin/sh -c \
    '[ "$CMAKE_ROOT" = "$1" ]' sh "$DIRENV_CMAKE_ROOT" ||
    fail "valid real-direnv activation failed"
DIRENV_STATUS_LOG="$TEST_ROOT/direnv-status.log"
# shellcheck disable=SC2016  # Expanded by the child shell under direnv.
HOME="$TEST_HOME" \
XDG_CACHE_HOME="$TEST_HOME/.cache" \
XDG_CONFIG_HOME="$TEST_HOME/.config" \
XDG_DATA_HOME="$TEST_HOME/.local/share" \
    direnv exec "$DIRENV_PROJECT" /bin/sh -c \
    'cd "$1" && exec direnv status' sh "$DIRENV_PROJECT" \
    >"$DIRENV_STATUS_LOG" 2>&1 ||
    fail "could not inspect the real direnv watched closure"
for watched_environment in \
        ".dotfiles/tools/direnvrc" \
        ".dotfiles/tools/platform.sh" \
        ".dotfiles/tools/versions.sh" \
        ".dotfiles/tools/cmake/env.sh"; do
    if ! grep -qF "$watched_environment" "$DIRENV_STATUS_LOG"; then
        sed -n '/Loaded watch/,/Loaded RC/p' "$DIRENV_STATUS_LOG" >&2
        fail "real direnv did not watch $watched_environment"
    fi
done

cat > "$DIRENV_PROJECT/.envrc.local" << 'EOF'
export DOTFILES_REJECTED_LOCAL_ENVIRONMENT=leaked
false
export DOTFILES_AFTER_REJECTED_LOCAL_ENVIRONMENT=also-leaked
EOF
DIRENV_TOUCH_EXECUTABLE=$(command -v touch)
DIRENV_LOCAL_MARKER="$TEST_ROOT/rejected-local-child-ran"
if HOME="$TEST_HOME" \
        XDG_CACHE_HOME="$TEST_HOME/.cache" \
        XDG_CONFIG_HOME="$TEST_HOME/.config" \
        XDG_DATA_HOME="$TEST_HOME/.local/share" \
        direnv exec "$DIRENV_PROJECT" \
        "$DIRENV_TOUCH_EXECUTABLE" "$DIRENV_LOCAL_MARKER" \
        >"$DIRENV_LOG" 2>&1; then
    fail "real direnv accepted a failing local environment"
fi
[ ! -e "$DIRENV_LOCAL_MARKER" ] ||
    fail "real direnv ran a child with a rejected local environment"

printf '%s\n' : > "$DIRENV_PROJECT/.envrc.local"
unlink "$DIRENV_CMAKE_ROOT/bin/cmake"
DIRENV_TOOL_MARKER="$TEST_ROOT/rejected-tool-child-ran"
if HOME="$TEST_HOME" \
        XDG_CACHE_HOME="$TEST_HOME/.cache" \
        XDG_CONFIG_HOME="$TEST_HOME/.config" \
        XDG_DATA_HOME="$TEST_HOME/.local/share" \
        direnv exec "$DIRENV_PROJECT" \
        "$DIRENV_TOUCH_EXECUTABLE" "$DIRENV_TOOL_MARKER" \
        >"$DIRENV_LOG" 2>&1; then
    fail "real direnv accepted an incomplete CMake root"
fi
[ ! -e "$DIRENV_TOOL_MARKER" ] ||
    fail "real direnv ran a child with a rejected CMake root"
grep -qF "CMake root is incomplete" "$DIRENV_LOG" ||
    fail "real direnv did not report the rejected CMake root"

JSON_PROJECT_DIRECTORY="$TEST_ROOT/json-project"
JSON_BUILD_DIRECTORY="$TEST_ROOT/build & pipe|quote\"backslash\\"
mkdir -p "$JSON_PROJECT_DIRECTORY"
HOME="$TEST_HOME" \
XDG_CACHE_HOME="$TEST_HOME/.cache" \
XDG_CONFIG_HOME="$TEST_HOME/.config" \
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
    --no-env --vscode --cmake \
    --cmake-build-dir "$JSON_BUILD_DIRECTORY" \
    "$JSON_PROJECT_DIRECTORY" >/dev/null
jq -e --arg expected "$JSON_BUILD_DIRECTORY" \
    '.settings["cmake.buildDirectory"] == $expected' \
    "$JSON_PROJECT_DIRECTORY/json-project.code-workspace" >/dev/null ||
    fail "JSON template path did not round-trip"

# Workspace-only mode rejects environment and Claude options instead of
# silently dropping them.
NO_ENV_REJECT_PROJECT="$TEST_ROOT/no-env-reject-project"
mkdir -p "$NO_ENV_REJECT_PROJECT"
if HOME="$TEST_HOME" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
        --no-env --vscode --build \
        "$NO_ENV_REJECT_PROJECT" >/dev/null 2>&1; then
    fail "--no-env silently ignored an environment-selection option"
fi
if HOME="$TEST_HOME" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
        --no-env --vscode --claude-build-dir ./claude-build \
        "$NO_ENV_REJECT_PROJECT" >/dev/null 2>&1; then
    fail "--no-env silently ignored Claude settings"
fi
[ ! -e "$NO_ENV_REJECT_PROJECT/no-env-reject-project.code-workspace" ] ||
    fail "rejected --no-env options left a partial workspace"
[ ! -e "$NO_ENV_REJECT_PROJECT/.claude" ] ||
    fail "rejected --no-env options left partial Claude state"

# Comments are not environment activations. A commented ROCm example must not
# make workspace-only generation resolve an unavailable SDK.
COMMENTED_ROCM_PROJECT="$TEST_ROOT/commented-rocm-project"
mkdir -p "$COMMENTED_ROCM_PROJECT"
printf '%s\n' '# use_rocm ">=6.0.0"' > "$COMMENTED_ROCM_PROJECT/.envrc"
HOME="$TEST_HOME" \
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
    --no-env --vscode "$COMMENTED_ROCM_PROJECT" >/dev/null
jq -e '.settings["cmake.environment"] == null' \
    "$COMMENTED_ROCM_PROJECT/commented-rocm-project.code-workspace" \
    >/dev/null ||
    fail "commented ROCm directive enabled workspace integration"

# Replacements operate on the original template string exactly once. A tool
# root containing another token must remain literal.
LLVM_VERSION_WITH_TOKEN="home-\$BUILD_DIR-sentinel"
LLVM_ROOT_WITH_TOKEN="$TEST_HOME/tools/llvm/$LLVM_VERSION_WITH_TOKEN"
LLVM_TOKEN_PROJECT="$TEST_ROOT/llvm-token-project"
mkdir -p "$LLVM_ROOT_WITH_TOKEN" "$LLVM_TOKEN_PROJECT"
ln -s "$LLVM_VERSION_WITH_TOKEN" "$TEST_HOME/tools/llvm/latest"
HOME="$TEST_HOME" \
XDG_CACHE_HOME="$TEST_HOME/.cache" \
XDG_CONFIG_HOME="$TEST_HOME/.config" \
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
    --no-env --vscode --llvm \
    --cmake-build-dir "$JSON_BUILD_DIRECTORY" \
    "$LLVM_TOKEN_PROJECT" >/dev/null
jq -e --arg expected "$LLVM_ROOT_WITH_TOKEN/bin/clangd" \
    '.settings["clangd.path"] == $expected' \
    "$LLVM_TOKEN_PROJECT/llvm-token-project.code-workspace" >/dev/null ||
    fail "JSON template replacements cascaded through a replacement value"

# Filename-bearing options and generated destinations fail before escaping the
# project or following a user-controlled symlink.
if HOME="$TEST_HOME" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
        --no-env --vscode --vscode-name ../escaped \
        "$JSON_PROJECT_DIRECTORY" >/dev/null 2>&1; then
    fail "workspace name accepted a parent traversal"
fi
[ ! -e "$TEST_ROOT/escaped.code-workspace" ] ||
    fail "workspace name escaped the project"
if HOME="$TEST_HOME" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
        --no-env --vscode --vscode-settings missing.json \
        "$JSON_PROJECT_DIRECTORY" >/dev/null 2>&1; then
    fail "missing extra VS Code settings were silently ignored"
fi
if HOME="$TEST_HOME" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
        --no-env --vscode --vscode-name "" \
        "$JSON_PROJECT_DIRECTORY" >/dev/null 2>&1; then
    fail "workspace generation accepted an explicit empty name"
fi
if HOME="$TEST_HOME" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
        --no-env --vscode --vscode-settings "" \
        "$JSON_PROJECT_DIRECTORY" >/dev/null 2>&1; then
    fail "workspace generation accepted an explicit empty settings path"
fi
printf '%s\n' '{"files.trimTrailingWhitespace":false}' \
    > "$JSON_PROJECT_DIRECTORY/-settings.json"
HOME="$TEST_HOME" \
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
    --no-env --vscode --vscode-name -experiment \
    --vscode-settings -settings.json \
    "$JSON_PROJECT_DIRECTORY" >/dev/null
OPTION_SAFE_WORKSPACE="$JSON_PROJECT_DIRECTORY/-experiment.code-workspace"
[ -f "$OPTION_SAFE_WORKSPACE" ] ||
    fail "option-safe workspace name was not published"
jq -e '.settings["files.trimTrailingWhitespace"] == false' \
    -- "$OPTION_SAFE_WORKSPACE" >/dev/null ||
    fail "leading-hyphen settings filename was not merged"
OPTION_SAFE_IDENTITY=$(file_identity "$OPTION_SAFE_WORKSPACE")
HOME="$TEST_HOME" \
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
    --no-env --vscode --vscode-name -experiment \
    --vscode-settings -settings.json \
    "$JSON_PROJECT_DIRECTORY" >/dev/null
[ "$(file_identity "$OPTION_SAFE_WORKSPACE")" = \
    "$OPTION_SAFE_IDENTITY" ] ||
    fail "unchanged leading-hyphen workspace was republished"

# The color identity hashes project names as bytes. A name that is itself an
# echo option must not collapse to the empty-string checksum.
HOME="$TEST_HOME" \
    "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
    --no-env --vscode --vscode-name -n \
    "$JSON_PROJECT_DIRECTORY" >/dev/null
jq -e \
    '.settings["workbench.colorCustomizations"]
        ["[Default Dark Modern]"]["titleBar.activeBackground"] == "#845864"' \
    -- "$JSON_PROJECT_DIRECTORY/-n.code-workspace" >/dev/null ||
    fail "leading-hyphen project name did not retain its color identity"

LEADING_TARGET_PARENT="$TEST_ROOT/leading-target-parent"
mkdir -p "$LEADING_TARGET_PARENT"
(
    cd "$LEADING_TARGET_PARENT"
    HOME="$TEST_HOME" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
        --no-env --vscode -- -project >/dev/null
)
[ -f "$LEADING_TARGET_PARENT/-project/-project.code-workspace" ] ||
    fail "end-of-options did not preserve a leading-hyphen project path"
if (
    cd "$LEADING_TARGET_PARENT"
    HOME="$TEST_HOME" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
        --no-env --vscode -- -one -two >/dev/null 2>&1
); then
    fail "end-of-options accepted multiple project directories"
fi

# Directory obstructions are rejected before any requested file publication.
OBSTRUCTED_PROJECT="$TEST_ROOT/obstructed-project"
mkdir -p "$OBSTRUCTED_PROJECT"
printf '%s\n' "not a directory" > "$OBSTRUCTED_PROJECT/blocked-parent"
if HOME="$TEST_HOME" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
        --build --yes --vscode --claude \
        --history-path "blocked-parent/history" \
        "$OBSTRUCTED_PROJECT" >/dev/null 2>&1; then
    fail "project initialization accepted an obstructed history path"
fi
for unexpected_output in \
        .envrc \
        .envrc.local \
        obstructed-project.code-workspace \
        .claude/settings.local.json; do
    [ ! -e "$OBSTRUCTED_PROJECT/$unexpected_output" ] ||
        fail "directory preflight left partial output: $unexpected_output"
done

# Every replaceable output is generation-bound before interactive selection.
# An editor save while fzf is open aborts the complete publication set before
# any sibling output changes.
GENERATION_RACE_BIN="$TEST_ROOT/generation-race-bin"
mkdir -p "$GENERATION_RACE_BIN"
cat > "$GENERATION_RACE_BIN/fzf" << 'EOF'
#!/bin/bash
case "$*" in
    *"Select Build Tools:"*)
        if [ ! -e "$GENERATION_RACE_MARKER" ]; then
            printf '%s\n' "user edit during interactive selection" \
                > "$GENERATION_RACE_TARGET"
            : > "$GENERATION_RACE_MARKER"
        fi
        IFS= read -r selected || exit 0
        printf '%s\n' "$selected"
        ;;
    *"Per-project history:"*)
        while IFS= read -r option; do
            case "$option" in
                no$'\t'*)
                    printf '%s\n' "$option"
                    exit 0
                    ;;
            esac
        done
        exit 1
        ;;
    *) exit 0 ;;
esac
EOF
chmod +x "$GENERATION_RACE_BIN/fzf"

for raced_artifact in envrc workspace claude; do
    generation_race_project="$TEST_ROOT/generation-race-$raced_artifact"
    generation_race_snapshot="$TEST_ROOT/generation-race-$raced_artifact-snapshot"
    mkdir -p "$generation_race_project" "$generation_race_snapshot"
    run_project_init_on_host \
        Linux x86_64 false \
        "$PLATFORM_MATRIX_HOME" "$generation_race_project" \
        --build --yes --no-history --vscode \
        --cmake-build-dir initial-build \
        --claude --claude-build-dir initial-claude
    generation_race_workspace="$generation_race_project/$(basename "$generation_race_project").code-workspace"
    cp "$generation_race_project/.envrc" \
        "$generation_race_snapshot/envrc"
    cp "$generation_race_workspace" \
        "$generation_race_snapshot/workspace"
    cp "$generation_race_project/.claude/settings.local.json" \
        "$generation_race_snapshot/claude"
    case "$raced_artifact" in
        envrc)
            GENERATION_RACE_TARGET="$generation_race_project/.envrc"
            ;;
        workspace)
            GENERATION_RACE_TARGET="$generation_race_workspace"
            ;;
        claude)
            GENERATION_RACE_TARGET="$generation_race_project/.claude/settings.local.json"
            ;;
    esac
    GENERATION_RACE_MARKER="$TEST_ROOT/generation-race-$raced_artifact-marker"
    export GENERATION_RACE_TARGET GENERATION_RACE_MARKER
    if printf 'y' | PATH="$GENERATION_RACE_BIN:$PATH" \
            run_project_init_on_host \
                Linux x86_64 false \
                "$PLATFORM_MATRIX_HOME" "$generation_race_project" \
                --no-history --vscode \
                --cmake-build-dir updated-build \
                --claude --claude-build-dir updated-claude \
                >/dev/null 2>&1; then
        fail "interactive $raced_artifact edit was overwritten"
    fi
    grep -qxF "user edit during interactive selection" \
        "$GENERATION_RACE_TARGET" ||
        fail "interactive $raced_artifact edit was not retained"
    for stable_artifact in envrc workspace claude; do
        [ "$stable_artifact" = "$raced_artifact" ] && continue
        case "$stable_artifact" in
            envrc) stable_path="$generation_race_project/.envrc" ;;
            workspace) stable_path="$generation_race_workspace" ;;
            claude)
                stable_path="$generation_race_project/.claude/settings.local.json"
                ;;
        esac
        cmp -s \
            "$generation_race_snapshot/$stable_artifact" \
            "$stable_path" ||
            fail "$raced_artifact race partially published $stable_artifact"
    done
done
unset GENERATION_RACE_TARGET GENERATION_RACE_MARKER

# A user file appearing after initial absence wins the no-replace race.
RACE_PROJECT="$TEST_ROOT/race-project"
RACE_BIN="$TEST_ROOT/race-bin"
mkdir -p "$RACE_PROJECT" "$RACE_BIN"
REAL_MKTEMP=$(command -v mktemp)
cat > "$RACE_BIN/mktemp" << EOF
#!/bin/bash
staging_path=\$("$REAL_MKTEMP" "\$@") || exit
printf '%s\n' "\$staging_path"
case "\${1:-}" in
    */.project-init.XXXXXX)
        if [ "\$PWD" = "$RACE_PROJECT" ] &&
                [ ! -e "$RACE_PROJECT/.envrc.local" ]; then
            printf '%s\n' 'user-created during confirmation race' \
                > "$RACE_PROJECT/.envrc.local"
        fi
        ;;
esac
EOF
chmod +x "$RACE_BIN/mktemp"
if HOME="$TEST_HOME" \
        PATH="$RACE_BIN:$PATH" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
        --build --yes --no-history "$RACE_PROJECT" \
        >/dev/null 2>&1; then
    fail "project initialization overwrote a racing .envrc.local"
fi
grep -qxF \
    "user-created during confirmation race" "$RACE_PROJECT/.envrc.local" ||
    fail "racing .envrc.local content was overwritten"
[ ! -e "$RACE_PROJECT/.envrc" ] ||
    fail "failed no-replace publication left a partial .envrc"

# Replacement repeats the generation check at its atomic publication boundary,
# after staging. A save after the all-destination preflight still wins.
REPLACE_RACE_PROJECT="$TEST_ROOT/replace-race-project"
REPLACE_RACE_BIN="$TEST_ROOT/replace-race-bin"
mkdir -p "$REPLACE_RACE_PROJECT" "$REPLACE_RACE_BIN"
run_project_init_on_host \
    Linux x86_64 false \
    "$PLATFORM_MATRIX_HOME" "$REPLACE_RACE_PROJECT" \
    --build --yes --no-history
cat > "$REPLACE_RACE_BIN/mktemp" << EOF
#!/bin/bash
staging_path=\$("$REAL_MKTEMP" "\$@") || exit
printf '%s\n' "\$staging_path"
case "\${1:-}" in
    */.project-init.XXXXXX)
        if [ "\$PWD" = "$REPLACE_RACE_PROJECT" ]; then
            printf '%s\n' 'user edit at replacement boundary' \
                > "$REPLACE_RACE_PROJECT/.envrc"
        fi
        ;;
esac
EOF
chmod +x "$REPLACE_RACE_BIN/mktemp"
if PATH="$REPLACE_RACE_BIN:$PATH" \
        run_project_init_on_host \
            Linux x86_64 false \
            "$PLATFORM_MATRIX_HOME" "$REPLACE_RACE_PROJECT" \
            --none --yes --no-history >/dev/null 2>&1; then
    fail "replacement overwrote a racing existing environment"
fi
grep -qxF \
    "user edit at replacement boundary" "$REPLACE_RACE_PROJECT/.envrc" ||
    fail "replacement-boundary edit was not retained"

SYMLINK_WORKSPACE_PROJECT="$TEST_ROOT/symlink-workspace-project"
SYMLINK_WORKSPACE_TARGET="$TEST_ROOT/workspace-target"
mkdir -p "$SYMLINK_WORKSPACE_PROJECT"
printf '%s\n' "workspace target sentinel" > "$SYMLINK_WORKSPACE_TARGET"
ln -s \
    "$SYMLINK_WORKSPACE_TARGET" \
    "$SYMLINK_WORKSPACE_PROJECT/symlink-workspace-project.code-workspace"
if HOME="$TEST_HOME" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
        --no-env --vscode "$SYMLINK_WORKSPACE_PROJECT" \
        >/dev/null 2>&1; then
    fail "workspace generation followed an existing symlink"
fi
grep -qxF "workspace target sentinel" "$SYMLINK_WORKSPACE_TARGET" ||
    fail "workspace generation changed a symlink target"

SYMLINK_ENV_PROJECT="$TEST_ROOT/symlink-env-project"
SYMLINK_ENV_TARGET="$TEST_ROOT/env-target"
mkdir -p "$SYMLINK_ENV_PROJECT"
printf '%s\n' "environment target sentinel" > "$SYMLINK_ENV_TARGET"
ln -s "$SYMLINK_ENV_TARGET" "$SYMLINK_ENV_PROJECT/.envrc"
if HOME="$TEST_HOME" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
        --build --yes --no-history "$SYMLINK_ENV_PROJECT" \
        >/dev/null 2>&1; then
    fail "environment generation followed an existing symlink"
fi
grep -qxF "environment target sentinel" "$SYMLINK_ENV_TARGET" ||
    fail "environment generation changed a symlink target"

# Older TheRock installs select a packaging venv whose rocm-sdk command
# resolves the effective SDK root consumed by CMake and VS Code.
if [ "$(uname -s)" = "Linux" ]; then
    ROCM_VENV="$TEST_HOME/tools/rocm/7.0.0"
    ROCM_SDK="$TEST_HOME/assembled ROCm SDK"
    ROCM_PROJECT_DIRECTORY="$TEST_ROOT/rocm-project"
    mkdir -p "$ROCM_VENV/bin" "$ROCM_SDK" "$ROCM_PROJECT_DIRECTORY"
    touch "$ROCM_VENV/pyvenv.cfg"
    cat > "$ROCM_VENV/bin/rocm-sdk" << EOF
#!/bin/sh
[ "\${1:-} \${2:-}" = "path --root" ] || exit 2
printf '%s\n' '$ROCM_SDK'
EOF
    chmod +x "$ROCM_VENV/bin/rocm-sdk"
    ln -s "7.0.0" "$TEST_HOME/tools/rocm/latest"

    HOME="$TEST_HOME" \
    XDG_CACHE_HOME="$TEST_HOME/.cache" \
    XDG_CONFIG_HOME="$TEST_HOME/.config" \
        "$BASH_EXECUTABLE" "$DOTFILES/bin/project-init" \
        --all --yes --no-history --vscode \
        "$ROCM_PROJECT_DIRECTORY" >/dev/null
    ROCM_WORKSPACE="$ROCM_PROJECT_DIRECTORY/rocm-project.code-workspace"
    [ "$(jq -r '.settings["cmake.environment"].HIP_PATH' \
        "$ROCM_WORKSPACE")" = "$ROCM_SDK" ] ||
        fail "workspace HIP_PATH did not use the effective ROCm SDK root"
    [ "$(jq -r '.settings["cmake.environment"].ROCM_HOME' \
        "$ROCM_WORKSPACE")" = "$ROCM_SDK" ] ||
        fail "workspace ROCM_HOME did not use the effective ROCm SDK root"
    [ "$(jq -r '.settings["cmake.environment"].ROCM_PATH' \
        "$ROCM_WORKSPACE")" = "$ROCM_SDK" ] ||
        fail "workspace ROCM_PATH did not use the effective ROCm SDK root"
fi

echo "project initialization passed"
