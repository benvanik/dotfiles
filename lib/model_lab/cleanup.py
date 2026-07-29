"""One lazily started absolute budget for a cleanup transaction."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable


class CleanupBudget:
    """Mint an absolute cleanup deadline exactly once, on first use."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise ValueError(
                "cleanup timeout must be positive and finite"
            )
        self.timeout_seconds = float(timeout_seconds)
        self.monotonic = monotonic
        self._deadline: float | None = None
        self._lock = threading.Lock()

    def deadline(self) -> float:
        """Return the one deadline, starting the budget if necessary."""

        with self._lock:
            if self._deadline is None:
                self._deadline = self.monotonic() + self.timeout_seconds
            return self._deadline

    @property
    def started_deadline(self) -> float | None:
        """Expose whether cleanup has started without minting a deadline."""

        with self._lock:
            return self._deadline
