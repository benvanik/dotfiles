#!/bin/bash
# Smoketest for LLVM toolchain.
# Verifies clang and core LLVM tools are runnable.

set -e

# Check clang.
clang --version >/dev/null
echo "  clang: $(clang --version | head -1)"

# Check clang++.
clang++ --version >/dev/null

# Check llvm-config.
if command -v llvm-config &>/dev/null; then
    echo "  llvm-config: $(llvm-config --version)"
fi

# Check mlir-opt if available.
if command -v mlir-opt &>/dev/null; then
    mlir-opt --version >/dev/null
    echo "  mlir-opt: available"
fi
