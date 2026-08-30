"""Fail-closed configuration for Hunter live-network benchmarks."""

# Configuration parsing intentionally emits field-specific validation messages.
# ruff: noqa: C901, PLR0912, PLR2004, TRY003, TRY004

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from core.pubkeys import is_sol_paired, resolve_quote_mint

LIVE_ACKNOWLEDGEMENT = "I UNDERSTAND HUNTER LIVE BENCHMARK USES REAL FUNDS"


class ExitPolicy(StrEnum):
    """Explicit handling of tokens acquired by an economic benchmark."""

    MANUAL = "manual"
    IMMEDIATE = "sell_immediately_after_confirmed_buy"
    AFTER_SECONDS = "sell_after_seconds"


@dataclass(frozen=True, slots=True)
class BenchmarkRoute:
    """One explicitly authorized economic route/variant trial."""

    route_id: str
    providers: tuple[str, ...]
    mode: str = "single"
    execution_variant: str = "standard"

    def __post_init__(self) -> None:
        if not self.route_id or any(character.isspace() for character in self.route_id):
            raise ValueError("benchmark route IDs must be stable non-blank identifiers")
        if not self.providers or len(self.providers) != len(set(self.providers)):
            raise ValueError("each benchmark route needs unique provider IDs")
        if self.mode not in {"single", "race", "hedged", "fallback"}:
            raise ValueError("benchmark route mode is invalid")
        if self.mode == "single" and len(self.providers) != 1:
            raise ValueError("single benchmark routes require exactly one provider")
        if self.execution_variant not in {
            "standard",
            "jito_tipped",
            "helius_sender_tipped",
        }:
            raise ValueError("benchmark execution variant is invalid")


@dataclass(frozen=True, slots=True)
class BenchmarkCaps:
    """Limits that are independent from, and stricter than, trading settings."""

    maximum_sol_spend_per_trade_lamports: int
    maximum_quote_amount_raw: int
    maximum_live_trades: int
    maximum_cumulative_spend_raw: int
    maximum_priority_fee_lamports: int
    maximum_tip_lamports: int
    maximum_combined_transaction_cost_lamports: int
    minimum_wallet_reserve_lamports: int
    maximum_duration_seconds: float

    def __post_init__(self) -> None:
        for name in (
            "maximum_sol_spend_per_trade_lamports",
            "maximum_quote_amount_raw",
            "maximum_live_trades",
            "maximum_cumulative_spend_raw",
            "maximum_priority_fee_lamports",
            "maximum_tip_lamports",
            "maximum_combined_transaction_cost_lamports",
            "minimum_wallet_reserve_lamports",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"benchmark.caps.{name} must be a positive integer")
        if self.maximum_duration_seconds <= 0:
            raise ValueError("benchmark.caps.maximum_duration_seconds must be positive")


