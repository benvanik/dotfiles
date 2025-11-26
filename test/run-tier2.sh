#!/bin/bash
# ~/.dotfiles/test/run-tier2.sh - Docker-based integration tests
# Builds and runs dotfiles tests in clean Ubuntu containers.
set -e

DOTFILES="${DOTFILES:-$HOME/.dotfiles}"
cd "$DOTFILES"

# Colors.
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BOLD='\033[1m'
NC='\033[0m'

info() { printf "${GREEN}[tier2]${NC} %s\n" "$1"; }
warn() { printf "${YELLOW}[tier2]${NC} %s\n" "$1"; }
error() { printf "${RED}[tier2]${NC} %s\n" "$1" >&2; }

# Parse arguments.
NO_CACHE=""
SHELL_MODE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-cache) NO_CACHE="--no-cache"; shift ;;
        --shell) SHELL_MODE="yes"; shift ;;
        *) error "Unknown option: $1"; exit 1 ;;
    esac
done

# Check Docker is available.
if ! command -v docker &>/dev/null; then
    error "Docker not found"
    error "Install Docker to run Tier 2 tests"
    exit 1
fi

# Check Docker daemon is running.
if ! docker info &>/dev/null; then
    error "Docker daemon not running"
    error "Start Docker and try again"
    exit 1
fi

echo ""
printf "${BOLD}[dotfiles]${NC} Running Tier 2 Docker tests...\n"
echo ""

# ============================================================================
# Ubuntu Tests (full integration)
# ============================================================================
run_ubuntu() {
    info "Building Ubuntu test image..."
    if ! docker build $NO_CACHE -t dotfiles-test:ubuntu -f test/Dockerfile.ubuntu . ; then
        error "Ubuntu image build failed"
        return 1
    fi

    if [ -n "$SHELL_MODE" ]; then
        info "Starting interactive shell (Ubuntu)..."
        docker run -it --rm --entrypoint /bin/bash dotfiles-test:ubuntu
        return 0
    fi

    info "Running Ubuntu tests..."
    if docker run --rm dotfiles-test:ubuntu; then
        info "Ubuntu tests passed"
        return 0
    else
        error "Ubuntu tests failed"
        return 1
    fi
}

# ============================================================================
# Run Tests
# ============================================================================
info "=== Ubuntu (full integration) ==="
if run_ubuntu; then
    echo ""
    echo "========================================"
    printf "${GREEN}All Tier 2 tests passed!${NC}\n"
    exit 0
else
    echo ""
    echo "========================================"
    printf "${RED}Tier 2 tests failed!${NC}\n"
    exit 1
fi
