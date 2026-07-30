# ROCm environment.
# Sourced by tools.sh and direnvrc after ROCM_ROOT is selected.
# shellcheck disable=SC2154
if [ -n "$ROCM_ROOT" ]; then
    # TheRock Python packages use a venv as the tool installation root and
    # expose the assembled SDK prefix through rocm-sdk. Source-built TheRock
    # trees are already flat prefixes and do not have this indirection.
    _rocm_tool_root="$ROCM_ROOT"
    if [ -x "$_rocm_tool_root/bin/rocm-sdk" ]; then
        export ROCM_VENV_ROOT="$_rocm_tool_root"
        ROCM_ROOT="$("$_rocm_tool_root/bin/rocm-sdk" path --root)" || return 1
        export ROCM_ROOT
    else
        unset ROCM_VENV_ROOT
    fi

    export ROCM_HOME="$ROCM_ROOT"
    export HIP_PATH="$ROCM_ROOT"
    export HIP_CLANG_PATH="$ROCM_ROOT/lib/llvm/bin"
    export PATH="$_rocm_tool_root/bin:$ROCM_ROOT/bin:$PATH"
    export CMAKE_PREFIX_PATH="$ROCM_ROOT${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}"
    export LD_LIBRARY_PATH="$ROCM_ROOT/lib:$ROCM_ROOT/lib/rocm_sysdeps/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    unset _rocm_tool_root
fi
