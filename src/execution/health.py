"""Rolling, non-sticky execution-provider health observations."""

# Landing observations retain explicit provider and endpoint dimensions.
# ruff: noqa: PLR0913, TRY003

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from statistics import median
from time import monotonic
from typing import TYPE_CHECKING

from execution.errors import ErrorClassification

if TYPE_CHECKING:
    from execution.ports import SubmissionResult

MINIMUM_TREND_SAMPLES = 20


@dataclass(frozen=True, slots=True)
class ProviderHealthSnapshot:
    """Recent evidence for one endpoint; not a universal provider ranking."""

    provider_id: str
    endpoint_id: str
    sample_count: int
    successful_acknowledgements: int
    submission_success_rate: float | None
    median_submission_rtt_ms: float | None
    status_query_median_rtt_ms: float | None
    transport_errors: int
    rate_limits: int
    accepted_not_landed: int
    landed_sample_count: int
    median_slots_to_land: float | None
    last_observed_mono: float | None

    @property
    def enough_samples(self) -> bool:
        """Require a non-trivial window before using a sample as a trend."""
        return self.sample_count >= MINIMUM_TREND_SAMPLES


@dataclass(slots=True)
class _HealthSample:
    signature: str
    accepted: bool
    submission_rtt_ms: float | None
    classification: ErrorClassification | None
    observed_mono: float
    status_query_rtt_ms: float | None = None
    landed: bool | None = None
    slots_to_land: int | None = None


@dataclass(slots=True)
class ProviderHealthTracker:
    """Bounded rolling samples suitable for lightweight routing hints."""

    provider_id: str
    endpoint_id: str
    window_size: int = 100
    _samples: deque[_HealthSample] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.window_size <= 0:
            raise ValueError("health window must be positive")
        self._samples = deque(maxlen=self.window_size)

    def record_submission(self, result: SubmissionResult) -> None:
        """Record a transport acknowledgement or normalized failure."""
        self._samples.append(
            _HealthSample(
                result.signature,
                result.acceptable_acknowledgement,
                result.submit_rtt_ms,
                result.error_classification,
                monotonic(),
            )
        )

    def record_landing(
        self,
        signature: str,
        *,
        landed: bool | None,
        slots_to_land: int | None,
        status_query_rtt_ms: float | None = None,
    ) -> None:
        """Attach later confirmation evidence to the newest accepted sample."""
        for sample in reversed(self._samples):
            if (
                sample.signature == signature
                and sample.accepted
                and sample.landed is None
            ):
                sample.landed = landed
                sample.slots_to_land = slots_to_land
                sample.status_query_rtt_ms = status_query_rtt_ms
                return

    def snapshot(self) -> ProviderHealthSnapshot:
        samples = list(self._samples)
        accepted = sum(sample.accepted for sample in samples)
        rtts = [
            sample.submission_rtt_ms
            for sample in samples
            if sample.submission_rtt_ms is not None
        ]
        status_rtts = [
            sample.status_query_rtt_ms
            for sample in samples
            if sample.status_query_rtt_ms is not None
        ]
        landed_slots = [
            sample.slots_to_land
            for sample in samples
            if sample.landed and sample.slots_to_land is not None
        ]
        return ProviderHealthSnapshot(
            provider_id=self.provider_id,
            endpoint_id=self.endpoint_id,
            sample_count=len(samples),
            successful_acknowledgements=accepted,
            submission_success_rate=(accepted / len(samples) if samples else None),
            median_submission_rtt_ms=median(rtts) if rtts else None,
            status_query_median_rtt_ms=median(status_rtts) if status_rtts else None,
            transport_errors=sum(
                sample.classification
                in {
                    ErrorClassification.RPC_TRANSPORT_FAILURE,
                    ErrorClassification.PROVIDER_UNAVAILABLE,
                    ErrorClassification.LEADER_ROUTING_FAILURE,
                }
                for sample in samples
            ),
            rate_limits=sum(
                sample.classification == ErrorClassification.RPC_RATE_LIMIT
                for sample in samples
            ),
            accepted_not_landed=sum(
                sample.accepted and sample.landed is False for sample in samples
            ),
            landed_sample_count=len(landed_slots),
            median_slots_to_land=median(landed_slots) if landed_slots else None,
            last_observed_mono=(samples[-1].observed_mono if samples else None),
        )


class ProviderHealthRegistry:
    """Registry keyed by provider and sanitized endpoint identities."""

    def __init__(self, window_size: int = 100) -> None:
        self.window_size = window_size
        self._trackers: dict[tuple[str, str], ProviderHealthTracker] = {}

    def record_submission(self, result: SubmissionResult) -> None:
        key = (result.provider_id, result.endpoint_id)
        tracker = self._trackers.setdefault(
            key,
            ProviderHealthTracker(*key, window_size=self.window_size),
        )
        tracker.record_submission(result)

    def record_landing(
        self,
        provider_id: str,
        endpoint_id: str,
        signature: str,
        *,
        landed: bool | None,
        slots_to_land: int | None,
        status_query_rtt_ms: float | None = None,
    ) -> None:
        """Attach confirmation evidence to an existing endpoint sample."""
        tracker = self._trackers.get((provider_id, endpoint_id))
        if tracker is None:
            return
        tracker.record_landing(
            signature,
            landed=landed,
            slots_to_land=slots_to_land,
            status_query_rtt_ms=status_query_rtt_ms,
        )

    def snapshots(self) -> list[ProviderHealthSnapshot]:
        """Return deterministic snapshots without claiming a best provider."""
        return [self._trackers[key].snapshot() for key in sorted(self._trackers)]

    def order_provider_ids(self, provider_ids: list[str]) -> list[str]:
        """Order only when every candidate has enough recent evidence.

        No endpoint is excluded, and configured order is preserved until the
        complete candidate set reaches the evidence floor.
        """
        by_provider = {item.provider_id: item for item in self.snapshots()}
        if not provider_ids or any(
            provider_id not in by_provider
            or not by_provider[provider_id].enough_samples
            for provider_id in provider_ids
        ):
            return list(provider_ids)
        configured_index = {
            provider_id: index for index, provider_id in enumerate(provider_ids)
        }
        return sorted(
            provider_ids,
            key=lambda provider_id: (
                -(by_provider[provider_id].submission_success_rate or 0.0),
                by_provider[provider_id].median_submission_rtt_ms
                if by_provider[provider_id].median_submission_rtt_ms is not None
                else float("inf"),
                configured_index[provider_id],
            ),
        )
