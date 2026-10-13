"""Narrow application and renderer ports for the terminal adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

from terminal_ui.models import OperatorAction, OperatorConfirmation, TerminalSnapshot

if TYPE_CHECKING:
    from application.runtime_models import RuntimeStatus


class TerminalApplication(Protocol):
    """Safe application methods consumed by the terminal service."""

    bot_id: str
    config: dict[str, Any]
    event_bus: Any
    features: Any

    def runtime_status(self) -> RuntimeStatus: ...

    def enable_trading(self) -> None: ...

    def disable_trading(self) -> None: ...

    def activate_kill_switch(self) -> None: ...

    def list_positions_page(
        self, *, limit: int = 25, offset: int = 0
    ) -> list[object]: ...

    def list_executions(self, limit: int = 20) -> list[object]: ...

    def list_recent_telemetry(self, limit: int = 200) -> list[dict[str, Any]]: ...

    def risk_settings(self) -> dict[str, Any]: ...

    def get_settings(self) -> dict[str, Any]: ...

    def operator_settings(self) -> dict[str, Any]: ...

    def list_tracked_wallets(self) -> tuple[str, ...]: ...

    async def wallet_balances(self) -> tuple[dict[str, object], ...]: ...


ActionRequester = Callable[[OperatorAction], Awaitable[OperatorConfirmation]]
ConfirmationRequester = Callable[[str], Awaitable[None]]
RefreshRequester = Callable[[], None]
ExitRequester = Callable[[], None]
PageRequester = Callable[[str, int], None]


class TerminalDriver(Protocol):
    """UI framework boundary; fake implementations keep tests offline."""

    async def start(
        self,
        *,
        request_action: ActionRequester,
        confirm_action: ConfirmationRequester,
        request_refresh: RefreshRequester,
        request_exit: ExitRequester,
        request_page: PageRequester,
    ) -> None: ...

    async def wait_ready(self) -> None: ...

    def publish_snapshot(self, snapshot: TerminalSnapshot) -> None: ...

    async def close(self) -> None: ...
