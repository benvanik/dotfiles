#!/bin/bash
# Integration coverage for portable, repository-owned tool environments.
# shellcheck disable=SC2030,SC2031

set -euo pipefail

DOTFILES="${DOTFILES:-$HOME/.dotfiles}"
TEST_PARENT="${TMPDIR:-/tmp}"
TEST_ROOT=$(mktemp -d "$TEST_PARENT/dotfiles-tool-environment-test.XXXXXX")
cleanup_test_root() {
    case "$TEST_ROOT" in
        "$TEST_PARENT"/dotfiles-tool-environment-test.*) ;;
        *)
            echo "refusing unexpected test cleanup path: $TEST_ROOT" >&2
            return 1
            ;;
    esac
    [ ! -e "$TEST_ROOT" ] || find "$TEST_ROOT" -xdev -depth -delete
}
trap cleanup_test_root EXIT

fail() {
    echo "tool environment test: $1" >&2
    exit 1
}

advance_file_mtime() {
    python3 - "$1" << 'PY'
import os
import sys

status = os.stat(sys.argv[1], follow_symlinks=False)
os.utime(
    sys.argv[1],
    ns=(status.st_atime_ns, status.st_mtime_ns + 2_000_000_000),
    follow_symlinks=False,
)
PY
}

count_colon_entry() {
    local list="$1"
    local expected="$2"
    local remaining="$list"
    local component=""
    local count=0
    local has_more=0

    while :; do
        case "$remaining" in
            *:*)
                component="${remaining%%:*}"
                remaining="${remaining#*:}"
                has_more=1
                ;;
            *)
                component="$remaining"
                has_more=0
                ;;
        esac
        if [ "$component" = "$expected" ]; then
            count=$((count + 1))
        fi
        [ "$has_more" -eq 1 ] || break
    done
    printf '%s\n' "$count"
}

assert_one_colon_entry() {
    local label="$1"
    local list="$2"
    local expected="$3"
    local actual=""

    actual=$(count_colon_entry "$list" "$expected")
    [ "$actual" -eq 1 ] || \
        fail "$label contains $actual copies of $expected"
}

assert_zero_colon_entry() {
    local label="$1"
    local list="$2"
    local expected="$3"
    local actual=""

    actual=$(count_colon_entry "$list" "$expected")
    [ "$actual" -eq 0 ] || \
        fail "$label retained $actual copies of $expected"
}

TEST_HOME="$TEST_ROOT/home with spaces"
FAKE_BIN="$TEST_ROOT/fake-bin"
BAZEL_SELECTED="$TEST_HOME/tools/bazel/8.4.2"
CMAKE_LINUX_DEFAULT="$TEST_HOME/tools/cmake/4.3.3"
CMAKE_LINUX_SELECTED="$TEST_HOME/tools/cmake/4.2.0"
CMAKE_MACOS_SELECTED="$TEST_HOME/tools/cmake/4.4.0"
LLVM_SELECTED="$TEST_HOME/tools/llvm/21.1.6"
MOLD_SELECTED="$TEST_HOME/tools/mold/2.40.4"
VULKAN_SELECTED="$TEST_HOME/tools/vulkan/1.4.328.1"
mkdir -p "$BAZEL_SELECTED/bin"
mkdir -p "$CMAKE_LINUX_DEFAULT/bin"
mkdir -p "$CMAKE_LINUX_SELECTED/bin"
mkdir -p "$CMAKE_MACOS_SELECTED/CMake.app/Contents/bin"
mkdir -p "$TEST_HOME/tools/cmake/incomplete"
mkdir -p \
    "$LLVM_SELECTED/bin" \
    "$LLVM_SELECTED/lib/cmake/clang" \
    "$LLVM_SELECTED/lib/cmake/llvm" \
    "$LLVM_SELECTED/lib/cmake/mlir"
mkdir -p "$MOLD_SELECTED/bin"
mkdir -p "$TEST_HOME/tools/beads/1.0"
mkdir -p "$TEST_HOME/tools/ninja/1.13.2/bin"
mkdir -p \
    "$VULKAN_SELECTED/x86_64/bin" \
    "$VULKAN_SELECTED/x86_64/include/vulkan" \
    "$VULKAN_SELECTED/x86_64/lib/pkgconfig" \
    "$VULKAN_SELECTED/x86_64/share/vulkan/explicit_layer.d"
mkdir -p \
    "$TEST_HOME/tools/vulkan/debug/lib" \
    "$TEST_HOME/tools/vulkan/debug/share/vulkan/explicit_layer.d" \
    "$TEST_HOME/tools/vulkan/release/lib" \
    "$TEST_HOME/tools/vulkan/release/share/vulkan/explicit_layer.d"
mkdir -p "$FAKE_BIN"
touch "$BAZEL_SELECTED/bin/bazel"
touch "$CMAKE_LINUX_DEFAULT/bin/cmake"
touch "$CMAKE_LINUX_SELECTED/bin/cmake"
touch "$CMAKE_MACOS_SELECTED/CMake.app/Contents/bin/cmake"
touch "$LLVM_SELECTED/bin/clang" "$LLVM_SELECTED/bin/clang++"
touch "$MOLD_SELECTED/bin/mold"
touch "$TEST_HOME/tools/ninja/1.13.2/bin/ninja"
touch \
    "$VULKAN_SELECTED/x86_64/bin/glslangValidator" \
    "$VULKAN_SELECTED/x86_64/bin/spirv-val" \
    "$VULKAN_SELECTED/x86_64/include/vulkan/vulkan.h"
chmod +x \
    "$BAZEL_SELECTED/bin/bazel" \
    "$CMAKE_LINUX_DEFAULT/bin/cmake" \
    "$CMAKE_LINUX_SELECTED/bin/cmake" \
    "$CMAKE_MACOS_SELECTED/CMake.app/Contents/bin/cmake" \
    "$LLVM_SELECTED/bin/clang" \
    "$LLVM_SELECTED/bin/clang++" \
    "$MOLD_SELECTED/bin/mold" \
    "$TEST_HOME/tools/ninja/1.13.2/bin/ninja" \
    "$VULKAN_SELECTED/x86_64/bin/glslangValidator" \
    "$VULKAN_SELECTED/x86_64/bin/spirv-val"
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

assert_invalid_tool_root_rejected() (
    local tool="$1"
    local root_variable="$2"
    local invalid_root="$TEST_ROOT/incomplete-tools/$tool"
    local original_path="/usr/bin:/bin"

    mkdir -p "$invalid_root"
    HOME="$TEST_HOME"
    PATH="$original_path"
    export HOME PATH
    export "$root_variable=$invalid_root"
    # shellcheck source=../tools/platform.sh
    . "$DOTFILES/tools/platform.sh"
    if . "$DOTFILES/tools/$tool/env.sh" >/dev/null 2>&1; then
        fail "$tool environment accepted an incomplete selected root"
    fi
    [ "$PATH" = "$original_path" ] || \
        fail "$tool environment mutated PATH before rejecting its root"
)

