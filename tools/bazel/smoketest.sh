#!/bin/bash
# Smoketest for Bazel tools installation.
set -e

# Check bazel (bazelisk).
bazel --version >/dev/null 2>&1
echo "  bazel: $(bazel --version | head -1)"

# Check buildifier.
buildifier --version >/dev/null 2>&1
echo "  buildifier: $(buildifier --version 2>&1 | head -1)"

# Check buildozer.
buildozer -version >/dev/null 2>&1
echo "  buildozer: $(buildozer -version 2>&1 | head -1)"
