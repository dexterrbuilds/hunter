"""Exit strategies emit decisions; they never submit transactions."""

from strategies.exit import (
    ExitDecision,
    TakeProfitStopLossStrategy,
    TimedExitStrategy,
    TrailingStopStrategy,
)

__all__ = [
    "ExitDecision",
    "TakeProfitStopLossStrategy",
    "TimedExitStrategy",
    "TrailingStopStrategy",
]
