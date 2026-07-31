# LLVM/Clang environment.
# Sourced by tools.sh and direnvrc after LLVM_ROOT is selected.
if [ -n "${LLVM_ROOT:-}" ]; then
    if [ ! -x "$LLVM_ROOT/bin/clang" ] ||
       [ ! -x "$LLVM_ROOT/bin/clang++" ] ||
       [ ! -d "$LLVM_ROOT/lib/cmake/llvm" ] ||
       [ ! -d "$LLVM_ROOT/lib/cmake/clang" ] ||
       [ ! -d "$LLVM_ROOT/lib/cmake/mlir" ]; then
        printf 'LLVM root is incomplete: %s\n' "$LLVM_ROOT" >&2
        return 1
    fi

    _replace_managed_path_entry \
        PATH "$LLVM_ROOT/bin" DOTFILES_LLVM_PATH_ENTRY || return 1
    export CC="$LLVM_ROOT/bin/clang"
    export CXX="$LLVM_ROOT/bin/clang++"
    export LLVM_DIR="$LLVM_ROOT/lib/cmake/llvm"
    export CLANG_DIR="$LLVM_ROOT/lib/cmake/clang"
    export MLIR_DIR="$LLVM_ROOT/lib/cmake/mlir"
    _replace_managed_path_entry \
        LD_LIBRARY_PATH "$LLVM_ROOT/lib" DOTFILES_LLVM_LIBRARY_ENTRY ||
        return 1
fi