# Explicit roots are an input boundary, not an assertion that the payload is
# healthy. Every tracked environment must fail before mutation when its
# required executable or SDK surface is absent.
assert_invalid_tool_root_rejected bazel BAZEL_ROOT
assert_invalid_tool_root_rejected cmake CMAKE_ROOT
assert_invalid_tool_root_rejected cuda CUDA_ROOT
assert_invalid_tool_root_rejected hf HF_ROOT
assert_invalid_tool_root_rejected llvm LLVM_ROOT
assert_invalid_tool_root_rejected mold MOLD_ROOT
assert_invalid_tool_root_rejected ninja NINJA_ROOT
assert_invalid_tool_root_rejected rocm ROCM_ROOT
assert_invalid_tool_root_rejected vulkan VULKAN_ROOT

# The shared activation predicate matches the native installer matrix. Override
# detection functions directly so every host target is covered on one machine.
(
    # shellcheck source=../tools/platform.sh
    . "$DOTFILES/tools/platform.sh"
    TEST_PLATFORM=linux
    TEST_ARCHITECTURE=x86_64
    _detect_os() { printf '%s\n' "$TEST_PLATFORM"; }
    _detect_arch() { printf '%s\n' "$TEST_ARCHITECTURE"; }

    _platform_supports rocm ||
        fail "Linux x86-64 did not support ROCm activation"
    _platform_supports vulkan ||
        fail "Linux x86-64 did not support Vulkan activation"

    TEST_ARCHITECTURE=arm64
    _platform_supports cuda ||
        fail "Linux ARM64 did not support CUDA activation"
    _platform_supports llvm ||
        fail "Linux ARM64 did not support LLVM activation"
    if _platform_supports rocm || _platform_supports vulkan; then
        fail "Linux ARM64 accepted an x86-64-only GPU tool"
    fi

    TEST_PLATFORM=darwin
    TEST_ARCHITECTURE=x86_64
    if _platform_supports llvm; then
        fail "Intel macOS accepted an unavailable LLVM archive"
    fi
    TEST_ARCHITECTURE=arm64
    _platform_supports llvm ||
        fail "Apple Silicon macOS did not support LLVM activation"

    TEST_PLATFORM=wsl
    TEST_ARCHITECTURE=x86_64
    _platform_supports llvm ||
        fail "WSL x86-64 did not support the Linux LLVM archive"
    _platform_supports vulkan ||
        fail "WSL x86-64 did not support the Linux Vulkan archive"
    if _platform_supports cuda ||
       _platform_supports rocm ||
       _platform_supports mold; then
        fail "WSL activated a native-Linux-only tool"
    fi

    TEST_PLATFORM=unknown
    TEST_ARCHITECTURE=unknown
    if _platform_supports bazel || _platform_supports unregistered; then
        fail "unknown host or tool bypassed the exact activation matrix"
    fi
)

# Required shared helpers are part of activation, not optional shell polish.
MISSING_HELPER_HOME="$TEST_ROOT/missing-helper-home"
mkdir -p "$MISSING_HELPER_HOME/tools" "$MISSING_HELPER_HOME/.dotfiles/tools"
(
    HOME="$MISSING_HELPER_HOME"
    export HOME
    if . "$DOTFILES/tools/tools.sh" >/dev/null 2>&1; then
        fail "ambient loader accepted missing platform/version helpers"
    fi
)
(
    HOME="$TEST_HOME"
    helper_watch_calls=0
    export HOME
    watch_file() {
        helper_watch_calls=$((helper_watch_calls + 1))
        return 1
    }
    log_error() { :; }
    if . "$DOTFILES/tools/direnvrc" >/dev/null 2>&1; then
        fail "direnv loader accepted an unwatched canonical loader"
    fi
    [ "$helper_watch_calls" -eq 1 ] || \
        fail "direnv continued after its own watch failed"
)
(
    HOME="$TEST_HOME"
    helper_watch_calls=0
    export HOME
    watch_file() {
        helper_watch_calls=$((helper_watch_calls + 1))
        [ "$helper_watch_calls" -eq 1 ]
    }
    log_error() { :; }
    if . "$DOTFILES/tools/direnvrc" >/dev/null 2>&1; then
        fail "direnv loader masked a failed platform helper watch"
    fi
    [ "$helper_watch_calls" -eq 2 ] || \
        fail "direnv continued after its platform helper failed"
)
(
    HOME="$TEST_HOME"
    helper_watch_calls=0
    export HOME
    watch_file() {
        helper_watch_calls=$((helper_watch_calls + 1))
        [ "$helper_watch_calls" -le 2 ]
    }
    log_error() { :; }
    if . "$DOTFILES/tools/direnvrc" >/dev/null 2>&1; then
        fail "direnv loader masked a failed version helper watch"
    fi
    [ "$helper_watch_calls" -eq 3 ] || \
        fail "direnv did not reach the version-helper failure"
)

# Exact-entry normalization preserves unrelated and empty PATH components
# while collapsing every copy of the selected entry.
(
    PATH=":/one:/selected:/two:/selected:"
    export PATH
    # shellcheck source=../tools/platform.sh
    . "$DOTFILES/tools/platform.sh"
    _prepend_path_entry PATH "/selected"
    [ "$PATH" = "/selected::/one:/two:" ] || \
        fail "path helper did not preserve non-selected entries exactly"

    unset TEST_SEARCH_PATH
    _prepend_path_entry TEST_SEARCH_PATH "/selected"
    [ "$TEST_SEARCH_PATH" = "/selected" ] || \
        fail "path helper changed an unset variable into an empty search list"
    TEST_SEARCH_PATH=""
    export TEST_SEARCH_PATH
    _prepend_path_entry TEST_SEARCH_PATH "/selected"
    [ "$TEST_SEARCH_PATH" = "/selected:" ] || \
        fail "path helper discarded an explicit empty search-list component"
)

