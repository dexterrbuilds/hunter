"""Pump.fun create_v2 instructions isolated from the trading builders."""

# solders AccountMeta uses positional signer/writable booleans.
# ruff: noqa: FBT003, PLR0913, TC001

from __future__ import annotations

import struct
from dataclasses import dataclass

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from spl.token.instructions import get_associated_token_address

from core.pubkeys import SystemAddresses
from platforms.pumpfun.address_provider import PumpFunAddresses
from utils.idl_parser import IDLParser

MINT_AUTHORITY = Pubkey.from_string("TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM")
MAYHEM_PROGRAM = Pubkey.from_string("MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e")


@dataclass(frozen=True, slots=True)
class PumpFunLaunchAddresses:
    bonding_curve: Pubkey
    associated_bonding_curve: Pubkey
    global_params: Pubkey
    sol_vault: Pubkey
    mayhem_state: Pubkey
    mayhem_token_vault: Pubkey


class PumpFunLaunchInstructionBuilder:
    """Build create_v2 and extend_account exactly from the vendored IDL."""

    def __init__(self, idl_parser: IDLParser) -> None:
        discriminators = idl_parser.get_instruction_discriminators()
        self._create_v2 = discriminators["create_v2"]
        self._extend_account = discriminators["extend_account"]

    @staticmethod
    def derive_addresses(mint: Pubkey) -> PumpFunLaunchAddresses:
        curve, _ = Pubkey.find_program_address(
            [b"bonding-curve", bytes(mint)], PumpFunAddresses.PROGRAM
        )
        associated_curve = get_associated_token_address(
            curve, mint, SystemAddresses.TOKEN_2022_PROGRAM
        )
        global_params, _ = Pubkey.find_program_address(
            [b"global-params"], MAYHEM_PROGRAM
        )
        sol_vault, _ = Pubkey.find_program_address([b"sol-vault"], MAYHEM_PROGRAM)
        mayhem_state, _ = Pubkey.find_program_address(
            [b"mayhem-state", bytes(mint)], MAYHEM_PROGRAM
        )
        mayhem_token_vault = get_associated_token_address(
            sol_vault, mint, SystemAddresses.TOKEN_2022_PROGRAM
        )
        return PumpFunLaunchAddresses(
            curve,
            associated_curve,
            global_params,
            sol_vault,
            mayhem_state,
            mayhem_token_vault,
        )

    @staticmethod
    def _string(value: str) -> bytes:
        encoded = value.encode("utf-8")
        return struct.pack("<I", len(encoded)) + encoded

    def build_create_v2(
        self,
        *,
        mint: Pubkey,
        user: Pubkey,
        creator: Pubkey,
        name: str,
        symbol: str,
        uri: str,
        is_mayhem_mode: bool,
        is_cashback_enabled: bool,
    ) -> Instruction:
        addresses = self.derive_addresses(mint)
        accounts = [
            AccountMeta(mint, True, True),
            AccountMeta(MINT_AUTHORITY, False, False),
            AccountMeta(addresses.bonding_curve, False, True),
            AccountMeta(addresses.associated_bonding_curve, False, True),
            AccountMeta(PumpFunAddresses.GLOBAL, False, False),
            AccountMeta(user, True, True),
            AccountMeta(SystemAddresses.SYSTEM_PROGRAM, False, False),
            AccountMeta(SystemAddresses.TOKEN_2022_PROGRAM, False, False),
            AccountMeta(SystemAddresses.ASSOCIATED_TOKEN_PROGRAM, False, False),
            AccountMeta(MAYHEM_PROGRAM, False, True),
            AccountMeta(addresses.global_params, False, False),
            AccountMeta(addresses.sol_vault, False, True),
            AccountMeta(addresses.mayhem_state, False, True),
            AccountMeta(addresses.mayhem_token_vault, False, True),
            AccountMeta(PumpFunAddresses.EVENT_AUTHORITY, False, False),
            AccountMeta(PumpFunAddresses.PROGRAM, False, False),
        ]
        data = (
            self._create_v2
            + self._string(name)
            + self._string(symbol)
            + self._string(uri)
            + bytes(creator)
            + struct.pack("<?", is_mayhem_mode)
            + struct.pack("<?", is_cashback_enabled)
        )
        return Instruction(PumpFunAddresses.PROGRAM, data, accounts)

    def build_extend_account(self, *, mint: Pubkey, user: Pubkey) -> Instruction:
        addresses = self.derive_addresses(mint)
        return Instruction(
            PumpFunAddresses.PROGRAM,
            self._extend_account,
            [
                AccountMeta(addresses.bonding_curve, False, True),
                AccountMeta(user, True, False),
                AccountMeta(SystemAddresses.SYSTEM_PROGRAM, False, False),
                AccountMeta(PumpFunAddresses.EVENT_AUTHORITY, False, False),
                AccountMeta(PumpFunAddresses.PROGRAM, False, False),
            ],
        )

    def build_create_transaction_instructions(
        self, **values: object
    ) -> list[Instruction]:
        """Return create + curve extension; buy_v2 remains a second component."""
        create = self.build_create_v2(**values)  # type: ignore[arg-type]
        return [
            create,
            self.build_extend_account(
                mint=values["mint"],  # type: ignore[arg-type]
                user=values["user"],  # type: ignore[arg-type]
            ),
        ]
