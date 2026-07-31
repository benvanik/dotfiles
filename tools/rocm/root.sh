# ROCm selected-root resolution shared by shell activation and generated tools.

# Resolves a configured ROCm version root into its effective SDK root.
#
# Older TheRock packages used a Python virtual environment as the selected
# version directory. Those environments expose the assembled SDK through
# `rocm-sdk path --root`. Conventional and source-build roots are already SDK
# roots and remain unchanged.
#
# On success, sets:
#   ROCM_RESOLVED_SDK_ROOT   Effective SDK root.
#   ROCM_RESOLVED_VENV_ROOT Packaging virtualenv, or empty for direct roots.
# These output globals are consumed by the sourcing caller.
# shellcheck disable=SC2034
rocm_resolve_selected_root() {
    local configured_root="$1"
    local sdk_root="$configured_root"

    ROCM_RESOLVED_SDK_ROOT=""
    ROCM_RESOLVED_VENV_ROOT=""

    if [ ! -d "$configured_root" ]; then
        printf 'ROCm root does not exist: %s\n' "$configured_root" >&2
        return 1
    fi

    if [ -f "$configured_root/pyvenv.cfg" ]; then
        if [ ! -x "$configured_root/bin/rocm-sdk" ]; then
            printf 'ROCm virtualenv is missing rocm-sdk: %s\n' \
                "$configured_root" >&2
            return 1
        fi
        if ! sdk_root="$("$configured_root/bin/rocm-sdk" path --root)"; then
            printf 'Could not resolve the ROCm SDK payload: %s\n' \
                "$configured_root" >&2
            return 1
        fi
        if [ ! -d "$sdk_root" ]; then
            printf 'Resolved ROCm SDK root does not exist: %s\n' "$sdk_root" >&2
            return 1
        fi
        ROCM_RESOLVED_VENV_ROOT="$configured_root"
    fi

    ROCM_RESOLVED_SDK_ROOT="$sdk_root"
}