(
    PATH="$FAKE_BIN:/usr/bin:/bin"
    export PATH
    # shellcheck source=../tools/versions.sh
    . "$DOTFILES/tools/versions.sh"

    expected="$CMAKE_LINUX_DEFAULT"
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
    unset \
        BAZEL_ROOT \
        CMAKE_ROOT \
        CUDA_ROOT \
        LD_LIBRARY_PATH \
        LLVM_ROOT \
        NINJA_ROOT \
        ROCM_ROOT \
        ROCM_VENV_ROOT \
        VULKAN_ROOT

    # shellcheck source=../tools/tools.sh
    . "$DOTFILES/tools/tools.sh"
    . "$DOTFILES/tools/tools.sh"
    [ "$CMAKE_ROOT" = "$CMAKE_LINUX_DEFAULT" ] || \
        fail "default CMake root is incorrect"
    [ "$NINJA_ROOT" = "$TEST_HOME/tools/ninja/1.13.2" ] || \
        fail "default Ninja root is incorrect"
    case ":$PATH:" in
        *":$CMAKE_ROOT/bin:"*) ;;
        *) fail "tracked CMake environment did not update PATH" ;;
    esac
    assert_one_colon_entry \
        "repeated shell PATH" "$PATH" "$CMAKE_ROOT/bin"
    assert_one_colon_entry \
        "repeated shell PATH" "$PATH" "$NINJA_ROOT/bin"
)

(
    HOME="$TEST_HOME"
    CMAKE_ROOT="$CMAKE_LINUX_SELECTED"
    LLVM_ROOT="$LLVM_SELECTED"
    PATH="$FAKE_BIN:/usr/bin:/bin"
    export HOME CMAKE_ROOT LLVM_ROOT PATH
    unset \
        BAZEL_ROOT \
        CUDA_ROOT \
        LD_LIBRARY_PATH \
        NINJA_ROOT \
        ROCM_ROOT \
        ROCM_VENV_ROOT \
        VULKAN_ROOT

    # shellcheck source=../tools/tools.sh
    . "$DOTFILES/tools/tools.sh"
    . "$DOTFILES/tools/tools.sh"
    [ "$CMAKE_ROOT" = "$CMAKE_LINUX_SELECTED" ] || \
        fail "explicit CMake selection was overwritten"
    [ "$LLVM_ROOT" = "$LLVM_SELECTED" ] || \
        fail "explicit LLVM selection was overwritten"
    case ":$PATH:" in
        *":$CMAKE_ROOT/bin:"*) ;;
        *) fail "explicit CMake selection was not activated" ;;
    esac
    [ "$CC" = "$LLVM_SELECTED/bin/clang" ] || \
        fail "tracked LLVM environment did not set CC"
    [ "$CXX" = "$LLVM_SELECTED/bin/clang++" ] || \
        fail "tracked LLVM environment did not set CXX"
    assert_one_colon_entry \
        "explicit tool PATH" "$PATH" "$CMAKE_ROOT/bin"
    assert_one_colon_entry \
        "explicit tool PATH" "$PATH" "$LLVM_ROOT/bin"
    assert_one_colon_entry \
        "explicit tool library path" \
        "$LD_LIBRARY_PATH" "$LLVM_ROOT/lib"
)

# Machine-local root selections are inputs to ambient activation, not
# post-activation variable overrides. Sourcing the real common shell must keep
# every derived path and compiler on the selected generation.
SHRC_HOME="$TEST_ROOT/shrc-home"
SHRC_LATEST_LLVM="$SHRC_HOME/tools/llvm/22.0.0"
SHRC_SELECTED_LLVM="$SHRC_HOME/tools/llvm/21.1.6"
mkdir -p \
    "$SHRC_LATEST_LLVM/bin" \
    "$SHRC_LATEST_LLVM/lib/cmake/clang" \
    "$SHRC_LATEST_LLVM/lib/cmake/llvm" \
    "$SHRC_LATEST_LLVM/lib/cmake/mlir" \
    "$SHRC_SELECTED_LLVM/bin" \
    "$SHRC_SELECTED_LLVM/lib/cmake/clang" \
    "$SHRC_SELECTED_LLVM/lib/cmake/llvm" \
    "$SHRC_SELECTED_LLVM/lib/cmake/mlir"
touch \
    "$SHRC_LATEST_LLVM/bin/clang" \
    "$SHRC_LATEST_LLVM/bin/clang++" \
    "$SHRC_SELECTED_LLVM/bin/clang" \
    "$SHRC_SELECTED_LLVM/bin/clang++"
chmod +x \
    "$SHRC_LATEST_LLVM/bin/clang" \
    "$SHRC_LATEST_LLVM/bin/clang++" \
    "$SHRC_SELECTED_LLVM/bin/clang" \
    "$SHRC_SELECTED_LLVM/bin/clang++"
ln -s "$DOTFILES" "$SHRC_HOME/.dotfiles"
ln -s 22.0.0 "$SHRC_HOME/tools/llvm/latest"
# shellcheck disable=SC2016  # Expanded when the generated local file is sourced.
printf '%s\n' \
    'export LLVM_ROOT="$HOME/tools/llvm/21.1.6"' \
    > "$SHRC_HOME/.shrc.local"
(
    HOME="$SHRC_HOME"
    PATH="/usr/bin:/bin"
    export HOME PATH
    unset \
        BAZEL_ROOT \
        CC \
        CMAKE_ROOT \
        CLANG_DIR \
        CUDA_ROOT \
        CXX \
        DOTFILES_LLVM_LIBRARY_ENTRY \
        DOTFILES_LLVM_PATH_ENTRY \
        LD_LIBRARY_PATH \
        LLVM_DIR \
        LLVM_ROOT \
        MLIR_DIR \
        NINJA_ROOT \
        ROCM_ROOT \
        ROCM_VENV_ROOT \
        VULKAN_ROOT

    # shellcheck source=../shell/shrc
    . "$DOTFILES/shell/shrc"
    [ "$LLVM_ROOT" = "$SHRC_SELECTED_LLVM" ] ||
        fail "common shell overwrote the machine-selected LLVM root"
    [ "$CC" = "$SHRC_SELECTED_LLVM/bin/clang" ] ||
        fail "common shell left CC on the default LLVM generation"
    [ "$CXX" = "$SHRC_SELECTED_LLVM/bin/clang++" ] ||
        fail "common shell left CXX on the default LLVM generation"
    [ "$LLVM_DIR" = "$SHRC_SELECTED_LLVM/lib/cmake/llvm" ] ||
        fail "common shell left LLVM_DIR on the default generation"
    [ "$CLANG_DIR" = "$SHRC_SELECTED_LLVM/lib/cmake/clang" ] ||
        fail "common shell left CLANG_DIR on the default generation"
    [ "$MLIR_DIR" = "$SHRC_SELECTED_LLVM/lib/cmake/mlir" ] ||
        fail "common shell left MLIR_DIR on the default generation"
    assert_one_colon_entry \
        "machine-selected LLVM PATH" "$PATH" "$SHRC_SELECTED_LLVM/bin"
    assert_zero_colon_entry \
        "machine-selected LLVM PATH" "$PATH" "$SHRC_LATEST_LLVM/bin"
    assert_one_colon_entry \
        "machine-selected LLVM library path" \
        "$LD_LIBRARY_PATH" "$SHRC_SELECTED_LLVM/lib"
)

