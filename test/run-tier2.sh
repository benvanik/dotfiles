#!/bin/bash
# ~/.dotfiles/test/run-tier2.sh - Docker-based integration tests
# Builds and runs dotfiles tests in clean Ubuntu containers.
set -e

DOTFILES="${DOTFILES:-$HOME/.dotfiles}"
cd "$DOTFILES"

# Colors.
RED='\033[0;31m'
GREEN='\033[0;32m'
BOLD='\033[1m'
NC='\033[0m'

info() { printf "${GREEN}[tier2]${NC} %s\n" "$1"; }
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

# Check Docker daemon access and preserve the mechanism of any failure.
DOCKER_INFO_ERROR=""
if ! DOCKER_INFO_ERROR=$(docker info 2>&1); then
    case "$DOCKER_INFO_ERROR" in
        *[Pp]ermission*denied*)
            error "Docker daemon is not accessible to this user"
            error "Grant access to the Docker socket and try again"
            ;;
        *)
            error "Docker daemon not running"
            error "Start Docker and try again"
            ;;
    esac
    exit 1
fi

echo ""
printf "%b[dotfiles]%b Running Tier 2 Docker tests...\n" "$BOLD" "$NC"
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
    printf "%bAll Tier 2 tests passed!%b\n" "$GREEN" "$NC"
    exit 0
else
    echo ""
    echo "========================================"
    printf "%bTier 2 tests failed!%b\n" "$RED" "$NC"
    exit 1
fi
