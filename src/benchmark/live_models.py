"""Persistent records and measurements for controlled network benchmarks."""

# ErrorClassification is retained as a typed persistent field.
# ruff: noqa: TC001

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from execution.errors import ErrorClassification


class BenchmarkKind(StrEnum):
    DETECTION = "detection"
    TRANSPORT = "transport"
    ECONOMIC = "economic"


class ConnectionState(StrEnum):
    COLD = "cold"
    WARM = "warm"
    RECONNECTED = "reconnected"


@dataclass(frozen=True, slots=True)
class DetectionObservation:
    session_id: str
    source: str
    mint: str
    observed_at: datetime
    observed_mono_ns: int
    creation_signature: str | None = None
    launch_slot: int | None = None
    detection_slot: int | None = None
    transaction_slot: int | None = None
    processing_started_mono_ns: int | None = None
    trade_request_mono_ns: int | None = None

    @property
    def correlation_key(self) -> str:
        return self.creation_signature or self.mint


@dataclass(frozen=True, slots=True)
class BenchmarkAttempt:
    session_id: str
    attempt_id: str
    kind: BenchmarkKind
    provider_id: str
    endpoint_id: str
    route_mode: str
    route_id: str
    connection_state: ConnectionState
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    acknowledgement_rtt_ms: float | None = None
    detection_to_build_ms: float | None = None
    quote_generation_ms: float | None = None
    transaction_build_ms: float | None = None
    signing_ms: float | None = None
    detection_to_submit_ms: float | None = None
    submit_to_processed_ms: float | None = None
    submit_to_confirmed_ms: float | None = None
    submit_to_finalized_ms: float | None = None
    submit_to_landed_ms: float | None = None
    detection_to_landed_ms: float | None = None
    launch_to_detection_ms: float | None = None
    launch_to_landed_ms: float | None = None
    launch_slot: int | None = None
    detection_slot: int | None = None
    submission_slot: int | None = None
    landed_slot: int | None = None
    success: bool = False
    ambiguous: bool = False
    error_classification: ErrorClassification | None = None
    base_fee_lamports: int | None = None
    priority_fee_lamports: int | None = None
    jito_tip_lamports: int = 0
    rent_lamports: int | None = None
    other_known_cost_lamports: int = 0
    compute_unit_price_micro_lamports: int | None = None
    compute_units_consumed: int | None = None
    transaction_size_bytes: int | None = None
    blockhash_age_ms: float | None = None
    signature: str | None = None
    logical_trade_id: str | None = None
    execution_variant: str = "standard"
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def slots_from_detection(self) -> int | None:
        if self.detection_slot is None or self.landed_slot is None:
            return None
        return self.landed_slot - self.detection_slot

    @property
    def slots_from_launch(self) -> int | None:
        if self.launch_slot is None or self.landed_slot is None:
            return None
        return self.landed_slot - self.launch_slot

    @property
    def known_cost_lamports(self) -> int | None:
        if self.base_fee_lamports is None or self.rent_lamports is None:
            return None
        return (
            self.base_fee_lamports
            + (self.priority_fee_lamports or 0)
            + self.jito_tip_lamports
            + self.rent_lamports
            + self.other_known_cost_lamports
        )