# The ambient loader may continue checking independent defaults after one
# damaged explicit root, but it must return failure instead of reporting a
# fully healthy shell environment.
(
    HOME="$TEST_HOME"
    CMAKE_ROOT="$TEST_HOME/tools/cmake/incomplete"
    PATH="$FAKE_BIN:/usr/bin:/bin"
    export HOME CMAKE_ROOT PATH
    unset \
        BAZEL_ROOT \
        CUDA_ROOT \
        LLVM_ROOT \
        ROCM_ROOT \
        ROCM_VENV_ROOT \
        VULKAN_ROOT

    if . "$DOTFILES/tools/tools.sh" >/dev/null 2>&1; then
        fail "ambient loader masked an incomplete explicit CMake root"
    fi
    assert_zero_colon_entry \
        "failed ambient CMake PATH" "$PATH" "$CMAKE_ROOT/bin"
)

# The CMake environment recognizes the application-bundle layout shipped by
# the supported macOS universal archive.
(
    # shellcheck disable=SC2030
    CMAKE_ROOT="$CMAKE_MACOS_SELECTED"
    PATH="/usr/bin:/bin"
    export CMAKE_ROOT PATH

    # shellcheck source=../tools/platform.sh
    . "$DOTFILES/tools/platform.sh"
    # shellcheck source=../tools/cmake/env.sh
    . "$DOTFILES/tools/cmake/env.sh"
    . "$DOTFILES/tools/cmake/env.sh"
    case ":$PATH:" in
        *":$CMAKE_ROOT/CMake.app/Contents/bin:"*) ;;
        *) fail "macOS CMake application bundle was not activated" ;;
    esac
    assert_one_colon_entry \
        "CMake bundle PATH" \
        "$PATH" "$CMAKE_ROOT/CMake.app/Contents/bin"
)

# direnv uses the same tracked definition as interactive shells.
(
    HOME="$TEST_HOME"
    PATH="$FAKE_BIN:/usr/bin:/bin"
    LDFLAGS="-Wl,--as-needed"
    export HOME PATH LDFLAGS
    # shellcheck disable=SC2317  # Invoked while sourcing direnvrc.
    watch_file() { :; }
    # shellcheck disable=SC2317  # Invoked by direnvrc on source failure.
    log_error() { echo "$*" >&2; }

    # shellcheck source=../tools/direnvrc
    . "$DOTFILES/tools/direnvrc"
    use_cmake latest
    use_cmake latest
    # shellcheck disable=SC2031
    [ "$CMAKE_ROOT" = "$TEST_HOME/tools/cmake/4.3.3" ] || \
        fail "direnv CMake root is incorrect"
    # shellcheck disable=SC2031
    case ":$PATH:" in
        *":$CMAKE_ROOT/bin:"*) ;;
        *) fail "direnv CMake environment did not update PATH" ;;
    esac
    assert_one_colon_entry \
        "direnv CMake PATH" "$PATH" "$CMAKE_ROOT/bin"

    use_cmake 4.4.0
    # shellcheck disable=SC2031
    [ "$CMAKE_ROOT" = "$CMAKE_MACOS_SELECTED" ] || \
        fail "direnv macOS CMake root is incorrect"
    # shellcheck disable=SC2031
    case ":$PATH:" in
        *":$CMAKE_ROOT/CMake.app/Contents/bin:"*) ;;
        *) fail "direnv macOS CMake bundle did not update PATH" ;;
    esac
    assert_zero_colon_entry \
        "switched CMake PATH" "$PATH" "$CMAKE_LINUX_DEFAULT/bin"

    use_cmake latest
    [ "${PATH%%:*}" = "$CMAKE_LINUX_DEFAULT/bin" ] || \
        fail "reselected CMake did not return to the front of PATH"
    assert_one_colon_entry \
        "reselected CMake PATH" "$PATH" "$CMAKE_LINUX_DEFAULT/bin"
    assert_zero_colon_entry \
        "reselected CMake PATH" \
        "$PATH" "$CMAKE_MACOS_SELECTED/CMake.app/Contents/bin"
    [ "$_DIRENV_TOOLS" = "cmake:4.3.3" ] || \
        fail "direnv tool summary retained superseded CMake selections"

    use_mold 2.40.4
    use_mold 2.40.4
    assert_one_colon_entry \
        "explicit mold PATH" "$PATH" "$MOLD_SELECTED/bin"
    [ "$LDFLAGS" = "-fuse-ld=mold -Wl,--as-needed" ] || \
        fail "repeated mold activation duplicated or reordered LDFLAGS"
    [ "$_DIRENV_TOOLS" = "cmake:4.3.3 mold:2.40.4" ] || \
        fail "direnv tool summary duplicated mold"

    use_vulkan_layers debug
    use_vulkan_layers debug
    assert_one_colon_entry \
        "Vulkan debug layers" \
        "$LD_LIBRARY_PATH" "$TEST_HOME/tools/vulkan/debug/lib"
    use_vulkan_layers release
    assert_zero_colon_entry \
        "switched Vulkan layers" \
        "$LD_LIBRARY_PATH" "$TEST_HOME/tools/vulkan/debug/lib"
    assert_one_colon_entry \
        "switched Vulkan layers" \
        "$LD_LIBRARY_PATH" "$TEST_HOME/tools/vulkan/release/lib"

    previous_path="$PATH"
    previous_root="$CMAKE_ROOT"
    previous_tools="$_DIRENV_TOOLS"
    if use_cmake incomplete >/dev/null 2>&1; then
        fail "direnv recorded an incomplete CMake root as active"
    fi
    [ "$PATH" = "$previous_path" ] || \
        fail "failed direnv activation changed PATH"
    [ "$CMAKE_ROOT" = "$previous_root" ] || \
        fail "failed direnv activation retained the rejected root"
    [ "$_DIRENV_TOOLS" = "$previous_tools" ] || \
        fail "failed direnv activation changed the tool summary"

    DOTFILES_CMAKE_PATH_ENTRY="invalid:entry"
    export DOTFILES_CMAKE_PATH_ENTRY
    if use_cmake 4.4.0 >/dev/null 2>&1; then
        fail "direnv masked a managed-path activation failure"
    fi
    [ "$PATH" = "$previous_path" ] || \
        fail "managed-path activation failure changed PATH"
    [ "$CMAKE_ROOT" = "$previous_root" ] || \
        fail "managed-path activation failure retained the rejected root"
    [ "$_DIRENV_TOOLS" = "$previous_tools" ] || \
        fail "managed-path activation failure changed the tool summary"

    BEADS_ROOT="previous missing-env selection"
    export BEADS_ROOT
    if _use_tool beads 1.0 >/dev/null 2>&1; then
        fail "direnv accepted a tool without a tracked environment"
    fi
    [ "$BEADS_ROOT" = "previous missing-env selection" ] || \
        fail "missing environment definition changed the prior root"

    LOCAL_ENV_FAILURE="$TEST_ROOT/local-env-failure"
    mkdir -p "$LOCAL_ENV_FAILURE"
    printf '%s\n' "return 1" > "$LOCAL_ENV_FAILURE/.envrc.local"
    cd "$LOCAL_ENV_FAILURE"
    if source_local_envrc >/dev/null 2>&1; then
        fail "source_local_envrc masked a failing local override"
    fi
)

