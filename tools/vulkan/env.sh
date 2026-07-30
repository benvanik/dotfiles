# Vulkan SDK environment.
# Sourced by tools.sh and direnvrc after VULKAN_ROOT is selected.
# shellcheck disable=SC2154
if [ -n "$VULKAN_ROOT" ] && [ -d "$VULKAN_ROOT/x86_64" ]; then
    export VULKAN_SDK="$VULKAN_ROOT/x86_64"
    export PATH="$VULKAN_SDK/bin:$PATH"
    export LD_LIBRARY_PATH="$VULKAN_SDK/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    export VK_LAYER_PATH="$VULKAN_SDK/share/vulkan/explicit_layer.d"
    export PKG_CONFIG_PATH="$VULKAN_SDK/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
    export CMAKE_PREFIX_PATH="$VULKAN_SDK${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}"
fi
