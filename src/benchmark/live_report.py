"""Detection, transport, landing, failure, and cost benchmark reports."""

# Export validation has a direct operator-facing error message.
# ruff: noqa: TC001, TRY003

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from typing import Any

from benchmark.live_store import BenchmarkStore


@dataclass(frozen=True, slots=True)
class DetectionSummary:
    source: str
    observations: int
    median_relative_delay_ms: float | None
    p50_relative_delay_ms: float | None
    p90_relative_delay_ms: float | None
    p95_relative_delay_ms: float | None
    median_slot_delay: float | None


@dataclass(frozen=True, slots=True)
class RouteSummary:
    provider_id: str
    endpoint_id: str
    route_id: str
    route_mode: str
    execution_variant: str
    connection_state: str
    samples: int
    median_acknowledgement_ms: float | None
    p90_acknowledgement_ms: float | None
    median_submit_to_land_ms: float | None
    p90_submit_to_land_ms: float | None
    median_detection_to_land_ms: float | None
    same_detection_slot_percentage: float | None
    detection_plus_one_percentage: float | None
    detection_plus_two_percentage: float | None
    detection_plus_three_or_later_percentage: float | None
    launch_block_zero_percentage: float | None
    launch_plus_one_percentage: float | None
    launch_plus_two_percentage: float | None
    launch_plus_three_or_later_percentage: float | None
    failure_rate: float
    ambiguous_rate: float
    median_priority_fee_lamports: float | None
    median_tip_lamports: float | None
    median_known_cost_lamports: float | None
    cost_per_successful_same_detection_slot_lamports: float | None
    cost_per_successful_launch_block_zero_lamports: float | None
    ranking_eligible: bool


def build_live_report(
    store: BenchmarkStore,
    *,
    session_id: str | None = None,
    minimum_ranking_samples: int = 20,
) -> dict[str, Any]:
    detections = store.list_detections(session_id)
    attempts = store.list_attempts(session_id)
    return {
        "sessions": store.list_sessions(session_id),
        "detection": [asdict(item) for item in _detection_summaries(detections)],
        "routes": [
            asdict(item) for item in _route_summaries(attempts, minimum_ranking_samples)
        ],
    }


