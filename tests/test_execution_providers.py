from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from execution.errors import ErrorClassification  # noqa: E402
from execution.ports import (  # noqa: E402
    BlockhashContext,
    ExecutionContext,
    SignedTransaction,
)
from execution.providers.adapters import (  # noqa: E402
    HeliusSenderSubmitter,
    JitoTransactionSubmitter,
    JsonRpcTransactionSubmitter,
    classify_provider_response,
    safe_fallback_failure,
)
from execution.providers.config import (  # noqa: E402
    ProviderEndpoint,
    ProviderKind,
    ProviderRole,
)
from execution.providers.transport import TransportResponse  # noqa: E402


class FakeTransport:
    def __init__(self, response=None, error=None):
        self.response = response or TransportResponse(200, {"result": "sig"}, {})
        self.error = error
        self.calls = []
        self.warm_calls = []
        self.closed = 0

    async def post_json(self, endpoint, payload, **kwargs):
        self.calls.append((endpoint, payload, kwargs))
        if self.error:
            raise self.error
        return self.response

    async def warm(self, endpoint, **kwargs):
        self.warm_calls.append((endpoint, kwargs))
        return True

    async def close(self):
        self.closed += 1


def endpoint(kind=ProviderKind.STANDARD_RPC, **kwargs):
    return ProviderEndpoint(
        provider_id=kwargs.pop("provider_id", "provider"),
        kind=kind,
        endpoint=kwargs.pop("url", "https://rpc.example/?api-key=secret"),
        **kwargs,
    )


def context(variant="standard", tip=0, price=1):
    return ExecutionContext(
        logical_trade_id="trade",
        execution_id="execution",
        execution_variant=variant,
        blockhash=BlockhashContext.observed("hash", 10),
        signature="sig",
        compute_unit_limit=100_000,
        compute_unit_price_micro_lamports=price,
        priority_fee_lamports=1,
        jito_tip_lamports=tip,
        metadata={"tip_instruction_count": 1 if tip else 0},
    )


