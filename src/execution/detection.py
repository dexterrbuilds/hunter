"""Listener-neutral helpers for carrying detection timing with TokenInfo."""

# Detector observations intentionally accept explicit source timing dimensions.
# ruff: noqa: PLR0913

from __future__ import annotations

from typing import TYPE_CHECKING

from execution.telemetry import DetectionTelemetry

if TYPE_CHECKING:
    from datetime import datetime

    from interfaces.core import TokenInfo

DETECTION_METADATA_KEY = "_hunter_detection_telemetry"


def record_detection(
    token_info: TokenInfo,
    *,
    source: str,
    event_slot: int | None = None,
    transaction_slot: int | None = None,
    launch_slot: int | None = None,
    observed_at: datetime | None = None,
    observed_mono_ns: int | None = None,
    source_region: str | None = None,
    transaction_signature: str | None = None,
    parser_completed_mono_ns: int | None = None,
    validation_completed_mono_ns: int | None = None,
    correlation_completed_mono_ns: int | None = None,
    claim_completed_mono_ns: int | None = None,
) -> DetectionTelemetry:
    """Attach one detector observation without changing protocol token fields."""
    telemetry = DetectionTelemetry(
        source=source,
        event_slot=event_slot,
        transaction_slot=transaction_slot,
        launch_slot=launch_slot,
        source_region=source_region,
        transaction_signature=transaction_signature,
    )
    if observed_at is not None:
        telemetry.event_observed_at = observed_at
    if observed_mono_ns is not None:
        telemetry.event_observed_mono_ns = observed_mono_ns
        telemetry.socket_received_mono_ns = observed_mono_ns
    telemetry.parser_completed_mono_ns = parser_completed_mono_ns
    telemetry.validation_completed_mono_ns = validation_completed_mono_ns
    telemetry.correlation_completed_mono_ns = correlation_completed_mono_ns
    telemetry.claim_completed_mono_ns = claim_completed_mono_ns
    if token_info.additional_data is None:
        token_info.additional_data = {}
    token_info.additional_data[DETECTION_METADATA_KEY] = telemetry
    return telemetry


def detection_for(token_info: TokenInfo) -> DetectionTelemetry | None:
    """Read attached detection timing, if the listener supports it."""
    if not token_info.additional_data:
        return None
    value = token_info.additional_data.get(DETECTION_METADATA_KEY)
    return value if isinstance(value, DetectionTelemetry) else None
