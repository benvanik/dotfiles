#!/bin/bash
set -euo pipefail

HF_ROOT="${HF_ROOT:-$HOME/tools/hf/latest}"
[ -x "$HF_ROOT/bin/hf" ]
[ "$("$HF_ROOT/bin/python" -c \
    'import importlib.metadata; print(importlib.metadata.version("hf"))')" = \
    "1.24.0" ]
HF_HUB_DISABLE_UPDATE_CHECK=1 "$HF_ROOT/bin/hf" --help >/dev/null
