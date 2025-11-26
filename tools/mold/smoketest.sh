#!/bin/bash
# Smoketest for Mold linker.

set -e

mold --version >/dev/null
echo "  mold: $(mold --version | head -1)"
