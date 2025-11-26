#!/bin/bash
# Smoketest for Ninja build system.

set -e

ninja --version >/dev/null
echo "  ninja: $(ninja --version)"
