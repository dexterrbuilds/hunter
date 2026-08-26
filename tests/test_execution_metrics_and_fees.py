from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from application.risk import FeeExposure  # noqa: E402
from core.client import SolanaClient  # noqa: E402
from core.priority_fee.manager import PriorityFeeManager  # noqa: E402
from core.priority_fee.strategy import PriorityFeeCache  # noqa: E402
from domain.amounts import Lamports  # noqa: E402
from execution.errors import ErrorClassification  # noqa: E402
from execution.health import ProviderHealthRegistry, ProviderHealthTracker  # noqa: E402
from execution.metrics import (  # noqa: E402
    LandingMetrics,
    LatencyBudgets,
    budget_warnings,
    same_slot_classification,
    slot_delta,
)
from execution.ports import BlockhashContext, SubmissionResult  # noqa: E402
from execution.providers.config import ExecutionRoutingConfig  # noqa: E402
from execution.telemetry import (  # noqa: E402
    ExecutionTelemetry,
    ProviderAttemptTelemetry,
    priority_fee_lamports,
)


class ExecutionMetricsAndFeeTests(unittest.TestCase):
    def test_submission_slot_capture_reads_solana_response_value(self):
        class SlotRpc:
            async def get_slot(self, *, commitment):
                self.commitment = commitment
                return SimpleNamespace(value=321)

        client = SolanaClient.__new__(SolanaClient)
        rpc = SlotRpc()

        async def get_client(_role):
            return rpc

        client._get_client_for_role = get_client
        telemetry = ExecutionTelemetry("execution")
        asyncio.run(client._capture_submission_slot(telemetry))
        self.assertEqual(telemetry.submitted_slot, 321)
        self.assertEqual(rpc.commitment, "processed")

    def test_blockhash_age_rule_uses_monotonic_time(self):
        context = BlockhashContext(
            "hash",
            10,
            fetched_at=datetime.now(UTC),
            fetched_mono=100.0,
        )
        self.assertEqual(context.age_ms(100.125), 125)
        self.assertTrue(context.is_acceptable_age(125, 100.125))
        self.assertFalse(context.is_acceptable_age(124, 100.125))

    def test_blockhash_negative_age_budget_is_invalid(self):
        context = BlockhashContext("hash", 10)
        with self.assertRaises(ValueError):
            context.is_acceptable_age(-1)

    def test_priority_fee_rounds_up_to_lamports(self):
        self.assertEqual(priority_fee_lamports(1, 1), 1)
        self.assertEqual(priority_fee_lamports(1_000_000, 100_000), 100_000)

    def test_jito_tip_is_separate_and_included_in_fee_guard(self):
        exposure = FeeExposure(
            Lamports(5_000),
            Lamports(2_000),
            Lamports(3_000),
            jito_tip=Lamports(4_000),
            other_known_cost=Lamports(1_000),
        )
        self.assertEqual(exposure.maximum_known_lamports, 15_000)

    def test_combined_fee_guard_rejects_tip_plus_priority(self):
        config = ExecutionRoutingConfig(maximum_combined_fee_lamports=10)
        with self.assertRaisesRegex(Exception, "known fee exposure"):
            config.validate_fee_exposure(
                base_fee_lamports=None,
                priority_fee_lamports=6,
                jito_tip_lamports=5,
                rent_lamports=0,
            )

    def test_priority_fee_cache_records_source_age_and_latency(self):
        calls = []

        async def estimator(accounts):
            calls.append(accounts)
            return 123

        async def run():
            cache = PriorityFeeCache(estimator, source="fake", ttl_seconds=10)
            first = await cache.get(["account"])
            second = await cache.get(["other"])
            await cache.close()
            return first, second

        first, second = asyncio.run(run())
        self.assertEqual(len(calls), 1)
        self.assertEqual(first.selected_micro_lamports_per_cu, 123)
        self.assertEqual(second.source, "fake")
        self.assertGreaterEqual(second.estimate_age_ms, 0)

    def test_periodic_priority_fee_refresh_is_bounded_and_stoppable(self):
        calls = 0

        async def estimator(_accounts):
            nonlocal calls
            calls += 1
            return calls

        async def run():
            cache = PriorityFeeCache(
                estimator,
                source="periodic",
                ttl_seconds=1,
                refresh_interval_seconds=0.005,
            )
            await cache.start_periodic()
            await asyncio.sleep(0.016)
            await cache.close()

        asyncio.run(run())
        self.assertGreaterEqual(calls, 2)

    def test_landing_metrics_keep_detection_and_submission_slots_separate(self):
        telemetry = ExecutionTelemetry("execution")
        telemetry.detected_mono_ns = 1_000_000
        telemetry.build_started_mono_ns = 2_000_000
        telemetry.build_completed_mono_ns = 4_000_000
        telemetry.signing_started_mono_ns = 4_000_000
        telemetry.signing_completed_mono_ns = 5_000_000
        telemetry.submission_started_mono_ns = 6_000_000
        telemetry.rpc_responded_mono_ns = 8_000_000
        telemetry.processed_mono_ns = 16_000_000
        telemetry.detection_slot = 100
        telemetry.launch_slot = 99
        telemetry.submitted_slot = 101
        telemetry.landed_slot = 102
        metrics = LandingMetrics.from_telemetry(telemetry)
        self.assertEqual(metrics.detection_to_build_ms, 1)
        self.assertEqual(metrics.build_ms, 2)
        self.assertEqual(metrics.sign_ms, 1)
        self.assertEqual(metrics.submit_rtt_ms, 2)
        self.assertEqual(metrics.submit_to_landed_ms, 10)
        self.assertEqual(metrics.detection_to_landed_ms, 15)
        self.assertEqual(metrics.slots_to_land, 1)
        self.assertEqual(metrics.slots_from_detection, 2)
        self.assertEqual(metrics.slots_from_launch, 3)

    def test_same_slot_classification_is_explicit(self):
        self.assertEqual(same_slot_classification(10, 10), "same_slot")
        self.assertEqual(same_slot_classification(10, 11), "+1_slot")
        self.assertEqual(same_slot_classification(10, 13), "+3_slots")
        self.assertEqual(same_slot_classification(None, 13), "unknown")
        self.assertEqual(same_slot_classification(14, 13), "inconsistent")
        self.assertEqual(slot_delta(10, 13), 3)

    def test_latency_budgets_warn_but_do_not_raise(self):
        warnings = budget_warnings(
            {"transaction_build_ms": 6.0, "signing_ms": 0.5},
            LatencyBudgets(transaction_build_ms=5.0, signing_ms=1.0),
        )
        self.assertEqual(len(warnings), 1)
        self.assertIn("transaction_build_ms", warnings[0])

    def test_provider_health_uses_rolling_statistics(self):
        tracker = ProviderHealthTracker("provider", "endpoint", window_size=3)
        for rtt, accepted, classification in (
            (10, True, None),
            (20, False, ErrorClassification.RPC_RATE_LIMIT),
            (30, True, None),
            (40, False, ErrorClassification.PROVIDER_UNAVAILABLE),
        ):
            tracker.record_submission(
                SubmissionResult(
                    "sig",
                    "provider",
                    "endpoint",
                    accepted=accepted,
                    submit_started_mono_ns=0,
                    acknowledged_mono_ns=rtt * 1_000_000,
                    error_classification=classification,
                )
            )
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot.sample_count, 3)
        self.assertEqual(snapshot.median_submission_rtt_ms, 30)
        self.assertEqual(snapshot.rate_limits, 1)
        self.assertEqual(snapshot.transport_errors, 1)
        self.assertFalse(snapshot.enough_samples)

    def test_provider_health_registry_does_not_rank_tiny_samples(self):
        registry = ProviderHealthRegistry()
        registry.record_submission(SubmissionResult("sig", "p", "e"))
        registry.record_landing(
            "p",
            "e",
            "sig",
            landed=True,
            slots_to_land=1,
            status_query_rtt_ms=4.0,
        )
        snapshot = registry.snapshots()[0]
        self.assertFalse(snapshot.enough_samples)
        self.assertEqual(snapshot.landed_sample_count, 1)
        self.assertEqual(snapshot.median_slots_to_land, 1)
        self.assertEqual(snapshot.status_query_median_rtt_ms, 4.0)

    def test_provider_health_landing_matches_signature_not_completion_order(self):
        tracker = ProviderHealthTracker("provider", "endpoint")
        tracker.record_submission(SubmissionResult("first", "provider", "endpoint"))
        tracker.record_submission(SubmissionResult("second", "provider", "endpoint"))
        tracker.record_landing("first", landed=True, slots_to_land=2)

        samples = list(tracker._samples)
        self.assertTrue(samples[0].landed)
        self.assertEqual(samples[0].slots_to_land, 2)
        self.assertIsNone(samples[1].landed)

    def test_provider_estimated_mode_requires_an_explicit_estimator(self):
        with self.assertRaisesRegex(ValueError, "require a provider estimator"):
            PriorityFeeManager(
                client=object(),
                enable_dynamic_fee=False,
                enable_fixed_fee=False,
                fixed_fee=0,
                extra_fee=0,
                hard_cap=1,
                strategy="provider_estimated",
            )

    def test_dynamic_priority_fee_telemetry_uses_zero_age_and_monotonic_selection(self):
        manager = PriorityFeeManager(
            client=object(),
            enable_dynamic_fee=True,
            enable_fixed_fee=False,
            fixed_fee=0,
            extra_fee=0,
            hard_cap=500,
        )

        async def estimate(_accounts):
            return 123

        manager.dynamic_fee_plugin.get_priority_fee = estimate
        self.assertEqual(asyncio.run(manager.calculate_priority_fee()), 123)
        self.assertEqual(manager.last_selection.estimate_age_ms, 0.0)
        self.assertGreater(manager.last_selection.selected_at_mono, 0)

    def test_provider_attempt_rtt_is_monotonic(self):
        attempt = ProviderAttemptTelemetry(
            provider_id="p",
            endpoint_id="e",
            execution_variant="standard",
            signature="sig",
            accepted=True,
            acknowledgement="signature",
            bytes_sent=10,
            submit_started_mono_ns=1,
            acknowledged_mono_ns=1_000_001,
        )
        self.assertEqual(attempt.submit_rtt_ms, 1)


if __name__ == "__main__":
    unittest.main()
