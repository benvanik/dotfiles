#!/bin/bash
# ~/.dotfiles/test/docker-test.sh - Test runner for inside Docker containers
set -e

DOTFILES="$HOME/.dotfiles"

# Colors.
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BOLD='\033[1m'
NC='\033[0m'

PASSED=0
FAILED=0

pass() { printf "  ${GREEN}PASS${NC} %s\n" "$1"; ((++PASSED)); }
fail() { printf "  ${RED}FAIL${NC} %s\n" "$1"; ((++FAILED)); }
section() { printf "\n${BOLD}[%s]${NC} %s\n" "$1" "$2"; }

echo ""
printf "%bDotfiles Docker Test%b (Ubuntu)\n" "$BOLD" "$NC"
echo "========================================"

# ============================================================================
# Phase 1: Dependency Installation
# ============================================================================
section "deps" "Installing dependencies (install-deps.sh)"

if "$DOTFILES/install-deps.sh"; then
    pass "install-deps.sh completed"
else
    fail "install-deps.sh failed"
fi

# Verify core tools installed.
section "tools" "Verifying installed tools"

for tool in zsh git fzf rg jq python3; do
    if command -v "$tool" &>/dev/null; then
        pass "$tool installed"
    else
        fail "$tool not found"
    fi
done

# Check optional tools (don't fail, just report).
for tool in fd bat eza; do
    if command -v "$tool" &>/dev/null || command -v "${tool}find" &>/dev/null || command -v "${tool}cat" &>/dev/null; then
        pass "$tool available"
    else
        printf "  ${YELLOW}INFO${NC} %s not available (optional)\n" "$tool"
    fi
done

# Check p10k installed.
if [ -d "$HOME/.local/p10k" ]; then
    pass "Powerlevel10k installed"
else
    fail "Powerlevel10k not found"
fi

# ============================================================================
# Phase 2: Symlink Installation
# ============================================================================
section "install" "Running dotfiles install"

# Reproduce the real migration state: Claude has a dangling link to its retired
# provider-specific contract while Codex has a differing user-owned copy.
mkdir -p "$HOME/.claude" "$HOME/.codex"
ln -s "$DOTFILES/claude/CLAUDE.md" "$HOME/.claude/CLAUDE.md"
printf '%s\n' "contract state before migration" > "$HOME/.codex/AGENTS.md"

if "$DOTFILES/bin/dotfiles" install; then
    pass "dotfiles install completed"
else
    fail "dotfiles install failed"
fi

# Verify critical symlinks.
section "symlinks" "Verifying symlinks"

symlinks=(
    ".zshrc"
    ".bashrc"
    ".shrc"
    ".aliases"
    ".profile"
    ".gitconfig"
)

for link in "${symlinks[@]}"; do
    if [ -L "$HOME/$link" ]; then
        pass "symlink exists: $link"
    else
        fail "symlink missing: $link"
    fi
done

# Verify template files created.
section "templates" "Verifying template files"

templates=(
    ".shrc.local"
    ".secrets"
    ".gitconfig.local"
)

for tmpl in "${templates[@]}"; do
    if [ -f "$HOME/$tmpl" ]; then
        pass "template created: $tmpl"
    else
        fail "template missing: $tmpl"
    fi
done

# Both agent clients receive byte-identical regular-file copies of the one
# provider-neutral contract.
if [ -f "$HOME/.codex/AGENTS.md" ] &&
        [ ! -L "$HOME/.codex/AGENTS.md" ] &&
        [ -f "$HOME/.claude/CLAUDE.md" ] &&
        [ ! -L "$HOME/.claude/CLAUDE.md" ]; then
    pass "agent contracts installed as regular files"
else
    fail "agent contracts were not installed as regular files"
fi
if cmp -s "$HOME/.codex/AGENTS.md" "$HOME/.claude/CLAUDE.md"; then
    pass "agent contracts share one canonical payload"
else
    fail "agent contracts differ"
fi
if cmp -s "$DOTFILES/agents/WORKING_CONTRACT.md" \
        "$HOME/.codex/AGENTS.md" &&
        cmp -s "$DOTFILES/agents/WORKING_CONTRACT.md" \
            "$HOME/.claude/CLAUDE.md"; then
    pass "agent contracts match the canonical payload"
else
    fail "agent contracts do not match the canonical payload"
fi
if find "$HOME/.local/share/dotfiles/backups" -type f \
        -exec grep -qxF "contract state before migration" {} \; \
        -print -quit | grep -q .; then
    pass "differing agent contract was backed up"
else
    fail "differing agent contract backup is missing"
fi

# ============================================================================
# Phase 3: Shell Startup
# ============================================================================
section "shell" "Testing shell startup"

# Test bash startup.
if bash -i -c 'echo "bash-ok"' 2>&1 | grep -q "bash-ok"; then
    pass "bash interactive startup"
else
    fail "bash interactive startup"
fi

# Test zsh syntax.
if zsh -n "$HOME/.zshrc" 2>&1; then
    pass "zsh config syntax valid"
else
    fail "zsh config syntax errors"
fi

# Test zsh interactive (use TERM=dumb to avoid p10k terminal issues).
if TERM=dumb zsh -i -c 'echo "zsh-ok"' 2>&1 | grep -q "zsh-ok"; then
    pass "zsh interactive startup"
else
    fail "zsh interactive startup"
fi

# Verify shrc functions loaded.
if TERM=dumb zsh -i -c 'type _add_path' 2>&1 | grep -q "function"; then
    pass "shrc functions loaded (zsh)"
else
    fail "shrc functions not loaded (zsh)"
fi

if bash -i -c 'type _add_path' 2>&1 | grep -q "function"; then
    pass "shrc functions loaded (bash)"
else
    fail "shrc functions not loaded (bash)"
fi

# ============================================================================
# Phase 4: Integration
# ============================================================================
section "integration" "Testing tool integration"

# Test fzf is available in shell.
if TERM=dumb zsh -i -c 'command -v fzf' &>/dev/null; then
    pass "fzf available in zsh"
else
    fail "fzf not available in zsh"
fi

# Test aliases loaded.
if TERM=dumb zsh -i -c 'alias' 2>&1 | grep -q "ls="; then
    pass "aliases loaded"
else
    fail "aliases not loaded"
fi

# Test PATH includes dotfiles bin.
if TERM=dumb zsh -i -c 'echo $PATH' 2>&1 | grep -q ".dotfiles/bin"; then
    pass "PATH includes ~/.dotfiles/bin"
else
    fail "PATH missing ~/.dotfiles/bin"
fi

# ============================================================================
# Summary
# ============================================================================
echo ""
echo "========================================"
if [ $FAILED -eq 0 ]; then
    printf "${GREEN}All tests passed!${NC} (%s passed)\n" "$PASSED"
    exit 0
else
    printf "${RED}Tests failed!${NC} (%s passed, %s failed)\n" "$PASSED" "$FAILED"
    exit 1
fi