# Every tracked environment uses the same exact-entry semantics for search
# paths. Exercise the remaining single-root and multi-variable definitions
# directly so a typo cannot hide behind the shared helper's coverage.
(
    BAZEL_ROOT="$BAZEL_SELECTED"
    VULKAN_ROOT="$VULKAN_SELECTED"
    PATH="/usr/bin:/bin"
    export BAZEL_ROOT VULKAN_ROOT PATH
    unset CMAKE_PREFIX_PATH LD_LIBRARY_PATH PKG_CONFIG_PATH

    # shellcheck source=../tools/platform.sh
    . "$DOTFILES/tools/platform.sh"
    # shellcheck source=../tools/bazel/env.sh
    . "$DOTFILES/tools/bazel/env.sh"
    . "$DOTFILES/tools/bazel/env.sh"
    # shellcheck source=../tools/vulkan/env.sh
    . "$DOTFILES/tools/vulkan/env.sh"
    . "$DOTFILES/tools/vulkan/env.sh"

    assert_one_colon_entry "Bazel PATH" "$PATH" "$BAZEL_ROOT/bin"
    assert_one_colon_entry "Vulkan PATH" "$PATH" "$VULKAN_SDK/bin"
    assert_one_colon_entry \
        "Vulkan library path" "$LD_LIBRARY_PATH" "$VULKAN_SDK/lib"
    assert_one_colon_entry \
        "Vulkan package path" \
        "$PKG_CONFIG_PATH" "$VULKAN_SDK/lib/pkgconfig"
    assert_one_colon_entry \
        "Vulkan CMake prefix" "$CMAKE_PREFIX_PATH" "$VULKAN_SDK"
)

# CUDA uses one tracked environment definition with the standard SDK variables
# consumed by shell tools and CMake.
CUDA_SDK="$TEST_HOME/tools/cuda/13.0.0"
mkdir -p \
    "$CUDA_SDK/bin" \
    "$CUDA_SDK/include" \
    "$CUDA_SDK/lib64" \
    "$CUDA_SDK/nvvm/libdevice"
touch \
    "$CUDA_SDK/bin/nvcc" \
    "$CUDA_SDK/include/cuda.h" \
    "$CUDA_SDK/nvvm/libdevice/libdevice.10.bc"
chmod +x "$CUDA_SDK/bin/nvcc"
(
    CUDA_ROOT="$CUDA_SDK"
    PATH="/usr/bin:/bin"
    export CUDA_ROOT PATH

    # shellcheck source=../tools/platform.sh
    . "$DOTFILES/tools/platform.sh"
    # shellcheck source=../tools/cuda/env.sh
    . "$DOTFILES/tools/cuda/env.sh"
    . "$DOTFILES/tools/cuda/env.sh"
    [ "$CUDA_HOME" = "$CUDA_SDK" ] || fail "CUDA_HOME is incorrect"
    [ "$CUDAToolkit_ROOT" = "$CUDA_SDK" ] || \
        fail "CMake CUDA toolkit root is incorrect"
    [ "$CUDACXX" = "$CUDA_SDK/bin/nvcc" ] || \
        fail "CUDA compiler path is incorrect"
    assert_one_colon_entry "CUDA PATH" "$PATH" "$CUDA_SDK/bin"
    assert_one_colon_entry \
        "CUDA library path" "$LD_LIBRARY_PATH" "$CUDA_SDK/lib64"
)

# The managed Hugging Face payload remains off PATH so bin/hf retains
# credential and cache policy authority.
HF_TOOL="$TEST_HOME/tools/hf/1.24.0"
mkdir -p "$HF_TOOL/bin"
touch "$HF_TOOL/bin/hf"
chmod +x "$HF_TOOL/bin/hf"
(
    HF_ROOT="$HF_TOOL"
    PATH="/usr/bin:/bin"
    export HF_ROOT PATH

    # shellcheck source=../tools/hf/env.sh
    . "$DOTFILES/tools/hf/env.sh"
    [ "$PATH" = "/usr/bin:/bin" ] || \
        fail "raw Hugging Face CLI escaped onto PATH"
)

# Packaged TheRock roots resolve through rocm-sdk while retaining the venv
# tools on PATH.
ROCM_VENV="$TEST_HOME/tools/rocm/7.0.0"
ROCM_SDK="$TEST_HOME/assembled ROCm SDK"
mkdir -p \
    "$ROCM_VENV/bin" \
    "$ROCM_SDK/bin" \
    "$ROCM_SDK/include/hip" \
    "$ROCM_SDK/lib/cmake/hip" \
    "$ROCM_SDK/lib/llvm/bin" \
    "$ROCM_SDK/lib/rocm_sysdeps/lib"
