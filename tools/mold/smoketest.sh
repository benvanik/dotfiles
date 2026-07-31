#!/bin/bash
# Smoketest for an explicitly activated mold linker.
set -euo pipefail

mold --version >/dev/null
echo "  mold: $(mold --version | head -1)"
