from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from application.risk import (  # noqa: E402
    FeeExposure,
    RiskContext,
    RiskLimits,
    RiskService,
)
from core.pubkeys import WSOL_MINT  # noqa: E402
from domain.amounts import (  # noqa: E402
    BasisPoints,
    Lamports,
    QuoteAmountRaw,
    TokenAmountRaw,
)
from domain.lifecycle import ExecutionState, is_retryable  # noqa: E402
from domain.quotes import (  # noqa: E402
    CurveState,
    ExecutionPlan,
    ExecutionResult,
    FeeRates,
    quote_buy,
    quote_sell,
)
from execution.confirmation import TransactionObservation  # noqa: E402
from execution.coordinator import (  # noqa: E402
    ExecutionCoordinator,
    SubmittedTransaction,
)
from execution.errors import ErrorClassification, ExecutionError  # noqa: E402
from execution.ports import BlockhashContext  # noqa: E402
from storage.sqlite import SQLitePositionStore  # noqa: E402

TOKEN = Pubkey.new_unique()


def plan():
    state = CurveState(
        TOKEN,
        WSOL_MINT,
        6,
        9,
        1_000_000_000_000,
        30_000_000_000,
        800_000_000_000,
        1_000_000_000_000_000,
        True,
    )
    quote = quote_buy(
        spend=QuoteAmountRaw(100_000, WSOL_MINT, 9),
        curve=state,
        fee_rates=FeeRates(BasisPoints(100), BasisPoints(50)),
        slippage=BasisPoints(100),
    )
    return ExecutionPlan.for_buy(quote, "logical")


def sell_plan():
    state = plan().quote.reserve_state
    quote = quote_sell(
        tokens=TokenAmountRaw(100_000, TOKEN, 6),
        curve=state,
        fee_rates=FeeRates(BasisPoints(100), BasisPoints(50)),
        slippage=BasisPoints(100),
    )
    return ExecutionPlan.for_sell(quote, "logical-sell")


def risk_context(priority=10, base=5000, rent=0):
    return RiskContext(
        wallet_lamports=1_000_000,
        existing_position_exposure_raw=0,
        aggregate_exposure_raw=0,
        fee_exposure=FeeExposure(
            Lamports(base) if base is not None else None,
            Lamports(priority),
            Lamports(rent),
        ),
    )


class FakeGateway:
    def __init__(self, observation):
        self.observation = observation
        self.submit_calls = 0
        self.inspect_calls = 0

    async def submit(self, _plan, telemetry):
        self.submit_calls += 1
        telemetry.mark("build_started")
        telemetry.mark("build_completed")
        return SubmittedTransaction("signature", "blockhash", 123)

    async def observe(self, signature, _last_valid):
        return TransactionObservation(signature, self.observation)

    async def inspect_result(self, requested_plan, signature):
        self.inspect_calls += 1
        return ExecutionResult(
            requested_plan.logical_execution_id,
            requested_plan.side,
            signature,
            True,
            10,
            100,
            5,
            1,
            1,
            1,
            0,
            10,
        )


class RiskTests(unittest.TestCase):
    def test_disabled_risk_preserves_legacy_economics(self):
        RiskService().assess(plan(), risk_context(priority=999_999))

    def test_global_trading_disabled(self):
        service = RiskService(RiskLimits(enforce=True, trading_enabled=False))
        with self.assertRaises(ExecutionError):
            service.assess(plan(), risk_context())

    def test_kill_switch(self):
        service = RiskService(RiskLimits(enforce=True, emergency_kill_switch=True))
        with self.assertRaisesRegex(ExecutionError, "kill switch"):
            service.assess(plan(), risk_context())

    def test_global_halt_flags_allow_sell_but_retain_fee_guards(self):
        service = RiskService(
            RiskLimits(
                enforce=True,
                trading_enabled=False,
                emergency_kill_switch=True,
                maximum_priority_fee_lamports=10,
            )
        )
        service.assess(sell_plan(), risk_context(priority=10))
        with self.assertRaisesRegex(ExecutionError, "priority fee"):
            service.assess(sell_plan(), risk_context(priority=11))

    def test_maximum_buy_amount(self):
        service = RiskService(
            RiskLimits(enforce=True, maximum_buy_raw_by_quote={WSOL_MINT: 99_999})
        )
        with self.assertRaisesRegex(ExecutionError, "buy amount"):
            service.assess(plan(), risk_context())

    def test_aggregate_exposure(self):
        service = RiskService(
            RiskLimits(
                enforce=True,
                maximum_aggregate_exposure_raw_by_quote={WSOL_MINT: 150_000},
            )
        )
        context = risk_context()
        context = RiskContext(context.wallet_lamports, 0, 100_000, context.fee_exposure)
        with self.assertRaisesRegex(ExecutionError, "aggregate exposure"):
            service.assess(plan(), context)

    def test_maximum_position_size(self):
        service = RiskService(
            RiskLimits(
                enforce=True,
                maximum_position_raw_by_quote={WSOL_MINT: 150_000},
            )
        )
        context = risk_context()
        context = RiskContext(
            context.wallet_lamports, 60_000, 60_000, context.fee_exposure
        )
        with self.assertRaisesRegex(ExecutionError, "position size"):
            service.assess(plan(), context)

    def test_maximum_priority_fee(self):
        service = RiskService(RiskLimits(enforce=True, maximum_priority_fee_lamports=9))
        with self.assertRaisesRegex(ExecutionError, "priority fee"):
            service.assess(plan(), risk_context(priority=10))

    def test_total_fee_includes_base_priority_and_rent(self):
        service = RiskService(
            RiskLimits(enforce=True, maximum_total_transaction_fee_lamports=6000)
        )
        with self.assertRaisesRegex(ExecutionError, "total transaction fee"):
            service.assess(plan(), risk_context(base=5000, priority=1000, rent=1))

    def test_unknown_base_fee_can_fail_closed(self):
        service = RiskService(RiskLimits(enforce=True, reject_unknown_base_fee=True))
        with self.assertRaisesRegex(ExecutionError, "base transaction fee is unknown"):
            service.assess(plan(), risk_context(base=None))

    def test_minimum_wallet_reserve_includes_native_spend_and_fees(self):
        service = RiskService(
            RiskLimits(enforce=True, minimum_wallet_reserve_lamports=900_000)
        )
        base = risk_context(base=5_000, priority=1_000, rent=2_000)
        context = RiskContext(
            wallet_lamports=1_000_000,
            existing_position_exposure_raw=0,
            aggregate_exposure_raw=0,
            fee_exposure=base.fee_exposure,
            native_trade_spend_lamports=100_000,
        )
        with self.assertRaisesRegex(ExecutionError, "configured reserve"):
            service.assess(plan(), context)

    def test_trade_rate_limit(self):
        service = RiskService(RiskLimits(enforce=True, maximum_trades_per_interval=1))
        service.record_trade()
        with self.assertRaisesRegex(ExecutionError, "trades per interval"):
            service.assess(plan(), risk_context())


class ExecutionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLitePositionStore(Path(self.temp.name) / "state.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_fresh_execution_persists_identity_before_result(self):
        gateway = FakeGateway(ExecutionState.CONFIRMED)
        result = asyncio.run(ExecutionCoordinator(gateway, self.store).execute(plan()))
        self.assertTrue(result.success)
        stored = self.store.get_execution("logical")
        self.assertEqual(stored.signature, "signature")
        self.assertEqual(stored.submission_attempt, 1)
        self.assertEqual(stored.state, ExecutionState.CONFIRMED)

    def test_ambiguous_existing_signature_is_not_resubmitted(self):
        self.store.create_execution("logical", position_id=None, side="buy")
        self.store.update_execution(
            "logical",
            state=ExecutionState.SIGNATURE_RECEIVED,
            signature="existing",
            blockhash="hash",
            last_valid_block_height=10,
            increment_attempt=True,
        )
        gateway = FakeGateway(ExecutionState.NOT_OBSERVED)
        with self.assertRaises(ExecutionError) as raised:
            asyncio.run(ExecutionCoordinator(gateway, self.store).execute(plan()))
        self.assertEqual(gateway.submit_calls, 0)
        self.assertTrue(raised.exception.retryable)

    def test_dropped_unknown_is_not_sufficient_evidence_to_resubmit(self):
        self.store.create_execution("logical", position_id=None, side="buy")
        self.store.update_execution(
            "logical",
            state=ExecutionState.SIGNATURE_RECEIVED,
            signature="existing",
            increment_attempt=True,
        )
        gateway = FakeGateway(ExecutionState.DROPPED_UNKNOWN)
        with self.assertRaises(ExecutionError):
            asyncio.run(ExecutionCoordinator(gateway, self.store).execute(plan()))
        self.assertEqual(gateway.submit_calls, 0)

    def test_confirmed_existing_signature_is_inspected_not_resubmitted(self):
        self.store.create_execution("logical", position_id=None, side="buy")
        self.store.update_execution(
            "logical",
            state=ExecutionState.SIGNATURE_RECEIVED,
            signature="existing",
            blockhash="hash",
            last_valid_block_height=10,
            increment_attempt=True,
        )
        gateway = FakeGateway(ExecutionState.CONFIRMED)
        result = asyncio.run(ExecutionCoordinator(gateway, self.store).execute(plan()))
        self.assertEqual(result.signature, "existing")
        self.assertEqual(gateway.submit_calls, 0)
        self.assertEqual(gateway.inspect_calls, 1)

    def test_failed_on_chain_is_permanent(self):
        self.store.create_execution("logical", position_id=None, side="buy")
        self.store.update_execution(
            "logical",
            state=ExecutionState.SIGNATURE_RECEIVED,
            signature="existing",
            increment_attempt=True,
        )
        gateway = FakeGateway(ExecutionState.FAILED_ON_CHAIN)
        with self.assertRaises(ExecutionError) as raised:
            asyncio.run(ExecutionCoordinator(gateway, self.store).execute(plan()))
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(gateway.submit_calls, 0)

    def test_retry_taxonomy_is_narrow(self):
        self.assertTrue(is_retryable(ErrorClassification.RPC_RATE_LIMIT))
        self.assertTrue(is_retryable(ErrorClassification.BLOCKHASH_EXPIRED))
        self.assertFalse(is_retryable(ErrorClassification.ON_CHAIN_PROGRAM_FAILURE))
        self.assertFalse(is_retryable(ErrorClassification.INSUFFICIENT_BALANCE))

    def test_blockhash_context_age_and_expiry(self):
        context = BlockhashContext.observed("hash", 100, observed_slot=50)
        self.assertGreaterEqual(context.age_seconds(), 0)
        self.assertFalse(context.is_expired(100))
        self.assertTrue(context.is_expired(101))


if __name__ == "__main__":
    unittest.main()
