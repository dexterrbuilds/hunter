"""Public Hunter trading facade, independent of Telegram and token listeners."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from application.universal_execution import UniversalFastExecution
from domain.intents import (
    TradeAction,
    TradeIntent,
    TradeIntentSource,
    default_urgency,
)

if TYPE_CHECKING:
    from application.models import BuyRequest, SellRequest
    from application.positions import PositionService
    from domain.quotes import ExecutionResult
    from storage.sqlite import StoredPosition


class BuyService(Protocol):
    async def buy(self, request: BuyRequest) -> ExecutionResult: ...


class SellService(Protocol):
    async def sell(self, request: SellRequest) -> ExecutionResult: ...


class WalletBalanceService(Protocol):
    async def get_wallet_balance(self) -> int: ...


@dataclass(slots=True)
class TradingEngine:
    """Stable interface that CLI/web/Telegram adapters may call later."""

    buy_service: BuyService
    sell_service: SellService
    position_service: PositionService
    wallet_service: WalletBalanceService
    intent_executor: UniversalFastExecution | None = None

    async def get_wallet_balance(self) -> int:
        return await self.wallet_service.get_wallet_balance()

    async def buy(self, request: BuyRequest) -> ExecutionResult:
        if request.intent is None:
            source = TradeIntentSource.MANUAL_BUY
            intent = TradeIntent(
                action=TradeAction.BUY,
                source=source,
                mint=request.token_mint,
                wallet_id="primary",
                quote_mint=request.quote_mint,
                quote_amount=request.spend,
                token_decimals=request.token_decimals,
                slippage=request.slippage,
                urgency=default_urgency(source),
                intent_id=request.logical_execution_id or _new_intent_id(),
            )
            request = replace(
                request,
                logical_execution_id=intent.intent_id,
                intent=intent,
            )
        return await self.buy_service.buy(request)

    async def sell(self, request: SellRequest) -> ExecutionResult:
        if request.intent is None:
            source = TradeIntentSource.MANUAL_SELL
            intent = TradeIntent(
                action=TradeAction.SELL,
                source=source,
                mint=request.token_mint,
                wallet_id="primary",
                quote_mint=request.quote_mint,
                token_amount=request.amount,
                position_id=request.position_id,
                slippage=request.slippage,
                urgency=default_urgency(source),
                intent_id=request.logical_execution_id or _new_intent_id(),
            )
            request = replace(
                request,
                logical_execution_id=intent.intent_id,
                intent=intent,
            )
        return await self.sell_service.sell(request)

    async def execute_intent(self, intent: TradeIntent) -> ExecutionResult:
        """Execute any interface request through Hunter's common pipeline."""
        if self.intent_executor is None:
            self.intent_executor = UniversalFastExecution(
                self.buy_service.buy,
                self.sell_service.sell,
            )
        return await self.intent_executor.execute(intent)

    def list_positions(self) -> list[StoredPosition]:
        return self.position_service.list_positions()

    def get_position(self, position_id: str) -> StoredPosition:
        return self.position_service.get_position(position_id)

    def get_realized_pnl(self, position_id: str) -> dict[str, int | None]:
        return self.position_service.get_realized_pnl(position_id)

    def get_settings(self) -> dict[str, Any]:
        return self.position_service.store.get_settings()

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        for key, value in values.items():
            self.position_service.store.set_setting(key, value)
        return self.get_settings()


def _new_intent_id() -> str:
    return str(uuid4())