@dataclass(frozen=True, slots=True)
class LiveBenchmarkConfig:
    """Operator-controlled economic benchmark configuration."""

    live_enabled: bool
    acknowledgement: str
    mint: str
    quote_amount_raw: int
    quote_mint: str
    provider_matrix: tuple[BenchmarkRoute, ...]
    caps: BenchmarkCaps
    region_label: str = "unspecified"
    dedicated_wallet: bool = False
    warm_providers: bool = True
    exit_policy: ExitPolicy = ExitPolicy.MANUAL
    exit_after_seconds: float | None = None
    authoritative_launch_slot: int | None = None
    authoritative_launch_timestamp: datetime | None = None
    detection_slot: int | None = None

    def __post_init__(self) -> None:
        if not self.mint.strip():
            raise ValueError("benchmark.mint must be explicitly configured")
        if not self.quote_mint.strip():
            raise ValueError("benchmark.quote_mint must be explicitly configured")
        if isinstance(self.quote_amount_raw, bool) or self.quote_amount_raw <= 0:
            raise ValueError("benchmark.quote_amount_raw must be a positive integer")
        if not self.provider_matrix:
            raise ValueError("benchmark.provider_matrix must not be empty")
        route_ids = [route.route_id for route in self.provider_matrix]
        if len(route_ids) != len(set(route_ids)):
            raise ValueError("benchmark.provider_matrix contains duplicate route IDs")
        if not self.region_label.strip() or len(self.region_label) > 64:
            raise ValueError("benchmark.region_label must be 1..64 characters")
        if self.exit_policy == ExitPolicy.AFTER_SECONDS:
            if self.exit_after_seconds is None or self.exit_after_seconds <= 0:
                raise ValueError(
                    "benchmark.exit_after_seconds must be positive for after_seconds"
                )
        elif self.exit_after_seconds is not None:
            raise ValueError(
                "benchmark.exit_after_seconds is only valid with sell_after_seconds"
            )
        if self.exit_policy != ExitPolicy.MANUAL and self.caps.maximum_live_trades < 2:
            raise ValueError(
                "automatic benchmark exits require a trade cap of at least 2"
            )
        for label, slot in (
            ("authoritative_launch_slot", self.authoritative_launch_slot),
            ("detection_slot", self.detection_slot),
        ):
            if slot is not None and (
                isinstance(slot, bool) or not isinstance(slot, int) or slot < 0
            ):
                raise ValueError(f"benchmark.{label} must be a non-negative integer")
        if (
            self.authoritative_launch_timestamp is not None
            and self.authoritative_launch_timestamp.tzinfo is None
        ):
            raise ValueError("benchmark launch timestamp must include a timezone")

    def authorize(self, *, cli_allow_live: bool, risk_enforced: bool) -> None:
        """Require every deliberate authorization condition."""
        if not self.live_enabled:
            raise PermissionError("benchmark.live_enabled is false")
        if self.acknowledgement != LIVE_ACKNOWLEDGEMENT:
            raise PermissionError("benchmark acknowledgement does not exactly match")
        if not cli_allow_live:
            raise PermissionError("the --allow-live flag is required")
        if not risk_enforced:
            raise PermissionError("live benchmarking requires risk.enforce: true")
        self.validate_amounts()

    def validate_amounts(self) -> None:
        """Reject an amount outside the independent benchmark envelope."""
        if self.quote_amount_raw > self.caps.maximum_quote_amount_raw:
            raise ValueError("benchmark amount exceeds maximum_quote_amount_raw")
        if is_sol_paired(resolve_quote_mint(self.quote_mint)) and (
            self.quote_amount_raw > self.caps.maximum_sol_spend_per_trade_lamports
        ):
            raise ValueError("benchmark amount exceeds SOL per-trade cap")

    def maximum_exposure_summary(self) -> dict[str, int | float | str]:
        """Return the exact upper bounds printed before a live run."""
        return {
            "quote_mint": self.quote_mint,
            "amount_per_trade_raw": self.quote_amount_raw,
            "maximum_quote_exposure_per_trade_raw": (
                self.maximum_per_trade_exposure_raw
            ),
            "configured_economic_trials": len(self.provider_matrix),
            "maximum_trades": self.caps.maximum_live_trades,
            "maximum_cumulative_spend_raw": (self.caps.maximum_cumulative_spend_raw),
            "maximum_cost_per_transaction_lamports": (
                self.caps.maximum_combined_transaction_cost_lamports
            ),
            "maximum_duration_seconds": self.caps.maximum_duration_seconds,
        }

    @property
    def maximum_per_trade_exposure_raw(self) -> int:
        """Conservative quote exposure reserved before a buy."""
        maximum = self.caps.maximum_quote_amount_raw
        if is_sol_paired(resolve_quote_mint(self.quote_mint)):
            maximum = min(maximum, self.caps.maximum_sol_spend_per_trade_lamports)
        return maximum

    def validate_encoded_input(self, maximum_input_raw: int) -> None:
        """Check the slippage-expanded instruction maximum before construction."""
        if maximum_input_raw > self.maximum_per_trade_exposure_raw:
            raise ValueError(
                "slippage-expanded benchmark input exceeds the per-trade cap"
            )


