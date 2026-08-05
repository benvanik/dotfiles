#!/bin/bash
# ~/.dotfiles/test/run-tier1.sh - Fast local validation tests
# Runs quickly with no external or provider dependencies (shellcheck optional).
set -e

DOTFILES="${DOTFILES:-$HOME/.dotfiles}"
cd "$DOTFILES"

# Colors.
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BOLD='\033[1m'
NC='\033[0m'

# Counters.
PASSED=0
FAILED=0
SKIPPED=0

pass() { printf "  ${GREEN}PASS${NC} %s\n" "$1"; ((++PASSED)); }
fail() { printf "  ${RED}FAIL${NC} %s\n" "$1"; ((++FAILED)); }
skip() { printf "  ${YELLOW}SKIP${NC} %s\n" "$1"; ((++SKIPPED)); }
section() { printf "\n${BOLD}[%s]${NC} %s\n" "$1" "$2"; }

# Track start time with the Bash timer available on Linux and macOS.
START_TIME=$SECONDS

echo ""
printf "%b[dotfiles]%b Running Tier 1 tests...\n" "$BOLD" "$NC"

# ============================================================================
# Syntax Validation
# ============================================================================
section "syntax" "Shell syntax validation"

# Classify every tracked shell source once. Files with an explicit shell
# contract take precedence over their extension; extension-only .sh files use
# Bash because the tool environment sources intentionally use Bash-compatible
# local variables without shebangs.
posix_files=()
bash_files=()
zsh_files=()
while IFS= read -r f; do
    [ -f "$f" ] || continue
    case "$f" in
        shell/shrc|shell/profile|shell/platform/*.sh)
            posix_files+=("$f")
            continue
            ;;
        shell/bashrc)
            bash_files+=("$f")
            continue
            ;;
        shell/zshrc|*.zsh)
            zsh_files+=("$f")
            continue
            ;;
    esac

    first_line=$(head -n 1 "$f" 2>/dev/null || true)
    case "$first_line" in
        '#!'*bash*)
            bash_files+=("$f")
            ;;
        '#!'*zsh*)
            zsh_files+=("$f")
            ;;
        '#!'*sh*)
            posix_files+=("$f")
            ;;
        *)
            case "$f" in
                *.sh) bash_files+=("$f") ;;
            esac
            ;;
    esac
done < <(git ls-files)

posix_pass=0
posix_fail=0
for f in "${posix_files[@]}"; do
    if [ -f "$f" ]; then
        if sh -n "$f" 2>/dev/null; then
            ((++posix_pass))
        else
            fail "POSIX syntax: $f"
            ((++posix_fail))
        fi
    fi
done
if [ $posix_fail -eq 0 ]; then
    pass "POSIX syntax: $posix_pass files"
fi

bash_pass=0
bash_fail=0
for f in "${bash_files[@]}"; do
    if [ -f "$f" ]; then
        if "$BASH" -n "$f" 2>/dev/null; then
            ((++bash_pass))
        else
            fail "bash syntax: $f"
            ((++bash_fail))
        fi
    fi
done
if [ $bash_fail -eq 0 ]; then
    pass "bash syntax: $bash_pass files"
fi

# ============================================================================
# Python Command Tests
# ============================================================================
section "python" "Python command validation"

if command -v python3 &>/dev/null; then
    if PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$DOTFILES/lib" \
        python3 -m unittest discover -s test -p 'test_runpod_*.py'; then
        pass "RunPod host-control tests"
    else
        fail "RunPod host-control tests"
    fi
    if PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$DOTFILES/lib" \
        python3 -m unittest discover -s test -p 'test_model_session_*.py'; then
        pass "Model-session isolation tests"
    else
        fail "Model-session isolation tests"
    fi
    if PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$DOTFILES/lib" \
        python3 -m unittest discover -s test -p 'test_model_lab_*.py'; then
        pass "Model-lab orchestration tests"
    else
        fail "Model-lab orchestration tests"
    fi
    if [ "$(uname -s)" = "Linux" ]; then
        if PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$DOTFILES/lib" \
            python3 -m unittest discover -s test -p 'test_benchmark_*.py'; then
            pass "Benchmark broker tests"
        else
            fail "Benchmark broker tests"
        fi
        if PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$DOTFILES/lib" \
            python3 -O -m unittest discover \
                -s test -p 'test_benchmark_*.py'; then
            pass "Benchmark broker optimized-mode tests"
        else
            fail "Benchmark broker optimized-mode tests"
        fi
        if PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$DOTFILES/lib" \
            "$DOTFILES/bin/benchmark-admin" --help >/dev/null; then
            pass "Benchmark administrator help"
        else
            fail "Benchmark administrator help"
        fi
        if "$DOTFILES/bin/benchmark-lock" --agents-md >/dev/null; then
            pass "Benchmark agent instructions"
        else
            fail "Benchmark agent instructions"
        fi
    else
        skip "benchmark broker tests require Linux"
        skip "benchmark broker optimized-mode tests require Linux"
        skip "benchmark administrator requires Linux"
        skip "benchmark agent instructions require Linux"
    fi
else
    fail "python3 required for Python commands"
fi

zsh_pass=0
zsh_fail=0
for f in "${zsh_files[@]}"; do
    if [ -f "$f" ]; then
        if zsh -n "$f" 2>/dev/null; then
            ((++zsh_pass))
        else
            fail "zsh syntax: $f"
            ((++zsh_fail))
        fi
    fi
done
if [ $zsh_fail -eq 0 ]; then
    pass "zsh syntax: $zsh_pass files"
fi

# Agent-driven commands must never enter the direct shell file-removal path;
# the Codex UI can block for hours before the subprocess starts. Use exact
# unlink or bounded find-delete operations instead.
deletion_command_name=$(printf '\162\155')
deletion_command_pattern="(^|[;&|[:space:]\\\\/\"'])${deletion_command_name}([;&|[:space:]\"']|$)"
deletion_command_fail=0

# Keep the safety detector honest without spelling the forbidden command in a
# tracked shell source (which would correctly make that source fail the scan).
for specimen in \
    "${deletion_command_name} -f target" \
    "sudo ${deletion_command_name} -f target" \
    "/bin/${deletion_command_name} -f target" \
    "/usr/bin/${deletion_command_name} -f target" \
    "\\${deletion_command_name} -f target" \
    "\"/bin/${deletion_command_name}\" -f target"; do
    if ! printf '%s\n' "$specimen" | rg -q "$deletion_command_pattern"; then
        fail "shell deletion detector missed a command form"
        deletion_command_fail=$((deletion_command_fail + 1))
    fi
done

for f in \
    "${posix_files[@]}" \
    "${bash_files[@]}" \
    "${zsh_files[@]}" \
    test/Dockerfile.ubuntu; do
    [ -f "$f" ] || continue
    if rg -n "$deletion_command_pattern" "$f" >/dev/null; then
        fail "forbidden shell file-removal command: $f"
        deletion_command_fail=$((deletion_command_fail + 1))
    fi
done
if [ $deletion_command_fail -eq 0 ]; then
    pass "shell deletion paths use bounded operations"
fi

# ============================================================================
# Symlink Target Verification
# ============================================================================
section "links" "Symlink target verification"

# Extract managed source paths from bin/dotfiles and verify they exist.
link_pass=0
link_fail=0
link_pattern='^[[:space:]]*_(link|copy)[[:space:]]+([^[:space:]]+)'
while IFS= read -r line; do
    # Parse: _link/_copy <source> <dest>
    if [[ "$line" =~ $link_pattern ]]; then
        src="${BASH_REMATCH[2]}"
        if [ -e "$DOTFILES/$src" ]; then
            ((++link_pass))
        else
            fail "missing target: $src"
            ((++link_fail))
        fi
    fi
done < bin/dotfiles

if [ $link_fail -eq 0 ]; then
    pass "symlink targets: $link_pass verified"
fi

# ============================================================================
# Pre-commit Hook Isolation
# ============================================================================
section "pre-commit" "Pre-commit Git environment isolation"

if "$BASH" "$DOTFILES/test/pre-commit-hook-test.sh"; then
    pass "pre-commit Git environment isolation"
else
    fail "pre-commit Git environment isolation"
fi

# ============================================================================
# Project Worktree Lifecycle
# ============================================================================
section "worktrees" "Project worktree lifecycle"

if "$BASH" "$DOTFILES/test/project-worktree-test.sh"; then
    pass "project worktree lifecycle"
else
    fail "project worktree lifecycle"
fi

# ============================================================================
# Project Initialization
# ============================================================================
section "project-init" "Project initialization"

if "$BASH" "$DOTFILES/test/project-init-test.sh"; then
    pass "project initialization"
else
    fail "project initialization"
fi

# ============================================================================
# Benchmark Lock Recovery
# ============================================================================
section "benchmark-unlock" "Benchmark lock legacy-state recovery"

if "$BASH" "$DOTFILES/test/benchmark-unlock-test.sh"; then
    pass "benchmark legacy-state recovery"
else
    fail "benchmark legacy-state recovery"
fi

# ============================================================================
# Git Signing Setup
# ============================================================================
section "git-signing" "Machine-local SSH signing setup"

if "$BASH" "$DOTFILES/test/git-signing-test.sh"; then
    pass "git signing setup"
else
    fail "git signing setup"
fi

# ============================================================================
# Agent Contract Lifecycle
# ============================================================================
section "agent-contract" "Canonical agent-contract publication"

if "$BASH" "$DOTFILES/test/agent-contract-test.sh"; then
    pass "canonical agent-contract publication"
else
    fail "canonical agent-contract publication"
fi

# ============================================================================
# Machine-local Configuration Publication
# ============================================================================
section "local-config" "Atomic machine-local configuration"

if "$BASH" "$DOTFILES/test/local-config-publication-test.sh"; then
    pass "atomic machine-local configuration"
else
    fail "atomic machine-local configuration"
fi

# ============================================================================
# Tool Environments
# ============================================================================
section "tool-env" "Portable tracked tool environments"

if "$BASH" "$DOTFILES/test/tool-environment-test.sh"; then
    pass "tool environment portability"
else
    fail "tool environment portability"
fi

# ============================================================================
# Tool Installer Safety
# ============================================================================
section "tool-install" "Tool installer path and checksum safety"

if "$BASH" "$DOTFILES/test/tool-installer-test.sh"; then
    pass "tool installer path and checksum safety"
else
    fail "tool installer path and checksum safety"
fi

section "installer-transactions" "Managed installer publication safety"

if "$BASH" "$DOTFILES/test/installer-transaction-test.sh"; then
    pass "managed installer publication safety"
else
    fail "managed installer publication safety"
fi

section "installer-production" "Offline managed installer production paths"

if "$BASH" "$DOTFILES/test/installer-production-test.sh"; then
    pass "offline managed installer production paths"
else
    fail "offline managed installer production paths"
fi

section "bootstrap-installers" "Pinned shell-component bootstrap"

if "$BASH" "$DOTFILES/test/bootstrap-installer-test.sh"; then
    pass "pinned shell-component bootstrap"
else
    fail "pinned shell-component bootstrap"
fi

section "multiplexer-publish" "Atomic multiplexer stack activation"

if "$BASH" "$DOTFILES/test/multiplexer-publication-test.sh"; then
    pass "atomic multiplexer stack activation"
else
    fail "atomic multiplexer stack activation"
fi
if "$BASH" "$DOTFILES/test/multiplexer-update-test.sh"; then
    pass "offline multiplexer update production path"
else
    fail "offline multiplexer update production path"
fi

section "platform-installers" "Offline platform-installer production paths"

if "$BASH" "$DOTFILES/test/platform-installer-test.sh"; then
    pass "offline platform-installer production paths"
else
    fail "offline platform-installer production paths"
fi

# ============================================================================
# Package Resolution
# ============================================================================
section "packages" "Portable package resolution"

if "$BASH" "$DOTFILES/test/packages-test.sh"; then
    pass "package resolution"
else
    fail "package resolution"
fi

# ============================================================================
# POSIX Compliance (deeper checks)
# ============================================================================
section "posix" "POSIX compliance validation"

posix_violations=0
for f in "${posix_files[@]}"; do
    if [ -f "$f" ]; then
        violations=""

        # Check for bash-isms (excluding comments).
        # Use grep -v to filter lines that are comments (start with optional whitespace then #).
        if grep '\[\[' "$f" 2>/dev/null | grep -vE '^[[:space:]]*#' | grep -q '\[\['; then
            violations+=" [["
        fi
        if grep '<<<' "$f" 2>/dev/null | grep -vE '^[[:space:]]*#' | grep -q '<<<'; then
            violations+=" <<<"
        fi
        # Check for 'function name {' syntax (not 'name() {').
        if grep -E '^[[:space:]]*function[[:space:]]+[a-zA-Z_]' "$f" 2>/dev/null | grep -vE '^[[:space:]]*#' | grep -q 'function'; then
            violations+=" function-keyword"
        fi

        if [ -n "$violations" ]; then
            fail "POSIX violation in $f:$violations"
            ((++posix_violations))
        fi
    fi
done

if [ $posix_violations -eq 0 ]; then
    pass "POSIX compliance: ${#posix_files[@]} files clean"
fi

# ============================================================================
# Secret Detection
# ============================================================================
section "secrets" "Secret/credential detection"

# Patterns that indicate committed secrets.
secret_patterns=(
    'ANTHROPIC_API_KEY=sk-ant-'
    'OPENAI_API_KEY=sk-'
    'HF_TOKEN=hf_[a-zA-Z0-9]'
    'RUNPOD_API_KEY=[A-Za-z0-9_-]{16,}'
    'ghp_[a-zA-Z0-9]{36}'
    'gho_[a-zA-Z0-9]{36}'
    'github_pat_[a-zA-Z0-9]'
    'AKIA[0-9A-Z]{16}'
    'ASIA[0-9A-Z]{16}'
    'AIza[0-9A-Za-z_-]{35}'
    'ya29\.[0-9A-Za-z._-]+'
    'https://hooks\.slack\.com/services/'
    'xox[baprs]-[0-9A-Za-z-]{10,}'
    '-----BEGIN (RSA |DSA |EC )?PRIVATE KEY-----'
    '-----BEGIN OPENSSH PRIVATE KEY-----'
)

secrets_found=0
for pattern in "${secret_patterns[@]}"; do
    # Search all files except .git, deps/*/* (submodules), templates, and this test file.
    if grep -rEq \
        --include="*" \
        --exclude-dir=".git" \
        --exclude-dir="deps" \
        --exclude="*.template" \
        --exclude="run-tier1.sh" \
        "$pattern" . 2>/dev/null; then
        fail "Potential secret found matching: $pattern"
        ((++secrets_found))
    fi
