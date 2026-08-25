"""Characterization tests for the audited Pump.fun V2 protocol behavior."""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import sys
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from solders.pubkey import Pubkey  # noqa: E402
from spl.token.instructions import get_associated_token_address  # noqa: E402

from core.pubkeys import (  # noqa: E402
    USDC_MINT,
    WSOL_MINT,
    SystemAddresses,
    normalize_quote_mint,
    quote_token_program,
)
from interfaces.core import Platform, TokenInfo  # noqa: E402
from platforms.pumpfun.address_provider import (  # noqa: E402
    PumpFunAddresses,
    PumpFunAddressProvider,
)
from platforms.pumpfun.curve_manager import PumpFunCurveManager  # noqa: E402
from platforms.pumpfun.event_parser import PumpFunEventParser  # noqa: E402
from platforms.pumpfun.instruction_builder import (  # noqa: E402
    _BUY_V2_ACCOUNTS,
    _SELL_V2_ACCOUNTS,
    PumpFunInstructionBuilder,
)
from protocol_fixtures import CREATOR, MINT, USER, token_info  # noqa: E402
from utils.idl_manager import get_idl_manager  # noqa: E402


class PumpFunProtocolTests(unittest.TestCase):
    """Lock the protocol-sensitive account, data, and decoding contracts."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.parser = get_idl_manager().get_parser(Platform.PUMP_FUN)
        cls.provider = PumpFunAddressProvider()
        cls.builder = PumpFunInstructionBuilder(cls.parser)

    def test_v2_layout_matches_vendored_idl_exactly(self) -> None:
        idl = json.loads((ROOT / "idl" / "pump_fun_idl.json").read_text())
        instructions = {item["name"]: item for item in idl["instructions"]}

        for name, layout in (
            ("buy_v2", _BUY_V2_ACCOUNTS),
            ("sell_v2", _SELL_V2_ACCOUNTS),
        ):
            expected = [
                (account["name"], bool(account.get("writable")))
                for account in instructions[name]["accounts"]
            ]
            self.assertEqual(layout, expected)
            signer_names = [
                account["name"]
                for account in instructions[name]["accounts"]
                if account.get("signer")
            ]
            self.assertEqual(signer_names, ["user"])

    def test_v2_discriminators_are_stable(self) -> None:
        discriminators = self.parser.get_instruction_discriminators()

        self.assertEqual(discriminators["buy_v2"].hex(), "b817ee6167c5d33d")
        self.assertEqual(discriminators["sell_v2"].hex(), "5df6823ce7e940b2")

    def test_pda_and_token_2022_ata_derivations_are_stable(self) -> None:
        curve = self.provider.derive_pool_address(MINT)
        creator_vault = self.provider.derive_creator_vault(CREATOR)
        curve_ata = self.provider.derive_associated_bonding_curve(
            MINT, curve, SystemAddresses.TOKEN_2022_PROGRAM
        )
        user_ata = self.provider.derive_user_token_account(
            USER, MINT, SystemAddresses.TOKEN_2022_PROGRAM
        )

        self.assertEqual(str(curve), "7TW1gobQyM7WoigqNa4Dvc2SoPep1XwmPpG5xRcGzQTC")
        self.assertEqual(
            str(curve_ata), "5cVKd4dbkAtPAQfshz7dg4BWAjnskhUp3V8USyjFnCgm"
        )
        self.assertEqual(
            str(user_ata), "AnE8qsUMtiqV8MSKQ2HaL8FyaXWoE2zvv2TTJNB2hK7J"
        )
        self.assertEqual(
            str(creator_vault), "TRLWXxwxev8WTVpryXqHzgQwQfoaihRPKpxArqESeSs"
        )

    def test_buy_and_sell_v2_encoding_account_flags_and_order(self) -> None:
        with patch(
            "platforms.pumpfun.address_provider.secrets.choice",
            side_effect=lambda values: values[0],
        ):
            info = token_info()
            buy = asyncio.run(
                self.builder.build_buy_v2_instruction(
                    info, USER, 1_500_000, 20_000_000, self.provider
                )
            )
            sell = asyncio.run(
                self.builder.build_sell_v2_instruction(
                    info, USER, 20_000_000, 900_000, self.provider
                )
            )
            buy_accounts = self.provider.get_buy_v2_instruction_accounts(info, USER)
            sell_accounts = self.provider.get_sell_v2_instruction_accounts(info, USER)

        self.assertEqual(len(buy), 2)
        self.assertEqual(len(sell), 1)
        buy_trade = buy[-1]
        sell_trade = sell[-1]
        self.assertEqual(buy_trade.program_id, PumpFunAddresses.PROGRAM)
        self.assertEqual(sell_trade.program_id, PumpFunAddresses.PROGRAM)
        self.assertEqual(
            bytes(buy_trade.data),
            bytes.fromhex("b817ee6167c5d33d")
            + struct.pack("<QQ", 20_000_000, 1_500_000),
        )
        self.assertEqual(
            bytes(sell_trade.data),
            bytes.fromhex("5df6823ce7e940b2") + struct.pack("<QQ", 20_000_000, 900_000),
        )

        for instruction, layout, resolved in (
            (buy_trade, _BUY_V2_ACCOUNTS, buy_accounts),
            (sell_trade, _SELL_V2_ACCOUNTS, sell_accounts),
        ):
            self.assertEqual(
                [meta.pubkey for meta in instruction.accounts],
                [resolved[name] for name, _ in layout],
            )
            self.assertEqual(
                [meta.is_writable for meta in instruction.accounts],
                [writable for _, writable in layout],
            )
            self.assertEqual(
                [meta.pubkey for meta in instruction.accounts if meta.is_signer],
                [USER],
            )

    def test_quote_token_behavior_for_sol_and_usdc(self) -> None:
        with patch(
            "platforms.pumpfun.address_provider.secrets.choice",
            side_effect=lambda values: values[0],
        ):
            sol_buy = asyncio.run(
                self.builder.build_buy_v2_instruction(
                    token_info(WSOL_MINT), USER, 1, 2, self.provider
                )
            )
            usdc_info = token_info(USDC_MINT)
            usdc_buy = asyncio.run(
                self.builder.build_buy_v2_instruction(
                    usdc_info, USER, 1, 2, self.provider
                )
            )
            usdc_sell = asyncio.run(
                self.builder.build_sell_v2_instruction(
                    usdc_info, USER, 2, 1, self.provider
                )
            )

        self.assertEqual(len(sol_buy), 2)
        self.assertEqual(len(usdc_buy), 3)
        self.assertEqual(len(usdc_sell), 2)
        self.assertEqual(
            normalize_quote_mint(SystemAddresses.DEFAULT_PUBKEY), WSOL_MINT
        )
        self.assertEqual(quote_token_program(USDC_MINT), SystemAddresses.TOKEN_PROGRAM)

        expected_quote_ata = get_associated_token_address(
            USER, USDC_MINT, SystemAddresses.TOKEN_PROGRAM
        )
        self.assertIn(
            expected_quote_ata, [meta.pubkey for meta in usdc_buy[1].accounts]
        )

    def test_bonding_curve_fixture_decodes_with_current_layout(self) -> None:
        fixture_path = (
            ROOT
            / "learning-examples"
            / "raw_bonding_curve_from_getaccountinfo.json"
        )
        fixture = json.loads(fixture_path.read_text())
        raw = base64.b64decode(fixture["result"]["value"]["data"][0])
        manager = PumpFunCurveManager(None, self.parser)

        state = manager._decode_curve_state_with_idl(raw)

        self.assertEqual(state["virtual_token_reserves"], 1_045_502_991_298_183)
        self.assertEqual(state["virtual_quote_reserves"], 30_789_008_086)
        self.assertEqual(state["real_token_reserves"], 765_602_991_298_183)
        self.assertEqual(state["real_quote_reserves"], 789_008_086)
        self.assertFalse(state["complete"])
        self.assertFalse(state["is_mayhem_mode"])
        self.assertTrue(state["is_cashback_coin"])
        self.assertEqual(state["quote_mint"], WSOL_MINT)
        self.assertAlmostEqual(state["price_per_token"], 2.944899090893066e-08)

    def test_create_event_fixture_parses_authoritative_token_2022_state(self) -> None:
        fixture_path = (
            ROOT / "learning-examples" / "raw_create_tx_from_gettransaction.json"
        )
        fixture = json.loads(fixture_path.read_text())["result"]
        parser = PumpFunEventParser(self.parser)

        info = parser.parse_token_creation_from_logs(
            fixture["meta"]["logMessages"], "offline-fixture"
        )

        self.assertIsNotNone(info)
        info = cast(TokenInfo, info)
        self.assertEqual(str(info.mint), "CWiTGbCiDd8BKtNYNE2boT9fJGFG2MU53Uf6HQk4pump")
        self.assertEqual(info.token_program_id, SystemAddresses.TOKEN_2022_PROGRAM)
        self.assertEqual(info.quote_mint, WSOL_MINT)
        self.assertEqual(info.quote_token_program_id, SystemAddresses.TOKEN_PROGRAM)
        self.assertTrue(info.state_from_event)
        self.assertTrue(info.is_cashback_coin)


if __name__ == "__main__":
    unittest.main()
