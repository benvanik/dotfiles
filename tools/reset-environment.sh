#!/bin/sh
# shellcheck shell=sh
# Reset repository-owned tool state at an ambient process boundary.
#
# Explicit project selections remain active while direnv owns the environment.
# Base shells and project launchers source this file before selecting their own
# defaults so a long-lived parent process cannot turn an old resolved root into
# an accidental version pin.

_dotfiles_strip_tool_path_entries() {
    _dotfiles_list_name="$1"
    _dotfiles_list_set=""
    _dotfiles_remaining=""
    _dotfiles_component=""
    _dotfiles_rebuilt=""
    _dotfiles_rebuilt_set=0
    _dotfiles_has_more=0
    _dotfiles_remove_component=0

    eval "_dotfiles_list_set=\${${_dotfiles_list_name}+x}"
    [ "$_dotfiles_list_set" = "x" ] || return 0
    eval "_dotfiles_remaining=\${${_dotfiles_list_name}-}"

    while :; do
        case "$_dotfiles_remaining" in
            *:*)
                _dotfiles_component="${_dotfiles_remaining%%:*}"
                _dotfiles_remaining="${_dotfiles_remaining#*:}"
                _dotfiles_has_more=1
                ;;
            *)
                _dotfiles_component="$_dotfiles_remaining"
                _dotfiles_has_more=0
                ;;
        esac

        _dotfiles_remove_component=0
        case "$_dotfiles_component" in
            "$HOME/tools"|"$HOME/tools/"*)
                _dotfiles_remove_component=1
                ;;
        esac
        if [ -n "$_dotfiles_virtual_environment_bin" ] &&
                [ "$_dotfiles_component" = \
                    "$_dotfiles_virtual_environment_bin" ]; then
            _dotfiles_remove_component=1
        fi
        if [ "$_dotfiles_remove_component" -eq 0 ]; then
            if [ "$_dotfiles_rebuilt_set" -eq 1 ]; then
                _dotfiles_rebuilt="$_dotfiles_rebuilt:$_dotfiles_component"
            else
                _dotfiles_rebuilt="$_dotfiles_component"
                _dotfiles_rebuilt_set=1
            fi
        fi

        [ "$_dotfiles_has_more" -eq 1 ] || break
    done

    export "$_dotfiles_list_name=$_dotfiles_rebuilt"
}

_dotfiles_mold_environment_active=0
if [ -n "${MOLD_ROOT:-}" ] ||
        [ -n "${DOTFILES_MOLD_PATH_ENTRY:-}" ]; then
    _dotfiles_mold_environment_active=1
fi

_dotfiles_virtual_environment_bin=""
if [ -n "${VIRTUAL_ENV:-}" ]; then
    _dotfiles_virtual_environment_bin="$VIRTUAL_ENV/bin"
fi

for _dotfiles_path_list in \
        PATH LD_LIBRARY_PATH CMAKE_PREFIX_PATH PKG_CONFIG_PATH; do
    _dotfiles_strip_tool_path_entries "$_dotfiles_path_list"
done

unset \
    BAZEL_ROOT \
    CC \
    CLANG_DIR \
    CMAKE_ROOT \
    CUDA_HOME \
    CUDA_PATH \
    CUDA_ROOT \
    CUDA_TOOLKIT_ROOT_DIR \
    CUDACXX \
    CUDAToolkit_ROOT \
    CXX \
    DOTFILES_BAZEL_PATH_ENTRY \
    DOTFILES_CMAKE_PATH_ENTRY \
    DOTFILES_CUDA_LIBRARY_ENTRY \
    DOTFILES_CUDA_PATH_ENTRY \
    DOTFILES_LLVM_LIBRARY_ENTRY \
    DOTFILES_LLVM_PATH_ENTRY \
    DOTFILES_MOLD_PATH_ENTRY \
    DOTFILES_NINJA_PATH_ENTRY \
    DOTFILES_ROCM_CMAKE_ENTRY \
    DOTFILES_ROCM_COMPILER_PATH_ENTRY \
    DOTFILES_ROCM_LIBRARY_ENTRY \
    DOTFILES_ROCM_RUNTIME_PATH_ENTRY \
    DOTFILES_ROCM_SYSDEPS_LIBRARY_ENTRY \
    DOTFILES_ROCM_VENV_PATH_ENTRY \
    DOTFILES_VULKAN_CMAKE_ENTRY \
    DOTFILES_VULKAN_LAYERS_LIBRARY_ENTRY \
    DOTFILES_VULKAN_LIBRARY_ENTRY \
    DOTFILES_VULKAN_PACKAGE_ENTRY \
    DOTFILES_VULKAN_PATH_ENTRY \
    HF_ROOT \
    HIP_CLANG_PATH \
    HIP_PATH \
    HIP_PLATFORM \
    LLVM_DIR \
    LLVM_ROOT \
    MLIR_DIR \
    MOLD_ROOT \
    NINJA_ROOT \
    ROCM_HOME \
    ROCM_PATH \
    ROCM_ROOT \
    ROCM_VENV_ROOT \
    VIRTUAL_ENV \
    VIRTUAL_ENV_PROMPT \
    VK_LAYER_PATH \
    VULKAN_ROOT \
    VULKAN_SDK

if [ "$_dotfiles_mold_environment_active" -eq 1 ]; then
    unset LDFLAGS
fi

unset -f _dotfiles_strip_tool_path_entries
unset \
    _dotfiles_component \
    _dotfiles_has_more \
    _dotfiles_list_name \
    _dotfiles_list_set \
    _dotfiles_mold_environment_active \
    _dotfiles_path_list \
    _dotfiles_rebuilt \
    _dotfiles_rebuilt_set \
    _dotfiles_remove_component \
    _dotfiles_virtual_environment_bin \
    _dotfiles_remaining
