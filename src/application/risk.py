"""Interface-independent pre-trade and fee risk controls."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from time import monotonic

from solders.pubkey import Pubkey

from domain.amounts import Lamports
from domain.quotes import ExecutionPlan, ExecutionSide
from execution.errors import ErrorClassification, ExecutionError


@dataclass(frozen=True, slots=True)
class FeeExposure:
    """Maximum known lamport exposure before signing."""

    base_fee: Lamports | None
    priority_fee: Lamports
    rent: Lamports
    jito_tip: Lamports = field(default_factory=lambda: Lamports(0))
    other_known_cost: Lamports = field(default_factory=lambda: Lamports(0))

    @property
    def maximum_known_lamports(self) -> int | None:
        if self.base_fee is None:
            return None
        return (
            self.base_fee.value
            + self.priority_fee.value
            + self.rent.value
            + self.jito_tip.value
            + self.other_known_cost.value
        )


@dataclass(slots=True)
class RiskLimits:
    """Raw-unit limits; disabled by default for Milestone 1 compatibility."""

    enforce: bool = False
    trading_enabled: bool = True
    emergency_kill_switch: bool = False
    maximum_buy_raw_by_quote: dict[Pubkey, int] = field(default_factory=dict)
    maximum_position_raw_by_quote: dict[Pubkey, int] = field(default_factory=dict)
    maximum_aggregate_exposure_raw_by_quote: dict[Pubkey, int] = field(
        default_factory=dict
    )
    maximum_total_transaction_fee_lamports: int | None = None
    maximum_priority_fee_lamports: int | None = None
    minimum_wallet_reserve_lamports: int | None = None
    maximum_trades_per_interval: int | None = None
    trade_interval_seconds: float = 60.0
    reject_unknown_base_fee: bool = False


@dataclass(frozen=True, slots=True)
class RiskContext:
    wallet_lamports: int
    existing_position_exposure_raw: int
    aggregate_exposure_raw: int
    fee_exposure: FeeExposure
    native_trade_spend_lamports: int = 0


class RiskService:
    """Deterministic guards applied before any signer is invoked."""

    def __init__(self, limits: RiskLimits | None = None):
        self.limits = limits or RiskLimits()
        self._trade_times: deque[float] = deque()

    def assess(self, plan: ExecutionPlan, context: RiskContext) -> None:
        """Raise a typed error when a configured guard rejects a trade."""
        limits = self.limits
        if not limits.enforce:
            return
        if limits.emergency_kill_switch:
            raise _risk_error("emergency kill switch is active")
        if not limits.trading_enabled:
            raise _risk_error("trading is disabled")

        quote = plan.quote_mint
        if plan.side == ExecutionSide.BUY:
            self._guard_optional_max(
                plan.input_raw,
                limits.maximum_buy_raw_by_quote.get(quote),
                "buy amount",
            )
            self._guard_optional_max(
                context.existing_position_exposure_raw + plan.input_raw,
                limits.maximum_position_raw_by_quote.get(quote),
                "position size",
            )
            self._guard_optional_max(
                context.aggregate_exposure_raw + plan.input_raw,
                limits.maximum_aggregate_exposure_raw_by_quote.get(quote),
                "aggregate exposure",
            )

        fee = context.fee_exposure
        self._guard_optional_max(
            fee.priority_fee.value,
            limits.maximum_priority_fee_lamports,
            "priority fee",
        )
        maximum_known = fee.maximum_known_lamports
        if maximum_known is None and limits.reject_unknown_base_fee:
            raise _risk_error("base transaction fee is unknown")
        if maximum_known is not None:
            self._guard_optional_max(
                maximum_known,
                limits.maximum_total_transaction_fee_lamports,
                "total transaction fee exposure",
            )
        if limits.minimum_wallet_reserve_lamports is not None:
            required = limits.minimum_wallet_reserve_lamports + (
                maximum_known if maximum_known is not None else 0
            )
            required += context.native_trade_spend_lamports
            if context.wallet_lamports < required:
                raise ExecutionError(
                    ErrorClassification.INSUFFICIENT_BALANCE,
                    "wallet balance would fall below configured reserve",
                    retryable=False,
                )
        self._guard_rate_limit()

    def record_trade(self) -> None:
        if self.limits.enforce:
            self._trade_times.append(monotonic())

    def update_limits(self, limits: RiskLimits) -> None:
        self.limits = limits

    def _guard_rate_limit(self) -> None:
        maximum = self.limits.maximum_trades_per_interval
        if maximum is None:
            return
        now = monotonic()
        cutoff = now - self.limits.trade_interval_seconds
        while self._trade_times and self._trade_times[0] <= cutoff:
            self._trade_times.popleft()
        if len(self._trade_times) >= maximum:
            raise _risk_error("maximum trades per interval reached")

    @staticmethod
    def _guard_optional_max(value: int, maximum: int | None, label: str) -> None:
        if maximum is not None and value > maximum:
            raise _risk_error(f"{label} {value} exceeds configured maximum {maximum}")


def _risk_error(message: str) -> ExecutionError:
    return ExecutionError(
        ErrorClassification.RISK_LIMIT_EXCEEDED, message, retryable=False
    )
