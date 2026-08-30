from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from solders.pubkey import Pubkey

from benchmark.cli import _transport_probe_specs
from benchmark.live_config import (
    LIVE_ACKNOWLEDGEMENT,
    BenchmarkCaps,
    BenchmarkRoute,
    ExitPolicy,
    LiveBenchmarkConfig,
    live_benchmark_config_from_dict,
)
from benchmark.live_models import (
    BenchmarkAttempt,
    BenchmarkKind,
    ConnectionState,
    DetectionObservation,
)
from benchmark.live_report import build_live_report, export_report, render_text_report
from benchmark.live_session import (
    AsyncDetectionSink,
    DetectionCorrelator,
    EconomicOutcome,
    LiveBenchmarkSession,
    run_transport_probe,
    validate_provider_matrix,
)
from benchmark.live_store import BenchmarkStore
from execution.detection import record_detection
from execution.errors import ErrorClassification
from execution.providers.factory import routing_config_from_dict
from interfaces.core import Platform, TokenInfo


def caps(**overrides):
    values = {
        "maximum_sol_spend_per_trade_lamports": 100_000,
        "maximum_quote_amount_raw": 100_000,
        "maximum_live_trades": 2,
        "maximum_cumulative_spend_raw": 200_000,
        "maximum_priority_fee_lamports": 10_000,
        "maximum_tip_lamports": 5_000,
        "maximum_combined_transaction_cost_lamports": 30_000,
        "minimum_wallet_reserve_lamports": 1_000_000,
        "maximum_duration_seconds": 10.0,
    }
    values.update(overrides)
    return BenchmarkCaps(**values)


def config(**overrides):
    values = {
        "live_enabled": True,
        "acknowledgement": LIVE_ACKNOWLEDGEMENT,
        "mint": str(Pubkey.new_unique()),
        "quote_amount_raw": 50_000,
        "quote_mint": "sol",
        "provider_matrix": (BenchmarkRoute("rpc-a", ("rpc-a",)),),
        "caps": caps(),
        "region_label": "test-region",
        "dedicated_wallet": True,
    }
    values.update(overrides)
    return LiveBenchmarkConfig(**values)


class FakeExecutor:
    def __init__(self):
        self.buy_calls = []
        self.exit_calls = []

    async def execute_buy(self, **kwargs):
        self.buy_calls.append(kwargs)
        return EconomicOutcome(
            success=True,
            signature="signature",
            execution_variant=kwargs["execution_variant"],
            quote_spent_raw=49_000,
        )

    async def execute_exit(self, **kwargs):
        self.exit_calls.append(kwargs)
        return EconomicOutcome(
            success=True,
            signature="exit-signature",
            execution_variant="exit",
        )


class LiveAuthorizationTests(unittest.TestCase):
    def test_requires_configuration_switch(self):
        with self.assertRaises(PermissionError):
            config(live_enabled=False).authorize(
                cli_allow_live=True, risk_enforced=True
            )

    def test_requires_exact_acknowledgement(self):
        with self.assertRaises(PermissionError):
            config(acknowledgement="yes").authorize(
                cli_allow_live=True, risk_enforced=True
            )

    def test_requires_cli_flag(self):
        with self.assertRaises(PermissionError):
            config().authorize(cli_allow_live=False, risk_enforced=True)

    def test_requires_risk_enforcement(self):
        with self.assertRaises(PermissionError):
            config().authorize(cli_allow_live=True, risk_enforced=False)

    def test_rejects_amount_above_raw_cap(self):
        with self.assertRaises(ValueError):
            config(quote_amount_raw=100_001).validate_amounts()

    def test_rejects_amount_above_sol_cap(self):
        with self.assertRaises(ValueError):
            config(
                quote_amount_raw=90_000,
                caps=caps(maximum_sol_spend_per_trade_lamports=80_000),
            ).validate_amounts()

    def test_requires_explicit_route(self):
        with self.assertRaises(ValueError):
            config(provider_matrix=())

    def test_single_route_has_one_provider(self):
        with self.assertRaises(ValueError):
            BenchmarkRoute("bad", ("a", "b"), mode="single")

    def test_parser_requires_every_cap(self):
        with self.assertRaises(ValueError):
            live_benchmark_config_from_dict({"live_enabled": False, "caps": {}})

    def test_optional_slots_reject_negative_values(self):
        with self.assertRaises(ValueError):
            config(authoritative_launch_slot=-1)

    def test_slippage_expanded_input_must_fit_cap(self):
        with self.assertRaises(ValueError):
            config().validate_encoded_input(100_001)

    def test_automatic_exit_reserves_second_trade(self):
        with self.assertRaises(ValueError):
            config(
                exit_policy=ExitPolicy.IMMEDIATE,
                caps=caps(maximum_live_trades=1),
            )


class BenchmarkPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = BenchmarkStore(Path(self.temp.name) / "benchmark.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_one_trial_is_reserved_and_duplicate_is_blocked(self):
        session = LiveBenchmarkSession(
            config(),
            self.store,
            cli_allow_live=True,
            risk_enforced=True,
            session_id="session",
        )
        executor = FakeExecutor()
        result = await session.execute(executor, route_id="rpc-a")
        self.assertTrue(result.buy.success)
        with self.assertRaises(RuntimeError):
            await session.execute(executor, route_id="rpc-a")

    async def test_logical_identity_is_stable_across_command_sessions(self):
        benchmark = config()
        first = LiveBenchmarkSession(
            benchmark,
            self.store,
            cli_allow_live=True,
            risk_enforced=True,
            session_id="first",
        )
        second = LiveBenchmarkSession(
            benchmark,
            self.store,
            cli_allow_live=True,
            risk_enforced=True,
            session_id="second",
        )
        first_executor = FakeExecutor()
        second_executor = FakeExecutor()
        await first.execute(first_executor, route_id="rpc-a")
        await second.execute(second_executor, route_id="rpc-a")
        self.assertEqual(
            first_executor.buy_calls[0]["logical_trade_id"],
            second_executor.buy_calls[0]["logical_trade_id"],
        )

    async def test_immediate_exit_is_explicit_and_persisted(self):
        session = LiveBenchmarkSession(
            config(exit_policy=ExitPolicy.IMMEDIATE),
            self.store,
            cli_allow_live=True,
            risk_enforced=True,
            session_id="exit-session",
        )
        executor = FakeExecutor()
        result = await session.execute(executor, route_id="rpc-a")
        self.assertEqual(result.exit.signature, "exit-signature")
        self.assertEqual(self.store.economic_totals("exit-session"), (2, 100_000))

    async def test_manual_exit_does_not_sell(self):
        session = LiveBenchmarkSession(
            config(exit_policy=ExitPolicy.MANUAL),
            self.store,
            cli_allow_live=True,
            risk_enforced=True,
            session_id="manual-session",
        )
        executor = FakeExecutor()
        await session.execute(executor, route_id="rpc-a")
        self.assertEqual(executor.exit_calls, [])

    async def test_trade_count_cap_is_enforced(self):
        routes = (
            BenchmarkRoute("a", ("a",)),
            BenchmarkRoute("b", ("b",)),
        )
        session = LiveBenchmarkSession(
            config(provider_matrix=routes, caps=caps(maximum_live_trades=1)),
            self.store,
            cli_allow_live=True,
            risk_enforced=True,
            session_id="capped",
        )
        await session.execute(FakeExecutor(), route_id="a")
        with self.assertRaises(PermissionError):
            await session.execute(FakeExecutor(), route_id="b")
        failure = next(
            item
            for item in self.store.list_attempts("capped")
            if item["error_classification"] == "risk_limit_exceeded"
        )
        self.assertEqual(failure["provider_id"], "hunter")
        self.assertEqual(failure["error_classification"], "risk_limit_exceeded")

    async def test_transport_failure_is_retained(self):
        self.store.create_session(
            "transport", BenchmarkKind.TRANSPORT, region_label="test"
        )

        async def fails():
            raise TimeoutError

        attempt = await run_transport_probe(
            store=self.store,
            session_id="transport",
            provider_id="rpc",
            endpoint_id="https://rpc.invalid",
            probe_name="blockhash",
            probe=fails,
            connection_state=ConnectionState.COLD,
        )
        self.assertFalse(attempt.success)
        self.assertEqual(
            attempt.error_classification,
            ErrorClassification.RPC_TRANSPORT_FAILURE,
        )

    async def test_economic_provider_telemetry_is_copied_to_benchmark_store(self):
        telemetry = {
            "execution_id": "execution",
            "logical_trade_id": "trade",
            "execution_variant": "standard",
            "detected_mono_ns": 1_000_000,
            "processed_mono_ns": 4_000_000,
            "detection_slot": 10,
            "landed_slot": 11,
            "base_network_fee_lamports": 5_000,
            "priority_fee_lamports": 1_000,
            "rent_lamports": 0,
            "provider_attempts": [
                {
                    "provider_id": "rpc-a",
                    "endpoint_id": "https://rpc.example#safe",
                    "accepted": True,
                    "submit_started_mono_ns": 2_000_000,
                    "acknowledged_mono_ns": 2_500_000,
                    "submitted_slot": 10,
                    "connection_session_created": True,
                    "connection_session_generation": 1,
                }
            ],
        }

        class TelemetryExecutor(FakeExecutor):
            async def execute_buy(self, **kwargs):
                return EconomicOutcome(
                    success=True,
                    signature="signature",
                    execution_variant="standard",
                    quote_spent_raw=49_000,
                    telemetry_records=(telemetry,),
                )

        session = LiveBenchmarkSession(
            config(),
            self.store,
            cli_allow_live=True,
            risk_enforced=True,
            session_id="telemetry",
        )
        await session.execute(TelemetryExecutor(), route_id="rpc-a")
        attempt = self.store.list_attempts("telemetry")[0]
        self.assertEqual(attempt["acknowledgement_rtt_ms"], 0.5)
        self.assertEqual(attempt["submit_to_landed_ms"], 2.0)
        self.assertEqual(attempt["connection_state"], "cold")

    async def test_passive_detection_correlation(self):
        self.store.create_session(
            "detection", BenchmarkKind.DETECTION, region_label="test"
        )
        mint = Pubkey.new_unique()
        token = TokenInfo("name", "SYM", "", mint, Platform.PUMP_FUN)
        record_detection(
            token,
            source="geyser",
            event_slot=12,
            transaction_slot=12,
            launch_slot=12,
            observed_mono_ns=1_000,
        )
        token.additional_data["creation_signature"] = "creation"
        await DetectionCorrelator(self.store, "detection").observe(token)
        row = self.store.list_detections("detection")[0]
        self.assertEqual(row["correlation_key"], "creation")
        self.assertEqual(row["launch_slot"], 12)

    async def test_passive_detection_can_persist_off_hot_path(self):
        self.store.create_session(
            "async-detection", BenchmarkKind.DETECTION, region_label="test"
        )
        sink = AsyncDetectionSink(self.store)
        await sink.start()
        token = TokenInfo("name", "SYM", "", Pubkey.new_unique(), Platform.PUMP_FUN)
        record_detection(token, source="logs", observed_mono_ns=123)
        await DetectionCorrelator(self.store, "async-detection", sink).observe(token)
        await sink.close()
        self.assertEqual(
            self.store.list_detections("async-detection")[0]["source"], "logs"
        )


class BenchmarkReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = BenchmarkStore(Path(self.temp.name) / "benchmark.sqlite3")
        self.store.create_session("report", BenchmarkKind.ECONOMIC, region_label="test")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_detection_relative_delays_are_correlated(self):
        for source, observed in (("geyser", 1_000_000), ("logs", 3_000_000)):
            self.store.record_detection(
                DetectionObservation(
                    "report",
                    source,
                    "mint",
                    datetime.now(UTC),
                    observed,
                    creation_signature="tx",
                    detection_slot=10 if source == "geyser" else 11,
                )
            )
        report = build_live_report(self.store, session_id="report")
        rows = {item["source"]: item for item in report["detection"]}
        self.assertEqual(rows["geyser"]["median_relative_delay_ms"], 0)
        self.assertEqual(rows["logs"]["median_relative_delay_ms"], 2)
        self.assertEqual(rows["logs"]["median_slot_delay"], 1)

    def test_launch_and_detection_slots_are_separate(self):
        self.store.record_attempt(
            BenchmarkAttempt(
                "report",
                "one",
                BenchmarkKind.ECONOMIC,
                "jito",
                "jito.example",
                "single",
                "jito-route",
                ConnectionState.WARM,
                success=True,
                launch_slot=10,
                detection_slot=11,
                landed_slot=11,
                submission_slot=11,
                base_fee_lamports=5_000,
                priority_fee_lamports=1_000,
                jito_tip_lamports=2_000,
                rent_lamports=0,
            )
        )
        route = build_live_report(self.store, session_id="report")["routes"][0]
        self.assertEqual(route["same_detection_slot_percentage"], 100)
        self.assertEqual(route["launch_block_zero_percentage"], 0)
        self.assertEqual(route["median_known_cost_lamports"], 8_000)

    def test_failures_are_included(self):
        for number, success in enumerate((True, False)):
            self.store.record_attempt(
                BenchmarkAttempt(
                    "report",
                    str(number),
                    BenchmarkKind.TRANSPORT,
                    "rpc",
                    "rpc.example",
                    "read",
                    "blockhash",
                    ConnectionState.WARM,
                    success=success,
                    ambiguous=not success,
                )
            )
        route = build_live_report(self.store, session_id="report")["routes"][0]
        self.assertEqual(route["failure_rate"], 0.5)
        self.assertEqual(route["ambiguous_rate"], 0.5)

    def test_json_and_csv_export(self):
        report = build_live_report(self.store, session_id="report")
        json_path = Path(self.temp.name) / "report.json"
        csv_path = Path(self.temp.name) / "report.csv"
        export_report(report, json_path, "json")
        export_report(report, csv_path, "csv")
        self.assertIn('"detection"', json_path.read_text())
        self.assertIn("section", csv_path.read_text())

    def test_human_report_does_not_invent_values(self):
        text = render_text_report(build_live_report(self.store, session_id="report"))
        self.assertIn("No correlated detection observations", text)


class ProviderMatrixTests(unittest.TestCase):
    def test_matrix_requires_configured_provider(self):
        routing = routing_config_from_dict(
            {
                "enabled": True,
                "providers": [
                    {
                        "id": "rpc-a",
                        "kind": "standard_rpc",
                        "endpoint": "https://rpc.example/key",
                        "roles": ["submit"],
                    }
                ],
            }
        )
        validate_provider_matrix(("rpc-a",), routing)
        with self.assertRaises(ValueError):
            validate_provider_matrix(("rpc-b",), routing)

    def test_default_transport_probes_are_non_economic(self):
        specs = _transport_probe_specs({})
        self.assertEqual(
            [method for _name, method, _parameters in specs],
            ["getHealth", "getLatestBlockhash"],
        )

    def test_controlled_transport_probes_require_public_inputs(self):
        with self.assertRaises(ValueError):
            _transport_probe_specs({"probes": ["account"]})

    def test_transport_probe_matrix_supports_reads_and_fee_estimation(self):
        specs = _transport_probe_specs(
            {
                "probes": ["account", "status", "priority_fee"],
                "account_address": str(Pubkey.new_unique()),
                "status_signature": "signature-fixture",
                "priority_fee_accounts": [str(Pubkey.new_unique())],
            }
        )
        self.assertEqual(
            [method for _name, method, _parameters in specs],
            [
                "getAccountInfo",
                "getSignatureStatuses",
                "getRecentPrioritizationFees",
            ],
        )


if __name__ == "__main__":
    unittest.main()