touch "$ROCM_VENV/pyvenv.cfg"
touch "$ROCM_SDK/include/hip/hip_runtime.h"
touch "$ROCM_SDK/lib/libamdhip64.so"
touch "$ROCM_SDK/lib/cmake/hip/hip-config.cmake"
cat > "$ROCM_VENV/bin/rocm-sdk" << EOF
#!/bin/sh
printf '%s\n' '$ROCM_SDK'
EOF
chmod +x "$ROCM_VENV/bin/rocm-sdk"
cat > "$ROCM_SDK/bin/hipconfig" << 'EOF'
#!/bin/sh
[ "${1:-}" = "--platform" ] || exit 2
[ -z "${HIP_PLATFORM:-}" ] || {
    printf '%s\n' "$HIP_PLATFORM"
    exit 0
}
printf 'amd\n'
EOF
chmod +x "$ROCM_SDK/bin/hipconfig"
(
    ROCM_ROOT="$ROCM_VENV"
    PATH="/usr/bin:/bin"
    export ROCM_ROOT PATH

    # shellcheck source=../tools/platform.sh
    . "$DOTFILES/tools/platform.sh"
    # shellcheck source=../tools/rocm/env.sh
    . "$DOTFILES/tools/rocm/env.sh"
    . "$DOTFILES/tools/rocm/env.sh"
    [ "$ROCM_VENV_ROOT" = "$ROCM_VENV" ] || fail "ROCm venv root was lost"
    [ "$ROCM_ROOT" = "$ROCM_SDK" ] || fail "assembled ROCm root is incorrect"
    [ "$HIP_CLANG_PATH" = "$ROCM_SDK/lib/llvm/bin" ] || \
        fail "HIP clang path is incorrect"
    case ":$PATH:" in
        *":$ROCM_VENV/bin:"*) ;;
        *) fail "ROCm venv tools are missing from PATH" ;;
    esac
    assert_one_colon_entry "ROCm SDK PATH" "$PATH" "$ROCM_SDK/bin"
    assert_one_colon_entry "ROCm venv PATH" "$PATH" "$ROCM_VENV/bin"
    assert_one_colon_entry \
        "ROCm CMake prefix" "$CMAKE_PREFIX_PATH" "$ROCM_SDK"
    assert_one_colon_entry \
        "ROCm library path" "$LD_LIBRARY_PATH" "$ROCM_SDK/lib"
    assert_one_colon_entry \
        "ROCm sysdeps library path" \
        "$LD_LIBRARY_PATH" "$ROCM_SDK/lib/rocm_sysdeps/lib"
)

# A source-build layout uses separate compiler and HIP-runtime binary roots.
ROCM_BUILD="$TEST_HOME/tools/rocm/build"
mkdir -p \
    "$ROCM_BUILD/compiler/amd-llvm/bin" \
    "$ROCM_BUILD/core/hip-runtime/bin"
touch "$ROCM_BUILD/build.ninja"
cat > "$ROCM_BUILD/core/hip-runtime/bin/hipconfig" << 'EOF'
#!/bin/sh
[ "${1:-}" = "--platform" ] || exit 2
[ -z "${HIP_PLATFORM:-}" ] || {
    printf '%s\n' "$HIP_PLATFORM"
    exit 0
}
printf 'nvidia\n'
EOF
chmod +x "$ROCM_BUILD/core/hip-runtime/bin/hipconfig"

# A conventional materialized root may expose rocm-sdk as a convenience
# symlink, but without pyvenv.cfg it remains the selected SDK authority.
ROCM_CONVENTIONAL="$TEST_HOME/tools/rocm/8.0.0"
ROCM_REDIRECT="$TEST_HOME/unexpected redirected SDK"
mkdir -p \
    "$ROCM_CONVENTIONAL/bin" \
    "$ROCM_CONVENTIONAL/include/hip" \
    "$ROCM_CONVENTIONAL/lib/cmake/hip" \
    "$ROCM_CONVENTIONAL/lib/llvm/bin" \
    "$ROCM_CONVENTIONAL/lib/rocm_sysdeps/lib"
touch "$ROCM_CONVENTIONAL/include/hip/hip_runtime.h"
touch "$ROCM_CONVENTIONAL/lib/libamdhip64.so"
touch "$ROCM_CONVENTIONAL/lib/cmake/hip/hip-config.cmake"
cat > "$ROCM_CONVENTIONAL/bin/rocm-sdk" << EOF
#!/bin/sh
printf '%s\n' '$ROCM_REDIRECT'
EOF
cat > "$ROCM_CONVENTIONAL/bin/hipconfig" << 'EOF'
#!/bin/sh
[ "${1:-}" = "--platform" ] || exit 2
[ -z "${HIP_PLATFORM:-}" ] || {
    printf '%s\n' "$HIP_PLATFORM"
    exit 0
}
printf 'nvidia\n'
EOF
chmod +x \
    "$ROCM_CONVENTIONAL/bin/rocm-sdk" \
    "$ROCM_CONVENTIONAL/bin/hipconfig"
(
    ROCM_ROOT="$ROCM_CONVENTIONAL"
    PATH="/usr/bin:/bin"
    export ROCM_ROOT PATH
    unset ROCM_VENV_ROOT

    # shellcheck source=../tools/platform.sh
    . "$DOTFILES/tools/platform.sh"
    # shellcheck source=../tools/rocm/env.sh
    . "$DOTFILES/tools/rocm/env.sh"
    . "$DOTFILES/tools/rocm/env.sh"
    [ "$ROCM_ROOT" = "$ROCM_CONVENTIONAL" ] || \
        fail "conventional ROCm root was redirected through rocm-sdk"
    [ -z "${ROCM_VENV_ROOT:-}" ] || \
        fail "conventional ROCm root was mislabeled as a virtualenv"
    [ "$HIP_PLATFORM" = "nvidia" ] || \
        fail "conventional ROCm backend was not queried"
    assert_one_colon_entry \
        "conventional ROCm PATH" "$PATH" "$ROCM_CONVENTIONAL/bin"
)