done

if [ $secrets_found -eq 0 ]; then
    pass "No secrets detected"
fi

# Docker receives the repository root as its build context during Tier 2.
# Every ignored credential class must remain outside that external boundary.
docker_context_exclusions=(
    ".git"
    ".secrets"
    "secrets"
    "credentials.json"
    ".credentials.json"
    "*.pem"
    "*.key"
    ".env"
    ".env.*"
)
docker_context_fail=0
for exclusion in "${docker_context_exclusions[@]}"; do
    if ! grep -qxF "$exclusion" .dockerignore; then
        fail "Docker context permits ignored private path: $exclusion"
        ((++docker_context_fail))
    fi
done
if [ $docker_context_fail -eq 0 ]; then
    pass "Docker context excludes private repository state"
fi

# ============================================================================
# Structure Validation
# ============================================================================
section "structure" "Repository structure"

# Required files.
required_files=(
    "install-deps.sh"
    "README.md"
    "CLAUDE.md"
    ".dockerignore"
    "agents/WORKING_CONTRACT.md"
    "secrets.template"
    "shell/shrc"
    "shell/zshrc"
    "shell/bashrc"
    "shell/profile"
    "shell/aliases"
    "git/config"
    "git/ignore_global"
)

missing=0
for f in "${required_files[@]}"; do
    if [ ! -e "$f" ]; then
        fail "missing required file: $f"
        ((++missing))
    fi
