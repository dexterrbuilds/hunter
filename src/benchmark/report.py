"""Summarize Hunter's own provider telemetry without universal rankings."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from statistics import mean, median
from typing import TYPE_CHECKING, Any

from execution.errors import ErrorClassification
from storage.sqlite import SQLitePositionStore

if TYPE_CHECKING:
    from collections.abc import Callable

TWO_OR_MORE_SLOTS = 2


@dataclass(frozen=True, slots=True)
class ProviderComparison:
    provider_id: str
    endpoint_id: str
    sample_count: int
    median_submit_rtt_ms: float | None
    p90_submit_rtt_ms: float | None
    median_submit_to_land_ms: float | None
    p90_submit_to_land_ms: float | None
    median_slots_to_land: float | None
    same_slot_percentage: float | None
    plus_one_slot_percentage: float | None
    plus_two_or_later_percentage: float | None
    failure_rate: float
    ambiguous_outcome_rate: float
    estimated_average_fee_lamports: float | None
    ranking_eligible: bool


def summarize_provider_telemetry(
    executions: list[dict[str, Any]], *, minimum_ranking_samples: int = 20
) -> list[ProviderComparison]:
    """Group per-provider attempts; never rank an insufficient sample."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for execution in executions:
        for attempt in execution.get("provider_attempts", []):
            item = dict(attempt)
            item["execution"] = execution
            key = (str(item["provider_id"]), str(item["endpoint_id"]))
            grouped.setdefault(key, []).append(item)

    summaries: list[ProviderComparison] = []
    ambiguous_values = {
        ErrorClassification.ACCEPTED_BUT_NOT_OBSERVED.value,
        ErrorClassification.CONFIRMATION_TIMEOUT.value,
        ErrorClassification.TRANSACTION_DROPPED.value,
    }
    for (provider_id, endpoint_id), items in sorted(grouped.items()):
        rtts = [_attempt_rtt(item) for item in items]
        rtts = [value for value in rtts if value is not None]
        land_ms = [_submit_to_land(item) for item in items]
        land_ms = [value for value in land_ms if value is not None]
        slots = [_slots_to_land(item["execution"]) for item in items]
        slots = [value for value in slots if value is not None]
        fees = [_known_fee(item["execution"]) for item in items]
        fees = [value for value in fees if value is not None]
        failures = sum(not bool(item.get("accepted")) for item in items)
        ambiguous = sum(
            item["execution"].get("error_classification") in ambiguous_values
            for item in items
        )
        summaries.append(
            ProviderComparison(
                provider_id,
                endpoint_id,
                len(items),
                median(rtts) if rtts else None,
                _percentile(rtts, 0.9),
                median(land_ms) if land_ms else None,
                _percentile(land_ms, 0.9),
                median(slots) if slots else None,
                _percentage(slots, lambda value: value == 0),
                _percentage(slots, lambda value: value == 1),
                _percentage(slots, lambda value: value >= TWO_OR_MORE_SLOTS),
                failures / len(items),
                ambiguous / len(items),
                mean(fees) if fees else None,
                len(items) >= minimum_ranking_samples,
            )
        )
    return summaries


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize credential-free Hunter execution telemetry"
    )
    parser.add_argument("database", help="Hunter SQLite database path")
    parser.add_argument("--minimum-samples", type=int, default=20)
    args = parser.parse_args()
    store = SQLitePositionStore(args.database)
    try:
        report = summarize_provider_telemetry(
            store.list_telemetry(), minimum_ranking_samples=args.minimum_samples
        )
    finally:
        store.close()
    print(json.dumps([asdict(item) for item in report], indent=2, sort_keys=True))


def _attempt_rtt(item: dict[str, Any]) -> float | None:
    start = item.get("submit_started_mono_ns")
    end = item.get("acknowledged_mono_ns")
    if start is None or end is None:
        return None
    return (end - start) / 1_000_000


def _submit_to_land(item: dict[str, Any]) -> float | None:
    start = item.get("submit_started_mono_ns")
    execution = item["execution"]
    end = execution.get("processed_mono_ns")
    if start is None or end is None:
        return None
    return (end - start) / 1_000_000


def _slots_to_land(execution: dict[str, Any]) -> int | None:
    submitted = execution.get("submitted_slot")
    landed = execution.get("landed_slot")
    if submitted is None or landed is None:
        return None
    return landed - submitted


def _known_fee(execution: dict[str, Any]) -> int | None:
    base = execution.get("base_network_fee_lamports")
    rent = execution.get("rent_lamports")
    if base is None or rent is None:
        return None
    return sum(
        (
            base,
            execution.get("priority_fee_lamports") or 0,
            execution.get("jito_tip_lamports") or 0,
            rent,
            execution.get("other_known_cost_lamports") or 0,
        )
    )


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = fraction * (len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _percentage(values: list[int], predicate: Callable[[int], bool]) -> float | None:
    if not values:
        return None
    return 100 * sum(predicate(value) for value in values) / len(values)


if __name__ == "__main__":
    main()
