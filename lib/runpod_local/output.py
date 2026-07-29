"""Stable JSON and concise human output."""

from __future__ import annotations

import json
import sys
from typing import Any


def print_json(value: Any) -> None:
    json.dump(value, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
