from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from application.positions import PositionService  # noqa: E402
from application.risk import RiskService  # noqa: E402
from core.pubkeys import WSOL_MINT  # noqa: E402
from domain.accounting import PositionAccounting  # noqa: E402
from domain.lifecycle import ExecutionState, PositionStatus  # noqa: E402
from domain.quotes import ExecutionResult, ExecutionSide  # noqa: E402
from execution.confirmation import TransactionObservation  # noqa: E402
from execution.errors import ErrorClassification  # noqa: E402
from execution.ports import BlockhashContext  # noqa: E402
from execution.telemetry import ExecutionTelemetry  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from storage.sqlite import (  # noqa: E402
    SCHEMA_VERSION,
    SQLitePositionStore,
    StoredPosition,
)
from trading.base import TradeResult  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402

TOKEN = Pubkey.new_unique()
OWNER = Pubkey.new_unique()


def buy_result(signature="buy"):
    return ExecutionResult(
        "e-buy",
        ExecutionSide.BUY,
        signature,
        True,
        100,
        1000,
        10,
        4,
        8,
        2,
        5,
        123,
    )


def sell_result(quantity=100, proceeds=1200, signature="sell"):
    return ExecutionResult(
        "e-sell",
        ExecutionSide.SELL,
        signature,
        True,
        quantity,
        proceeds,
        10,
        4,
        8,
        2,
        0,
        124,
    )


class FakeBalanceReader:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    async def get_token_balance_raw(self, _owner, _mint, _token_program=None):
        if self.error:
            raise self.error
        return self.value


class AmbiguousSeller:
    def __init__(self):
        self.submissions = 0
        self.existing_signatures = []

    async def execute(self, token_info, **kwargs):
        existing = kwargs.get("existing_signature")
        self.existing_signatures.append(existing)
        if existing is None:
            self.submissions += 1
            kwargs["submission_recorder"](
                "persisted-signature", BlockhashContext("hash", 123)
            )
        return TradeResult(
            success=False,
            platform=token_info.platform,
            tx_signature=existing or "persisted-signature",
            error_message="accepted but not observed",
            error_classification=ErrorClassification.ACCEPTED_BUT_NOT_OBSERVED,
        )


class AmbiguousBuyer:
    def __init__(self):
        self.submissions = 0
        self.existing_signatures = []

    async def execute(self, token_info, **kwargs):
        existing = kwargs.get("existing_signature")
        self.existing_signatures.append(existing)
        if existing is None:
            self.submissions += 1
            kwargs["submission_recorder"](
                "persisted-buy-signature", BlockhashContext("hash", 123)
            )
        return TradeResult(
            success=False,
            platform=token_info.platform,
            tx_signature=existing or "persisted-buy-signature",
            error_message="accepted but not observed",
            error_classification=ErrorClassification.ACCEPTED_BUT_NOT_OBSERVED,
        )


class PersistenceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "hunter.sqlite3"
        self.store = SQLitePositionStore(self.path)
        self.service = PositionService(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_schema_migration_is_versioned_and_idempotent(self):
        self.assertEqual(self.store.schema_version, SCHEMA_VERSION)
        self.store.migrate()
        self.assertEqual(self.store.schema_version, SCHEMA_VERSION)

    def test_position_survives_restart(self):
        opened = self.service.open_from_execution(
            buy_result(),
            token_mint=TOKEN,
            quote_mint=WSOL_MINT,
            token_decimals=6,
            quote_decimals=9,
            position_id="persisted",
        )
        self.assertEqual(opened.accounting.remaining_quantity_raw, 100)
        self.store.close()
        self.store = SQLitePositionStore(self.path)
        self.service = PositionService(self.store)
        loaded = self.service.get_position("persisted")
        self.assertEqual(loaded.accounting.quote_cost_raw, 1000)
        self.assertEqual(len(self.store.list_fills("persisted")), 1)

    def test_u64_raw_amounts_round_trip_exactly(self):
        maximum = 2**64 - 1
        accounting = PositionAccounting(
            "wide",
            TOKEN,
            WSOL_MINT,
            6,
            9,
            acquired_quantity_raw=maximum,
            quote_cost_raw=maximum,
            remaining_cost_basis_raw=maximum,
        )
        self.store.save_position(StoredPosition(accounting, {}))
        loaded = self.service.get_position("wide").accounting
        self.assertEqual(loaded.acquired_quantity_raw, maximum)
        self.assertEqual(loaded.remaining_cost_basis_raw, maximum)

    def test_lifecycle_transitions_are_persisted(self):
        self.service.open_from_execution(
            buy_result(),
            token_mint=TOKEN,
            quote_mint=WSOL_MINT,
            token_decimals=6,
            quote_decimals=9,
            position_id="p",
        )
        transitioned = self.store.transition_position(
            "p", PositionStatus.EXIT_REQUESTED, "test"
        )
        self.assertEqual(transitioned.accounting.status, PositionStatus.EXIT_REQUESTED)

    def test_invalid_lifecycle_transition_fails(self):
        accounting = PositionAccounting(
            "closed", TOKEN, WSOL_MINT, 6, 9, status=PositionStatus.CLOSED
        )
        self.store.save_position(StoredPosition(accounting, {}))
        with self.assertRaises(ValueError):
            self.store.transition_position("closed", PositionStatus.OPEN, "invalid")

    def test_execution_id_is_idempotent(self):
        first = self.store.create_execution("logical", position_id=None, side="buy")
        second = self.store.create_execution("logical", position_id=None, side="buy")
        self.assertEqual(first.logical_execution_id, second.logical_execution_id)
        self.assertEqual(second.submission_attempt, 0)
        updated = self.store.update_execution(
            "logical",
            state=ExecutionState.SIGNATURE_RECEIVED,
            signature="sig",
            blockhash="hash",
            last_valid_block_height=10,
            increment_attempt=True,
        )
        self.assertEqual(updated.submission_attempt, 1)
        self.assertEqual(updated.signature, "sig")
        attempts = self.store.list_execution_attempts("logical")
        self.assertEqual([item["signature"] for item in attempts], ["sig"])

    def test_multiple_submission_attempt_identities_survive_restart(self):
        self.store.create_execution("logical", position_id=None, side="buy")
        self.store.update_execution(
            "logical",
            state=ExecutionState.SIGNATURE_RECEIVED,
            signature="sig-one",
            increment_attempt=True,
        )
        self.store.update_execution(
            "logical",
            state=ExecutionState.EXPIRED,
            error_classification=ErrorClassification.BLOCKHASH_EXPIRED,
        )
        self.store.update_execution(
            "logical",
            state=ExecutionState.SIGNATURE_RECEIVED,
            signature="sig-two",
            increment_attempt=True,
        )
        self.store.close()
        self.store = SQLitePositionStore(self.path)
        self.service = PositionService(self.store)
        self.assertEqual(
            [row["signature"] for row in self.store.list_execution_attempts("logical")],
            ["sig-one", "sig-two"],
        )

    def test_matching_restart_balance_is_eligible(self):
        self.service.open_from_execution(
            buy_result(),
            token_mint=TOKEN,
            quote_mint=WSOL_MINT,
            token_decimals=6,
            quote_decimals=9,
            position_id="p",
        )
        report = asyncio.run(
            self.service.recover(owner=OWNER, balance_reader=FakeBalanceReader(100))
        )
        self.assertEqual(report.eligible_position_ids, ("p",))
        self.assertFalse(report.issues)

    def test_mismatch_requires_reconciliation_and_never_sells(self):
        self.service.open_from_execution(
            buy_result(),
            token_mint=TOKEN,
            quote_mint=WSOL_MINT,
            token_decimals=6,
            quote_decimals=9,
            position_id="p",
        )
        reader = FakeBalanceReader(99)
        report = asyncio.run(self.service.recover(owner=OWNER, balance_reader=reader))
        self.assertEqual(len(report.issues), 1)
        self.assertEqual(
            self.service.get_position("p").accounting.status,
            PositionStatus.RECONCILIATION_REQUIRED,
        )

    def test_balance_read_failure_is_safe_reconciliation(self):
        self.service.open_from_execution(
            buy_result(),
            token_mint=TOKEN,
            quote_mint=WSOL_MINT,
            token_decimals=6,
            quote_decimals=9,
            position_id="p",
        )
        report = asyncio.run(
            self.service.recover(
                owner=OWNER, balance_reader=FakeBalanceReader(error=TimeoutError())
            )
        )
        self.assertIn("TimeoutError", report.issues[0].reason)

    def test_missing_token_account_requires_reconciliation(self):
        self.service.open_from_execution(
            buy_result(),
            token_mint=TOKEN,
            quote_mint=WSOL_MINT,
            token_decimals=6,
            quote_decimals=9,
            position_id="p",
        )
        report = asyncio.run(
            self.service.recover(owner=OWNER, balance_reader=FakeBalanceReader(0))
        )
        self.assertEqual(report.issues[0].wallet_quantity_raw, 0)
        self.assertEqual(
            self.service.get_position("p").accounting.status,
            PositionStatus.RECONCILIATION_REQUIRED,
        )

    def test_partial_sell_and_closed_position_survive_restart(self):
        self.service.open_from_execution(
            buy_result(),
            token_mint=TOKEN,
            quote_mint=WSOL_MINT,
            token_decimals=6,
            quote_decimals=9,
            position_id="p",
        )
        self.store.transition_position("p", PositionStatus.EXIT_REQUESTED, "test")
        self.store.transition_position("p", PositionStatus.SELL_SUBMITTED, "test")
        self.store.transition_position("p", PositionStatus.SELL_CONFIRMED, "test")
        self.service.apply_sell_execution("p", sell_result(quantity=40, proceeds=500))
        partial = self.service.get_position("p").accounting
        self.assertEqual(partial.remaining_quantity_raw, 60)
        self.assertEqual(partial.remaining_cost_basis_raw, 600)
        self.store.transition_position("p", PositionStatus.EXIT_REQUESTED, "test")
        self.store.transition_position("p", PositionStatus.SELL_SUBMITTED, "test")
        self.store.transition_position("p", PositionStatus.SELL_CONFIRMED, "test")
        self.service.apply_sell_execution(
            "p", sell_result(quantity=60, proceeds=700, signature="sell-two")
        )
        self.store.close()
        self.store = SQLitePositionStore(self.path)
        self.service = PositionService(self.store)
        closed = self.service.get_position("p").accounting
        self.assertEqual(closed.status, PositionStatus.CLOSED)
        self.assertEqual(closed.remaining_quantity_raw, 0)
        self.assertEqual(closed.remaining_cost_basis_raw, 0)

    def test_active_sell_identity_survives_ambiguous_restart_without_resubmit(self):
        self.service.open_from_execution(
            buy_result(),
            token_mint=TOKEN,
            quote_mint=WSOL_MINT,
            token_decimals=6,
            quote_decimals=9,
            position_id="p",
        )
        seller = AmbiguousSeller()
        token = TokenInfo("Token", "TOK", "", TOKEN, Platform.PUMP_FUN)

        def coordinator():
            trader = UniversalTrader.__new__(UniversalTrader)
            trader.position_store = self.store
            trader.position_service = self.service
            trader.active_position_ids = {str(TOKEN): "p"}
            trader.pending_sell_signatures = {}
            trader.seller = seller
            trader.risk_service = RiskService()
            trader.solana_client = SimpleNamespace(
                last_transaction_observation=TransactionObservation(
                    "persisted-signature",
                    ExecutionState.NOT_OBSERVED,
                    error_classification=(
                        ErrorClassification.ACCEPTED_BUT_NOT_OBSERVED
                    ),
                )
            )
            return trader

        asyncio.run(
            coordinator()._execute_managed_sell(
                token, token_amount=0.0001, token_price=0.01
            )
        )
        execution = self.store.get_latest_execution("p", side="sell")
        self.assertEqual(execution.signature, "persisted-signature")
        self.assertEqual(execution.submission_attempt, 1)

        asyncio.run(
            coordinator()._execute_managed_sell(
                token, token_amount=0.0001, token_price=0.01
            )
        )
        self.assertEqual(seller.submissions, 1)
        self.assertEqual(seller.existing_signatures, [None, "persisted-signature"])

    def test_active_buy_identity_survives_ambiguous_restart_without_resubmit(self):
        buyer = AmbiguousBuyer()
        token = TokenInfo("Token", "TOK", "", TOKEN, Platform.PUMP_FUN)

        def coordinator():
            trader = UniversalTrader.__new__(UniversalTrader)
            trader.position_store = self.store
            trader.buyer = buyer
            trader.solana_client = SimpleNamespace(
                last_transaction_observation=TransactionObservation(
                    "persisted-buy-signature",
                    ExecutionState.NOT_OBSERVED,
                    error_classification=(
                        ErrorClassification.ACCEPTED_BUT_NOT_OBSERVED
                    ),
                )
            )
            return trader

        asyncio.run(coordinator()._execute_managed_buy(token))
        execution = self.store.get_execution(f"buy:{TOKEN}")
        self.assertEqual(execution.signature, "persisted-buy-signature")
        self.assertEqual(execution.submission_attempt, 1)
        asyncio.run(coordinator()._execute_managed_buy(token))
        self.assertEqual(buyer.submissions, 1)
        self.assertEqual(buyer.existing_signatures, [None, "persisted-buy-signature"])

    def test_restart_applies_confirmed_pending_sell_effects(self):
        self.service.open_from_execution(
            buy_result(),
            token_mint=TOKEN,
            quote_mint=WSOL_MINT,
            token_decimals=6,
            quote_decimals=9,
            position_id="p",
        )
        self.store.transition_position("p", PositionStatus.EXIT_REQUESTED, "test")
        self.store.transition_position("p", PositionStatus.SELL_SUBMITTED, "test")
        self.store.create_execution("sell:p:0", position_id="p", side="sell")
        self.store.update_execution(
            "sell:p:0",
            state=ExecutionState.SIGNATURE_RECEIVED,
            signature="persisted-signature",
            last_valid_block_height=123,
            increment_attempt=True,
        )

        class ConfirmedClient:
            async def observe_transaction(self, signature, **_kwargs):
                return TransactionObservation(signature, ExecutionState.CONFIRMED)

            async def get_execution_effects(self, **_kwargs):
                return sell_result(signature="persisted-signature")

        trader = UniversalTrader.__new__(UniversalTrader)
        trader.position_store = self.store
        trader.position_service = self.service
        trader.solana_client = ConfirmedClient()
        trader.wallet = SimpleNamespace(pubkey=OWNER)
        trader.active_position_ids = {str(TOKEN): "p"}
        asyncio.run(
            trader._reconcile_pending_sell_executions([self.service.get_position("p")])
        )
        self.assertEqual(
            self.service.get_position("p").accounting.status,
            PositionStatus.CLOSED,
        )
        self.assertNotIn(str(TOKEN), trader.active_position_ids)

    def test_restart_ambiguous_pending_sell_requires_reconciliation(self):
        self.service.open_from_execution(
            buy_result(),
            token_mint=TOKEN,
            quote_mint=WSOL_MINT,
            token_decimals=6,
            quote_decimals=9,
            position_id="p",
        )
        self.store.transition_position("p", PositionStatus.EXIT_REQUESTED, "test")
        self.store.transition_position("p", PositionStatus.SELL_SUBMITTED, "test")
        self.store.create_execution("sell:p:0", position_id="p", side="sell")
        self.store.update_execution(
            "sell:p:0",
            state=ExecutionState.SIGNATURE_RECEIVED,
            signature="persisted-signature",
            increment_attempt=True,
        )

        class AmbiguousClient:
            async def observe_transaction(self, signature, **_kwargs):
                return TransactionObservation(
                    signature,
                    ExecutionState.NOT_OBSERVED,
                    error_classification=(
                        ErrorClassification.ACCEPTED_BUT_NOT_OBSERVED
                    ),
                )

        trader = UniversalTrader.__new__(UniversalTrader)
        trader.position_store = self.store
        trader.position_service = self.service
        trader.solana_client = AmbiguousClient()
        trader.wallet = SimpleNamespace(pubkey=OWNER)
        trader.active_position_ids = {str(TOKEN): "p"}
        asyncio.run(
            trader._reconcile_pending_sell_executions([self.service.get_position("p")])
        )
        self.assertEqual(
            self.service.get_position("p").accounting.status,
            PositionStatus.RECONCILIATION_REQUIRED,
        )

    def test_settings_round_trip(self):
        self.store.set_setting("sell_slippage_bps", 500)
        self.assertEqual(self.store.get_settings(), {"sell_slippage_bps": 500})

    def test_execution_telemetry_is_persisted_by_attempt(self):
        telemetry = ExecutionTelemetry(
            execution_id="telemetry",
            provider_id="solana-json-rpc",
            endpoint_id="https://rpc.invalid#safe",
        )
        for stage in (
            "trade_requested",
            "build_started",
            "build_completed",
            "signing_started",
            "signing_completed",
            "submission_started",
            "rpc_responded",
            "signature_received",
            "confirmed",
        ):
            telemetry.mark(stage)
        telemetry.transaction_signature = "signature"
        telemetry.landed_slot = 123
        self.store.save_telemetry(telemetry, 1)
        stored = self.store.get_telemetry("telemetry", 1)
        self.assertEqual(stored["transaction_signature"], "signature")
        self.assertEqual(stored["landed_slot"], 123)
        self.assertIsNotNone(stored["submission_started_mono_ns"])


if __name__ == "__main__":
    unittest.main()
