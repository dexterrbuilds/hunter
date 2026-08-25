"""Tests for the unintegrated future execution data contracts."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from execution.errors import ErrorClassification, ExecutionError  # noqa: E402
from execution.telemetry import ExecutionTelemetry  # noqa: E402
from utils.redaction import endpoint_identifier  # noqa: E402


class ExecutionContractTests(unittest.TestCase):
    """Check schema coverage and monotonic latency behavior."""

    def test_error_taxonomy_contains_required_categories(self) -> None:
        expected = {
            "configuration_error",
            "signing_failure",
            "rpc_transport_failure",
            "rpc_rate_limit",
            "rpc_rejection",
            "simulation_failure",
            "on_chain_program_failure",
            "blockhash_expired",
            "transaction_dropped",
            "confirmation_timeout",
            "accepted_but_not_observed",
            "insufficient_balance",
            "fee_rent_insufficiency",
            "unsupported_quote_token",
            "malformed_event_state",
        }
        self.assertTrue(expected.issubset({item.value for item in ErrorClassification}))

    def test_execution_error_renders_classification_and_code(self) -> None:
        error = ExecutionError(
            ErrorClassification.RPC_RATE_LIMIT,
            "provider throttled request",
            code=429,
            retryable=True,
        )
        self.assertEqual(
            str(error), "rpc_rate_limit (429): provider throttled request"
        )

    def test_telemetry_marks_monotonic_stages_and_calculates_latency(self) -> None:
        telemetry = ExecutionTelemetry(execution_id="offline-test")
        telemetry.mark("build_started")
        telemetry.mark("build_completed")

        self.assertIsNotNone(telemetry.build_started_at)
        self.assertIsNotNone(telemetry.build_completed_at)
        self.assertIsNotNone(
            telemetry.latency_ms("build_started", "build_completed")
        )
        self.assertGreaterEqual(
            telemetry.latency_ms("build_started", "build_completed"), 0
        )
        self.assertIsNone(telemetry.latency_ms("signing_started", "signing_completed"))
        with self.assertRaises(ValueError):
            telemetry.mark("not_a_stage")

    def test_endpoint_identifier_is_credential_free(self) -> None:
        endpoint = "wss://user:pass@provider.example/v2/secret?api-key=value"
        value = endpoint_identifier(endpoint)

        self.assertTrue(value.startswith("wss://provider.example#"))
        for secret in ("user", "pass", "/v2/secret", "api-key", "value"):
            self.assertNotIn(secret, value)


if __name__ == "__main__":
    unittest.main()