done

if [ $missing -eq 0 ]; then
    pass "required files: ${#required_files[@]} present"
fi

# Check for *.local files that shouldn't be committed.
# Exclude .git and deps/*/* (submodules in deps/).
local_files=$(find . \( -path "./.git" -o -path "./deps/*/*" \) -prune -o \( -name "*.local" -o -name "*.local.*" \) -print 2>/dev/null | head -5)
if [ -n "$local_files" ]; then
    fail "*.local files should not be committed"
    echo "$local_files" | while read -r f; do echo "    $f"; done
else
    pass "no *.local files committed"
fi

# ============================================================================
# Executable Permissions
# ============================================================================
section "permissions" "Executable permissions"

exec_pass=0
exec_fail=0
for f in bin/*; do
    if [ -f "$f" ] && [ ! -x "$f" ]; then
        fail "not executable: $f"
        ((++exec_fail))
    else
        ((++exec_pass))
    fi
done

if [ $exec_fail -eq 0 ]; then
    pass "executable bits: $exec_pass scripts"
fi

# ============================================================================
# Shebang Validation
# ============================================================================
section "shebang" "Shebang validation"

shebang_pass=0
shebang_fail=0
for f in bin/*; do
    if [ -f "$f" ]; then
        first_line=$(head -1 "$f")
        if [[ "$first_line" =~ ^#! ]]; then
            ((++shebang_pass))
        else
            fail "missing shebang: $f"
            ((++shebang_fail))
        fi
    fi
done

if [ $shebang_fail -eq 0 ]; then
    pass "shebangs: $shebang_pass scripts"
fi

# ============================================================================
# Config File Validation
# ============================================================================
section "configs" "Config file syntax"

# tmux config.
if command -v tmux &>/dev/null; then
    tmux_socket="dotfiles-test-$$"
    if tmux -L "$tmux_socket" -f /dev/null new-session -d 2>/dev/null; then
        if tmux -L "$tmux_socket" source-file "$DOTFILES/tmux.conf" 2>/dev/null; then
            pass "tmux.conf syntax"

            click_binding_found=false
            for key_table in root copy-mode copy-mode-vi; do
                if tmux -L "$tmux_socket" list-keys -T "$key_table" 2>/dev/null |
                    grep -Eq 'DoubleClick1Pane|TripleClick1Pane'; then
                    click_binding_found=true
                    break
                fi
            done
            if [ "$click_binding_found" = false ]; then
                pass "tmux double/triple-click copy bindings disabled"
            else
                fail "tmux double/triple-click copy bindings disabled"
            fi
        else
            fail "tmux.conf syntax"
        fi
        tmux -L "$tmux_socket" kill-server 2>/dev/null || true
    else
        fail "could not start isolated tmux server"
    fi
else
    skip "tmux not installed"
fi

# git config.
if git config --file git/config --list &>/dev/null; then
    pass "git/config syntax"
else
    fail "git/config syntax"
fi

# ============================================================================
# Shellcheck (optional)
# ============================================================================
section "shellcheck" "Static analysis"

if command -v shellcheck &>/dev/null; then
    shellcheck_pass=0
    shellcheck_fail=0

    # Check bash files.
    # -x: Follow source statements.  --source-path=SCRIPTDIR: Look relative to script.
    # SC1090: Can't follow non-constant source (dynamic paths using variables).
    # SC1091: Can't follow source (file doesn't exist - optional tools like nvm, venv).
    for f in "${bash_files[@]}"; do
        if [ -f "$f" ]; then
            if shellcheck -x --source-path=SCRIPTDIR -s bash -e SC1090,SC1091 "$f" 2>/dev/null; then
                ((++shellcheck_pass))
            else
                fail "shellcheck: $f"
                ((++shellcheck_fail))
            fi
        fi
    done

    # Check POSIX files.
    # SC1090,SC1091: Source following (same as above).
    # SC2039: POSIX sh warning, SC3037: echo flags.
    for f in "${posix_files[@]}"; do
        if [ -f "$f" ]; then
            if shellcheck -x --source-path=SCRIPTDIR -s sh -e SC1090,SC1091,SC2039,SC3037 "$f" 2>/dev/null; then
                ((++shellcheck_pass))
            else
                fail "shellcheck: $f"
                ((++shellcheck_fail))
            fi
        fi
    done

    if [ $shellcheck_fail -eq 0 ]; then
        pass "shellcheck: $shellcheck_pass files"
    fi
else
    skip "shellcheck not installed"
fi

# ============================================================================
# Tool Smoketests (only for installed tools)
# ============================================================================
section "smoketest" "Tool installation verification"

TOOLS_DIR="$HOME/tools"
smoketest_pass=0
smoketest_fail=0
smoketest_skip=0
# shellcheck source=../tools/versions.sh
. "$DOTFILES/tools/versions.sh"
# shellcheck source=../tools/platform.sh
. "$DOTFILES/tools/platform.sh"

for tool_dir in "$DOTFILES"/tools/*/; do
    tool=$(basename "$tool_dir")
    smoketest="$tool_dir/smoketest.sh"

    # Skip if no smoketest exists.
    [ -f "$smoketest" ] || continue

    # Check if tool is installed.
    # Most tools use ~/tools/<tool>/latest symlink.
    # Special cases: nvm uses ~/.nvm.
    tool_installed=false
    case "$tool" in
        nvm)
            [ -d "$HOME/.nvm" ] && tool_installed=true
            ;;
        *)
            [ -L "$TOOLS_DIR/$tool/latest" ] && tool_installed=true
            ;;
    esac

    if [ "$tool_installed" = false ]; then
        ((++smoketest_skip))
        continue
    fi

    # Set up tool environment (skip for nvm - uses shrc PATH setup).
    if [ "$tool" != "nvm" ]; then
        environment_file="$DOTFILES/tools/$tool/env.sh"
        if [ ! -f "$environment_file" ]; then
            fail "missing tool environment: $tool"
            ((++smoketest_fail))
            continue
        fi
        if ! tool_root=$(_find_version "$TOOLS_DIR/$tool" latest); then
            fail "broken latest link: $tool"
            ((++smoketest_fail))
            continue
        fi

        # Set up the ROOT variable the tracked environment expects.
        root_var="$(echo "$tool" | tr '[:lower:]' '[:upper:]')_ROOT"
        export "$root_var=$tool_root"
        if ! . "$environment_file"; then
            fail "environment setup: $tool"
            ((++smoketest_fail))
            continue
        fi
    fi

    # Run smoketest.
    if "$BASH" "$smoketest" 2>/dev/null; then
        ((++smoketest_pass))
    else
        fail "smoketest: $tool"
        ((++smoketest_fail))
    fi
done

if [ $smoketest_fail -eq 0 ]; then
    if [ $smoketest_pass -gt 0 ]; then
        pass "smoketests: $smoketest_pass tools"
    fi
    if [ $smoketest_skip -gt 0 ]; then
        skip "smoketests: $smoketest_skip tools not installed"
    fi
fi

# ============================================================================
# Summary
# ============================================================================
DURATION=$((SECONDS - START_TIME))

echo ""
echo "========================================"
if [ $FAILED -eq 0 ]; then
    printf "%bAll tests passed!%b (%s passed, %s skipped) [%ss]\n" \
        "$GREEN" "$NC" "$PASSED" "$SKIPPED" "$DURATION"
    exit 0
else
    printf "%bTests failed!%b (%s passed, %s failed, %s skipped) [%ss]\n" \
        "$RED" "$NC" "$PASSED" "$FAILED" "$SKIPPED" "$DURATION"
    exit 1
fi
