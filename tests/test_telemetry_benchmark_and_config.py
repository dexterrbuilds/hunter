from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from benchmark.replay import BenchmarkConfig, OfflineReplayBenchmark  # noqa: E402
from benchmark.report import summarize_provider_telemetry  # noqa: E402
from execution.detection import detection_for, record_detection  # noqa: E402
from execution.errors import ErrorClassification  # noqa: E402
from execution.providers.config import (  # noqa: E402
    BroadcastMode,
    ProviderRole,
    role_endpoints,
)
from execution.providers.factory import routing_config_from_dict  # noqa: E402
from execution.telemetry import (  # noqa: E402
    ExecutionTelemetry,
    ProviderAttemptTelemetry,
)
from execution.telemetry_sink import AsyncTelemetrySink  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from storage.sqlite import SQLitePositionStore  # noqa: E402


class TelemetryBenchmarkConfigTests(unittest.TestCase):
    def test_provider_configuration_supports_separate_roles(self):
        config = routing_config_from_dict(
            {
                "enabled": True,
                "mode": "hedged",
                "providers": [
                    {
                        "id": "read",
                        "kind": "standard_rpc",
                        "endpoint": "https://read.example",
                        "roles": ["account_read", "blockhash", "confirm"],
                    },
                    {
                        "id": "write",
                        "kind": "standard_rpc",
                        "endpoint": "https://write.example",
                        "roles": ["submit"],
                    },
                    {
                        "id": "ws",
                        "kind": "standard_rpc",
                        "endpoint": "wss://ws.example",
                        "roles": ["websocket"],
                    },
                ],
            }
        )
        self.assertEqual(config.mode, BroadcastMode.HEDGED)
        roles = role_endpoints(config)
        self.assertEqual(roles[ProviderRole.ACCOUNT_READ][0].provider_id, "read")
        self.assertEqual(roles[ProviderRole.SUBMIT][0].provider_id, "write")
        self.assertEqual(roles[ProviderRole.WEBSOCKET][0].provider_id, "ws")

    def test_disabled_execution_config_exposes_no_active_roles(self):
        config = routing_config_from_dict(
            {
                "enabled": False,
                "providers": [
                    {
                        "id": "unused",
                        "kind": "standard_rpc",
                        "endpoint": "https://unused.example",
                        "roles": ["account_read", "submit", "confirm"],
                    }
                ],
            }
        )

        self.assertTrue(all(not items for items in role_endpoints(config).values()))

    def test_unknown_provider_kind_fails_validation(self):
        with self.assertRaises(ValueError):
            routing_config_from_dict(
                {
                    "enabled": True,
                    "providers": [
                        {"id": "x", "kind": "invented", "endpoint": "https://x"}
                    ],
                }
            )

    def test_provider_ids_are_unique(self):
        with self.assertRaises(ValueError):
            routing_config_from_dict(
                {
                    "providers": [
                        {"id": "x", "kind": "standard_rpc", "endpoint": "https://a"},
                        {"id": "x", "kind": "standard_rpc", "endpoint": "https://b"},
                    ]
                }
            )

    def test_credentials_do_not_appear_in_endpoint_identifier(self):
        config = routing_config_from_dict(
            {
                "providers": [
                    {
                        "id": "rpc",
                        "kind": "standard_rpc",
                        "endpoint": "https://user:pass@rpc.example/path?api-key=secret",
                    }
                ]
            }
        )
        endpoint_id = config.providers[0].endpoint_id
        for secret in ("user", "pass", "path", "secret", "api-key"):
            self.assertNotIn(secret, endpoint_id)
            self.assertNotIn(secret, repr(config))

    def test_latency_budgets_are_validated_and_configurable(self):
        config = routing_config_from_dict(
            {
                "latency_budgets": {
                    "transaction_build_ms": 10,
                    "submission_rtt_ms": 150.5,
                }
            }
        )
        self.assertEqual(config.latency_budgets.transaction_build_ms, 10.0)
        self.assertEqual(config.latency_budgets.submission_rtt_ms, 150.5)
        with self.assertRaises(ValueError):
            routing_config_from_dict({"latency_budgets": {"submission_rtt_ms": 0}})
        with self.assertRaisesRegex(ValueError, "unknown execution latency budget"):
            routing_config_from_dict({"latency_budgets": {"invented_ms": 1}})

    def test_tipped_provider_requires_explicit_fee_bounds(self):
        base = {
            "enabled": True,
            "execution_variant": "jito_tipped",
            "jito_tip_lamports": 1_000,
            "jito_tip_account": str(Pubkey.new_unique()),
            "providers": [
                {
                    "id": "jito",
                    "kind": "jito",
                    "endpoint": "https://jito.example/api/v1/transactions",
                    "roles": ["submit"],
                    "minimum_tip_lamports": 1_000,
                    "maximum_tip_lamports": 2_000,
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "maximum_combined_fee"):
            routing_config_from_dict(base)

        base["maximum_combined_fee_lamports"] = 10_000
        config = routing_config_from_dict(base)
        self.assertEqual(config.jito_tip_lamports, 1_000)

        base["providers"][0]["maximum_tip_lamports"] = 500
        with self.assertRaisesRegex(ValueError, "exceed"):
            routing_config_from_dict(base)

    def test_detection_metadata_keeps_authoritative_slots(self):
        token = TokenInfo("n", "s", "u", Pubkey.new_unique(), Platform.PUMP_FUN)
        telemetry = record_detection(
            token,
            source="solana_logs",
            event_slot=10,
            transaction_slot=10,
            launch_slot=10,
        )
        telemetry.mark_processing_started()
        telemetry.mark_trade_request_created()
        telemetry.authoritative_refresh_started_mono_ns = 100
        telemetry.authoritative_refresh_completed_mono_ns = 200
        telemetry.account_read_duration_ms = 0.0001
        self.assertIs(detection_for(token), telemetry)
        execution = ExecutionTelemetry("x")
        execution.apply_detection(telemetry)
        self.assertEqual(execution.detection_source, "solana_logs")
        self.assertEqual(execution.launch_slot, 10)
        self.assertIsNotNone(execution.trade_requested_mono_ns)
        self.assertEqual(execution.authoritative_refresh_started_mono_ns, 100)
        self.assertEqual(execution.authoritative_refresh_completed_mono_ns, 200)
        self.assertEqual(execution.account_read_duration_ms, 0.0001)

    def test_async_telemetry_persistence_and_schema_migration(self):
        async def run(path):
            store = SQLitePositionStore(path)
            sink = AsyncTelemetrySink(store)
            await sink.start()
            telemetry = ExecutionTelemetry("logical")
            telemetry.transaction_signature = "sig"
            telemetry.provider_attempts.append(
                ProviderAttemptTelemetry(
                    provider_id="provider",
                    endpoint_id="endpoint",
                    execution_variant="standard",
                    signature="sig",
                    accepted=True,
                    acknowledgement="signature",
                    bytes_sent=123,
                )
            )
            sink.record_nowait(telemetry, 1)
            telemetry.provider_attempts[0].provider_id = "mutated-after-capture"
            await sink.flush()
            loaded = store.get_telemetry("logical", 1)
            provider_rows = store.list_provider_submission_telemetry()
            version = store.schema_version
            await sink.close()
            store.close()
            return loaded, provider_rows, version

        with tempfile.TemporaryDirectory() as directory:
            loaded, rows, version = asyncio.run(run(Path(directory) / "hunter.sqlite3"))
        self.assertEqual(version, 3)
        self.assertEqual(loaded["provider_attempts"][0]["provider_id"], "provider")
        self.assertEqual(rows[0]["signature"], "sig")

    def test_offline_replay_does_not_need_network(self):
        samples = asyncio.run(
            OfflineReplayBenchmark(lambda value: value * 2).run([1, 2])
        )
        self.assertEqual([sample.result for sample in samples], [2, 4])
        self.assertTrue(all(sample.elapsed_ms >= 0 for sample in samples))

    def test_live_benchmark_requires_explicit_opt_in(self):
        with self.assertRaises(PermissionError):
            BenchmarkConfig().require_live_opt_in()
        BenchmarkConfig(allow_live_submission=True).require_live_opt_in()

    def test_provider_report_computes_percentiles_and_same_slot(self):
        executions = []
        for index, slots in enumerate((0, 1, 2, 3)):
            executions.append(
                {
                    "provider_attempts": [
                        {
                            "provider_id": "p",
                            "endpoint_id": "e",
                            "accepted": index != 3,
                            "submit_started_mono_ns": 0,
                            "acknowledged_mono_ns": (index + 1) * 1_000_000,
                        }
                    ],
                    "submission_started_mono_ns": 0,
                    "processed_mono_ns": (index + 2) * 1_000_000,
                    "submitted_slot": 100,
                    "landed_slot": 100 + slots,
                    "base_network_fee_lamports": 5_000,
                    "priority_fee_lamports": 1_000,
                    "jito_tip_lamports": 0,
                    "rent_lamports": 0,
                    "error_classification": (
                        ErrorClassification.CONFIRMATION_TIMEOUT.value
                        if index == 3
                        else None
                    ),
                }
            )
        report = summarize_provider_telemetry(executions, minimum_ranking_samples=5)[0]
        self.assertEqual(report.sample_count, 4)
        self.assertEqual(report.same_slot_percentage, 25)
        self.assertEqual(report.plus_one_slot_percentage, 25)
        self.assertEqual(report.plus_two_or_later_percentage, 50)
        self.assertEqual(report.failure_rate, 0.25)
        self.assertEqual(report.ambiguous_outcome_rate, 0.25)
        self.assertEqual(report.estimated_average_fee_lamports, 6_000)
        self.assertFalse(report.ranking_eligible)


if __name__ == "__main__":
    unittest.main()
