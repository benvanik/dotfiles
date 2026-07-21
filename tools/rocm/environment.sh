# ROCm environment.
# ROCM_ROOT is set by tools.sh or direnvrc before this file is sourced.

_rocm_environment_activate() {
    [ -n "${ROCM_ROOT:-}" ] || return 0

    local configured_root="$ROCM_ROOT"
    local sdk_root="$configured_root"
    local layout=""
    local hipconfig_executable=""
    local hip_platform="${HIP_PLATFORM:-}"

    if [ ! -d "$configured_root" ]; then
        printf 'ROCm root does not exist: %s\n' "$configured_root" >&2
        return 1
    fi

    # Installations made before the conventional SDK-root layout kept the
    # TheRock Python environment at the version root. Resolve its actual SDK
    # payload instead of exposing the virtualenv as ROCM_ROOT.
    if [ -f "$configured_root/pyvenv.cfg" ]; then
        if [ ! -x "$configured_root/bin/rocm-sdk" ]; then
            printf 'ROCm virtualenv is missing rocm-sdk: %s\n' "$configured_root" >&2
            return 1
        fi
        if ! sdk_root="$("$configured_root/bin/rocm-sdk" path --root)"; then
            printf 'Could not resolve the ROCm SDK payload: %s\n' "$configured_root" >&2
            return 1
        fi
    fi

    if [ -d "$sdk_root/lib/cmake" ]; then
        if [ ! -e "$sdk_root/include/hip/hip_runtime.h" ] ||
           [ ! -e "$sdk_root/lib/libamdhip64.so" ] ||
           [ ! -e "$sdk_root/lib/cmake/hip/hip-config.cmake" ]; then
            printf 'ROCm SDK surface is incomplete: %s\n' "$sdk_root" >&2
            return 1
        fi
        layout="installed"
        hipconfig_executable="$sdk_root/bin/hipconfig"
    elif [ -f "$sdk_root/build.ninja" ]; then
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
    # Export the platform they would otherwise query so find_package(hip) has
    # an explicit, validated backend selection.
    if [ -z "$hip_platform" ]; then
        if ! hip_platform="$("$hipconfig_executable" --platform)"; then
            printf 'Could not determine the ROCm HIP platform: %s\n' "$sdk_root" >&2
            return 1
        fi
    fi
    case "$hip_platform" in
        amd|nvidia) ;;
        *)
            printf 'Unexpected ROCm HIP platform %s: %s\n' "$hip_platform" "$sdk_root" >&2
            return 1
            ;;
    esac

    export ROCM_ROOT="$sdk_root"
    export ROCM_PATH="$sdk_root"
    export ROCM_HOME="$sdk_root"
    export HIP_PATH="$sdk_root"
    export HIP_PLATFORM="$hip_platform"
    export CMAKE_PREFIX_PATH="$sdk_root${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}"
    if [ "$layout" = "installed" ]; then
        export PATH="$sdk_root/bin:$PATH"
        export LD_LIBRARY_PATH="$sdk_root/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    else
        # TheRock build directories have not been installed into one SDK root.
        export PATH="$sdk_root/compiler/amd-llvm/bin:$sdk_root/core/hip-runtime/bin:$PATH"
    fi
}

if ! _rocm_environment_activate; then
    unset -f _rocm_environment_activate
    return 1
fi
unset -f _rocm_environment_activate
