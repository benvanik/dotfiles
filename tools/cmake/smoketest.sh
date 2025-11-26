#!/bin/bash
# Smoketest for CMake.

set -e

cmake --version >/dev/null
echo "  cmake: $(cmake --version | head -1)"
