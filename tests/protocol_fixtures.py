"""Deterministic Pump.fun fixtures shared by offline tests."""

from __future__ import annotations

from solders.pubkey import Pubkey

from core.pubkeys import SystemAddresses, WSOL_MINT
from interfaces.core import Platform, TokenInfo
from platforms.pumpfun.address_provider import PumpFunAddressProvider

MINT = Pubkey.from_string("CU7nUQaJ4beyYjC3xAUrh5RiSjw14fhU6oWTwRBse8gj")
CREATOR = Pubkey.from_string("5wyFsNExysbXf2hTtcn8Tqd3urs9Nv85Zx1zNdAfTMmX")
USER = Pubkey.from_string("Ba99j1dYxidfQZvuNGMaXGxJsUeWXu6VNW8damkrdLVd")


def token_info(
    quote_mint: Pubkey = WSOL_MINT,
    *,
    token_program: Pubkey = SystemAddresses.TOKEN_2022_PROGRAM,
    state_from_event: bool = True,
) -> TokenInfo:
    """Build deterministic, network-free Pump.fun token state."""
    provider = PumpFunAddressProvider()
    curve = provider.derive_pool_address(MINT)
    return TokenInfo(
        name="Hunter fixture",
        symbol="HUNT",
        uri="https://example.invalid/token.json",
        mint=MINT,
        platform=Platform.PUMP_FUN,
        bonding_curve=curve,
        associated_bonding_curve=provider.derive_associated_bonding_curve(
            MINT, curve, token_program
        ),
        creator=CREATOR,
        creator_vault=provider.derive_creator_vault(CREATOR),
        token_program_id=token_program,
        quote_mint=quote_mint,
        state_from_event=state_from_event,
    )
