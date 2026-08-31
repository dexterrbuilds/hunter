"""Bounded prioritized Telegram notifications isolated from trading paths."""

# Protocol/model imports make this queue's application boundary explicit.
# ruff: noqa: TC001

from __future__ import annotations

import asyncio
import heapq
from collections import OrderedDict
from itertools import count
from threading import RLock

from telegram_control.models import TelegramMetrics, TelegramNotification
from telegram_control.ports import TelegramTransport


class TelegramNotificationQueue:
    """Accept notifications synchronously and send them on a separate worker."""

    def __init__(
        self,
        transport: TelegramTransport,
        owner_chat_ids: tuple[int, ...],
        metrics: TelegramMetrics,
        *,
        maximum_pending: int,
        maximum_dedupe_keys: int = 2048,
    ) -> None:
        self.transport = transport
        self.owner_chat_ids = owner_chat_ids
        self.metrics = metrics
        self.maximum_pending = maximum_pending
        self.maximum_dedupe_keys = maximum_dedupe_keys
        self._items: list[tuple[int, int, TelegramNotification]] = []
        self._sequence = count()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lock = RLock()
        self._available = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._accepting = True
        self._in_flight = 0

    async def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._accepting = True
            self._worker = asyncio.create_task(
                self._run(), name="hunter-telegram-notifications"
            )

    def publish_nowait(self, notification: TelegramNotification) -> bool:
        """Never perform Telegram I/O or wait for queue capacity."""
        with self._lock:
            if not self._accepting or self._is_duplicate(notification.event_id):
                return False
            item = (int(notification.priority), next(self._sequence), notification)
            if len(self._items) >= self.maximum_pending:
                worst_index = max(
                    range(len(self._items)), key=lambda index: self._items[index][:2]
                )
                worst = self._items[worst_index]
                if item[:2] >= worst[:2]:
                    self.metrics.notifications_dropped += 1
                    return False
                self._items[worst_index] = self._items[-1]
                self._items.pop()
                heapq.heapify(self._items)
                self.metrics.notifications_dropped += 1
            heapq.heappush(self._items, item)
            self.metrics.notifications_queued += 1
            self._remember(notification.event_id)
            self._available.set()
            return True

    def stop_accepting(self) -> None:
        self._accepting = False

    async def close(self, *, flush_timeout_seconds: float) -> None:
        self.stop_accepting()
        worker = self._worker
        if worker is None:
            return
        try:
            await asyncio.wait_for(self._wait_empty(), timeout=flush_timeout_seconds)
        except TimeoutError:
            pass
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        self._worker = None

    async def _wait_empty(self) -> None:
        while True:
            with self._lock:
                if not self._items and self._in_flight == 0:
                    return
            await asyncio.sleep(0)

    async def _run(self) -> None:
        while True:
            await self._available.wait()
            while True:
                with self._lock:
                    if not self._items:
                        self._available.clear()
                        break
                    _priority, _sequence, notification = heapq.heappop(self._items)
                    self._in_flight += 1
                try:
                    for chat_id in self.owner_chat_ids:
                        try:
                            await self.transport.send_message(
                                chat_id, notification.text
                            )
                        except Exception:  # noqa: BLE001
                            self.metrics.telegram_errors += 1
                        else:
                            self.metrics.notifications_sent += 1
                finally:
                    with self._lock:
                        self._in_flight -= 1

    def _is_duplicate(self, event_id: str | None) -> bool:
        return event_id is not None and event_id in self._seen

    def _remember(self, event_id: str | None) -> None:
        if event_id is None:
            return
        self._seen[event_id] = None
        self._seen.move_to_end(event_id)
        while len(self._seen) > self.maximum_dedupe_keys:
            self._seen.popitem(last=False)
