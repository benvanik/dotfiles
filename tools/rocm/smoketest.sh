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

# Check rocminfo if available.
if command -v rocminfo &>/dev/null; then
    # Just check it runs, don't need full output.
    rocminfo >/dev/null 2>&1 || true
    echo "  rocminfo: available"
fi

# Check hip-config if available.
if command -v hipconfig &>/dev/null; then
    echo "  hipconfig: $(hipconfig --version 2>/dev/null || echo 'available')"
fi

if command -v cmake &>/dev/null; then
    cmake_tmp="${TMPDIR:-/tmp}/rocm-smoketest.$$"
    mkdir -p "$cmake_tmp"
    trap 'rm -rf "$cmake_tmp"' EXIT
    cat > "$cmake_tmp/CMakeLists.txt" << 'EOF'
cmake_minimum_required(VERSION 3.16)
project(rocm_smoketest LANGUAGES CXX)
find_package(hip CONFIG REQUIRED)
EOF
    cmake -S "$cmake_tmp" -B "$cmake_tmp/build" -DCMAKE_PREFIX_PATH="$rocm_root" >/dev/null
    echo "  cmake: find_package(hip CONFIG) ok"
fi
