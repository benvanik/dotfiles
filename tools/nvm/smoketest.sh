#!/bin/bash
# Smoketest for nvm and Node.js.
# Establish the managed NVM default explicitly so the result does not depend on
# whether the invoking process sourced the interactive shell configuration.

set -e

export NVM_DIR="$HOME/.nvm"
if [ ! -f "$NVM_DIR/nvm.sh" ]; then
    echo "nvm smoketest: managed runtime is missing: $NVM_DIR/nvm.sh" >&2
    exit 1
fi
# shellcheck source=/dev/null
. "$NVM_DIR/nvm.sh"
nvm use --silent default

# Check node.
node --version >/dev/null
echo "  node: $(node --version)"

# Check npm.
npm --version >/dev/null
echo "  npm: v$(npm --version)"
