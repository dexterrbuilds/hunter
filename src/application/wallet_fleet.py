"""Operator-owned wallet fleet accounting and coordinated exits."""

# Stable orchestration APIs favor explicit booleans and lifecycle branches.
# ruff: noqa: C901, FBT003, TRY003

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from solders.pubkey import Pubkey

from core.pubkeys import is_sol_paired
from domain.amounts import BasisPoints, TokenAmountRaw
from domain.intents import (
    ExecutionUrgency,
    TradeAction,
    TradeIntent,
    TradeIntentSource,
)
from domain.launch import FleetExecutionPolicy, FleetExitPolicy, FleetExitType


class FleetStore(Protocol):
    def save_fleet_position(self, values: dict) -> None: ...

    def list_fleet_positions(
        self, plan_id: str, *, status: str | None = None
    ) -> list[dict]: ...

    def claim_fleet_exit(self, **values: object) -> bool: ...

    def update_fleet_exit(
        self, logical_execution_id: str, *, state: str, signature: str | None = None
    ) -> None: ...

    def list_pending_fleet_exits(self, plan_id: str) -> list[dict]: ...

    def apply_fleet_sell(
        self,
        logical_execution_id: str,
        *,
        signature: str,
        sold_quantity_raw: int,
        quote_proceeds_raw: int,
        known_exit_cost_lamports: int | None,
    ) -> dict: ...


