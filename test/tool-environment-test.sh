#!/bin/bash
# Integration coverage for portable, repository-owned tool environments.

set -euo pipefail

DOTFILES="${DOTFILES:-$HOME/.dotfiles}"
TEST_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/dotfiles-tool-environment-test.XXXXXX")
trap 'rm -rf -- "$TEST_ROOT"' EXIT

fail() {
    echo "tool environment test: $1" >&2
    exit 1
}

TEST_HOME="$TEST_ROOT/home with spaces"
FAKE_BIN="$TEST_ROOT/fake-bin"
mkdir -p "$TEST_HOME/tools/cmake/4.3.3/bin"
mkdir -p "$TEST_HOME/tools/cmake/4.2.0/bin"
mkdir -p "$TEST_HOME/tools/ninja/1.13.2/bin"
mkdir -p "$FAKE_BIN"
ln -s "$DOTFILES" "$TEST_HOME/.dotfiles"
ln -s "4.3.3" "$TEST_HOME/tools/cmake/latest"
ln -s "$TEST_HOME/tools/ninja/1.13.2" "$TEST_HOME/tools/ninja/latest"

# Model BSD readlink: resolving a link is supported, GNU -f is not.
SYSTEM_READLINK=$(command -v readlink)
cat > "$FAKE_BIN/readlink" << EOF
#!/bin/sh
case "\${1:-}" in
    -*) exit 64 ;;
esac
exec "$SYSTEM_READLINK" "\$@"
EOF
chmod +x "$FAKE_BIN/readlink"

(
    PATH="$FAKE_BIN:/usr/bin:/bin"
    export PATH
    # shellcheck source=../tools/versions.sh
    . "$DOTFILES/tools/versions.sh"

    expected="$TEST_HOME/tools/cmake/4.3.3"
    [ "$(_find_version "$TEST_HOME/tools/cmake" latest)" = "$expected" ] || \
        fail "relative latest link did not resolve"

    expected="$TEST_HOME/tools/ninja/1.13.2"
    [ "$(_find_version "$TEST_HOME/tools/ninja" latest)" = "$expected" ] || \
        fail "absolute latest link did not resolve"

    ln -s "missing" "$TEST_HOME/tools/cmake/broken"
    if _resolve_linked_directory "$TEST_HOME/tools/cmake/broken" >/dev/null; then
        fail "broken link unexpectedly resolved"
    fi
)

# Interactive shell defaults use tracked definitions and preserve explicit
# selections inherited from direnv.
(
    HOME="$TEST_HOME"
    PATH="$FAKE_BIN:/usr/bin:/bin"
    export HOME PATH
    unset CMAKE_ROOT NINJA_ROOT

    # shellcheck source=../tools/tools.sh
    . "$DOTFILES/tools/tools.sh"
    [ "$CMAKE_ROOT" = "$TEST_HOME/tools/cmake/4.3.3" ] || \
        fail "default CMake root is incorrect"
    [ "$NINJA_ROOT" = "$TEST_HOME/tools/ninja/1.13.2" ] || \
        fail "default Ninja root is incorrect"
    case ":$PATH:" in
        *":$CMAKE_ROOT/bin:"*) ;;
        *) fail "tracked CMake environment did not update PATH" ;;
    esac
)

(
    HOME="$TEST_HOME"
    # shellcheck disable=SC2030
    CMAKE_ROOT="$TEST_HOME/tools/cmake/4.2.0"
    PATH="$FAKE_BIN:$CMAKE_ROOT/bin:/usr/bin:/bin"
    export HOME CMAKE_ROOT PATH

    # shellcheck source=../tools/tools.sh
    . "$DOTFILES/tools/tools.sh"
    [ "$CMAKE_ROOT" = "$TEST_HOME/tools/cmake/4.2.0" ] || \
        fail "explicit CMake selection was overwritten"
)

# direnv uses the same tracked definition as interactive shells.
(
    HOME="$TEST_HOME"
    PATH="$FAKE_BIN:/usr/bin:/bin"
    export HOME PATH
    source_env() { . "$1"; }
    log_error() { echo "$*" >&2; }

    # shellcheck source=../tools/direnvrc
    . "$DOTFILES/tools/direnvrc"
    use_cmake latest
    # shellcheck disable=SC2031
    [ "$CMAKE_ROOT" = "$TEST_HOME/tools/cmake/4.3.3" ] || \
        fail "direnv CMake root is incorrect"
    # shellcheck disable=SC2031
    case ":$PATH:" in
        *":$CMAKE_ROOT/bin:"*) ;;
        *) fail "direnv CMake environment did not update PATH" ;;
    esac
)

# Packaged TheRock roots resolve through rocm-sdk while retaining the venv
# tools on PATH.
ROCM_VENV="$TEST_HOME/tools/rocm/7.0.0"
ROCM_SDK="$TEST_HOME/assembled ROCm SDK"
mkdir -p "$ROCM_VENV/bin" "$ROCM_SDK/bin" "$ROCM_SDK/lib/llvm/bin"
cat > "$ROCM_VENV/bin/rocm-sdk" << EOF
#!/bin/sh
printf '%s\n' '$ROCM_SDK'
EOF
chmod +x "$ROCM_VENV/bin/rocm-sdk"
(
    ROCM_ROOT="$ROCM_VENV"
    PATH="/usr/bin:/bin"
    export ROCM_ROOT PATH

    # shellcheck source=../tools/rocm/env.sh
    . "$DOTFILES/tools/rocm/env.sh"
    [ "$ROCM_VENV_ROOT" = "$ROCM_VENV" ] || fail "ROCm venv root was lost"
    [ "$ROCM_ROOT" = "$ROCM_SDK" ] || fail "assembled ROCm root is incorrect"
    [ "$HIP_CLANG_PATH" = "$ROCM_SDK/lib/llvm/bin" ] || \
        fail "HIP clang path is incorrect"
    case ":$PATH:" in
        *":$ROCM_VENV/bin:"*) ;;
        *) fail "ROCm venv tools are missing from PATH" ;;
    esac
)

echo "tool environment portability passed"
