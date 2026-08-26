"""Cached priority-fee estimates for latency-sensitive execution."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic, monotonic_ns

FeeEstimator = Callable[[list | None], Awaitable[int | None]]
logger = logging.getLogger(__name__)


class PriorityFeeMode(StrEnum):
    """Supported fee selection timing strategies."""

    DISABLED = "disabled"
    FIXED = "fixed"
    DYNAMIC = "dynamic"
    CACHED_DYNAMIC = "cached_dynamic"
    PERIODIC_DYNAMIC = "periodic_dynamic"
    PROVIDER_ESTIMATED = "provider_estimated"


@dataclass(frozen=True, slots=True)
class PriorityFeeSelection:
    """Selected CU price and the estimate provenance used to choose it."""

    selected_micro_lamports_per_cu: int | None
    estimated_micro_lamports_per_cu: int | None
    source: str
    estimate_age_ms: float | None
    estimation_latency_ms: float | None
    selected_at_mono: float


class PriorityFeeCache:
    """Refresh dynamic estimates outside the trade hot path when configured."""

    def __init__(
        self,
        estimator: FeeEstimator,
        *,
        source: str,
        ttl_seconds: float = 5.0,
        refresh_interval_seconds: float = 2.0,
    ) -> None:
        if ttl_seconds <= 0 or refresh_interval_seconds <= 0:
            raise ValueError("priority fee cache timing must be positive")  # noqa: TRY003
        self.estimator = estimator
        self.source = source
        self.ttl_seconds = ttl_seconds
        self.refresh_interval_seconds = refresh_interval_seconds
        self._value: int | None = None
        self._observed_mono: float | None = None
        self._latency_ms: float | None = None
        self._refresh_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    def selection(self) -> PriorityFeeSelection | None:
        if self._value is None or self._observed_mono is None:
            return None
        age_ms = (monotonic() - self._observed_mono) * 1_000
        if age_ms > self.ttl_seconds * 1_000:
            return None
        return PriorityFeeSelection(
            self._value,
            self._value,
            self.source,
            age_ms,
            self._latency_ms,
            monotonic(),
        )

    async def refresh(self, accounts: list | None = None) -> PriorityFeeSelection:
        async with self._refresh_lock:
            started = monotonic_ns()
            value = await self.estimator(accounts)
            latency_ms = (monotonic_ns() - started) / 1_000_000
            if value is None:
                raise RuntimeError(  # noqa: TRY003
                    "priority fee estimator returned no value"
                )
            if value < 0:
                raise ValueError(  # noqa: TRY003
                    "priority fee estimate must be non-negative"
                )
            self._value = value
            self._observed_mono = monotonic()
            self._latency_ms = latency_ms
            return PriorityFeeSelection(
                value,
                value,
                self.source,
                0.0,
                latency_ms,
                self._observed_mono,
            )

    async def get(
        self, accounts: list | None = None, *, allow_refresh: bool = True
    ) -> PriorityFeeSelection | None:
        current = self.selection()
        if current is not None or not allow_refresh:
            return current
        return await self.refresh(accounts)

    async def start_periodic(self, accounts: list | None = None) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(accounts), name=f"hunter-fee-refresh-{self.source}"
            )

    async def _run(self, accounts: list | None) -> None:
        while True:
            try:
                await self.refresh(accounts)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - background refresh remains fail-open
                logger.debug("Priority fee refresh failed", exc_info=True)
            await asyncio.sleep(self.refresh_interval_seconds)

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
