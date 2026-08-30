"""Background execution-state caches kept outside the transaction hot path."""

# Public configuration guards intentionally raise direct actionable errors.
# ruff: noqa: PLR0913, S112, TC001, TRY003

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic, monotonic_ns

from execution.ports import BlockhashContext, BlockhashProvider


class BlockhashCacheRefresher:
    """Continuously retain a fresh, validity-aware blockhash."""

    def __init__(
        self,
        provider: BlockhashProvider,
        *,
        refresh_interval_seconds: float = 1.0,
        maximum_age_ms: int = 5_000,
    ) -> None:
        if refresh_interval_seconds <= 0 or maximum_age_ms <= 0:
            raise ValueError("blockhash cache timing must be positive")
        self.provider = provider
        self.refresh_interval_seconds = refresh_interval_seconds
        self.maximum_age_ms = maximum_age_ms
        self._value: BlockhashContext | None = None
        self._task: asyncio.Task[None] | None = None
        self._refresh_lock = asyncio.Lock()
        self.last_error: str | None = None

    async def refresh(self) -> BlockhashContext:
        async with self._refresh_lock:
            value = await self.provider.get_blockhash()
            self._value = value
            self.last_error = None
            return value

    def current(
        self,
        *,
        current_block_height: int | None = None,
        now_mono: float | None = None,
    ) -> BlockhashContext | None:
        value = self._value
        if value is None or not value.is_acceptable_age(self.maximum_age_ms, now_mono):
            return None
        if current_block_height is not None and value.is_expired(current_block_height):
            return None
        return value

    async def get_or_refresh(
        self, *, current_block_height: int | None = None
    ) -> BlockhashContext:
        value = self.current(current_block_height=current_block_height)
        return value if value is not None else await self.refresh()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            await self.refresh()
            self._task = asyncio.create_task(
                self._run(), name="hunter-blockhash-refresher"
            )

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.refresh_interval_seconds)
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - last good value may remain usable
                self.last_error = type(error).__name__

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


@dataclass(frozen=True, slots=True)
class TipSelection:
    """One bounded Jito tip selection and its estimation provenance."""

    selected_lamports: int
    estimated_lamports: int
    source: str
    strategy: str
    age_ms: float
    estimation_latency_ms: float


TipEstimator = Callable[[], Awaitable[int]]


class JitoTipCache:
    """Refresh a bounded Jito-tip estimate without adding hot-path I/O."""

    def __init__(
        self,
        estimator: TipEstimator,
        *,
        source: str,
        strategy: str,
        minimum_lamports: int,
        maximum_lamports: int,
        ttl_seconds: float = 5.0,
        refresh_interval_seconds: float = 2.0,
    ) -> None:
        if minimum_lamports < 0 or maximum_lamports < minimum_lamports:
            raise ValueError("Jito tip bounds are invalid")
        if ttl_seconds <= 0 or refresh_interval_seconds <= 0:
            raise ValueError("Jito tip cache timing must be positive")
        self.estimator = estimator
        self.source = source
        self.strategy = strategy
        self.minimum_lamports = minimum_lamports
        self.maximum_lamports = maximum_lamports
        self.ttl_seconds = ttl_seconds
        self.refresh_interval_seconds = refresh_interval_seconds
        self._selection: TipSelection | None = None
        self._observed_mono: float | None = None
        self._task: asyncio.Task[None] | None = None

    async def refresh(self) -> TipSelection:
        started = monotonic_ns()
        estimate = await self.estimator()
        if estimate < 0:
            raise ValueError("Jito tip estimate must be non-negative")
        selected = min(max(estimate, self.minimum_lamports), self.maximum_lamports)
        latency = (monotonic_ns() - started) / 1_000_000
        self._observed_mono = monotonic()
        self._selection = TipSelection(
            selected,
            estimate,
            self.source,
            self.strategy,
            0.0,
            latency,
        )
        return self._selection

    def selection(self) -> TipSelection | None:
        if self._selection is None or self._observed_mono is None:
            return None
        age_ms = (monotonic() - self._observed_mono) * 1_000
        if age_ms > self.ttl_seconds * 1_000:
            return None
        value = self._selection
        return TipSelection(
            value.selected_lamports,
            value.estimated_lamports,
            value.source,
            value.strategy,
            age_ms,
            value.estimation_latency_ms,
        )

    async def start(self) -> None:
        if self._task is None or self._task.done():
            await self.refresh()
            self._task = asyncio.create_task(
                self._run(), name="hunter-jito-tip-refresher"
            )

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.refresh_interval_seconds)
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - an expired estimate fails closed
                continue

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
