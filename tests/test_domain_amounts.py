from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from domain.amounts import (  # noqa: E402
    BasisPoints,
    MicroLamportsPerCU,
    QuoteAmountRaw,
    RoundingDirection,
    TokenAmountRaw,
    ceil_div,
    decimal_to_raw,
    maximum_after_slippage,
    minimum_after_slippage,
    priority_fee_lamports_ceiling,
    raw_to_decimal,
)

MINT = Pubkey.new_unique()


class RawAmountTests(unittest.TestCase):
    def test_down_rounding_is_explicit(self):
        self.assertEqual(
            decimal_to_raw("1.2345678", decimals=6, rounding=RoundingDirection.DOWN),
            1_234_567,
        )

    def test_up_rounding_is_explicit(self):
        self.assertEqual(
            decimal_to_raw("0.0000001", decimals=6, rounding=RoundingDirection.UP),
            1,
        )

    def test_nearest_even_boundary(self):
        self.assertEqual(
            decimal_to_raw(
                Decimal("1.2345665"),
                decimals=6,
                rounding=RoundingDirection.NEAREST_EVEN,
            ),
            1_234_566,
        )

    def test_binary_float_is_rejected(self):
        with self.assertRaises(TypeError):
            decimal_to_raw(0.1, decimals=9, rounding=RoundingDirection.DOWN)

    def test_decimals_are_never_implicit(self):
        with self.assertRaises(TypeError):
            decimal_to_raw("1", rounding=RoundingDirection.DOWN)

    def test_raw_round_trip_is_exact(self):
        amount = QuoteAmountRaw.from_decimal(
            "12.345678",
            mint=MINT,
            decimals=6,
            rounding=RoundingDirection.DOWN,
        )
        self.assertEqual(amount.value, 12_345_678)
        self.assertEqual(amount.to_decimal(), Decimal("12.345678"))
        self.assertEqual(raw_to_decimal(amount.value, decimals=6), amount.to_decimal())

    def test_token_identity_and_decimals_are_carried(self):
        amount = TokenAmountRaw(10, MINT, 6)
        self.assertEqual((amount.mint, amount.decimals), (MINT, 6))

    def test_invalid_raw_values_fail(self):
        with self.assertRaises(ValueError):
            TokenAmountRaw(-1, MINT, 6)
        with self.assertRaises(ValueError):
            QuoteAmountRaw(2**64, MINT, 6)

    def test_slippage_rounding_boundaries(self):
        bps = BasisPoints(1)
        self.assertEqual(minimum_after_slippage(1, bps), 0)
        self.assertEqual(maximum_after_slippage(1, bps), 2)
        self.assertEqual(minimum_after_slippage(10_001, bps), 9_999)

    def test_basis_points_range(self):
        self.assertEqual(BasisPoints(10_000).value, 10_000)
        with self.assertRaises(ValueError):
            BasisPoints(10_001)

    def test_ceil_div(self):
        self.assertEqual(ceil_div(0, 10), 0)
        self.assertEqual(ceil_div(1, 10), 1)
        self.assertEqual(ceil_div(11, 10), 2)

    def test_priority_fee_rounds_up_to_lamport(self):
        self.assertEqual(
            priority_fee_lamports_ceiling(MicroLamportsPerCU(1), 1).value, 1
        )
        self.assertEqual(
            priority_fee_lamports_ceiling(MicroLamportsPerCU(200_000), 180_000).value,
            36_000,
        )


if __name__ == "__main__":
    unittest.main()
