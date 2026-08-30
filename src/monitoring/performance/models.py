"""Cross-feed identities, observations, claims, and timing records."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from time import monotonic_ns
from typing import TYPE_CHECKING

from execution.telemetry import utc_now
from monitoring.performance.fast_path import FastPathConfidence

if TYPE_CHECKING:
    from datetime import datetime

    from interfaces.core import TokenInfo


class ClaimState(StrEnum):
    """Single-node logical creation claim lifecycle."""

    UNSEEN = "unseen"
    OBSERVED = "observed"
    CLAIMED = "claimed"
    TRADE_REQUEST_CREATED = "trade_request_created"


@dataclass(frozen=True, slots=True)
class DetectionIdentity:
    """Strongest known identity for one Pump.fun token creation."""

    mint: str
    creation_signature: str | None = None
    launch_slot: int | None = None

    @property
    def claim_key(self) -> str:
        """Mint is the stable economic identity even when early feeds lack a sig."""
        return self.mint


@dataclass(slots=True)
class DetectionObservation:
    """One source's view of a launch, timestamped at network ingress."""

    identity: DetectionIdentity
    token_info: TokenInfo
    source: str
    region: str | None = None
    received_at: datetime = field(default_factory=utc_now)
    received_mono_ns: int = field(default_factory=monotonic_ns)
    slot: int | None = None
    transaction_signature: str | None = None
    parser_completed_mono_ns: int | None = None
    validation_completed_mono_ns: int | None = None
    confidence: FastPathConfidence = FastPathConfidence.REQUIRES_REFRESH

    @property
    def parser_latency_ms(self) -> float | None:
        if self.parser_completed_mono_ns is None:
            return None
        return (self.parser_completed_mono_ns - self.received_mono_ns) / 1_000_000


@dataclass(slots=True)
class CorrelatedLaunch:
    """All observations and the single execution claim for one mint."""

    identity: DetectionIdentity
    observations: list[DetectionObservation] = field(default_factory=list)
    state: ClaimState = ClaimState.UNSEEN
    claimed_source: str | None = None
    claimed_mono_ns: int | None = None
    last_seen_mono_ns: int = field(default_factory=monotonic_ns)

    def relative_arrival_ms(self) -> dict[str, float]:
        """Return each source relative to the earliest observation."""
        if not self.observations:
            return {}
        first = min(item.received_mono_ns for item in self.observations)
        return {
            item.source: (item.received_mono_ns - first) / 1_000_000
            for item in self.observations
        }
