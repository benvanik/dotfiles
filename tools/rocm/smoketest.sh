#!/bin/bash
# Smoketest for ROCm.
# Verifies HIP compiler and runtime tools are runnable.

set -e

# Check hipcc (HIP compiler).
if command -v hipcc &>/dev/null; then
    hipcc --version >/dev/null
    echo "  hipcc: $(hipcc --version 2>&1 | grep -i 'HIP version' | head -1 || echo 'available')"
fi

# Check rocminfo if available.
if command -v rocminfo &>/dev/null; then
    # Just check it runs, don't need full output.
    rocminfo >/dev/null 2>&1 || true
    echo "  rocminfo: available"
fi

# Check hip-config if available.
if command -v hipconfig &>/dev/null; then
    echo "  hipconfig: $(hipconfig --version 2>/dev/null || echo 'available')"
fi
