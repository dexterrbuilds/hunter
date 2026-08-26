"""Characterization tests for current amount, slippage, curve, and PnL math."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from protocol_fixtures import USER, token_info  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.signature import Signature  # noqa: E402

from core.priority_fee.manager import PriorityFeeManager  # noqa: E402
from core.pubkeys import (  # noqa: E402
    USDC_MINT,
    WSOL_MINT,
    quote_decimals,
    quote_units_per_token,
)
from domain.amounts import BasisPoints, QuoteAmountRaw, TokenAmountRaw  # noqa: E402
from domain.quotes import (  # noqa: E402
    CurveState,
    FeeRates,
    FeeSchedule,
    quote_buy,
    quote_sell,
)
from execution.telemetry import priority_fee_lamports  # noqa: E402
from interfaces.core import Platform  # noqa: E402
from platforms.pumpfun.address_provider import PumpFunAddressProvider  # noqa: E402
from platforms.pumpfun.curve_manager import PumpFunCurveManager  # noqa: E402
from platforms.pumpfun.instruction_builder import (  # noqa: E402
    PumpFunInstructionBuilder,
)
from trading.platform_aware import PlatformAwareBuyer, PlatformAwareSeller  # noqa: E402
from trading.position import ExitReason, Position  # noqa: E402
from utils.idl_manager import get_idl_manager  # noqa: E402


class _FakeWallet:
    def __init__(self) -> None:
        self.keypair = Keypair.from_seed(bytes(range(32)))
        self.pubkey = self.keypair.pubkey()


class _FakePriorityFees:
    async def calculate_priority_fee(self, _accounts: list[Pubkey]) -> int:
        return 200_000


class _FakeClient:
    def __init__(self) -> None:
        self.transaction_kwargs: dict | None = None

    async def build_and_send_transaction(
        self, _instructions: list, _keypair: Keypair, **kwargs
    ):
        self.transaction_kwargs = kwargs
        return Signature.default()

    async def confirm_transaction(self, _signature: Signature) -> bool:
        return False

    @staticmethod
    def maximum_ata_rent_lamports(_instructions: list) -> int:
        return 0


class _CapturingBuilder:
    def __init__(self) -> None:
        self.buy_args: tuple[int, int] | None = None
        self.sell_args: tuple[int, int] | None = None

    async def build_buy_instruction(
        self, _info, _user, amount_in: int, minimum_amount_out: int, _provider
    ) -> list:
        self.buy_args = (amount_in, minimum_amount_out)
        return []

    async def build_sell_instruction(
        self, _info, _user, amount_in: int, minimum_amount_out: int, _provider
    ) -> list:
        self.sell_args = (amount_in, minimum_amount_out)
        return []

    def get_required_accounts_for_buy(self, _info, _user, _provider) -> list:
        return []

    def get_required_accounts_for_sell(self, _info, _user, _provider) -> list:
        return []

    def get_buy_compute_unit_limit(self, _override=None) -> int:
        return 180_000

    def get_sell_compute_unit_limit(self, _override=None) -> int:
        return 120_000


class _FakeCurveManager:
    async def get_pool_state(self, _pool_address, commitment=None) -> dict:
        return {
            "virtual_token_reserves": 1_000_000_000,
            "virtual_sol_reserves": 30_000_000_000,
            "quote_mint": WSOL_MINT,
            "is_mayhem_mode": False,
            "is_cashback_coin": False,
            "price_per_token": 0.00003,
        }


class _ExactPumpCurveManager(_FakeCurveManager):
    def __init__(self):
        self.state = CurveState(
            token_mint=token_info().mint,
            quote_mint=WSOL_MINT,
            token_decimals=6,
            quote_decimals=9,
            virtual_token_reserves=1_000_000_000_000,
            virtual_quote_reserves=30_000_000_000,
            real_token_reserves=800_000_000_000,
            token_total_supply=1_000_000_000_000_000,
            creator_present=True,
        )
        self.schedule = FeeSchedule(
            (), fallback=FeeRates(BasisPoints(100), BasisPoints(50))
        )

    async def get_pool_state_and_fee_schedule(self, _pool):
        return await self.get_pool_state(_pool), self.schedule

    async def get_fee_schedule(self, commitment=None):
        return self.schedule

    def curve_state(self, _pool_state, token_mint):
        return self.state


class AmountAndSlippageTests(unittest.TestCase):
    """Characterize the current float-to-integer transaction sizing."""

    @classmethod
    def setUpClass(cls) -> None:
        parser = get_idl_manager().get_parser(Platform.PUMP_FUN)
        cls.instruction_builder = PumpFunInstructionBuilder(parser)

    def test_raw_token_and_decimal_conversion_truncates(self) -> None:
        self.assertEqual(
            self.instruction_builder.calculate_token_amount_raw(1.2345678), 1_234_567
        )
        self.assertEqual(
            self.instruction_builder.calculate_token_amount_decimal(1_234_567),
            1.234567,
        )

    def test_quote_raw_amounts_and_decimals(self) -> None:
        self.assertEqual(quote_decimals(WSOL_MINT), 9)
        self.assertEqual(quote_decimals(USDC_MINT), 6)
        self.assertEqual(quote_units_per_token(WSOL_MINT), 1_000_000_000)
        self.assertEqual(quote_units_per_token(USDC_MINT), 1_000_000)
        self.assertEqual(int(0.0001 * quote_units_per_token(WSOL_MINT)), 100_000)
        self.assertEqual(int(1.25 * quote_units_per_token(USDC_MINT)), 1_250_000)

    def test_buy_slippage_arguments_match_current_execution_path(self) -> None:
        builder = _CapturingBuilder()
        client = _FakeClient()
        buyer = PlatformAwareBuyer(
            client=client,
            wallet=_FakeWallet(),
            priority_fee_manager=_FakePriorityFees(),
            amount=0.0001,
            slippage=0.3,
            max_retries=1,
            extreme_fast_token_amount=20,
            extreme_fast_mode=True,
        )
        implementations = SimpleNamespace(
            address_provider=PumpFunAddressProvider(),
            instruction_builder=builder,
            curve_manager=_FakeCurveManager(),
        )

        with patch(
            "trading.platform_aware.get_platform_implementations",
            return_value=implementations,
        ):
            result = asyncio.run(buyer.execute(token_info()))

        self.assertFalse(result.success)
        self.assertEqual(builder.buy_args, (130_000, 14_000_000))
        self.assertEqual(client.transaction_kwargs["priority_fee"], 200_000)
        self.assertEqual(client.transaction_kwargs["compute_unit_limit"], 180_000)

    def test_sell_slippage_arguments_match_current_execution_path(self) -> None:
        builder = _CapturingBuilder()
        client = _FakeClient()
        seller = PlatformAwareSeller(
            client=client,
            wallet=_FakeWallet(),
            priority_fee_manager=_FakePriorityFees(),
            slippage=0.3,
            max_retries=1,
        )
        implementations = SimpleNamespace(
            address_provider=PumpFunAddressProvider(),
            instruction_builder=builder,
            curve_manager=_FakeCurveManager(),
        )

        with patch(
            "trading.platform_aware.get_platform_implementations",
            return_value=implementations,
        ):
            result = asyncio.run(
                seller.execute(token_info(), token_amount=20, token_price=0.000005)
            )

        self.assertFalse(result.success)
        self.assertEqual(builder.sell_args, (20_000_000, 70_000))
        self.assertEqual(client.transaction_kwargs["compute_unit_limit"], 120_000)

    def test_regular_pump_buy_uses_exact_curve_quote(self) -> None:
        builder = _CapturingBuilder()
        client = _FakeClient()
        manager = _ExactPumpCurveManager()
        buyer = PlatformAwareBuyer(
            client=client,
            wallet=_FakeWallet(),
            priority_fee_manager=_FakePriorityFees(),
            amount=0.1,
            slippage=0.3,
            max_retries=1,
        )
        implementations = SimpleNamespace(
            address_provider=PumpFunAddressProvider(),
            instruction_builder=builder,
            curve_manager=manager,
        )
        expected = quote_buy(
            spend=QuoteAmountRaw(100_000_000, WSOL_MINT, 9),
            curve=manager.state,
            fee_rates=manager.schedule.select(manager.state),
            slippage=BasisPoints(3000),
        )
        with patch(
            "trading.platform_aware.get_platform_implementations",
            return_value=implementations,
        ):
            result = asyncio.run(buyer.execute(token_info()))
        self.assertFalse(result.success)
        self.assertEqual(
            builder.buy_args,
            (expected.maximum_input.value, expected.minimum_output.value),
        )

    def test_regular_pump_sell_uses_curve_output_not_reference_price(self) -> None:
        builder = _CapturingBuilder()
        client = _FakeClient()
        manager = _ExactPumpCurveManager()
        seller = PlatformAwareSeller(
            client=client,
            wallet=_FakeWallet(),
            priority_fee_manager=_FakePriorityFees(),
            slippage=0.3,
            max_retries=1,
        )
        implementations = SimpleNamespace(
            address_provider=PumpFunAddressProvider(),
            instruction_builder=builder,
            curve_manager=manager,
        )
        expected = quote_sell(
            tokens=TokenAmountRaw(20_000_000, token_info().mint, 6),
            curve=manager.state,
            fee_rates=manager.schedule.select(manager.state),
            slippage=BasisPoints(3000),
        )
        with patch(
            "trading.platform_aware.get_platform_implementations",
            return_value=implementations,
        ):
            result = asyncio.run(
                seller.execute(
                    token_info(),
                    token_amount=20,
                    token_price=999.0,
                )
            )
        self.assertFalse(result.success)
        self.assertEqual(
            builder.sell_args,
            (20_000_000, expected.minimum_output.value),
        )


class CurvePositionAndFeeTests(unittest.TestCase):
    """Characterize curve, position, compute-limit, and priority-fee arithmetic."""

    def test_bonding_curve_buy_and_sell_formulas(self) -> None:
        manager = PumpFunCurveManager(
            None, get_idl_manager().get_parser(Platform.PUMP_FUN)
        )

        async def pool_state(_address):
            return {
                "virtual_token_reserves": 1_000_000_000_000,
                "virtual_sol_reserves": 30_000_000_000,
            }

        manager.get_pool_state = pool_state
        pool = Pubkey.default()

        buy_out = asyncio.run(manager.calculate_buy_amount_out(pool, 100_000_000))
        sell_out = asyncio.run(manager.calculate_sell_amount_out(pool, 10_000_000))

        self.assertEqual(buy_out, 3_322_259_136)
        self.assertEqual(sell_out, 299_997)

    def test_position_thresholds_and_current_gross_pnl(self) -> None:
        position = Position.create_from_buy_result(
            mint=USER,
            symbol="HUNT",
            entry_price=0.0001,
            quantity=20,
            take_profit_percentage=0.5,
            stop_loss_percentage=0.2,
        )

        self.assertAlmostEqual(position.take_profit_price, 0.00015)
        self.assertAlmostEqual(position.stop_loss_price, 0.00008)
        self.assertEqual(position.should_exit(0.000151), (True, ExitReason.TAKE_PROFIT))
        self.assertEqual(position.should_exit(0.000079), (True, ExitReason.STOP_LOSS))
        pnl = position.get_pnl(0.00012)
        self.assertAlmostEqual(pnl["price_change_pct"], 20.0)
        self.assertAlmostEqual(pnl["unrealized_pnl_sol"], 0.0004)

    def test_compute_limits_and_fixed_priority_fee_cap(self) -> None:
        builder = PumpFunInstructionBuilder(
            get_idl_manager().get_parser(Platform.PUMP_FUN)
        )
        self.assertEqual(builder.get_buy_compute_unit_limit(), 180_000)
        self.assertEqual(builder.get_sell_compute_unit_limit(), 120_000)
        self.assertEqual(builder.get_buy_compute_unit_limit(222_222), 222_222)

        manager = PriorityFeeManager(
            client=None,
            enable_dynamic_fee=False,
            enable_fixed_fee=True,
            fixed_fee=200_000,
            extra_fee=0.1,
            hard_cap=210_000,
        )
        self.assertEqual(asyncio.run(manager.calculate_priority_fee()), 210_000)
        self.assertEqual(priority_fee_lamports(1_000_000, 180_000), 180_000)
        self.assertEqual(priority_fee_lamports(200_000, 180_000), 36_000)


if __name__ == "__main__":
    unittest.main()
