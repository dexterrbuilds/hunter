from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from core.pubkeys import USDC_MINT, WSOL_MINT  # noqa: E402
from domain.amounts import BasisPoints, QuoteAmountRaw, TokenAmountRaw  # noqa: E402
from domain.quotes import (  # noqa: E402
    CurveState,
    FeeRates,
    FeeSchedule,
    FeeTier,
    bonding_curve_market_cap,
    fee_amount,
    gross_buy_tokens,
    gross_sell_quote,
    quote_buy,
    quote_sell,
)

TOKEN = Pubkey.new_unique()


def curve(quote=WSOL_MINT, quote_decimals=9, **overrides):
    values = dict(
        token_mint=TOKEN,
        quote_mint=quote,
        token_decimals=6,
        quote_decimals=quote_decimals,
        virtual_token_reserves=1_000_000_000_000,
        virtual_quote_reserves=30_000_000_000,
        real_token_reserves=800_000_000_000,
        token_total_supply=1_000_000_000_000_000,
        creator_present=True,
    )
    values.update(overrides)
    return CurveState(**values)


RATES = FeeRates(BasisPoints(100), BasisPoints(50), source="fixture")


class CurveQuoteTests(unittest.TestCase):
    def test_market_cap_integer_formula(self):
        self.assertEqual(
            bonding_curve_market_cap(
                mint_supply=1_000_000_000_000_000,
                virtual_quote_reserves=30_000_000_000,
                virtual_token_reserves=1_000_000_000_000,
            ),
            30_000_000_000_000,
        )

    def test_fee_components_round_up_separately(self):
        self.assertEqual(fee_amount(1, BasisPoints(1)), 1)
        self.assertEqual(fee_amount(10_000, BasisPoints(1)), 1)

    def test_gross_buy_constant_product(self):
        self.assertEqual(
            gross_buy_tokens(100_000_000, 1_000_000_000_000, 30_000_000_000),
            3_322_259_136,
        )

    def test_gross_sell_constant_product(self):
        self.assertEqual(
            gross_sell_quote(10_000_000, 1_000_000_000_000, 30_000_000_000),
            299_997,
        )

    def test_buy_quote_matches_official_sdk_ordering(self):
        state = curve()
        spend = QuoteAmountRaw(100_000_000, WSOL_MINT, 9)
        quote = quote_buy(
            spend=spend,
            curve=state,
            fee_rates=RATES,
            slippage=BasisPoints(3000),
        )
        sdk_curve_input = (100_000_000 - 1) * 10_000 // 10_150
        expected_tokens = (
            sdk_curve_input * 1_000_000_000_000 // (30_000_000_000 + sdk_curve_input)
        )
        self.assertEqual(quote.expected_output.value, expected_tokens)
        self.assertEqual(quote.minimum_output.value, expected_tokens * 7000 // 10000)
        self.assertEqual(quote.maximum_input.value, 130_000_000)

    def test_buy_quote_caps_real_reserves(self):
        state = curve(real_token_reserves=10)
        result = quote_buy(
            spend=QuoteAmountRaw(100_000_000, WSOL_MINT, 9),
            curve=state,
            fee_rates=RATES,
            slippage=BasisPoints(0),
        )
        self.assertEqual(result.expected_output.value, 10)

    def test_sell_quote_subtracts_fees_before_slippage(self):
        state = curve()
        result = quote_sell(
            tokens=TokenAmountRaw(10_000_000, TOKEN, 6),
            curve=state,
            fee_rates=RATES,
            slippage=BasisPoints(2500),
        )
        gross = 299_997
        protocol = (gross * 100 + 9_999) // 10_000
        creator = (gross * 50 + 9_999) // 10_000
        net = gross - protocol - creator
        self.assertEqual(result.gross_output.value, gross)
        self.assertEqual(result.protocol_fee_raw, protocol)
        self.assertEqual(result.creator_fee_raw, creator)
        self.assertEqual(result.expected_output.value, net)
        self.assertEqual(result.minimum_output.value, net * 7500 // 10000)

    def test_sell_quote_accounts_for_price_impact(self):
        state = curve()
        small = quote_sell(
            tokens=TokenAmountRaw(1_000_000, TOKEN, 6),
            curve=state,
            fee_rates=FeeRates(BasisPoints(0), BasisPoints(0)),
            slippage=BasisPoints(0),
        )
        large = quote_sell(
            tokens=TokenAmountRaw(100_000_000_000, TOKEN, 6),
            curve=state,
            fee_rates=FeeRates(BasisPoints(0), BasisPoints(0)),
            slippage=BasisPoints(0),
        )
        self.assertGreater(
            small.expected_output.value / small.input.value,
            large.expected_output.value / large.input.value,
        )

    def test_fee_tier_selects_highest_reached_threshold(self):
        state = curve()
        schedule = FeeSchedule(
            (
                FeeTier(0, FeeRates(BasisPoints(100), BasisPoints(10), source="low")),
                FeeTier(
                    20_000_000_000_000,
                    FeeRates(BasisPoints(80), BasisPoints(20), source="high"),
                ),
            )
        )
        self.assertEqual(schedule.select(state).source, "high")

    def test_buy_budget_uses_actual_supply_but_trade_fee_uses_standard_supply(self):
        state = curve(token_total_supply=100_000_000_000_000)
        schedule = FeeSchedule(
            (
                FeeTier(0, FeeRates(BasisPoints(100), BasisPoints(10), source="low")),
                FeeTier(
                    10_000_000_000_000,
                    FeeRates(BasisPoints(80), BasisPoints(20), source="high"),
                ),
            )
        )
        self.assertEqual(schedule.select_for_buy_budget(state).source, "low")
        self.assertEqual(schedule.select(state).source, "high")

    def test_buy_quote_can_preserve_distinct_budget_and_trade_fee_rates(self):
        result = quote_buy(
            spend=QuoteAmountRaw(100_000_000, WSOL_MINT, 9),
            curve=curve(),
            fee_rates=FeeRates(BasisPoints(100), BasisPoints(50), source="budget"),
            trade_fee_rates=FeeRates(BasisPoints(80), BasisPoints(20), source="trade"),
            slippage=BasisPoints(100),
        )
        self.assertEqual(result.fee_rates.source, "budget")
        self.assertEqual(result.trade_fee_rates.source, "trade")
        self.assertEqual(
            result.protocol_fee_raw,
            fee_amount(result.curve_input_raw, BasisPoints(80)),
        )

    def test_non_creator_curve_disables_creator_fee(self):
        selected = FeeSchedule((FeeTier(0, RATES),)).select(
            curve(creator_present=False)
        )
        self.assertEqual(selected.creator_fee_bps.value, 0)

    def test_usdc_quote_preserves_six_decimals(self):
        state = curve(
            quote=USDC_MINT,
            quote_decimals=6,
            virtual_quote_reserves=30_000_000,
        )
        result = quote_sell(
            tokens=TokenAmountRaw(10_000_000, TOKEN, 6),
            curve=state,
            fee_rates=RATES,
            slippage=BasisPoints(100),
        )
        self.assertEqual(result.expected_output.decimals, 6)
        self.assertEqual(result.expected_output.mint, USDC_MINT)

    def test_mismatched_amount_identity_fails(self):
        with self.assertRaises(ValueError):
            quote_sell(
                tokens=TokenAmountRaw(1, Pubkey.new_unique(), 6),
                curve=curve(),
                fee_rates=RATES,
                slippage=BasisPoints(0),
            )


if __name__ == "__main__":
    unittest.main()