class ProviderAdapterTests(unittest.TestCase):
    def test_standard_rpc_submits_base64_wire_and_normalizes_ack(self):
        transport = FakeTransport(
            TransportResponse(
                200,
                {"result": "sig"},
                {"x-bundle-id": "bundle-reference"},
                connection_reused=True,
                session_generation=2,
                session_created_for_request=False,
            )
        )
        submitter = JsonRpcTransactionSubmitter(endpoint(), transport)
        result = asyncio.run(
            submitter.submit(SignedTransaction(b"wire", "sig"), context())
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.signature, "sig")
        self.assertEqual(result.bytes_sent, 4)
        self.assertTrue(result.connection_reused)
        self.assertEqual(result.connection_session_generation, 2)
        self.assertFalse(result.connection_session_created)
        self.assertEqual(result.provider_reference, "bundle-reference")
        self.assertGreaterEqual(result.submit_rtt_ms, 0)
        request = transport.calls[0][1]
        self.assertEqual(request["method"], "sendTransaction")
        self.assertEqual(request["params"][0], "d2lyZQ==")
        self.assertNotIn("secret", result.endpoint_id)

    def test_signature_mismatch_fails_closed(self):
        transport = FakeTransport(TransportResponse(200, {"result": "other"}, {}))
        result = asyncio.run(
            JsonRpcTransactionSubmitter(endpoint(), transport).submit(
                SignedTransaction(b"wire", "sig"), context()
            )
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.error_classification, ErrorClassification.RPC_REJECTION)

    def test_timeout_is_normalized_without_throwing(self):
        result = asyncio.run(
            JsonRpcTransactionSubmitter(
                endpoint(), FakeTransport(error=TimeoutError())
            ).submit(SignedTransaction(b"wire", "sig"), context())
        )
        self.assertEqual(
            result.error_classification, ErrorClassification.RPC_TRANSPORT_FAILURE
        )

    def test_http_authentication_and_rate_limit_are_normalized(self):
        self.assertEqual(
            classify_provider_response(401, {}),
            ErrorClassification.PROVIDER_AUTHENTICATION_FAILURE,
        )
        self.assertEqual(
            classify_provider_response(429, {}), ErrorClassification.RPC_RATE_LIMIT
        )

    def test_duplicate_signature_is_not_a_new_transaction_authority(self):
        classification = classify_provider_response(
            200, {"error": {"code": -1, "message": "already processed"}}
        )
        self.assertEqual(classification, ErrorClassification.DUPLICATE_SIGNATURE)
        self.assertFalse(safe_fallback_failure(classification))

        transport = FakeTransport(
            TransportResponse(
                200,
                {"error": {"code": -1, "message": "already processed"}},
                {},
            )
        )
        result = asyncio.run(
            JsonRpcTransactionSubmitter(endpoint(), transport).submit(
                SignedTransaction(b"wire", "sig"), context()
            )
        )
        self.assertTrue(result.acceptable_acknowledgement)
        self.assertEqual(result.signature, "sig")
        self.assertEqual(result.acknowledgement, "duplicate_signature")

    def test_helius_sender_requires_explicit_tipped_variant(self):
        item = endpoint(
            ProviderKind.HELIUS_SENDER,
            minimum_tip_lamports=5_000,
            maximum_tip_lamports=100_000,
        )
        result = asyncio.run(
            HeliusSenderSubmitter(item, FakeTransport()).submit(
                SignedTransaction(b"wire", "sig"), context()
            )
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.bytes_sent, 0)

    def test_helius_sender_accepts_one_tipped_priority_fee_variant(self):
        transport = FakeTransport()
        item = endpoint(
            ProviderKind.HELIUS_SENDER,
            minimum_tip_lamports=5_000,
            maximum_tip_lamports=100_000,
        )
        result = asyncio.run(
            HeliusSenderSubmitter(item, transport).submit(
                SignedTransaction(b"wire", "sig"),
                context("helius_sender_tipped", 5_000),
            )
        )
        self.assertTrue(result.accepted)
        options = transport.calls[0][1]["params"][1]
        self.assertTrue(options["skipPreflight"])
        self.assertEqual(options["maxRetries"], 0)

    def test_helius_sender_rejects_double_tip_metadata(self):
        value = context("helius_sender_tipped", 5_000)
        value.metadata["tip_instruction_count"] = 2
        item = endpoint(ProviderKind.HELIUS_SENDER, minimum_tip_lamports=1)
        result = asyncio.run(
            HeliusSenderSubmitter(item, FakeTransport()).submit(
                SignedTransaction(b"wire", "sig"), value
            )
        )
        self.assertEqual(
            result.error_classification, ErrorClassification.CONFIGURATION_ERROR
        )

    def test_jito_normal_transaction_can_relay_untipped_wire(self):
        result = asyncio.run(
            JitoTransactionSubmitter(
                endpoint(ProviderKind.JITO), FakeTransport()
            ).submit(SignedTransaction(b"wire", "sig"), context())
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.acknowledgement, "jito_signature")

    def test_jito_bundle_only_requires_minimum_tip_and_query_flag(self):
        transport = FakeTransport()
        item = endpoint(
            ProviderKind.JITO,
            url="https://mainnet.block-engine.jito.wtf/api/v1/transactions",
            bundle_only=True,
            minimum_tip_lamports=1_000,
            maximum_tip_lamports=10_000,
        )
        too_low = asyncio.run(
            JitoTransactionSubmitter(item, transport).submit(
                SignedTransaction(b"wire", "sig"), context("jito_tipped", 999)
            )
        )
        self.assertEqual(too_low.error_classification, ErrorClassification.TIP_TOO_LOW)
        accepted = asyncio.run(
            JitoTransactionSubmitter(item, transport).submit(
                SignedTransaction(b"wire", "sig"), context("jito_tipped", 1_000)
            )
        )
        self.assertTrue(accepted.accepted)
        self.assertIn("bundleOnly=true", transport.calls[-1][0])

    def test_jito_tip_above_cap_is_rejected_before_bytes_leave(self):
        item = endpoint(ProviderKind.JITO, maximum_tip_lamports=1_000)
        result = asyncio.run(
            JitoTransactionSubmitter(item, FakeTransport()).submit(
                SignedTransaction(b"wire", "sig"), context("jito_tipped", 1_001)
            )
        )
        self.assertEqual(
            result.error_classification, ErrorClassification.RISK_LIMIT_EXCEEDED
        )
        self.assertEqual(result.bytes_sent, 0)

    def test_provider_role_validation_rejects_sender_reads(self):
        with self.assertRaises(ValueError):
            endpoint(
                ProviderKind.HELIUS_SENDER,
                roles=frozenset({ProviderRole.ACCOUNT_READ}),
            )

    def test_warmup_reuses_adapter_transport(self):
        transport = FakeTransport()
        item = endpoint(warmup_endpoint="https://rpc.example/ping")
        submitter = JsonRpcTransactionSubmitter(item, transport)
        self.assertTrue(asyncio.run(submitter.warm()))
        asyncio.run(submitter.submit(SignedTransaction(b"wire", "sig"), context()))
        self.assertEqual(len(transport.warm_calls), 1)
        self.assertEqual(len(transport.calls), 1)

    def test_standard_rpc_warmup_uses_non_economic_gethealth(self):
        transport = FakeTransport(TransportResponse(200, {"result": "ok"}, {}))
        submitter = JsonRpcTransactionSubmitter(endpoint(), transport)
        self.assertTrue(asyncio.run(submitter.warm()))
        self.assertEqual(transport.calls[0][1]["method"], "getHealth")
        self.assertFalse(transport.warm_calls)


if __name__ == "__main__":
    unittest.main()
