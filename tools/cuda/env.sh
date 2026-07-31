# CUDA SDK environment.
# Sourced by tools.sh and direnvrc after CUDA_ROOT is selected.
if [ -n "${CUDA_ROOT:-}" ]; then
    if [ ! -f "$CUDA_ROOT/include/cuda.h" ] ||
       [ ! -x "$CUDA_ROOT/bin/nvcc" ] ||
       [ ! -f "$CUDA_ROOT/nvvm/libdevice/libdevice.10.bc" ] ||
       [ ! -d "$CUDA_ROOT/lib64" ]; then
        printf 'CUDA root is incomplete: %s\n' "$CUDA_ROOT" >&2
        return 1
    fi

    export CUDA_HOME="$CUDA_ROOT"
    export CUDA_PATH="$CUDA_ROOT"
    export CUDAToolkit_ROOT="$CUDA_ROOT"
    export CUDA_TOOLKIT_ROOT_DIR="$CUDA_ROOT"
    export CUDACXX="$CUDA_ROOT/bin/nvcc"
    _replace_managed_path_entry \
        PATH "$CUDA_ROOT/bin" DOTFILES_CUDA_PATH_ENTRY || return 1
    _replace_managed_path_entry \
        LD_LIBRARY_PATH "$CUDA_ROOT/lib64" DOTFILES_CUDA_LIBRARY_ENTRY ||
        return 1
fi
