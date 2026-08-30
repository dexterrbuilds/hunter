"""Asynchronous persistence for telemetry outside the submission hot path."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from execution.telemetry import ExecutionTelemetry, ProviderAttemptTelemetry

if TYPE_CHECKING:
    from execution.ports import SubmissionResult


class TelemetryStore(Protocol):
    """Minimal synchronous store called on a worker thread."""

    def save_telemetry(self, telemetry: ExecutionTelemetry, attempt: int) -> None: ...


@dataclass(frozen=True, slots=True)
class _TelemetryItem:
    telemetry: ExecutionTelemetry
    attempt: int


class AsyncTelemetrySink:
    """Capture cheaply in memory, then serialize SQLite writes on a worker."""

    def __init__(
        self, store: TelemetryStore, *, maximum_queue_size: int = 8_192
    ) -> None:
        if maximum_queue_size <= 0:
            raise ValueError("telemetry queue size must be positive")  # noqa: TRY003
        self.store = store
        self._queue: asyncio.Queue[_TelemetryItem | None] = asyncio.Queue(
            maxsize=maximum_queue_size
        )
        self._worker: asyncio.Task[None] | None = None
        self._failure: Exception | None = None
        self.dropped_records = 0

    @property
    def running(self) -> bool:
        return self._worker is not None and not self._worker.done()

    async def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._failure = None
            self._worker = asyncio.create_task(
                self._run(), name="hunter-telemetry-writer"
            )

    def record_nowait(self, telemetry: ExecutionTelemetry, attempt: int) -> bool:
        """Queue an immutable execution snapshot without disk I/O."""
        if self._worker is None or self._worker.done():
            raise RuntimeError("telemetry sink is not running")  # noqa: TRY003
        try:
            self._queue.put_nowait(_TelemetryItem(deepcopy(telemetry), attempt))
        except asyncio.QueueFull:
            self.dropped_records += 1
            return False
        return True

    async def record(self, telemetry: ExecutionTelemetry, attempt: int = 1) -> None:
        self.record_nowait(telemetry, attempt)

    async def flush(self) -> None:
        await self._queue.join()
        if self._failure is not None:
            raise RuntimeError(  # noqa: TRY003
                "telemetry persistence failed"
            ) from self._failure

    async def close(self) -> None:
        if self._worker is None:
            return
        await self._queue.put(None)
        await self._worker
        self._worker = None
        if self._failure is not None:
            raise RuntimeError(  # noqa: TRY003
                "telemetry persistence failed"
            ) from self._failure

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                await asyncio.to_thread(
                    self.store.save_telemetry,
                    item.telemetry,
                    item.attempt,
                )
            except Exception as error:  # noqa: BLE001 - surfaced by flush/close
                self._failure = error
            finally:
                self._queue.task_done()


def provider_attempt_from_result(result: SubmissionResult) -> ProviderAttemptTelemetry:
    """Convert a normalized provider response into durable telemetry."""
    return ProviderAttemptTelemetry(
        provider_id=result.provider_id,
        endpoint_id=result.endpoint_id,
        provider_region=result.provider_region,
        execution_variant=result.execution_variant,
        signature=result.signature,
        accepted=result.accepted,
        acknowledgement=result.acknowledgement,
        bytes_sent=result.bytes_sent,
        connection_reused=result.connection_reused,
        connection_session_generation=result.connection_session_generation,
        connection_session_created=result.connection_session_created,
        provider_reference=result.provider_reference,
        submit_started_mono_ns=result.submit_started_mono_ns,
        acknowledged_mono_ns=result.acknowledged_mono_ns,
        response_wall_time=result.response_wall_time,
        submitted_slot=result.submitted_slot,
        error_classification=result.error_classification,
        error_code=result.error_code,
        diagnostic=result.diagnostic,
    )