def export_report(report: dict[str, Any], path: str | Path, format_name: str) -> None:
    """Export aggregate data without provider endpoints or credentials."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if format_name == "json":
        target.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        return
    if format_name != "csv":
        raise ValueError("benchmark export format must be json or csv")
    rows: list[dict[str, Any]] = []
    for section in ("sessions", "detection", "routes"):
        for item in report[section]:
            rows.append({"section": section, **item})
    fieldnames = sorted({key for row in rows for key in row}) or ["section"]
    with target.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def render_text_report(report: dict[str, Any]) -> str:
    """Render a concise summary without fabricating missing measurements."""
    regions = sorted({item["region_label"] for item in report["sessions"]})
    lines = [
        f"Region(s): {', '.join(regions) if regions else 'unspecified'}",
        "",
        "Detection",
    ]
    if report["detection"]:
        for item in report["detection"]:
            delay = _format_ms(item["median_relative_delay_ms"])
            lines.append(
                f"{item['source']:<14} observations {item['observations']:<5} "
                f"median relative {delay}"
            )
    else:
        lines.append("No correlated detection observations recorded.")
    lines.extend(("", "Submission / landing"))
    if report["routes"]:
        for item in report["routes"]:
            note = "eligible" if item["ranking_eligible"] else "sample too small"
            lines.append(
                f"{item['provider_id']:<14} {item['route_id']:<12} "
                f"n={item['samples']:<4} ack={_format_ms(item['median_acknowledgement_ms'])} "
                f"land={_format_ms(item['median_submit_to_land_ms'])} "
                f"fail={item['failure_rate']:.1%} ({note})"
            )
    else:
        lines.append("No transport or economic attempts recorded.")
    return "\n".join(lines)


def _detection_summaries(rows: list[dict[str, Any]]) -> list[DetectionSummary]:
    by_correlation: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_correlation.setdefault(row["correlation_key"], []).append(row)
    delays: dict[str, list[float]] = {}
    slots: dict[str, list[int]] = {}
    counts: dict[str, int] = {}
    for correlated in by_correlation.values():
        earliest = min(row["observed_mono_ns"] for row in correlated)
        known_slots = [
            row["detection_slot"]
            for row in correlated
            if row["detection_slot"] is not None
        ]
        earliest_slot = min(known_slots) if known_slots else None
        for row in correlated:
            source = row["source"]
            counts[source] = counts.get(source, 0) + 1
            delays.setdefault(source, []).append(
                (row["observed_mono_ns"] - earliest) / 1_000_000
            )
            if earliest_slot is not None and row["detection_slot"] is not None:
                slots.setdefault(source, []).append(
                    row["detection_slot"] - earliest_slot
                )
    return [
        DetectionSummary(
            source,
            counts[source],
            median(delays[source]),
            _percentile(delays[source], 0.50),
            _percentile(delays[source], 0.90),
            _percentile(delays[source], 0.95),
            median(slots.get(source, [])) if slots.get(source) else None,
        )
        for source in sorted(counts)
    ]


def _route_summaries(
    rows: list[dict[str, Any]], minimum_ranking_samples: int
) -> list[RouteSummary]:
    groups: dict[tuple[str, str, str, str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (
            row["provider_id"],
            row["endpoint_id"],
            row["route_id"],
            row["route_mode"],
            row["execution_variant"],
            row["connection_state"],
        )
        groups.setdefault(key, []).append(row)
    summaries = []
    for (provider, endpoint, route_id, mode, variant, state), items in sorted(
        groups.items()
    ):
        ack = _values(items, "acknowledgement_rtt_ms")
        landing = _values(items, "submit_to_landed_ms")
        detection_landing = _values(items, "detection_to_landed_ms")
        detection_slots = [_slot_delta(item, "detection_slot") for item in items]
        detection_slots = [value for value in detection_slots if value is not None]
        launch_slots = [_slot_delta(item, "launch_slot") for item in items]
        launch_slots = [value for value in launch_slots if value is not None]
        priority = _values(items, "priority_fee_lamports")
        tips = _values(items, "jito_tip_lamports")
        known_costs = [_known_cost(item) for item in items]
        known_costs = [value for value in known_costs if value is not None]
        same_successes = sum(
            bool(item["success"]) and _slot_delta(item, "detection_slot") == 0
            for item in items
        )
        block_zero_successes = sum(
            bool(item["success"]) and _slot_delta(item, "launch_slot") == 0
            for item in items
        )
        summaries.append(
            RouteSummary(
                provider,
                endpoint,
                route_id,
                mode,
                variant,
                state,
                len(items),
                median(ack) if ack else None,
                _percentile(ack, 0.90),
                median(landing) if landing else None,
                _percentile(landing, 0.90),
                median(detection_landing) if detection_landing else None,
                _percentage(detection_slots, 0),
                _percentage(detection_slots, 1),
                _percentage(detection_slots, 2),
                _percentage_at_least(detection_slots, 3),
                _percentage(launch_slots, 0),
                _percentage(launch_slots, 1),
                _percentage(launch_slots, 2),
                _percentage_at_least(launch_slots, 3),
                sum(not bool(item["success"]) for item in items) / len(items),
                sum(bool(item["ambiguous"]) for item in items) / len(items),
                median(priority) if priority else None,
                median(tips) if tips else None,
                median(known_costs) if known_costs else None,
                (sum(known_costs) / same_successes if same_successes else None),
                (
                    sum(known_costs) / block_zero_successes
                    if block_zero_successes
                    else None
                ),
                len(items) >= minimum_ranking_samples,
            )
        )
    return summaries


def _values(items: list[dict[str, Any]], key: str) -> list[float]:
    return [float(item[key]) for item in items if item.get(key) is not None]


def _slot_delta(item: dict[str, Any], base: str) -> int | None:
    if item.get(base) is None or item.get("landed_slot") is None:
        return None
    return int(item["landed_slot"]) - int(item[base])


def _known_cost(item: dict[str, Any]) -> int | None:
    if item.get("base_fee_lamports") is None or item.get("rent_lamports") is None:
        return None
    return sum(
        (
            item["base_fee_lamports"],
            item.get("priority_fee_lamports") or 0,
            item.get("jito_tip_lamports") or 0,
            item["rent_lamports"],
            item.get("other_known_cost_lamports") or 0,
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


def _percentage(values: list[int], expected: int) -> float | None:
    if not values:
        return None
    return 100 * sum(value == expected for value in values) / len(values)


def _percentage_at_least(values: list[int], minimum: int) -> float | None:
    if not values:
        return None
    return 100 * sum(value >= minimum for value in values) / len(values)


def _format_ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}ms"
