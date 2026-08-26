"""Objective transaction landing metrics and latency-budget warnings."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from execution.telemetry import ExecutionTelemetry


def slot_delta(reference_slot: int | None, landed_slot: int | None) -> int | None:
    """Return landed minus reference slot when both observations exist."""
    if reference_slot is None or landed_slot is None:
        return None
    return landed_slot - reference_slot


def same_slot_classification(
    reference_slot: int | None, landed_slot: int | None
) -> str:
    """Classify measured slot distance without implying an unknown launch slot."""
    delta = slot_delta(reference_slot, landed_slot)
    if delta is None:
        return "unknown"
    if delta < 0:
        return "inconsistent"
    if delta == 0:
        return "same_slot"
    return f"+{delta}_slot" if delta == 1 else f"+{delta}_slots"


@dataclass(frozen=True, slots=True)
class LandingMetrics:
    """Derived monotonic latency and independently observed slot metrics."""

    detection_to_build_ms: float | None
    build_ms: float | None
    sign_ms: float | None
    submit_rtt_ms: float | None
    submit_to_processed_ms: float | None
    submit_to_landed_ms: float | None
    detection_to_landed_ms: float | None
    detection_slot: int | None
    submission_slot: int | None
    landed_slot: int | None
    slots_to_land: int | None
    slots_from_detection: int | None
    slots_from_launch: int | None
    detection_slot_classification: str
    launch_slot_classification: str

    @classmethod
    def from_telemetry(cls, value: ExecutionTelemetry) -> LandingMetrics:
        submit_rtt = value.latency_ms("submission_started", "rpc_responded")
        if submit_rtt is None and value.provider_attempts:
            submit_rtt = value.provider_attempts[0].submit_rtt_ms
        submit_to_landed = value.latency_ms("submission_started", "processed")
        return cls(
            detection_to_build_ms=_between(
                value.detected_mono_ns, value.build_started_mono_ns
            ),
            build_ms=value.latency_ms("build_started", "build_completed"),
            sign_ms=value.latency_ms("signing_started", "signing_completed"),
            submit_rtt_ms=submit_rtt,
            submit_to_processed_ms=value.latency_ms("submission_started", "processed"),
            submit_to_landed_ms=submit_to_landed,
            detection_to_landed_ms=_between(
                value.detected_mono_ns, value.processed_mono_ns
            ),
            detection_slot=value.detection_slot,
            submission_slot=value.submitted_slot,
            landed_slot=value.landed_slot,
            slots_to_land=slot_delta(value.submitted_slot, value.landed_slot),
            slots_from_detection=slot_delta(value.detection_slot, value.landed_slot),
            slots_from_launch=slot_delta(value.launch_slot, value.landed_slot),
            detection_slot_classification=same_slot_classification(
                value.detection_slot, value.landed_slot
            ),
            launch_slot_classification=same_slot_classification(
                value.launch_slot, value.landed_slot
            ),
        )


@dataclass(frozen=True, slots=True)
class LatencyBudgets:
    """Warning thresholds only; they do not reject a transaction."""

    detection_processing_ms: float | None = None
    quote_generation_ms: float | None = None
    blockhash_retrieval_ms: float | None = None
    transaction_build_ms: float | None = None
    signing_ms: float | None = None
    submission_rtt_ms: float | None = None


def budget_warnings(
    measurements: dict[str, float | None], budgets: LatencyBudgets
) -> list[str]:
    """Return deterministic non-blocking latency warnings."""
    warnings: list[str] = []
    for item in fields(budgets):
        maximum = getattr(budgets, item.name)
        actual = measurements.get(item.name)
        if maximum is not None and actual is not None and actual > maximum:
            warnings.append(f"{item.name} {actual:.3f}ms exceeded {maximum:.3f}ms")
    return warnings


def _between(start: int | None, end: int | None) -> float | None:
    if start is None or end is None:
        return None
    return (end - start) / 1_000_000