# Switching selected ROCm roots in one shell removes every path owned by the
# prior SDK and re-queries the new root's backend. The installed-to-build
# transition also replaces library paths with the compiler/runtime split.
(
    HOME="$TEST_HOME"
    ROCM_ROOT="$ROCM_VENV"
    PATH="/usr/bin:/bin"
    export HOME ROCM_ROOT PATH

    # shellcheck source=../tools/platform.sh
    . "$DOTFILES/tools/platform.sh"
    # shellcheck source=../tools/rocm/env.sh
    . "$DOTFILES/tools/rocm/env.sh"
    [ "$HIP_PLATFORM" = "amd" ] || fail "first ROCm backend is incorrect"

    ROCM_ROOT="$ROCM_BUILD"
    export ROCM_ROOT
    . "$DOTFILES/tools/rocm/env.sh"
    [ "$HIP_PLATFORM" = "nvidia" ] || \
        fail "ROCm switch retained the previous backend"
    [ -z "${ROCM_VENV_ROOT:-}" ] || \
        fail "ROCm switch retained the previous packaging venv"
    assert_one_colon_entry \
        "switched ROCm compiler PATH" \
        "$PATH" "$ROCM_BUILD/compiler/amd-llvm/bin"
    assert_one_colon_entry \
        "switched ROCm runtime PATH" \
        "$PATH" "$ROCM_BUILD/core/hip-runtime/bin"
    assert_zero_colon_entry \
        "switched ROCm PATH" "$PATH" "$ROCM_SDK/bin"
    assert_zero_colon_entry \
        "switched ROCm PATH" "$PATH" "$ROCM_VENV/bin"
    assert_zero_colon_entry \
        "switched ROCm CMake prefix" \
        "$CMAKE_PREFIX_PATH" "$ROCM_SDK"
    assert_one_colon_entry \
        "switched ROCm CMake prefix" \
        "$CMAKE_PREFIX_PATH" "$ROCM_BUILD"
    assert_zero_colon_entry \
        "switched ROCm library path" \
        "${LD_LIBRARY_PATH:-}" "$ROCM_SDK/lib"
    assert_zero_colon_entry \
        "switched ROCm sysdeps library path" \
        "${LD_LIBRARY_PATH:-}" "$ROCM_SDK/lib/rocm_sysdeps/lib"

    ROCM_ROOT="$ROCM_CONVENTIONAL"
    export ROCM_ROOT
    . "$DOTFILES/tools/rocm/env.sh"
    assert_one_colon_entry \
        "materialized ROCm PATH" "$PATH" "$ROCM_CONVENTIONAL/bin"
    assert_zero_colon_entry \
        "materialized ROCm PATH" \
        "$PATH" "$ROCM_BUILD/compiler/amd-llvm/bin"
    assert_zero_colon_entry \
        "materialized ROCm PATH" \
        "$PATH" "$ROCM_BUILD/core/hip-runtime/bin"
    assert_zero_colon_entry \
        "materialized ROCm CMake prefix" \
        "$CMAKE_PREFIX_PATH" "$ROCM_BUILD"
    assert_one_colon_entry \
        "materialized ROCm CMake prefix" \
        "$CMAKE_PREFIX_PATH" "$ROCM_CONVENTIONAL"
    assert_one_colon_entry \
        "materialized ROCm library path" \
        "$LD_LIBRARY_PATH" "$ROCM_CONVENTIONAL/lib"
    assert_one_colon_entry \
        "materialized ROCm sysdeps library path" \
        "$LD_LIBRARY_PATH" "$ROCM_CONVENTIONAL/lib/rocm_sysdeps/lib"
)

# direnv must invalidate a loaded project when either the global loader or a
# nested tool helper changes. Use an isolated dotfiles copy so the witness can
# change each dependency independently without mutating this checkout.
RELOAD_HOME="$TEST_ROOT/reload-home"
RELOAD_PROJECT="$TEST_ROOT/reload-project"
RELOAD_ROCM="$RELOAD_HOME/tools/rocm/8.0.0"
mkdir -p \
    "$RELOAD_HOME/.dotfiles/tools/rocm" \
    "$RELOAD_HOME/tools/rocm" \
    "$RELOAD_PROJECT" \
    "$RELOAD_ROCM/bin" \
    "$RELOAD_ROCM/include/hip" \
    "$RELOAD_ROCM/lib/cmake/hip" \
    "$RELOAD_ROCM/lib/llvm/bin" \
    "$RELOAD_ROCM/lib/rocm_sysdeps/lib"
cp "$DOTFILES/tools/platform.sh" "$RELOAD_HOME/.dotfiles/tools/platform.sh"
cp "$DOTFILES/tools/versions.sh" "$RELOAD_HOME/.dotfiles/tools/versions.sh"
cp "$DOTFILES/tools/direnvrc" "$RELOAD_HOME/.dotfiles/tools/direnvrc"
cp "$DOTFILES/tools/rocm/env.sh" "$RELOAD_HOME/.dotfiles/tools/rocm/env.sh"
cp "$DOTFILES/tools/rocm/root.sh" "$RELOAD_HOME/.dotfiles/tools/rocm/root.sh"
ln -s "$RELOAD_HOME/.dotfiles/tools/direnvrc" "$RELOAD_HOME/.direnvrc"
ln -s 8.0.0 "$RELOAD_HOME/tools/rocm/latest"
touch \
    "$RELOAD_ROCM/include/hip/hip_runtime.h" \
    "$RELOAD_ROCM/lib/libamdhip64.so" \
    "$RELOAD_ROCM/lib/cmake/hip/hip-config.cmake"
cat > "$RELOAD_ROCM/bin/hipconfig" << 'EOF'
#!/bin/sh
[ "${1:-}" = "--platform" ] || exit 2
printf 'amd\n'
EOF
chmod +x "$RELOAD_ROCM/bin/hipconfig"
cat > "$RELOAD_PROJECT/.envrc" << 'EOF'
set -o errexit -o pipefail
use_rocm "8.0.0"
source_local_envrc
EOF
printf '%s\n' \
    'export DOTFILES_DIRENVRC_RELOAD_MARKER=loader-one' \
    >> "$RELOAD_HOME/.dotfiles/tools/direnvrc"
printf '%s\n' \
    'export DOTFILES_ROCM_ROOT_RELOAD_MARKER=root-one' \
    >> "$RELOAD_HOME/.dotfiles/tools/rocm/root.sh"
HOME="$RELOAD_HOME" \
XDG_CACHE_HOME="$RELOAD_HOME/.cache" \
XDG_CONFIG_HOME="$RELOAD_HOME/.config" \
XDG_DATA_HOME="$RELOAD_HOME/.local/share" \
    direnv allow "$RELOAD_PROJECT"
(
    export HOME="$RELOAD_HOME"
    export XDG_CACHE_HOME="$RELOAD_HOME/.cache"
    export XDG_CONFIG_HOME="$RELOAD_HOME/.config"
    export XDG_DATA_HOME="$RELOAD_HOME/.local/share"
    export DIRENV_LOG_FORMAT=""
    cd "$RELOAD_PROJECT"

    eval "$(direnv export bash)"
    if [ "$DOTFILES_DIRENVRC_RELOAD_MARKER" != "loader-one" ] ||
       [ "$DOTFILES_ROCM_ROOT_RELOAD_MARKER" != "root-one" ]; then
        fail "initial real-direnv dependency markers were not loaded"
    fi
    direnv status > "$TEST_ROOT/reload-direnv-status"
    grep -qF ".dotfiles/tools/direnvrc" \
        "$TEST_ROOT/reload-direnv-status" ||
        fail "real direnv did not watch its global loader"
    grep -qF ".dotfiles/tools/rocm/root.sh" \
        "$TEST_ROOT/reload-direnv-status" ||
        fail "real direnv did not watch the nested ROCm helper"

    printf '%s\n' \
        'export DOTFILES_ROCM_ROOT_RELOAD_MARKER=root-two' \
        >> "$RELOAD_HOME/.dotfiles/tools/rocm/root.sh"
    advance_file_mtime "$RELOAD_HOME/.dotfiles/tools/rocm/root.sh"
    eval "$(direnv export bash)"
    if [ "$DOTFILES_DIRENVRC_RELOAD_MARKER" != "loader-one" ] ||
       [ "$DOTFILES_ROCM_ROOT_RELOAD_MARKER" != "root-two" ]; then
        fail "ROCm helper change did not invalidate the loaded environment"
    fi

    printf '%s\n' \
        'export DOTFILES_DIRENVRC_RELOAD_MARKER=loader-two' \
        >> "$RELOAD_HOME/.dotfiles/tools/direnvrc"
    advance_file_mtime "$RELOAD_HOME/.dotfiles/tools/direnvrc"
    eval "$(direnv export bash)"
    if [ "$DOTFILES_DIRENVRC_RELOAD_MARKER" != "loader-two" ] ||
       [ "$DOTFILES_ROCM_ROOT_RELOAD_MARKER" != "root-two" ]; then
        fail "global loader change did not invalidate the loaded environment"
    fi
)

