"""Credential-safe execution and detection telemetry models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic_ns

from execution.errors import ErrorClassification


def utc_now() -> datetime:
    """Return an aware UTC wall-clock timestamp for external correlation."""
    return datetime.now(UTC)


def priority_fee_lamports(
    compute_unit_price_micro_lamports: int, compute_unit_limit: int
) -> int:
    """Calculate the configured maximum priority fee using integer arithmetic."""
    if compute_unit_price_micro_lamports < 0 or compute_unit_limit < 0:
        raise ValueError("compute-unit price and limit must be non-negative")
    numerator = compute_unit_price_micro_lamports * compute_unit_limit
    return (numerator + 999_999) // 1_000_000


@dataclass(slots=True)
class DetectionTelemetry:
    """Observation timing from detector input through trade request creation."""

    source: str
    event_observed_at: datetime = field(default_factory=utc_now)
    event_observed_mono_ns: int = field(default_factory=monotonic_ns)
    event_slot: int | None = None
    transaction_slot: int | None = None
    launch_slot: int | None = None
    hunter_processing_started_at: datetime | None = None
    hunter_processing_started_mono_ns: int | None = None
    trade_request_created_at: datetime | None = None
    trade_request_created_mono_ns: int | None = None
    authoritative_refresh_started_mono_ns: int | None = None
    authoritative_refresh_completed_mono_ns: int | None = None
    account_read_duration_ms: float | None = None

    def mark_processing_started(self) -> None:
        self.hunter_processing_started_at = utc_now()
        self.hunter_processing_started_mono_ns = monotonic_ns()

    def mark_trade_request_created(self) -> None:
        self.trade_request_created_at = utc_now()
        self.trade_request_created_mono_ns = monotonic_ns()

    def processing_delay_ms(self) -> float | None:
        if self.hunter_processing_started_mono_ns is None:
            return None
        return (
            self.hunter_processing_started_mono_ns - self.event_observed_mono_ns
        ) / 1_000_000


@dataclass(slots=True)
class ProviderAttemptTelemetry:
    """One provider acknowledgement for one immutable transaction signature."""

    provider_id: str
    endpoint_id: str
    execution_variant: str
    signature: str
    accepted: bool
    acknowledgement: str
    bytes_sent: int
    connection_reused: bool | None = None
    connection_session_generation: int | None = None
    connection_session_created: bool | None = None
    provider_reference: str | None = None
    submit_started_mono_ns: int | None = None
    acknowledged_mono_ns: int | None = None
    response_wall_time: datetime | None = None
    submitted_slot: int | None = None
    error_classification: ErrorClassification | None = None
    error_code: str | int | None = None
    diagnostic: str | None = None

    @property
    def submit_rtt_ms(self) -> float | None:
        if self.submit_started_mono_ns is None or self.acknowledged_mono_ns is None:
            return None
        return (self.acknowledged_mono_ns - self.submit_started_mono_ns) / 1_000_000


@dataclass(slots=True)
class ExecutionTelemetry:
    """One transaction attempt's build, delivery, and confirmation lifecycle."""

    execution_id: str
    provider_id: str | None = None
    endpoint_id: str | None = None
    logical_trade_id: str | None = None
    execution_variant: str = "standard"

    detection_source: str | None = None
    event_observed_at: datetime | None = None
    event_observed_mono_ns: int | None = None
    detection_slot: int | None = None
    transaction_slot: int | None = None
    launch_slot: int | None = None
    processing_started_at: datetime | None = None
    processing_started_mono_ns: int | None = None
    authoritative_refresh_started_mono_ns: int | None = None
    authoritative_refresh_completed_mono_ns: int | None = None
    account_read_duration_ms: float | None = None

    detected_at: datetime | None = None
    detected_mono_ns: int | None = None
    trade_requested_at: datetime | None = None
    trade_requested_mono_ns: int | None = None
    build_started_at: datetime | None = None
    build_started_mono_ns: int | None = None
    build_completed_at: datetime | None = None
    build_completed_mono_ns: int | None = None
    signing_started_at: datetime | None = None
    signing_started_mono_ns: int | None = None
    signing_completed_at: datetime | None = None
    signing_completed_mono_ns: int | None = None
    submission_started_at: datetime | None = None
    submission_started_mono_ns: int | None = None
    rpc_responded_at: datetime | None = None
    rpc_responded_mono_ns: int | None = None
    signature_received_at: datetime | None = None
    signature_received_mono_ns: int | None = None
    processed_at: datetime | None = None
    processed_mono_ns: int | None = None
    confirmed_at: datetime | None = None
    confirmed_mono_ns: int | None = None
    finalized_at: datetime | None = None
    finalized_mono_ns: int | None = None

    submitted_slot: int | None = None
    landed_slot: int | None = None
    blockhash: str | None = None
    blockhash_source_provider: str | None = None
    blockhash_source_slot: int | None = None
    blockhash_age_ms_at_submission: float | None = None
    last_valid_block_height: int | None = None
    transaction_signature: str | None = None
    compute_unit_limit: int | None = None
    compute_unit_price_micro_lamports: int | None = None
    priority_fee_lamports: int | None = None
    priority_fee_estimate_micro_lamports: int | None = None
    priority_fee_estimation_source: str | None = None
    priority_fee_estimate_age_ms: float | None = None
    priority_fee_estimation_latency_ms: float | None = None
    base_network_fee_lamports: int | None = None
    jito_tip_lamports: int = 0
    rent_lamports: int | None = None
    maximum_rent_exposure_lamports: int = 0
    other_known_cost_lamports: int = 0
    transaction_size_bytes: int | None = None

    error_classification: ErrorClassification | None = None
    error_code: str | int | None = None
    error_detail: str | None = None
    attributes: dict[str, str | int | float | bool] = field(default_factory=dict)
    provider_attempts: list[ProviderAttemptTelemetry] = field(default_factory=list)

    def mark(self, stage: str) -> None:
        """Set one lifecycle stage's UTC and monotonic timestamps.

        Args:
            stage: Field prefix such as ``build_started`` or ``confirmed``.

        Raises:
            ValueError: If the stage is not part of the telemetry schema.
        """
        wall_name = f"{stage}_at"
        mono_name = f"{stage}_mono_ns"
        if not hasattr(self, wall_name) or not hasattr(self, mono_name):
            raise ValueError(f"Unknown execution stage: {stage}")
        setattr(self, wall_name, utc_now())
        setattr(self, mono_name, monotonic_ns())

    def latency_ms(self, start_stage: str, end_stage: str) -> float | None:
        """Calculate latency from two monotonic stage readings."""
        start = getattr(self, f"{start_stage}_mono_ns", None)
        end = getattr(self, f"{end_stage}_mono_ns", None)
        if start is None or end is None:
            return None
        return (end - start) / 1_000_000

    def apply_detection(self, detection: DetectionTelemetry | None) -> None:
        """Copy detector timing without coupling transaction code to listeners."""
        if detection is None:
            return
        self.detection_source = detection.source
        self.event_observed_at = detection.event_observed_at
        self.event_observed_mono_ns = detection.event_observed_mono_ns
        self.detected_at = detection.event_observed_at
        self.detected_mono_ns = detection.event_observed_mono_ns
        self.detection_slot = detection.event_slot
        self.transaction_slot = detection.transaction_slot
        self.launch_slot = detection.launch_slot
        self.processing_started_at = detection.hunter_processing_started_at
        self.processing_started_mono_ns = detection.hunter_processing_started_mono_ns
        self.authoritative_refresh_started_mono_ns = (
            detection.authoritative_refresh_started_mono_ns
        )
        self.authoritative_refresh_completed_mono_ns = (
            detection.authoritative_refresh_completed_mono_ns
        )
        self.account_read_duration_ms = detection.account_read_duration_ms
        if detection.trade_request_created_at is not None:
            self.trade_requested_at = detection.trade_request_created_at
            self.trade_requested_mono_ns = detection.trade_request_created_mono_ns

    def add_provider_attempt(self, attempt: ProviderAttemptTelemetry) -> None:
        """Attach transport evidence without changing economic execution identity."""
        if (
            attempt.signature != self.transaction_signature
            and self.transaction_signature
        ):
            raise ValueError(
                "provider attempt signature differs from execution identity"
            )
        self.provider_attempts.append(attempt)

    @property
    def known_total_fee_lamports(self) -> int | None:
        """Known SOL costs; unknown base fee stays unknown rather than zero."""
        if self.base_network_fee_lamports is None or self.rent_lamports is None:
            return None
        return (
            self.base_network_fee_lamports
            + (self.priority_fee_lamports or 0)
            + self.jito_tip_lamports
            + self.rent_lamports
            + self.other_known_cost_lamports
        )
