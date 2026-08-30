"""Bounded earliest-valid-event aggregation with exactly-once mint claiming."""

# Constructor validation intentionally returns an actionable config error.
# ruff: noqa: TRY003

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic_ns

from execution.detection import record_detection
from interfaces.core import TokenInfo
from monitoring.performance.fast_path import FastPathConfidence
from monitoring.performance.models import (
    ClaimState,
    CorrelatedLaunch,
    DetectionObservation,
)

ObservationCallback = Callable[[DetectionObservation], Awaitable[None] | None]
TokenCallback = Callable[[TokenInfo], Awaitable[None]]


class EarliestEventAggregator:
    """Correlate feeds by mint and emit one trade decision from the first valid event."""

    def __init__(
        self,
        callback: TokenCallback,
        *,
        queue_size: int = 4_096,
        claim_ttl_seconds: float = 600.0,
        observation_callback: ObservationCallback | None = None,
    ) -> None:
        if queue_size <= 0 or claim_ttl_seconds <= 0:
            raise ValueError("aggregator bounds must be positive")
        self.callback = callback
        self.claim_ttl_ns = int(claim_ttl_seconds * 1_000_000_000)
        self.observation_callback = observation_callback
        self.queue: asyncio.Queue[DetectionObservation | None] = asyncio.Queue(
            maxsize=queue_size
        )
        self.launches: dict[str, CorrelatedLaunch] = {}
        self.dropped_observations = 0
        self._worker: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._run(), name="hunter-earliest-event-aggregator"
            )

    def submit_nowait(self, observation: DetectionObservation) -> bool:
        """Timestamped feed loops never block behind parser/trading work."""
        try:
            self.queue.put_nowait(observation)
        except asyncio.QueueFull:
            self.dropped_observations += 1
            return False
        return True

    async def submit(self, observation: DetectionObservation) -> bool:
        return self.submit_nowait(observation)

    async def _run(self) -> None:
        while True:
            observation = await self.queue.get()
            try:
                if observation is None:
                    return
                await self._process(observation)
            finally:
                self.queue.task_done()

    async def _process(self, observation: DetectionObservation) -> None:
        callback_token = None
        async with self._lock:
            self._expire_old(observation.received_mono_ns)
            key = observation.identity.claim_key
            launch = self.launches.get(key)
            if launch is None:
                launch = CorrelatedLaunch(observation.identity)
                self.launches[key] = launch
            elif (
                launch.identity.creation_signature is None
                and observation.identity.creation_signature is not None
            ):
                launch.identity = observation.identity
            launch.observations.append(observation)
            launch.last_seen_mono_ns = observation.received_mono_ns
            if launch.state == ClaimState.UNSEEN:
                launch.state = ClaimState.OBSERVED
            valid = observation.confidence != FastPathConfidence.UNSUPPORTED
            if valid and launch.state == ClaimState.OBSERVED:
                correlated_mono_ns = monotonic_ns()
                launch.state = ClaimState.CLAIMED
                launch.claimed_source = observation.source
                launch.claimed_mono_ns = correlated_mono_ns
                callback_token = observation.token_info
                record_detection(
                    callback_token,
                    source=observation.source,
                    source_region=observation.region,
                    event_slot=observation.slot,
                    transaction_slot=observation.slot,
                    launch_slot=observation.identity.launch_slot,
                    transaction_signature=observation.transaction_signature,
                    observed_at=observation.received_at,
                    observed_mono_ns=observation.received_mono_ns,
                    parser_completed_mono_ns=observation.parser_completed_mono_ns,
                    validation_completed_mono_ns=(
                        observation.validation_completed_mono_ns
                    ),
                    correlation_completed_mono_ns=correlated_mono_ns,
                    claim_completed_mono_ns=launch.claimed_mono_ns,
                )
                launch.state = ClaimState.TRADE_REQUEST_CREATED
        if self.observation_callback is not None:
            returned = self.observation_callback(observation)
            if returned is not None:
                await returned
        if callback_token is not None:
            await self.callback(callback_token)

    def _expire_old(self, now_ns: int) -> None:
        expired = [
            key
            for key, launch in self.launches.items()
            if now_ns - launch.last_seen_mono_ns > self.claim_ttl_ns
        ]
        for key in expired:
            del self.launches[key]

    async def flush(self) -> None:
        await self.queue.join()

    async def close(self) -> None:
        if self._worker is None:
            return
        await self.queue.put(None)
        await self._worker
        self._worker = None
