# ROCm environment.
# Sourced by tools.sh and direnvrc after ROCM_ROOT is selected.

# Keep nested helper imports in direnv's watched closure. Interactive shells do
# not define the checked direnv importer and source the same repository helper
# directly.
_rocm_root_import_status=0
if [ "${_DIRENV_CHECKED_ENVIRONMENT_IMPORTS:-}" = "1" ]; then
    if ! command -v _source_checked_environment >/dev/null 2>&1; then
        printf '%s\n' \
            "ROCm environment is missing direnv's checked importer" >&2
        unset _rocm_root_import_status
        return 1
    fi
    _source_checked_environment "$HOME/.dotfiles/tools/rocm/root.sh"
    _rocm_root_import_status=$?
else
    # shellcheck source=root.sh
    . "$HOME/.dotfiles/tools/rocm/root.sh"
    _rocm_root_import_status=$?
fi
if [ "$_rocm_root_import_status" -ne 0 ]; then
    unset _rocm_root_import_status
    return 1
fi
unset _rocm_root_import_status

_rocm_environment_activate() {
    [ -n "${ROCM_ROOT:-}" ] || return 0

    local configured_root="$ROCM_ROOT"
    local sdk_root=""
    local layout=""
    local hipconfig_executable=""
    local hip_platform=""
    local retained_venv_root="${ROCM_VENV_ROOT:-}"

    # Activation exports the effective SDK as ROCM_ROOT for CMake consumers.
    # On a later shell startup, recover its packaging environment only when it
    # still resolves to that exact SDK. A stale venv must not override a newly
    # selected conventional root.
    if [ -n "$retained_venv_root" ] &&
       [ "$retained_venv_root" != "$configured_root" ] &&
       [ -f "$retained_venv_root/pyvenv.cfg" ] &&
       [ -x "$retained_venv_root/bin/rocm-sdk" ] &&
       rocm_resolve_selected_root "$retained_venv_root" 2>/dev/null &&
       [ "$ROCM_RESOLVED_SDK_ROOT" = "$configured_root" ]; then
        configured_root="$retained_venv_root"
    fi
    if ! rocm_resolve_selected_root "$configured_root"; then
        return 1
    fi
    sdk_root="$ROCM_RESOLVED_SDK_ROOT"
    if [ -n "$ROCM_RESOLVED_VENV_ROOT" ]; then
        export ROCM_VENV_ROOT="$ROCM_RESOLVED_VENV_ROOT"
    else
        unset ROCM_VENV_ROOT
    fi

    if [ -d "$sdk_root/lib/cmake" ]; then
        if [ ! -e "$sdk_root/include/hip/hip_runtime.h" ] ||
           [ ! -e "$sdk_root/lib/libamdhip64.so" ] ||
           [ ! -e "$sdk_root/lib/cmake/hip/hip-config.cmake" ] ||
           [ ! -d "$sdk_root/lib/llvm/bin" ] ||
           [ ! -d "$sdk_root/lib/rocm_sysdeps/lib" ]; then
            printf 'ROCm SDK surface is incomplete: %s\n' "$sdk_root" >&2
            return 1
        fi
        layout="installed"
        hipconfig_executable="$sdk_root/bin/hipconfig"
    elif [ -f "$sdk_root/build.ninja" ]; then
        if [ ! -d "$sdk_root/compiler/amd-llvm/bin" ] ||
           [ ! -d "$sdk_root/core/hip-runtime/bin" ]; then
            printf 'ROCm build surface is incomplete: %s\n' "$sdk_root" >&2
            return 1
        fi
        layout="build"
        hipconfig_executable="$sdk_root/core/hip-runtime/bin/hipconfig"
    else
        printf 'Unrecognized ROCm installation layout: %s\n' "$sdk_root" >&2
        return 1
    fi

    if [ ! -x "$hipconfig_executable" ]; then
        printf 'ROCm installation is missing hipconfig: %s\n' "$sdk_root" >&2
        return 1
    fi

    # Some TheRock hip-config.cmake packages do not encode the hipconfig path.
    # Query every selected root rather than carrying a backend from the prior
    # root across an in-process version switch. hipconfig also consults
    # ROCM_PATH while detecting its compiler backend; bind that input to the
    # selected SDK so an inherited environment cannot make this probe execute
    # tools from a superseded installation.
    if ! hip_platform="$(
        unset HIP_PLATFORM
        ROCM_PATH="$sdk_root" "$hipconfig_executable" --platform
    )"; then
        printf 'Could not determine the ROCm HIP platform: %s\n' \
            "$sdk_root" >&2
        return 1
    fi
    case "$hip_platform" in
        amd|nvidia) ;;
        *)
            printf 'Unexpected ROCm HIP platform %s: %s\n' \
                "$hip_platform" "$sdk_root" >&2
            return 1
            ;;
    esac

    export ROCM_ROOT="$sdk_root"
    export ROCM_PATH="$sdk_root"
    export ROCM_HOME="$sdk_root"
    export HIP_PATH="$sdk_root"
    export HIP_PLATFORM="$hip_platform"
    _replace_managed_path_entry \
        CMAKE_PREFIX_PATH "$sdk_root" DOTFILES_ROCM_CMAKE_ENTRY ||
        return 1

    if [ "$layout" = "installed" ]; then
        export HIP_CLANG_PATH="$sdk_root/lib/llvm/bin"
        _replace_managed_path_entry \
            PATH "$sdk_root/bin" DOTFILES_ROCM_RUNTIME_PATH_ENTRY ||
            return 1
        _clear_managed_path_entry \
            PATH DOTFILES_ROCM_COMPILER_PATH_ENTRY || return 1
        _replace_managed_path_entry \
            LD_LIBRARY_PATH \
            "$sdk_root/lib/rocm_sysdeps/lib" \
            DOTFILES_ROCM_SYSDEPS_LIBRARY_ENTRY || return 1
        _replace_managed_path_entry \
            LD_LIBRARY_PATH \
            "$sdk_root/lib" \
            DOTFILES_ROCM_LIBRARY_ENTRY || return 1
    else
        export HIP_CLANG_PATH="$sdk_root/compiler/amd-llvm/bin"
        _replace_managed_path_entry \
            PATH \
            "$sdk_root/core/hip-runtime/bin" \
            DOTFILES_ROCM_RUNTIME_PATH_ENTRY || return 1
        _replace_managed_path_entry \
            PATH "$HIP_CLANG_PATH" DOTFILES_ROCM_COMPILER_PATH_ENTRY ||
            return 1
        _clear_managed_path_entry \
            LD_LIBRARY_PATH DOTFILES_ROCM_LIBRARY_ENTRY || return 1
        _clear_managed_path_entry \
            LD_LIBRARY_PATH DOTFILES_ROCM_SYSDEPS_LIBRARY_ENTRY ||
            return 1
    fi

    if [ -n "${ROCM_VENV_ROOT:-}" ]; then
        _replace_managed_path_entry \
            PATH "$ROCM_VENV_ROOT/bin" DOTFILES_ROCM_VENV_PATH_ENTRY ||
            return 1
    else
        _clear_managed_path_entry \
            PATH DOTFILES_ROCM_VENV_PATH_ENTRY || return 1
    fi
}

if ! _rocm_environment_activate; then
    unset -f _rocm_environment_activate
    unset -f rocm_resolve_selected_root
    unset ROCM_RESOLVED_SDK_ROOT ROCM_RESOLVED_VENV_ROOT
    return 1
fi
unset -f _rocm_environment_activate
unset -f rocm_resolve_selected_root
unset ROCM_RESOLVED_SDK_ROOT ROCM_RESOLVED_VENV_ROOT
