# LLVM/Clang environment.
# Sourced by tools.sh and direnvrc after LLVM_ROOT is selected.
# shellcheck disable=SC2154
if [ -n "$LLVM_ROOT" ]; then
    export PATH="$LLVM_ROOT/bin:$PATH"
    export CC="$LLVM_ROOT/bin/clang"
    export CXX="$LLVM_ROOT/bin/clang++"
    export LLVM_DIR="$LLVM_ROOT/lib/cmake/llvm"
    export CLANG_DIR="$LLVM_ROOT/lib/cmake/clang"
    export MLIR_DIR="$LLVM_ROOT/lib/cmake/mlir"
    export LD_LIBRARY_PATH="$LLVM_ROOT/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
