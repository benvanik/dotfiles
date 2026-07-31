# Vulkan SDK environment.
# Sourced by tools.sh and direnvrc after VULKAN_ROOT is selected.
if [ -n "${VULKAN_ROOT:-}" ]; then
    if [ ! -x "$VULKAN_ROOT/x86_64/bin/glslangValidator" ] ||
       [ ! -x "$VULKAN_ROOT/x86_64/bin/spirv-val" ] ||
       [ ! -f "$VULKAN_ROOT/x86_64/include/vulkan/vulkan.h" ] ||
       [ ! -d "$VULKAN_ROOT/x86_64/lib" ] ||
       [ ! -d "$VULKAN_ROOT/x86_64/lib/pkgconfig" ] ||
       [ ! -d \
           "$VULKAN_ROOT/x86_64/share/vulkan/explicit_layer.d" ]; then
        printf 'Vulkan root is incomplete: %s\n' "$VULKAN_ROOT" >&2
        return 1
    fi

    export VULKAN_SDK="$VULKAN_ROOT/x86_64"
    _replace_managed_path_entry \
        PATH "$VULKAN_SDK/bin" DOTFILES_VULKAN_PATH_ENTRY || return 1
    _replace_managed_path_entry \
        LD_LIBRARY_PATH "$VULKAN_SDK/lib" DOTFILES_VULKAN_LIBRARY_ENTRY ||
        return 1
    export VK_LAYER_PATH="$VULKAN_SDK/share/vulkan/explicit_layer.d"
    _replace_managed_path_entry \
        PKG_CONFIG_PATH \
        "$VULKAN_SDK/lib/pkgconfig" \
        DOTFILES_VULKAN_PACKAGE_ENTRY || return 1
    _replace_managed_path_entry \
        CMAKE_PREFIX_PATH "$VULKAN_SDK" DOTFILES_VULKAN_CMAKE_ENTRY ||
        return 1
fi
