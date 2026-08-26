from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from core.pubkeys import USDC_MINT, WSOL_MINT  # noqa: E402
from domain.quotes import ExecutionSide  # noqa: E402
from execution.effects import owned_token_delta, parse_execution_result  # noqa: E402

USER = Pubkey.new_unique()
TOKEN = Pubkey.new_unique()


def token_balance(index, owner, mint, amount):
    return {
        "accountIndex": index,
        "owner": str(owner),
        "mint": str(mint),
        "uiTokenAmount": {"amount": str(amount)},
    }


def transaction(
    *, pre_lamports, post_lamports, pre_tokens=(), post_tokens=(), fee=5000, error=None
):
    return {
        "slot": 123,
        "transaction": {"message": {"accountKeys": [str(USER)]}},
        "meta": {
            "err": error,
            "fee": fee,
            "preBalances": [pre_lamports],
            "postBalances": [post_lamports],
            "preTokenBalances": list(pre_tokens),
            "postTokenBalances": list(post_tokens),
        },
    }


EVENT = {
    "fields": {
        "quote_amount": 100_000,
        "fee": 1_000,
        "creator_fee": 500,
        "buyback_fee": 100,
        "cashback": 50,
    }
}


class ExecutionEffectsTests(unittest.TestCase):
    def test_owned_balance_delta_sums_accounts(self):
        meta = transaction(
            pre_lamports=0,
            post_lamports=0,
            pre_tokens=[token_balance(1, USER, TOKEN, 10)],
            post_tokens=[
                token_balance(1, USER, TOKEN, 20),
                token_balance(2, USER, TOKEN, 5),
            ],
        )["meta"]
        self.assertEqual(owned_token_delta(meta, USER, TOKEN), 15)

    def test_sol_buy_parses_trade_fees_network_fee_and_rent(self):
        quote_spent = 101_550
        rent = 2_039_280
        tx = transaction(
            pre_lamports=10_000_000,
            post_lamports=10_000_000 - quote_spent - 5_000 - rent,
            post_tokens=[token_balance(1, USER, TOKEN, 2_000_000)],
        )
        result = parse_execution_result(
            logical_execution_id="buy",
            side=ExecutionSide.BUY,
            signature="sig",
            transaction=tx,
            user=USER,
            token_mint=TOKEN,
            quote_mint=WSOL_MINT,
            trade_event=EVENT,
            priority_fee_lamports=1000,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.token_delta_raw, 2_000_000)
        self.assertEqual(result.quote_delta_raw, quote_spent)
        self.assertEqual(result.protocol_fee_raw, 1_000)
        self.assertEqual(result.creator_fee_raw, 500)
        self.assertEqual(result.network_fee_lamports, 5_000)
        self.assertEqual(result.rent_lamports, rent)

    def test_delivery_tip_is_separate_from_rent(self):
        quote_spent = 101_550
        rent = 2_039_280
        delivery_tip = 1_000
        tx = transaction(
            pre_lamports=10_000_000,
            post_lamports=(10_000_000 - quote_spent - 5_000 - rent - delivery_tip),
            post_tokens=[token_balance(1, USER, TOKEN, 2_000_000)],
        )
        result = parse_execution_result(
            logical_execution_id="buy",
            side=ExecutionSide.BUY,
            signature="sig",
            transaction=tx,
            user=USER,
            token_mint=TOKEN,
            quote_mint=WSOL_MINT,
            trade_event=EVENT,
            priority_fee_lamports=1_000,
            delivery_tip_lamports=delivery_tip,
        )
        self.assertEqual(result.rent_lamports, rent)
        self.assertEqual(result.delivery_tip_lamports, delivery_tip)

    def test_sol_sell_parses_actual_proceeds(self):
        proceeds = 98_450
        tx = transaction(
            pre_lamports=1_000_000,
            post_lamports=1_000_000 + proceeds - 5_000,
            pre_tokens=[token_balance(1, USER, TOKEN, 2_000_000)],
            post_tokens=[token_balance(1, USER, TOKEN, 0)],
        )
        result = parse_execution_result(
            logical_execution_id="sell",
            side=ExecutionSide.SELL,
            signature="sig",
            transaction=tx,
            user=USER,
            token_mint=TOKEN,
            quote_mint=WSOL_MINT,
            trade_event=EVENT,
        )
        self.assertEqual(result.token_delta_raw, 2_000_000)
        self.assertEqual(result.quote_delta_raw, proceeds)
        self.assertEqual(result.rent_lamports, 0)

    def test_sell_ata_rent_refund_is_a_negative_native_cost(self):
        proceeds = 98_450
        rent_refund = 2_039_280
        tx = transaction(
            pre_lamports=1_000_000,
            post_lamports=1_000_000 + proceeds - 5_000 + rent_refund,
            pre_tokens=[token_balance(1, USER, TOKEN, 2_000_000)],
            post_tokens=[],
        )
        result = parse_execution_result(
            logical_execution_id="sell",
            side=ExecutionSide.SELL,
            signature="sig",
            transaction=tx,
            user=USER,
            token_mint=TOKEN,
            quote_mint=WSOL_MINT,
            trade_event=EVENT,
        )
        self.assertEqual(result.rent_lamports, -rent_refund)

    def test_spl_quote_uses_owner_balance_changes(self):
        tx = transaction(
            pre_lamports=2_000_000,
            post_lamports=1_995_000,
            pre_tokens=[token_balance(2, USER, USDC_MINT, 5_000_000)],
            post_tokens=[
                token_balance(1, USER, TOKEN, 2_000_000),
                token_balance(2, USER, USDC_MINT, 4_000_000),
            ],
        )
        result = parse_execution_result(
            logical_execution_id="buy",
            side=ExecutionSide.BUY,
            signature="sig",
            transaction=tx,
            user=USER,
            token_mint=TOKEN,
            quote_mint=USDC_MINT,
            trade_event=EVENT,
        )
        self.assertEqual(result.quote_delta_raw, 1_000_000)
        self.assertEqual(result.rent_lamports, 0)

    def test_failed_transaction_returns_no_economic_effects(self):
        result = parse_execution_result(
            logical_execution_id="x",
            side=ExecutionSide.BUY,
            signature="sig",
            transaction=transaction(
                pre_lamports=1, post_lamports=0, error={"InstructionError": [1, 2]}
            ),
            user=USER,
            token_mint=TOKEN,
            quote_mint=WSOL_MINT,
        )
        self.assertFalse(result.success)
        self.assertIsNone(result.token_delta_raw)
        self.assertIsNotNone(result.error)

    def test_missing_native_event_is_explicitly_unknown(self):
        result = parse_execution_result(
            logical_execution_id="x",
            side=ExecutionSide.BUY,
            signature="sig",
            transaction=transaction(pre_lamports=100, post_lamports=90),
            user=USER,
            token_mint=TOKEN,
            quote_mint=WSOL_MINT,
        )
        self.assertIsNone(result.quote_delta_raw)
        self.assertIn("native_trade_amount_or_protocol_fees", result.unknown_costs)


if __name__ == "__main__":
    unittest.main()
