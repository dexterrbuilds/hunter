from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from core.pubkeys import USDC_MINT, WSOL_MINT  # noqa: E402
from domain.accounting import BuyFill, PositionAccounting, SellFill  # noqa: E402

TOKEN = Pubkey.new_unique()


def position(quote=WSOL_MINT, quote_decimals=9):
    return PositionAccounting("p1", TOKEN, quote, 6, quote_decimals)


class PositionAccountingTests(unittest.TestCase):
    def test_full_profitable_sale(self):
        item = position()
        item.record_buy(BuyFill("buy", 100, 1000, 10, 4))
        pnl = item.record_sell(
            SellFill("sell", 100, 1500, 10, 3), quote_is_native_sol=True
        )
        self.assertEqual(pnl.gross_raw, 500)
        self.assertEqual(pnl.net_raw, 480)
        self.assertEqual(pnl.remaining_quantity_raw, 0)
        self.assertEqual(pnl.remaining_cost_basis_raw, 0)

    def test_full_losing_sale(self):
        item = position()
        item.record_buy(BuyFill("buy", 100, 1000, 10, 4))
        pnl = item.record_sell(
            SellFill("sell", 100, 800, 10, 3), quote_is_native_sol=True
        )
        self.assertEqual(pnl.gross_raw, -200)
        self.assertEqual(pnl.net_raw, -220)
        self.assertLess(pnl.return_bps, 0)

    def test_partial_sale_allocates_average_cost(self):
        item = position()
        item.record_buy(BuyFill("buy", 100, 1000, 100, 40))
        pnl = item.record_sell(
            SellFill("sell", 25, 400, 10, 3), quote_is_native_sol=True
        )
        self.assertEqual(pnl.allocated_cost_basis_raw, 250)
        self.assertEqual(pnl.gross_raw, 150)
        self.assertEqual(pnl.net_raw, 115)
        self.assertEqual(pnl.remaining_cost_basis_raw, 750)
        self.assertEqual(pnl.remaining_quantity_raw, 75)

    def test_multiple_partial_sales_preserve_all_cost_basis(self):
        item = position()
        item.record_buy(BuyFill("buy", 100, 1001, 101, 50))
        first = item.record_sell(
            SellFill("s1", 33, 500, 1, 1), quote_is_native_sol=True
        )
        second = item.record_sell(
            SellFill("s2", 33, 500, 1, 1), quote_is_native_sol=True
        )
        third = item.record_sell(
            SellFill("s3", 34, 500, 1, 1), quote_is_native_sol=True
        )
        self.assertEqual(
            first.allocated_cost_basis_raw
            + second.allocated_cost_basis_raw
            + third.allocated_cost_basis_raw,
            1001,
        )
        self.assertEqual(item.remaining_cost_basis_raw, 0)
        self.assertEqual(item.remaining_entry_cost_lamports, 0)

    def test_sale_cannot_exceed_inventory(self):
        item = position()
        item.record_buy(BuyFill("buy", 10, 100, 1, 1))
        with self.assertRaises(ValueError):
            item.record_sell(SellFill("sell", 11, 200, 1, 1), quote_is_native_sol=True)

    def test_usdc_network_cost_requires_conversion(self):
        item = position(USDC_MINT, 6)
        item.record_buy(BuyFill("buy", 100, 1000, 5000, 1000))
        pnl = item.record_sell(
            SellFill("sell", 100, 1500, 5000, 1000), quote_is_native_sol=False
        )
        self.assertEqual(pnl.gross_raw, 500)
        self.assertIsNone(pnl.net_raw)
        self.assertIn("native_fees_require_quote_conversion", pnl.unknown_costs)

    def test_usdc_zero_native_cost_has_known_net(self):
        item = position(USDC_MINT, 6)
        item.record_buy(BuyFill("buy", 100, 1000, 0, 0))
        pnl = item.record_sell(
            SellFill("sell", 100, 1500, 0, 0), quote_is_native_sol=False
        )
        self.assertEqual(pnl.net_raw, 500)

    def test_unknown_entry_fee_propagates(self):
        item = position()
        item.record_buy(BuyFill("buy", 100, 1000, None, None))
        pnl = item.record_sell(
            SellFill("sell", 100, 1500, 10, 2), quote_is_native_sol=True
        )
        self.assertIsNone(pnl.net_raw)
        self.assertIn("entry_network_fee", pnl.unknown_costs)

    def test_priority_fee_is_informational_not_double_counted(self):
        item = position()
        item.record_buy(BuyFill("buy", 100, 1000, 100, 90))
        pnl = item.record_sell(
            SellFill("sell", 100, 1500, 100, 90), quote_is_native_sol=True
        )
        self.assertEqual(pnl.net_raw, 300)
        self.assertEqual(item.entry_priority_fee_lamports, 90)
        self.assertEqual(item.exit_priority_fee_lamports, 90)


if __name__ == "__main__":
    unittest.main()
