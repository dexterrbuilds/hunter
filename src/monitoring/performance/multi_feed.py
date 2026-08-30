"""BaseTokenListener adapter that races independent creation feeds safely."""

# Listener construction reports a direct operator-facing configuration error.
# ruff: noqa: TRY003

from __future__ import annotations

import asyncio
from time import monotonic_ns
from typing import TYPE_CHECKING

from execution.detection import detection_for
from execution.telemetry import utc_now
from monitoring.base_listener import BaseTokenListener
from monitoring.performance.aggregator import EarliestEventAggregator
from monitoring.performance.fast_path import assess_fast_path
from monitoring.performance.models import DetectionIdentity, DetectionObservation

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from interfaces.core import TokenInfo


class MultiFeedListener(BaseTokenListener):
    """Run bounded feed tasks while one aggregator owns the trade callback."""

    def __init__(
        self,
        listeners: Sequence[BaseTokenListener],
        *,
        queue_size: int = 4_096,
        claim_ttl_seconds: float = 600.0,
    ) -> None:
        super().__init__()
        if not listeners:
            raise ValueError("multi-feed listener requires at least one source")
        self.listeners = tuple(listeners)
        self.queue_size = queue_size
        self.claim_ttl_seconds = claim_ttl_seconds
        self.aggregator: EarliestEventAggregator | None = None

    async def listen_for_tokens(
        self,
        token_callback: Callable[[TokenInfo], Awaitable[None]],
        match_string: str | None = None,
        creator_address: str | None = None,
    ) -> None:
        self.aggregator = EarliestEventAggregator(
            token_callback,
            queue_size=self.queue_size,
            claim_ttl_seconds=self.claim_ttl_seconds,
        )
        await self.aggregator.start()

        async def observed(token_info: TokenInfo) -> None:
            telemetry = detection_for(token_info)
            assessment = assess_fast_path(token_info)
            observed_at = telemetry.event_observed_at if telemetry else utc_now()
            observed_mono_ns = (
                telemetry.event_observed_mono_ns if telemetry else monotonic_ns()
            )
            observation = DetectionObservation(
                identity=DetectionIdentity(
                    mint=str(token_info.mint),
                    creation_signature=(
                        telemetry.transaction_signature if telemetry else None
                    ),
                    launch_slot=telemetry.launch_slot if telemetry else None,
                ),
                token_info=token_info,
                source=telemetry.source if telemetry else "unknown",
                region=telemetry.source_region if telemetry else None,
                received_at=observed_at,
                received_mono_ns=observed_mono_ns,
                slot=telemetry.event_slot if telemetry else None,
                transaction_signature=(
                    telemetry.transaction_signature if telemetry else None
                ),
                parser_completed_mono_ns=(
                    telemetry.parser_completed_mono_ns if telemetry else None
                ),
                validation_completed_mono_ns=(
                    telemetry.validation_completed_mono_ns if telemetry else None
                ),
                confidence=assessment.confidence,
            )
            self.aggregator.submit_nowait(observation)

        tasks = [
            asyncio.create_task(
                listener.listen_for_tokens(
                    observed,
                    match_string=match_string,
                    creator_address=creator_address,
                ),
                name=f"hunter-feed-{index}",
            )
            for index, listener in enumerate(self.listeners)
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.aggregator.close()