FleetIntentExecutor = Callable[[TradeIntent], Awaitable[object]]
FleetBundleExecutor = Callable[[tuple[TradeIntent, ...]], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class FleetValuation:
    expected_quote_proceeds_raw: dict[str, int]
    quote_mint: Pubkey
    quote_decimals: int
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class FleetExitDecision:
    should_exit: bool
    trigger: FleetExitType
    gross_return_bps: int | None
    net_return_bps: int | None
    reason: str


class FleetExitEvaluator:
    """Evaluate portfolio exits from expected quote proceeds, not spot price."""

    @staticmethod
    def evaluate(
        positions: list[dict],
        valuation: FleetValuation,
        policy: FleetExitPolicy,
        *,
        now: datetime | None = None,
    ) -> FleetExitDecision:
        if not positions:
            return FleetExitDecision(
                False, policy.exit_type, None, None, "no positions"
            )
        if any(item["marry_mode"] for item in positions) and policy.exit_type not in {
            FleetExitType.MANUAL,
            FleetExitType.EMERGENCY,
        }:
            return FleetExitDecision(False, policy.exit_type, None, None, "marry mode")
        cost = sum(item["quote_cost_basis_raw"] for item in positions)
        proceeds = sum(
            valuation.expected_quote_proceeds_raw.get(item["fleet_position_id"], 0)
            for item in positions
        )
        gross_bps = None if cost == 0 else (proceeds - cost) * 10_000 // cost
        net_bps = gross_bps
        if is_sol_paired(valuation.quote_mint) and cost:
            known_costs = sum(
                item.get("known_cost_lamports") or 0 for item in positions
            )
            net_bps = (proceeds - cost - known_costs) * 10_000 // cost
        elif not is_sol_paired(valuation.quote_mint):
            # Network costs are SOL-denominated and cannot be converted into an
            # SPL quote without an explicit FX source.
            net_bps = None
        if policy.exit_type in {FleetExitType.MANUAL, FleetExitType.EMERGENCY}:
            return FleetExitDecision(
                True, policy.exit_type, gross_bps, net_bps, "explicit exit"
            )
        if policy.exit_type == FleetExitType.TIME_BASED:
            current = now or datetime.now(UTC)
            due = all(
                item.get("scheduled_exit_at")
                and datetime.fromisoformat(item["scheduled_exit_at"]) <= current
                for item in positions
            )
            return FleetExitDecision(
                due,
                policy.exit_type,
                gross_bps,
                net_bps,
                "timer due" if due else "timer pending",
            )
        target = policy.target_bps or 0
        metric = net_bps if net_bps is not None else gross_bps
        if metric is None:
            return FleetExitDecision(
                False, policy.exit_type, gross_bps, net_bps, "valuation unavailable"
            )
        if policy.exit_type in {FleetExitType.PROFIT_TARGET, FleetExitType.TAKE_PROFIT}:
            should = metric >= target
        else:
            should = metric <= -abs(target)
        return FleetExitDecision(
            should,
            policy.exit_type,
            gross_bps,
            net_bps,
            "threshold reached" if should else "threshold pending",
        )


class WalletFleetService:
    """Persist authoritative component effects as fleet positions."""

    def __init__(self, store: FleetStore) -> None:
        self.store = store

    def record_buy(  # noqa: PLR0913
        self,
        *,
        plan_id: str,
        launch_id: str,
        mint: Pubkey,
        quote_mint: Pubkey,
        wallet_id: str,
        wallet_role: str,
        token_decimals: int,
        quote_decimals: int,
        buy_signature: str,
        acquired_quantity_raw: int,
        quote_cost_basis_raw: int,
        known_cost_lamports: int | None,
        marry_mode: bool,
        timed_exit_after_seconds: int | None = None,
    ) -> str:
        position_id = f"fleet:{plan_id}:{wallet_id}:{mint}"
        scheduled = (
            datetime.now(UTC) + timedelta(seconds=timed_exit_after_seconds)
            if timed_exit_after_seconds is not None
            else None
        )
        self.store.save_fleet_position(
            {
                "fleet_position_id": position_id,
                "plan_id": plan_id,
                "launch_id": launch_id,
                "mint": str(mint),
                "quote_mint": str(quote_mint),
                "wallet_id": wallet_id,
                "wallet_role": wallet_role,
                "token_decimals": token_decimals,
                "quote_decimals": quote_decimals,
                "buy_signature": buy_signature,
                "acquired_quantity_raw": acquired_quantity_raw,
                "remaining_quantity_raw": acquired_quantity_raw,
                "quote_cost_basis_raw": quote_cost_basis_raw,
                "known_cost_lamports": known_cost_lamports,
                "status": "active",
                "marry_mode": marry_mode,
                "scheduled_exit_at": scheduled.isoformat() if scheduled else None,
            }
        )
        return position_id

    def record_sell(
        self,
        logical_execution_id: str,
        *,
        signature: str,
        sold_quantity_raw: int,
        quote_proceeds_raw: int,
        known_exit_cost_lamports: int | None,
    ) -> dict:
        """Apply one authoritative landed effect exactly once in storage."""
        return self.store.apply_fleet_sell(
            logical_execution_id,
            signature=signature,
            sold_quantity_raw=sold_quantity_raw,
            quote_proceeds_raw=quote_proceeds_raw,
            known_exit_cost_lamports=known_exit_cost_lamports,
        )


class WalletFleetExitService:
    """Freeze and dispatch fleet sells with stable per-wallet identities."""

    def __init__(
        self,
        store: FleetStore,
        execute_intent: FleetIntentExecutor,
        *,
        bundle_executor: FleetBundleExecutor | None = None,
        submission_authorizer: Callable[[TradeIntent], None] | None = None,
        maximum_concurrency: int = 4,
    ) -> None:
        self.store = store
        self.execute_intent = execute_intent
        self.bundle_executor = bundle_executor
        self.submission_authorizer = submission_authorizer
        self.maximum_concurrency = maximum_concurrency

    async def execute_exit(
        self,
        *,
        plan_id: str,
        trigger: FleetExitType,
        policy: FleetExecutionPolicy,
        slippage: BasisPoints,
        exclude_invalid_positions: bool = False,
    ) -> tuple[TradeIntent, ...]:
        positions = await asyncio.to_thread(
            self.store.list_fleet_positions, plan_id, status="active"
        )
        intents: list[TradeIntent] = []
        for item in positions:
            quantity = item["remaining_quantity_raw"]
            if quantity <= 0:
                if exclude_invalid_positions:
                    continue
                raise ValueError(
                    f"fleet position {item['fleet_position_id']} has no inventory"
                )
            if item["marry_mode"] and trigger not in {
                FleetExitType.MANUAL,
                FleetExitType.EMERGENCY,
            }:
                continue
            logical_id = (
                f"{plan_id}:exit:{trigger.value}:{item['fleet_position_id']}:{quantity}"
            )
            claimed = await asyncio.to_thread(
                self.store.claim_fleet_exit,
                logical_execution_id=logical_id,
                plan_id=plan_id,
                fleet_position_id=item["fleet_position_id"],
                trigger_type=trigger.value,
            )
            if not claimed:
                continue
            source = (
                TradeIntentSource.EMERGENCY_EXIT
                if trigger == FleetExitType.EMERGENCY
                else TradeIntentSource.WALLET_FLEET_EXIT
            )
            intents.append(
                TradeIntent(
                    action=TradeAction.SELL,
                    source=source,
                    mint=Pubkey.from_string(item["mint"]),
                    wallet_id=item["wallet_id"],
                    quote_mint=Pubkey.from_string(item["quote_mint"]),
                    token_amount=TokenAmountRaw(
                        quantity,
                        Pubkey.from_string(item["mint"]),
                        item["token_decimals"],
                    ),
                    position_id=item["fleet_position_id"],
                    slippage=slippage,
                    urgency=(
                        ExecutionUrgency.CRITICAL
                        if trigger in {FleetExitType.EMERGENCY, FleetExitType.STOP_LOSS}
                        else ExecutionUrgency.HIGH
                    ),
                    intent_id=logical_id,
                    metadata={"fleet_plan_id": plan_id, "exit_trigger": trigger.value},
                )
            )
        frozen = tuple(intents)
        if policy == FleetExecutionPolicy.BUNDLE:
            if self.bundle_executor is None:
                raise ValueError("bundled exit selected without bundle executor")
            for intent in frozen:
                self._authorize_submission(intent)
            await self.bundle_executor(frozen)
            for intent in frozen:
                await asyncio.to_thread(
                    self.store.update_fleet_exit,
                    intent.intent_id,
                    state="submitted",
                )
            return frozen
        if policy == FleetExecutionPolicy.SEQUENTIAL:
            for intent in frozen:
                result = await self.execute_intent(intent)
                await asyncio.to_thread(
                    self.store.update_fleet_exit,
                    intent.intent_id,
                    state="submitted",
                    signature=getattr(result, "signature", None),
                )
            return frozen
        semaphore = asyncio.Semaphore(self.maximum_concurrency)

        async def execute(intent: TradeIntent) -> None:
            async with semaphore:
                result = await self.execute_intent(intent)
                await asyncio.to_thread(
                    self.store.update_fleet_exit,
                    intent.intent_id,
                    state="submitted",
                    signature=getattr(result, "signature", None),
                )

        await asyncio.gather(*(execute(intent) for intent in frozen))
        return frozen

    def _authorize_submission(self, intent: TradeIntent) -> None:
        """Retain the runtime defensive-exit gate for direct bundle submission."""
        if self.submission_authorizer is not None:
            self.submission_authorizer(intent)

    def pending_after_restart(self, plan_id: str) -> list[dict]:
        """Expose pending signatures for inspection; never resubmit here."""
        return self.store.list_pending_fleet_exits(plan_id)
