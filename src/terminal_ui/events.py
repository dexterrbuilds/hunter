"""Bounded non-blocking event capture for terminal presentation."""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from application.runtime_models import ApplicationEvent


class TerminalEventBuffer:
    """Retain only bounded recent events and account for overflow."""

    def __init__(self, maximum_pending: int, history_size: int) -> None:
        self._pending: deque[ApplicationEvent] = deque(maxlen=maximum_pending)
        self._history: deque[ApplicationEvent] = deque(maxlen=history_size)
        self.dropped = 0

    def publish_nowait(self, event: ApplicationEvent) -> None:
        if len(self._pending) == self._pending.maxlen:
            self.dropped += 1
        self._pending.append(event)

    def drain(self) -> tuple[ApplicationEvent, ...]:
        while self._pending:
            self._history.append(self._pending.popleft())
        return tuple(self._history)

    @property
    def recent(self) -> tuple[ApplicationEvent, ...]:
        return tuple(self._history)