def live_benchmark_config_from_dict(value: dict[str, Any]) -> LiveBenchmarkConfig:
    """Parse the benchmark section without environment-based live defaults."""
    if not isinstance(value, dict):
        raise TypeError("benchmark must be a mapping")
    caps_value = value.get("caps")
    if not isinstance(caps_value, dict):
        raise ValueError("benchmark.caps must be explicitly configured")
    matrix = value.get("provider_matrix")
    if not isinstance(matrix, list):
        raise ValueError("benchmark.provider_matrix must be a list")
    try:
        exit_policy = ExitPolicy(value.get("exit_policy", ExitPolicy.MANUAL.value))
    except ValueError as error:
        raise ValueError("benchmark.exit_policy is invalid") from error
    return LiveBenchmarkConfig(
        live_enabled=_required_bool(value, "live_enabled"),
        acknowledgement=_required_string(value, "acknowledgement"),
        mint=_required_string(value, "mint"),
        quote_amount_raw=_required_int(value, "quote_amount_raw"),
        quote_mint=_required_string(value, "quote_mint"),
        provider_matrix=tuple(_route(item) for item in matrix),
        caps=BenchmarkCaps(
            maximum_sol_spend_per_trade_lamports=_required_int(
                caps_value, "maximum_sol_spend_per_trade_lamports"
            ),
            maximum_quote_amount_raw=_required_int(
                caps_value, "maximum_quote_amount_raw"
            ),
            maximum_live_trades=_required_int(caps_value, "maximum_live_trades"),
            maximum_cumulative_spend_raw=_required_int(
                caps_value, "maximum_cumulative_spend_raw"
            ),
            maximum_priority_fee_lamports=_required_int(
                caps_value, "maximum_priority_fee_lamports"
            ),
            maximum_tip_lamports=_required_int(caps_value, "maximum_tip_lamports"),
            maximum_combined_transaction_cost_lamports=_required_int(
                caps_value, "maximum_combined_transaction_cost_lamports"
            ),
            minimum_wallet_reserve_lamports=_required_int(
                caps_value, "minimum_wallet_reserve_lamports"
            ),
            maximum_duration_seconds=float(
                _required_number(caps_value, "maximum_duration_seconds")
            ),
        ),
        region_label=_required_string(value, "region_label"),
        dedicated_wallet=_required_bool(value, "dedicated_wallet"),
        warm_providers=_optional_bool(value, "warm_providers", default=True),
        exit_policy=exit_policy,
        exit_after_seconds=(
            float(value["exit_after_seconds"])
            if value.get("exit_after_seconds") is not None
            else None
        ),
        authoritative_launch_slot=_optional_int(value, "authoritative_launch_slot"),
        authoritative_launch_timestamp=_optional_datetime(
            value, "authoritative_launch_timestamp"
        ),
        detection_slot=_optional_int(value, "detection_slot"),
    )


def _required_bool(value: dict[str, Any], key: str) -> bool:
    item = value.get(key)
    if not isinstance(item, bool):
        raise ValueError(f"benchmark.{key} must be explicitly true or false")
    return item


def _optional_bool(value: dict[str, Any], key: str, *, default: bool) -> bool:
    item = value.get(key, default)
    if not isinstance(item, bool):
        raise ValueError(f"benchmark.{key} must be true or false")
    return item


def _required_int(value: dict[str, Any], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise ValueError(f"benchmark.{key} must be an integer")
    return item


def _required_number(value: dict[str, Any], key: str) -> int | float:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int | float):
        raise ValueError(f"benchmark.{key} must be numeric")
    return item


def _required_string(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise ValueError(f"benchmark.{key} must be a string")
    return item


def _optional_int(value: dict[str, Any], key: str) -> int | None:
    item = value.get(key)
    if item is None:
        return None
    return _required_int(value, key)


def _optional_datetime(value: dict[str, Any], key: str) -> datetime | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str):
        raise ValueError(f"benchmark.{key} must be an ISO-8601 string")
    try:
        return datetime.fromisoformat(item.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"benchmark.{key} is not valid ISO-8601") from error


def _route(value: object) -> BenchmarkRoute:
    if isinstance(value, str):
        return BenchmarkRoute(route_id=value, providers=(value,))
    if not isinstance(value, dict):
        raise ValueError("benchmark provider matrix entries must be mappings")
    providers = value.get("providers")
    if not isinstance(providers, list) or not all(
        isinstance(provider, str) for provider in providers
    ):
        raise ValueError("benchmark route providers must be a list of IDs")
    return BenchmarkRoute(
        route_id=_required_string(value, "id"),
        providers=tuple(providers),
        mode=_required_string(value, "mode"),
        execution_variant=str(value.get("execution_variant", "standard")),
    )
