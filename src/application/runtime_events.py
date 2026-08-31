"""Bounded, asynchronous application events for future interface adapters."""

# Public guards intentionally return direct configuration errors.
# ruff: noqa: TRY003

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from application.runtime_models import ApplicationEvent

EventConsumer = Callable[[ApplicationEvent], Awaitable[None]]


class ApplicationEventBus:
    """Non-blocking bounded event dispatcher with observable loss."""

    def __init__(self, maximum_pending: int = 512) -> None:
        if maximum_pending < 1:
            raise ValueError("maximum_pending must be positive")
        self._queue: asyncio.Queue[ApplicationEvent | None] = asyncio.Queue(
            maxsize=maximum_pending
        )
        self._consumers: list[EventConsumer] = []
        self._worker: asyncio.Task[None] | None = None
        self.dropped_events = 0
        self.consumer_failures = 0

    def subscribe(self, consumer: EventConsumer) -> None:
        """Register an in-process consumer before or after start."""
        self._consumers.append(consumer)

    async def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._run(), name="hunter-application-events"
            )

    def publish_nowait(self, event: ApplicationEvent) -> bool:
        """Capture an event without delaying transaction execution."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped_events += 1
            return False
        return True

    async def close(self, *, flush: bool = True) -> None:
        worker = self._worker
        if worker is None:
            return
        if flush:
            await self._queue.join()
        await self._queue.put(None)
        await worker
        self._worker = None

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                if event is None:
                    return
                for consumer in tuple(self._consumers):
                    try:
                        await consumer(event)
                    except Exception:  # noqa: BLE001
                        self.consumer_failures += 1
            finally:
                self._queue.task_done()
