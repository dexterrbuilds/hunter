"""Persistent position accounting and restart reconciliation service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from solders.pubkey import Pubkey

from core.pubkeys import is_sol_paired
from domain.accounting import BuyFill, PositionAccounting, RealizedPnl, SellFill
from domain.lifecycle import PositionStatus
from domain.quotes import ExecutionResult, ExecutionSide
from storage.sqlite import SQLitePositionStore, StoredPosition


def _other_execution_cost(result: ExecutionResult) -> int | None:
    if result.rent_lamports is None:
        return None
    return result.rent_lamports + result.delivery_tip_lamports


class WalletBalanceReader(Protocol):
    async def get_token_balance_raw(
        self, owner: Pubkey, mint: Pubkey, token_program: Pubkey | None = None
    ) -> int: ...


class SolanaWalletBalanceReader:
    """Recovery adapter that does not assume legacy vs Token-2022 ownership."""

    def __init__(self, client):
        self.client = client

    async def get_token_balance_raw(
        self, owner: Pubkey, mint: Pubkey, token_program: Pubkey | None = None
    ) -> int:
        return await self.client.get_wallet_token_balance_raw(owner, mint)


@dataclass(frozen=True, slots=True)
class RecoveryIssue:
    position_id: str
    persisted_quantity_raw: int
    wallet_quantity_raw: int | None
    reason: str


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    eligible_position_ids: tuple[str, ...]
    issues: tuple[RecoveryIssue, ...]


class PositionService:
    """Owns durable accounting rather than an interface or monitor loop."""

    def __init__(self, store: SQLitePositionStore):
        self.store = store

    def open_from_execution(
        self,
        result: ExecutionResult,
        *,
        token_mint: Pubkey,
        quote_mint: Pubkey,
        token_decimals: int,
        quote_decimals: int,
        position_id: str | None = None,
        strategy_metadata: dict | None = None,
    ) -> StoredPosition:
        if result.side != ExecutionSide.BUY or not result.success:
            raise ValueError("an open position requires a successful buy result")
        if result.token_delta_raw is None or result.quote_delta_raw is None:
            raise ValueError("authoritative buy token and quote deltas are required")
        accounting = PositionAccounting(
            position_id=position_id or str(uuid4()),
            token_mint=token_mint,
            quote_mint=quote_mint,
            token_decimals=token_decimals,
            quote_decimals=quote_decimals,
        )
        accounting.record_buy(
            BuyFill(
                signature=result.signature,
                token_quantity_raw=result.token_delta_raw,
                quote_cost_raw=result.quote_delta_raw,
                network_fee_lamports=result.network_fee_lamports,
                priority_fee_lamports=result.priority_fee_lamports,
                other_cost_lamports=_other_execution_cost(result),
            )
        )
        position = StoredPosition(accounting, strategy_metadata or {})
        self.store.save_position(position)
        self.store.record_fill(
            position_id=accounting.position_id,
            side="buy",
            signature=result.signature,
            token_quantity_raw=result.token_delta_raw,
            quote_amount_raw=result.quote_delta_raw,
            network_fee_lamports=result.network_fee_lamports,
            priority_fee_lamports=result.priority_fee_lamports,
            other_cost_lamports=_other_execution_cost(result),
        )
        return position

    def apply_sell_execution(
        self, position_id: str, result: ExecutionResult
    ) -> RealizedPnl:
        if result.side != ExecutionSide.SELL or not result.success:
            raise ValueError("a position exit requires a successful sell result")
        if result.token_delta_raw is None or result.quote_delta_raw is None:
            raise ValueError("authoritative sell token and quote deltas are required")
        position = self.get_position(position_id)
        if any(
            fill["side"] == "sell" and fill["signature"] == result.signature
            for fill in self.store.list_fills(position_id)
        ):
            raise ValueError("sell execution was already applied to this position")
        pnl = position.accounting.record_sell(
            SellFill(
                signature=result.signature,
                token_quantity_raw=result.token_delta_raw,
                quote_proceeds_raw=result.quote_delta_raw,
                network_fee_lamports=result.network_fee_lamports,
                priority_fee_lamports=result.priority_fee_lamports,
                other_cost_lamports=_other_execution_cost(result),
            ),
            quote_is_native_sol=is_sol_paired(position.accounting.quote_mint),
        )
        target_status = (
            PositionStatus.CLOSED
            if position.accounting.remaining_quantity_raw == 0
            else PositionStatus.OPEN
        )
        self.store.save_position(position)
        self.store.record_fill(
            position_id=position_id,
            side="sell",
            signature=result.signature,
            token_quantity_raw=result.token_delta_raw,
            quote_amount_raw=result.quote_delta_raw,
            network_fee_lamports=result.network_fee_lamports,
            priority_fee_lamports=result.priority_fee_lamports,
            other_cost_lamports=_other_execution_cost(result),
        )
        self.store.transition_position(
            position_id,
            target_status,
            "execution effects applied to position accounting",
        )
        return pnl

    def list_positions(self) -> list[StoredPosition]:
        return self.store.list_positions()

    def get_position(self, position_id: str) -> StoredPosition:
        position = self.store.get_position(position_id)
        if position is None:
            raise KeyError(f"unknown position: {position_id}")
        return position

    def get_realized_pnl(self, position_id: str) -> dict[str, int | None]:
        accounting = self.get_position(position_id).accounting
        return {
            "gross_raw": accounting.realized_gross_pnl_raw,
            "net_raw": accounting.realized_net_pnl_raw,
            "remaining_cost_basis_raw": accounting.remaining_cost_basis_raw,
            "remaining_quantity_raw": accounting.remaining_quantity_raw,
        }

    async def recover(
        self,
        *,
        owner: Pubkey,
        balance_reader: WalletBalanceReader,
    ) -> RecoveryReport:
        """Reconcile open persisted quantities without initiating a sale."""
        managed = self.store.list_positions(
            {
                PositionStatus.OPEN,
                PositionStatus.EXIT_REQUESTED,
                PositionStatus.SELL_SUBMITTED,
                PositionStatus.SELL_FAILED_RETRYABLE,
            }
        )
        eligible: list[str] = []
        issues: list[RecoveryIssue] = []
        for position in managed:
            expected = position.accounting.remaining_quantity_raw
            try:
                actual = await balance_reader.get_token_balance_raw(
                    owner, position.accounting.token_mint
                )
            except Exception as error:  # noqa: BLE001
                actual = None
                reason = f"balance read failed: {type(error).__name__}"
            else:
                reason = (
                    "wallet token balance does not match persisted inventory"
                    if actual != expected
                    else ""
                )
            if reason:
                self.store.mark_reconciliation_required(
                    position.accounting.position_id, reason
                )
                issues.append(
                    RecoveryIssue(
                        position.accounting.position_id, expected, actual, reason
                    )
                )
            else:
                eligible.append(position.accounting.position_id)
        return RecoveryReport(tuple(eligible), tuple(issues))
