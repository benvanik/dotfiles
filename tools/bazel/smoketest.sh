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

# Check pinned ibazel.
ibazel_help=$(ibazel --help 2>&1)
ibazel_version=$(printf '%s\n' "$ibazel_help" | head -1)
[ "$ibazel_version" = "iBazel - Version v0.32.0" ]
echo "  ibazel: $ibazel_version"
