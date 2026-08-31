"""One execution entrance for all Hunter trade triggers."""

# ruff: noqa: TRY003

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic_ns

from application.models import BuyRequest, SellRequest
from domain.intents import TradeAction, TradeIntent
from domain.quotes import ExecutionResult

BuyExecutor = Callable[[BuyRequest], Awaitable[ExecutionResult]]
SellExecutor = Callable[[SellRequest], Awaitable[ExecutionResult]]
IntentObserver = Callable[[TradeIntent, str, int], None]


@dataclass(slots=True)
class UniversalFastExecution:
    """Translate trigger-neutral intents into the existing fast services.

    The buy and sell services retain responsibility for authoritative quotes,
    risk authorization, idempotent coordination, provider routing, confirmation,
    and accounting. This façade prevents trigger types from selecting their own
    transaction stack.
    """

    buy_executor: BuyExecutor
    sell_executor: SellExecutor
    observer: IntentObserver | None = None

    def _observe(self, intent: TradeIntent, stage: str) -> None:
        if self.observer is not None:
            self.observer(intent, stage, monotonic_ns())

    async def execute(self, intent: TradeIntent) -> ExecutionResult:
        """Execute one intent without interpreting its source as a route."""
        self._observe(intent, "intent_received")
        if intent.action == TradeAction.BUY:
            if (
                intent.quote_mint is None
                or intent.quote_amount is None
                or intent.token_decimals is None
            ):
                raise ValueError("invalid buy intent")
            self._observe(intent, "service_dispatched")
            return await self.buy_executor(
                BuyRequest(
                    token_mint=intent.mint,
                    quote_mint=intent.quote_mint,
                    spend=intent.quote_amount,
                    slippage=intent.slippage,
                    token_decimals=intent.token_decimals,
                    logical_execution_id=intent.logical_execution_id,
                    intent=intent,
                )
            )
        if intent.action == TradeAction.SELL:
            if (
                intent.quote_mint is None
                or intent.token_amount is None
                or intent.position_id is None
            ):
                raise ValueError("invalid sell intent")
            self._observe(intent, "service_dispatched")
            return await self.sell_executor(
                SellRequest(
                    position_id=intent.position_id,
                    token_mint=intent.mint,
                    quote_mint=intent.quote_mint,
                    amount=intent.token_amount,
                    slippage=intent.slippage,
                    logical_execution_id=intent.logical_execution_id,
                    intent=intent,
                )
            )
        raise ValueError("launch intents require TokenLaunchService")
