#!/bin/bash
# Smoketest for ROCm.
# Verifies the SDK root, HIP compiler, and runtime tools are usable.

set -euo pipefail

rocm_root="${ROCM_ROOT:-${ROCM_HOME:-${HIP_PATH:-}}}"
if [ -z "$rocm_root" ] && command -v hipcc &>/dev/null; then
    hipcc_path="$(command -v hipcc)"
    rocm_root="$(cd "$(dirname "$hipcc_path")/.." && pwd -P)"
fi

if [ -z "$rocm_root" ]; then
    echo "  rocm root: not active"
    exit 1
fi

if [ ! -e "$rocm_root/include/hip/hip_runtime.h" ]; then
    echo "  missing HIP headers under $rocm_root/include" >&2
    exit 1
fi

if [ ! -e "$rocm_root/lib/libamdhip64.so" ]; then
    echo "  missing HIP runtime library under $rocm_root/lib" >&2
    exit 1
fi

if [ ! -e "$rocm_root/lib/cmake/hip/hip-config.cmake" ]; then
    echo "  missing HIP CMake package under $rocm_root/lib/cmake" >&2
    exit 1
fi

echo "  rocm root: $rocm_root"

# Check hipcc (HIP compiler).
if command -v hipcc &>/dev/null; then
    hipcc --version >/dev/null
    echo "  hipcc: $(hipcc --version 2>&1 | grep -i 'HIP version' | head -1 || echo 'available')"
fi

# Runtime initialization must succeed. Executability alone missed crashes in
# ROCr initialization on otherwise well-formed SDK installations.
if ! command -v rocminfo &>/dev/null; then
    echo "  rocminfo: missing" >&2
    exit 1
fi
if ! rocminfo >/dev/null 2>&1; then
    echo "  rocminfo: runtime initialization failed" >&2
    exit 1
fi
echo "  rocminfo: available"

# Check hip-config if available.
if command -v hipconfig &>/dev/null; then
    echo "  hipconfig: $(hipconfig --version 2>/dev/null || echo 'available')"
fi

if command -v cmake &>/dev/null; then
    cmake_scratch_parent="${TMPDIR:-/tmp}"
    if [ ! -d "$cmake_scratch_parent" ]; then
        echo "  CMake scratch parent is not a directory: $cmake_scratch_parent" >&2
        exit 1
    fi
    cmake_scratch_parent="$(
        CDPATH=''
        cd -- "$cmake_scratch_parent" || exit 1
        pwd -P
    )"
    cmake_scratch_directory=$(
        mktemp -d "$cmake_scratch_parent/rocm-smoketest.XXXXXX"
    )
    cleanup_cmake_scratch() {
        case "$cmake_scratch_directory" in
            "$cmake_scratch_parent"/rocm-smoketest.*) ;;
            *)
                echo "  refusing unexpected CMake scratch cleanup path: $cmake_scratch_directory" >&2
                return 1
                ;;
        esac
        [ ! -e "$cmake_scratch_directory" ] ||
            find "$cmake_scratch_directory" -depth -delete
    }
    trap cleanup_cmake_scratch EXIT
    cat > "$cmake_scratch_directory/CMakeLists.txt" << 'EOF'
cmake_minimum_required(VERSION 3.16)
project(rocm_smoketest LANGUAGES CXX)
find_package(hip CONFIG REQUIRED)
EOF
    cmake \
        -S "$cmake_scratch_directory" \
        -B "$cmake_scratch_directory/build" \
        -DCMAKE_PREFIX_PATH="$rocm_root" \
        >/dev/null
    echo "  cmake: find_package(hip CONFIG) ok"
fi
