"""Public Hunter trading facade, independent of Telegram and token listeners."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from application.models import BuyRequest, SellRequest
from application.positions import PositionService
from domain.quotes import ExecutionResult


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

    async def get_wallet_balance(self) -> int:
        return await self.wallet_service.get_wallet_balance()

    async def buy(self, request: BuyRequest) -> ExecutionResult:
        return await self.buy_service.buy(request)

    async def sell(self, request: SellRequest) -> ExecutionResult:
        return await self.sell_service.sell(request)

    def list_positions(self):
        return self.position_service.list_positions()

    def get_position(self, position_id: str):
        return self.position_service.get_position(position_id)

    def get_realized_pnl(self, position_id: str) -> dict[str, int | None]:
        return self.position_service.get_realized_pnl(position_id)

    def get_settings(self) -> dict[str, Any]:
        return self.position_service.store.get_settings()

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        for key, value in values.items():
            self.position_service.store.set_setting(key, value)
        return self.get_settings()
