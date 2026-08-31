"""Convert tracked Pump.fun activity into universal trade intents."""

# ruff: noqa: PLR0911, TC001, TRY300

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from solders.pubkey import Pubkey

from domain.amounts import QuoteAmountRaw, floor_mul_bps
from domain.intents import (
    TradeAction,
    TradeIntent,
    TradeIntentSource,
    default_urgency,
)
from domain.wallet_tracking import (
    CopySizingMode,
    DuplicatePolicy,
    TrackedWallet,
    WalletActivity,
    WalletActivityType,
    WalletTrackingConfig,
)
from interfaces.core import TokenInfo


class WalletEventStore(Protocol):
    async def claim_wallet_event(
        self, event: WalletActivity, label: str | None
    ) -> bool: ...

    async def complete_wallet_event(
        self, event_id: str, *, state: str, intent_id: str | None, reason: str | None
    ) -> None: ...


class PositionLookup(Protocol):
    def has_open_position(self, wallet_id: str, mint: Pubkey) -> bool: ...


WalletBalanceProvider = Callable[[str, Pubkey], Awaitable[int]]
IntentExecutor = Callable[[TradeIntent], Awaitable[object]]


@dataclass(slots=True)
class TrackedWalletService:
    """Independent CREATE and BUY triggers with durable duplicate protection."""

    config: WalletTrackingConfig
    store: WalletEventStore
    positions: PositionLookup
    execute_intent: IntentExecutor
    wallet_balance_provider: WalletBalanceProvider | None = None
    _wallets: dict[Pubkey, TrackedWallet] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._wallets = {wallet.address: wallet for wallet in self.config.wallets}

    async def handle(
        self, activity: WalletActivity, token_info: TokenInfo | None = None
    ) -> TradeIntent | None:
        """Claim, validate, and dispatch one activity exactly once."""
        if not self.config.enabled:
            return None
        tracked = self._wallets.get(activity.wallet)
        if tracked is None or not self._watches(tracked, activity.activity_type):
            return None
        if not await self.store.claim_wallet_event(activity, tracked.label):
            return None
        try:
            if (
                self.config.duplicate_policy == DuplicatePolicy.IGNORE_EXISTING_POSITION
                and self.positions.has_open_position("primary", activity.mint)
            ):
                await self.store.complete_wallet_event(
                    activity.event_id,
                    state="ignored",
                    intent_id=None,
                    reason="existing position",
                )
                return None
            intent = await self._intent_for(tracked, activity, token_info)
            if intent is None:
                await self.store.complete_wallet_event(
                    activity.event_id,
                    state="ignored",
                    intent_id=None,
                    reason="action disabled or exact sizing unavailable",
                )
                return None
            await self.store.complete_wallet_event(
                activity.event_id,
                state="intent_created",
                intent_id=intent.intent_id,
                reason=None,
            )
            await self.execute_intent(intent)
            await self.store.complete_wallet_event(
                activity.event_id,
                state="executed",
                intent_id=intent.intent_id,
                reason=None,
            )
            return intent
        except Exception as error:
            await self.store.complete_wallet_event(
                activity.event_id,
                state="failed",
                intent_id=None,
                reason=type(error).__name__,
            )
            raise

    @staticmethod
    def _watches(wallet: TrackedWallet, event: WalletActivityType) -> bool:
        return (
            wallet.watch_create
            if event == WalletActivityType.CREATE
            else wallet.watch_buy
        )

    async def _intent_for(
        self,
        tracked: TrackedWallet,
        activity: WalletActivity,
        token_info: TokenInfo | None,
    ) -> TradeIntent | None:
        action = (
            tracked.create_action
            if activity.activity_type == WalletActivityType.CREATE
            else tracked.copy_action
        )
        if not action.enabled or activity.quote_mint is None:
            return None
        amount = action.fixed_quote_amount
        if action.sizing_mode == CopySizingMode.PERCENTAGE_OF_SOURCE:
            if activity.source_quote_amount is None or action.percentage_bps is None:
                return None
            amount = QuoteAmountRaw(
                floor_mul_bps(
                    activity.source_quote_amount.value, action.percentage_bps
                ),
                activity.source_quote_amount.mint,
                activity.source_quote_amount.decimals,
            )
        elif action.sizing_mode == CopySizingMode.PERCENTAGE_OF_WALLET:
            if self.wallet_balance_provider is None or action.percentage_bps is None:
                return None
            raw_balance = await self.wallet_balance_provider(
                "primary", activity.quote_mint
            )
            decimals = (
                action.fixed_quote_amount.decimals
                if action.fixed_quote_amount is not None
                else activity.source_quote_amount.decimals
                if activity.source_quote_amount is not None
                else None
            )
            if decimals is None:
                return None
            amount = QuoteAmountRaw(
                floor_mul_bps(raw_balance, action.percentage_bps),
                activity.quote_mint,
                decimals,
            )
        if amount is None or amount.value == 0:
            return None
        if amount.mint != activity.quote_mint:
            return None
        source = (
            TradeIntentSource.TRACKED_WALLET_CREATE
            if activity.activity_type == WalletActivityType.CREATE
            else TradeIntentSource.TRACKED_WALLET_BUY
        )
        return TradeIntent(
            action=TradeAction.BUY,
            source=source,
            mint=activity.mint,
            wallet_id="primary",
            quote_mint=amount.mint,
            quote_amount=amount,
            token_decimals=activity.token_decimals,
            slippage=action.slippage,
            source_signature=activity.signature,
            source_slot=activity.slot,
            urgency=default_urgency(source),
            metadata={
                "tracked_wallet": str(activity.wallet),
                "tracked_wallet_label": tracked.label or "",
                "event_source": activity.source,
                "state_from_event": token_info is not None,
            },
            intent_id=f"wallet:{activity.event_id}",
        )


class AsyncWalletEventStore:
    """Keep required durability off the listener/event-loop hot path."""

    def __init__(self, store: object) -> None:
        self.store = store

    async def claim_wallet_event(
        self, event: WalletActivity, label: str | None
    ) -> bool:
        return await asyncio.to_thread(
            self.store.claim_wallet_event,  # type: ignore[attr-defined]
            event,
            label,
        )

    async def complete_wallet_event(
        self,
        event_id: str,
        *,
        state: str,
        intent_id: str | None,
        reason: str | None,
    ) -> None:
        await asyncio.to_thread(
            self.store.complete_wallet_event,  # type: ignore[attr-defined]
            event_id,
            state=state,
            intent_id=intent_id,
            reason=reason,
        )
