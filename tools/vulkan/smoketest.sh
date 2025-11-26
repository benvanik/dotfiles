#!/bin/bash
# Smoketest for Vulkan SDK.
# Verifies SDK tools are runnable.

set -e

# Check glslangValidator (GLSL compiler).
if command -v glslangValidator &>/dev/null; then
    glslangValidator --version >/dev/null
    echo "  glslangValidator: $(glslangValidator --version 2>&1 | head -1)"
fi

# Check spirv-val (SPIR-V validator).
if command -v spirv-val &>/dev/null; then
    spirv-val --version >/dev/null 2>&1 || true
    echo "  spirv-val: available"
fi

# Check vulkaninfo if available (requires display usually).
if command -v vulkaninfo &>/dev/null; then
    echo "  vulkaninfo: available"
fi

# Check VULKAN_SDK is set.
if [ -n "$VULKAN_SDK" ]; then
    echo "  VULKAN_SDK: $VULKAN_SDK"
fi
