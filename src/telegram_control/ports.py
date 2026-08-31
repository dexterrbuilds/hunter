"""Narrow transport and application ports used by Telegram control."""

# Runtime-visible protocol types are intentional documentation.
# ruff: noqa: TC001

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from application.runtime_models import ApplicationEvent, RuntimeStatus
from domain.intents import TradeIntent
from telegram_control.models import IncomingCallback, IncomingMessage, MessageButton


class TelegramTransport(Protocol):
    async def start(
        self,
        message_handler: Callable[[IncomingMessage], Awaitable[None]],
        callback_handler: Callable[[IncomingCallback], Awaitable[None]],
        error_handler: Callable[[BaseException], None],
    ) -> None: ...

    async def send_message(
        self,
        chat_id: int,
        text: str,
        buttons: Sequence[MessageButton] = (),
    ) -> None: ...

    async def answer_callback(self, callback_id: str, text: str = "") -> None: ...

    async def close(self) -> None: ...


class TelegramApplication(Protocol):
    bot_id: str
    config: dict[str, Any]
    event_bus: Any
    trader: Any
    features: Any

    def runtime_status(self) -> RuntimeStatus: ...

    def enable_trading(self) -> None: ...

    def disable_trading(self) -> None: ...

    def activate_kill_switch(self) -> None: ...

    def list_positions(self) -> list[object]: ...

    def get_position(self, position_id: str) -> object: ...

    def get_realized_pnl(self, position_id: str) -> dict[str, int | None]: ...

    def list_executions(self, limit: int = 20) -> list[object]: ...

    def get_execution(self, identity: str) -> object: ...

    def get_settings(self) -> dict[str, Any]: ...

    def risk_settings(self) -> dict[str, Any]: ...

    async def wallet_balances(self) -> tuple[dict[str, object], ...]: ...

    def prepare_manual_buy(
        self, mint: str, amount: str, quote: str | None
    ) -> TradeIntent: ...

    def prepare_manual_sell(self, position_id: str, percentage: str) -> TradeIntent: ...

    def preview_manual_trade(self, intent: TradeIntent) -> dict[str, object]: ...

    async def submit_manual_trade(self, intent: TradeIntent) -> object: ...

    def list_tracked_wallets(self) -> tuple[str, ...]: ...

    def fleet_positions(self, plan_id: str) -> list[dict]: ...

    async def submit_fleet_exit(self, **values: object) -> object: ...

    def prepare_token_launch(self, request: object) -> object: ...

    async def submit_token_launch(
        self, request: object, **values: object
    ) -> object: ...

    def _emit(self, event_type: str, **attributes: object) -> None: ...


ApplicationEventConsumer = Callable[[ApplicationEvent], Awaitable[None]]
