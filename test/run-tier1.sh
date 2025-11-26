#!/bin/bash
# ~/.dotfiles/test/run-tier1.sh - Fast local validation tests
# Runs in <5 seconds with no external dependencies (shellcheck optional).
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

# Track start time.
START_TIME=$(date +%s.%N)

echo ""
printf "${BOLD}[dotfiles]${NC} Running Tier 1 tests...\n"

# ============================================================================
# Syntax Validation
# ============================================================================
section "syntax" "Shell syntax validation"

# POSIX files (must work with sh/dash).
posix_files=(
    "shell/shrc"
    "shell/profile"
    "shell/aliases"
)
# Add platform files if they exist.
for f in shell/platform/*.sh; do
    [ -f "$f" ] && posix_files+=("$f")
done
# Note: tools/*.sh use 'local' (bash-ism), checked as bash below.

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

# Bash files.
bash_files=(
    "install-deps.sh"
    "shell/bashrc"
)
# Add bin scripts with bash shebang.
for f in bin/*; do
    if [ -f "$f" ] && head -1 "$f" 2>/dev/null | grep -q "^#!/bin/bash"; then
        bash_files+=("$f")
    fi
done
# Add lib files (require bash 4+ for associative arrays).
for f in lib/*.sh; do
    [ -f "$f" ] && bash_files+=("$f")
done
# Add tools files (use 'local' which requires bash-mode checking).
for f in tools/*.sh; do
    [ -f "$f" ] && bash_files+=("$f")
done

bash_pass=0
bash_fail=0
for f in "${bash_files[@]}"; do
    if [ -f "$f" ]; then
        if bash -n "$f" 2>/dev/null; then
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

# Zsh files.
zsh_files=("shell/zshrc")
for f in shell/zshrc.d/*.zsh themes/*.zsh; do
    [ -f "$f" ] && zsh_files+=("$f")
done

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

# ============================================================================
# Symlink Target Verification
# ============================================================================
section "links" "Symlink target verification"

# Extract '_link X Y' calls from bin/dotfiles and verify X exists.
link_pass=0
link_fail=0
while IFS= read -r line; do
    # Parse: _link <source> <dest>
    if [[ "$line" =~ ^[[:space:]]*_link[[:space:]]+([^[:space:]]+) ]]; then
        src="${BASH_REMATCH[1]}"
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
    # Search all files except .git, templates, and this test file.
    if grep -rE "$pattern" --include="*" --exclude-dir=".git" --exclude="*.template" --exclude="run-tier1.sh" . 2>/dev/null | head -1 > /tmp/secret_check; then
        if [ -s /tmp/secret_check ]; then
            fail "Potential secret found matching: $pattern"
            ((++secrets_found))
        fi
    fi
done

if [ $secrets_found -eq 0 ]; then
    pass "No secrets detected"
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
local_files=$(find . -name "*.local" -o -name "*.local.*" 2>/dev/null | grep -v ".git" | head -5)
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
    if tmux -f tmux.conf source-file /dev/null 2>/dev/null; then
        pass "tmux.conf syntax"
    else
        fail "tmux.conf syntax"
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
        # Source the tool's env.sh if it exists.
        if [ -f "$TOOLS_DIR/$tool/env.sh" ]; then
            # Set up the ROOT variable the env.sh expects.
            root_var="$(echo "$tool" | tr '[:lower:]' '[:upper:]')_ROOT"
            eval "export ${root_var}=\"\$(readlink -f \"$TOOLS_DIR/$tool/latest\")\""
            . "$TOOLS_DIR/$tool/env.sh" 2>/dev/null || true
        fi
        # Add tool to PATH.
        export PATH="$TOOLS_DIR/$tool/latest/bin:$PATH"
    fi

    # Run smoketest.
    if bash "$smoketest" 2>/dev/null; then
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
END_TIME=$(date +%s.%N)
DURATION=$(echo "$END_TIME - $START_TIME" | bc 2>/dev/null || echo "?")

echo ""
echo "========================================"
if [ $FAILED -eq 0 ]; then
    printf "${GREEN}All tests passed!${NC} (%s passed, %s skipped) [%ss]\n" "$PASSED" "$SKIPPED" "${DURATION%.*}"
    exit 0
else
    printf "${RED}Tests failed!${NC} (%s passed, %s failed, %s skipped) [%ss]\n" "$PASSED" "$FAILED" "$SKIPPED" "${DURATION%.*}"
    exit 1
fi
