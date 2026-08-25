"""Requests accepted by Hunter's interface-neutral trading facade."""

from __future__ import annotations

from dataclasses import dataclass

from solders.pubkey import Pubkey

from domain.amounts import BasisPoints, QuoteAmountRaw, TokenAmountRaw
from domain.quotes import ExecutionPlan


@dataclass(frozen=True, slots=True)
class BuyRequest:
    token_mint: Pubkey
    quote_mint: Pubkey
    spend: QuoteAmountRaw
    slippage: BasisPoints
    token_decimals: int
    logical_execution_id: str | None = None
    plan: ExecutionPlan | None = None


@dataclass(frozen=True, slots=True)
class SellRequest:
    position_id: str
    token_mint: Pubkey
    quote_mint: Pubkey
    amount: TokenAmountRaw
    slippage: BasisPoints
    logical_execution_id: str | None = None
    plan: ExecutionPlan | None = None
