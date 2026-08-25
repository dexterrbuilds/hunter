"""Bounded ownership of independent persisted-position monitor tasks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

MonitorFactory = Callable[[], Awaitable[None]]
logger = logging.getLogger(__name__)


class MonitorRetry(Exception):
    """Request durable requeue by the bounded monitor worker."""

    def __init__(self, delay_seconds: float):
        self.delay_seconds = max(0.0, delay_seconds)


class PositionMonitorManager:
    """A fixed worker pool; held positions never block token detection."""

    def __init__(self, maximum_concurrency: int = 4):
        if maximum_concurrency < 1:
            raise ValueError("maximum_concurrency must be positive")
        self.maximum_concurrency = maximum_concurrency
        self._queue: asyncio.Queue[tuple[str, MonitorFactory]] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._keys: set[str] = set()

    async def start(self) -> None:
        if self._workers:
            return
        self._workers = [
            asyncio.create_task(self._worker(), name=f"position-monitor-{index}")
            for index in range(self.maximum_concurrency)
        ]

    async def submit(self, position_id: str, factory: MonitorFactory) -> bool:
        """Queue one monitor once; return False if already owned."""
        if position_id in self._keys:
            return False
        self._keys.add(position_id)
        await self._queue.put((position_id, factory))
        return True

    async def stop(self) -> None:
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._queue.task_done()
        self._keys.clear()

    async def join(self) -> None:
        await self._queue.join()

    async def _worker(self) -> None:
        while True:
            position_id, factory = await self._queue.get()
            try:
                await factory()
            except MonitorRetry as retry:
                await asyncio.sleep(retry.delay_seconds)
                await self._queue.put((position_id, factory))
                continue
            except Exception:  # noqa: BLE001
                logger.exception("Position monitor stopped after an unexpected error")
                self._keys.discard(position_id)
                continue
            finally:
                self._queue.task_done()
            self._keys.discard(position_id)
