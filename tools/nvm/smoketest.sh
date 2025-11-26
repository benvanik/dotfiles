#!/bin/bash
# Smoketest for nvm and Node.js.
# Note: nvm is loaded via shell config, not ~/tools/, so this just checks node/npm.

set -e

# Check node.
node --version >/dev/null
echo "  node: $(node --version)"

# Check npm.
npm --version >/dev/null
echo "  npm: v$(npm --version)"