# A failing watched helper must reject the whole project environment before
# direnv starts its child. Keeping the imports out of shell conditionals is
# what preserves errexit through every nested sourced file.
printf '%s\n' \
    'false' \
    'export DOTFILES_AFTER_REJECTED_ROCM_HELPER=leaked' \
    >> "$RELOAD_HOME/.dotfiles/tools/rocm/root.sh"
advance_file_mtime "$RELOAD_HOME/.dotfiles/tools/rocm/root.sh"
RELOAD_REJECTED_CHILD="$TEST_ROOT/rejected-rocm-helper-child-ran"
RELOAD_REJECTED_LOG="$TEST_ROOT/rejected-rocm-helper.log"
if HOME="$RELOAD_HOME" \
        XDG_CACHE_HOME="$RELOAD_HOME/.cache" \
        XDG_CONFIG_HOME="$RELOAD_HOME/.config" \
        XDG_DATA_HOME="$RELOAD_HOME/.local/share" \
        direnv exec "$RELOAD_PROJECT" \
        "$(command -v touch)" "$RELOAD_REJECTED_CHILD" \
        >"$RELOAD_REJECTED_LOG" 2>&1; then
    fail "real direnv accepted a failing nested ROCm helper"
fi
[ ! -e "$RELOAD_REJECTED_CHILD" ] ||
    fail "real direnv ran a child with a rejected nested ROCm helper"

# Interactive zsh does not provide direnv's checked importer. It must take the
# direct nested-helper path instead of interpreting zsh's `declare -F` behavior
# as proof that a function exists.
if command -v zsh >/dev/null 2>&1; then
    HOME="$TEST_HOME" zsh -fc '
        . "$HOME/.dotfiles/tools/platform.sh"
        ROCM_ROOT=""
        . "$HOME/.dotfiles/tools/rocm/env.sh"
    ' || fail "zsh could not source the inactive ROCm environment"
fi

# The common shrc is sourced by POSIX login shells and must never evaluate a
# Bash-specific direnv hook. Each interactive shell owns exactly one native
# hook after the common environment has loaded.
SHELL_HOOK_HOME="$TEST_ROOT/shell-hook-home"
SHELL_HOOK_BIN="$TEST_ROOT/shell-hook-bin"
SHELL_HOOK_LOG="$TEST_ROOT/shell-hook.log"
mkdir -p "$SHELL_HOOK_HOME" "$SHELL_HOOK_BIN"
ln -s "$DOTFILES/shell/shrc" "$SHELL_HOOK_HOME/.shrc"
cat > "$SHELL_HOOK_BIN/direnv" << 'EOF'
#!/bin/sh
[ "${1:-}" = "hook" ] || exit 64
printf '%s\n' "${2:-}" >> "$DIRENV_HOOK_LOG"
case "${2:-}" in
    bash) printf '%s\n' 'export DOTFILES_TEST_DIRENV_HOOK=bash' ;;
    zsh) printf '%s\n' 'export DOTFILES_TEST_DIRENV_HOOK=zsh' ;;
    *) exit 65 ;;
esac
EOF
chmod 0755 "$SHELL_HOOK_BIN/direnv"

dash_shell=$(command -v dash || true)
if [ -n "$dash_shell" ]; then
    SHELL_HOOK_DASH_STDERR="$TEST_ROOT/shell-hook-dash.stderr"
    # shellcheck disable=SC2016  # Expanded by the child dash process.
    if ! env -i \
            HOME="$SHELL_HOOK_HOME" \
            PATH="$SHELL_HOOK_BIN:/usr/bin:/bin" \
            DIRENV_HOOK_LOG="$SHELL_HOOK_LOG" \
            "$dash_shell" -c \
            '. "$1"; [ "${DIRENV_LOG_FORMAT+x}" = x ]' \
            dash "$DOTFILES/shell/shrc" \
            2>"$SHELL_HOOK_DASH_STDERR"; then
        fail "dash could not source the shell-neutral common environment"
    fi
    [ ! -s "$SHELL_HOOK_DASH_STDERR" ] ||
        fail "dash common-shell startup emitted shell-specific diagnostics"
    [ ! -e "$SHELL_HOOK_LOG" ] ||
        fail "common shrc invoked a shell-specific direnv hook"
fi

: > "$SHELL_HOOK_LOG"
if ! HOME="$SHELL_HOOK_HOME" \
        PATH="$SHELL_HOOK_BIN:/usr/bin:/bin" \
        DIRENV_HOOK_LOG="$SHELL_HOOK_LOG" \
        bash --noprofile --norc -ic \
        '. "$1"; [ "${DOTFILES_TEST_DIRENV_HOOK:-}" = bash ]' \
        bash "$DOTFILES/shell/bashrc" \
        >/dev/null 2>&1; then
    fail "interactive Bash did not install its native direnv hook"
fi
if [ "$(wc -l < "$SHELL_HOOK_LOG")" -ne 1 ] ||
        ! grep -qxF bash "$SHELL_HOOK_LOG"; then
    fail "interactive Bash installed duplicate or non-native direnv hooks"
fi

if command -v zsh >/dev/null 2>&1; then
    : > "$SHELL_HOOK_LOG"
    if ! HOME="$SHELL_HOOK_HOME" \
            PATH="$SHELL_HOOK_BIN:/usr/bin:/bin" \
            DIRENV_HOOK_LOG="$SHELL_HOOK_LOG" \
            zsh -dfc \
            '. "$1"; . "$2"; [ "${DOTFILES_TEST_DIRENV_HOOK:-}" = zsh ]' \
            zsh \
            "$DOTFILES/shell/shrc" \
            "$DOTFILES/shell/zshrc.d/direnv.zsh"; then
        fail "zsh did not install its native direnv hook"
    fi
    if [ "$(wc -l < "$SHELL_HOOK_LOG")" -ne 1 ] ||
            ! grep -qxF zsh "$SHELL_HOOK_LOG"; then
        fail "zsh installed duplicate or non-native direnv hooks"
    fi
fi

echo "tool environment portability passed"
