"""Small owner-level command limiter; emergency kill remains exempt."""

from __future__ import annotations

from collections import defaultdict, deque
from time import monotonic


class OwnerRateLimiter:
    def __init__(self, maximum: int, interval_seconds: float) -> None:
        self.maximum = maximum
        self.interval_seconds = interval_seconds
        self._events: dict[int, deque[float]] = defaultdict(deque)

    def allow(self, owner_user_id: int, *, exempt: bool = False) -> bool:
        if exempt:
            return True
        now = monotonic()
        events = self._events[owner_user_id]
        cutoff = now - self.interval_seconds
        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) >= self.maximum:
            return False
        events.append(now)
        return True
