"""Pure persisted-position exit decision strategies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class ExitDecisionReason(StrEnum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TRAILING_STOP = "trailing_stop"
    MAX_HOLD_TIME = "max_hold_time"


@dataclass(frozen=True, slots=True)
class ExitDecision:
    should_exit: bool
    reason: ExitDecisionReason | None = None
    observed_price_raw: int | None = None


@dataclass(frozen=True, slots=True)
class TakeProfitStopLossStrategy:
    take_profit_price_raw: int | None
    stop_loss_price_raw: int | None

    def evaluate(self, current_price_raw: int) -> ExitDecision:
        if (
            self.take_profit_price_raw is not None
            and current_price_raw >= self.take_profit_price_raw
        ):
            return ExitDecision(True, ExitDecisionReason.TAKE_PROFIT, current_price_raw)
        if (
            self.stop_loss_price_raw is not None
            and current_price_raw <= self.stop_loss_price_raw
        ):
            return ExitDecision(True, ExitDecisionReason.STOP_LOSS, current_price_raw)
        return ExitDecision(False, observed_price_raw=current_price_raw)


@dataclass(frozen=True, slots=True)
class TimedExitStrategy:
    opened_at: datetime
    maximum_hold_seconds: int

    def evaluate(self, now: datetime | None = None) -> ExitDecision:
        current = now or datetime.now(UTC)
        if current >= self.opened_at + timedelta(seconds=self.maximum_hold_seconds):
            return ExitDecision(True, ExitDecisionReason.MAX_HOLD_TIME)
        return ExitDecision(False)


@dataclass(slots=True)
class TrailingStopStrategy:
    trailing_stop_bps: int
    high_water_price_raw: int = 0

    def evaluate(self, current_price_raw: int) -> ExitDecision:
        self.high_water_price_raw = max(self.high_water_price_raw, current_price_raw)
        trigger = (
            self.high_water_price_raw * (10_000 - self.trailing_stop_bps) // 10_000
        )
        if self.high_water_price_raw > 0 and current_price_raw <= trigger:
            return ExitDecision(
                True, ExitDecisionReason.TRAILING_STOP, current_price_raw
            )
        return ExitDecision(False, observed_price_raw=current_price_raw)
