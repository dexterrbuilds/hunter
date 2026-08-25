"""Execution telemetry model reserved for a later instrumentation milestone."""

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
    return (compute_unit_price_micro_lamports * compute_unit_limit) // 1_000_000


@dataclass(slots=True)
class ExecutionTelemetry:
    """One transaction attempt's build, delivery, and confirmation lifecycle."""

    execution_id: str
    provider_id: str | None = None
    endpoint_id: str | None = None

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
    last_valid_block_height: int | None = None
    transaction_signature: str | None = None
    compute_unit_limit: int | None = None
    compute_unit_price_micro_lamports: int | None = None
    priority_fee_lamports: int | None = None
    transaction_size_bytes: int | None = None

    error_classification: ErrorClassification | None = None
    error_code: str | int | None = None
    error_detail: str | None = None
    attributes: dict[str, str | int | float | bool] = field(default_factory=dict)

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
