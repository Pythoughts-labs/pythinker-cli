from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class RenderScheduler:
    request_invalidate: Callable[[], None]
    min_interval_seconds: float = 1 / 30
    _last_render_request: float | None = None

    def request_render(self, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        if (
            self._last_render_request is not None
            and current - self._last_render_request < self.min_interval_seconds
        ):
            return False
        self._last_render_request = current
        self.request_invalidate()
        return True
