"""Parse authoritative wallet and Pump event effects from transaction metadata."""

from __future__ import annotations

from typing import Any

from solders.pubkey import Pubkey

from core.pubkeys import is_sol_paired
from domain.quotes import ExecutionResult, ExecutionSide


def owned_token_delta(meta: dict[str, Any], owner: Pubkey, mint: Pubkey) -> int:
    """Sum one owner's raw token changes across every account for a mint."""
    owner_text = str(owner)
    mint_text = str(mint)
    pre = _owned_balances(meta.get("preTokenBalances", []), owner_text, mint_text)
    post = _owned_balances(meta.get("postTokenBalances", []), owner_text, mint_text)
    return sum(post.values()) - sum(pre.values())


def parse_execution_result(
    *,
    logical_execution_id: str,
    side: ExecutionSide,
    signature: str,
    transaction: dict[str, Any],
    user: Pubkey,
    token_mint: Pubkey,
    quote_mint: Pubkey,
    trade_event: dict[str, Any] | None = None,
    priority_fee_lamports: int | None = None,
    delivery_tip_lamports: int = 0,
) -> ExecutionResult:
    """Build actual execution accounting without reference-price estimates."""
    meta = transaction.get("meta", {})
    error = meta.get("err")
    if error:
        return ExecutionResult(
            logical_execution_id,
            side,
            signature,
            False,
            None,
            None,
            meta.get("fee"),
            priority_fee_lamports,
            None,
            None,
            None,
            transaction.get("slot"),
            delivery_tip_lamports=delivery_tip_lamports,
            error=str(error),
        )

    unknown: list[str] = []
    token_change = owned_token_delta(meta, user, token_mint)
    token_amount = token_change if side == ExecutionSide.BUY else -token_change
    if token_amount < 0:
        unknown.append("unexpected_token_balance_direction")
        token_amount = None

    protocol_fee = _event_int(trade_event, "fee")
    creator_fee = _event_int(trade_event, "creator_fee")
    buyback_fee = _event_int(trade_event, "buyback_fee")
    cashback = _event_int(trade_event, "cashback")
    quote_amount: int | None
    if is_sol_paired(quote_mint):
        curve_quote = _event_int(trade_event, "quote_amount")
        if curve_quote is None:
            curve_quote = _event_int(trade_event, "sol_amount")
        if any(
            value is None
            for value in (
                curve_quote,
                protocol_fee,
                creator_fee,
                buyback_fee,
                cashback,
            )
        ):
            quote_amount = None
            unknown.append("native_trade_amount_or_protocol_fees")
        elif side == ExecutionSide.BUY:
            quote_amount = (
                curve_quote + protocol_fee + creator_fee + buyback_fee - cashback
            )
        else:
            quote_amount = max(
                0, curve_quote - protocol_fee - creator_fee - buyback_fee + cashback
            )
    else:
        quote_change = owned_token_delta(meta, user, quote_mint)
        quote_amount = -quote_change if side == ExecutionSide.BUY else quote_change
        if quote_amount < 0:
            quote_amount = None
            unknown.append("unexpected_quote_balance_direction")

    network_fee = meta.get("fee")
    if network_fee is None:
        unknown.append("network_fee")
    if priority_fee_lamports is None:
        unknown.append("priority_fee_breakdown")
    if protocol_fee is None:
        unknown.append("protocol_fee")
    if creator_fee is None:
        unknown.append("creator_fee")
    if buyback_fee is None:
        unknown.append("buyback_fee")
    if cashback is None:
        unknown.append("cashback")

    rent = _derive_rent(
        transaction,
        user,
        side,
        quote_mint,
        quote_amount,
        network_fee,
        delivery_tip_lamports,
    )
    if rent is None:
        unknown.append("rent_or_other_native_effects")

    return ExecutionResult(
        logical_execution_id=logical_execution_id,
        side=side,
        signature=signature,
        success=True,
        token_delta_raw=token_amount,
        quote_delta_raw=quote_amount,
        network_fee_lamports=network_fee,
        priority_fee_lamports=priority_fee_lamports,
        protocol_fee_raw=protocol_fee,
        creator_fee_raw=creator_fee,
        rent_lamports=rent,
        slot=transaction.get("slot"),
        delivery_tip_lamports=delivery_tip_lamports,
        unknown_costs=tuple(sorted(set(unknown))),
    )


def _owned_balances(
    balances: list[dict[str, Any]], owner: str, mint: str
) -> dict[int, int]:
    result = {}
    for balance in balances:
        if balance.get("owner") != owner or balance.get("mint") != mint:
            continue
        result[int(balance["accountIndex"])] = int(
            balance.get("uiTokenAmount", {}).get("amount", 0)
        )
    return result


def _event_int(event: dict[str, Any] | None, name: str) -> int | None:
    if event is None:
        return None
    fields = event.get("fields", event)
    value = fields.get(name)
    return int(value) if value is not None else None


def _derive_rent(
    transaction: dict[str, Any],
    user: Pubkey,
    side: ExecutionSide,
    quote_mint: Pubkey,
    quote_amount: int | None,
    network_fee: int | None,
    delivery_tip_lamports: int,
) -> int | None:
    if network_fee is None:
        return None
    message = transaction.get("transaction", {}).get("message", {})
    account_keys = message.get("accountKeys", [])
    user_index = None
    for index, key in enumerate(account_keys):
        key_text = key if isinstance(key, str) else key.get("pubkey")
        if key_text == str(user):
            user_index = index
            break
    pre = transaction.get("meta", {}).get("preBalances", [])
    post = transaction.get("meta", {}).get("postBalances", [])
    if user_index is None or user_index >= len(pre) or user_index >= len(post):
        return None
    native_delta = int(post[user_index]) - int(pre[user_index])
    if not is_sol_paired(quote_mint):
        return -native_delta - network_fee - delivery_tip_lamports
    if quote_amount is None:
        return None
    if side == ExecutionSide.BUY:
        return -native_delta - quote_amount - network_fee - delivery_tip_lamports
    return quote_amount - network_fee - native_delta - delivery_tip_lamports
