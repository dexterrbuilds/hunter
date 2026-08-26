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
    SubmissionResult,
)
from execution.providers.config import BroadcastMode  # noqa: E402
from execution.routing import SubmissionRouter  # noqa: E402


class FakeSubmitter:
    def __init__(self, provider_id, *, delay=0, accepted=True, classification=None):
        self._provider_id = provider_id
        self.delay = delay
        self.accepted = accepted
        self.classification = classification
        self.calls = []
        self.closed = 0

    @property
    def provider_id(self):
        return self._provider_id

    async def submit(self, transaction, context):
        self.calls.append((transaction, context))
        if self.delay:
            await asyncio.sleep(self.delay)
        return SubmissionResult(
            transaction.signature,
            self.provider_id,
            f"https://{self.provider_id}.invalid#safe",
            execution_variant=context.execution_variant,
            accepted=self.accepted,
            error_classification=self.classification,
        )

    async def close(self):
        self.closed += 1


def values():
    transaction = SignedTransaction(b"identical-wire", "sig")
    context = ExecutionContext(
        "trade",
        "execution",
        "standard",
        BlockhashContext.observed("hash", 10),
        "sig",
    )
    return transaction, context


class SubmissionRoutingTests(unittest.TestCase):
    def test_single_uses_only_primary(self):
        primary, secondary = FakeSubmitter("a"), FakeSubmitter("b")
        router = SubmissionRouter([primary, secondary])
        result = asyncio.run(router.submit(*values()))
        self.assertEqual(result.provider_id, "a")
        self.assertEqual(len(primary.calls), 1)
        self.assertEqual(len(secondary.calls), 0)

    def test_race_returns_first_ack_and_reuses_same_identity(self):
        async def run():
            slow = FakeSubmitter("slow", delay=0.02)
            fast = FakeSubmitter("fast")
            attempts = []
            router = SubmissionRouter(
                [slow, fast],
                mode=BroadcastMode.RACE,
                attempt_callback=attempts.append,
            )
            transaction, context = values()
            result = await router.submit(transaction, context)
            await router.drain()
            return result, slow, fast, attempts

        result, slow, fast, attempts = asyncio.run(run())
        self.assertEqual(result.provider_id, "fast")
        self.assertEqual({item.provider_id for item in attempts}, {"slow", "fast"})
        for submitter in (slow, fast):
            sent, context = submitter.calls[0]
            self.assertEqual(sent.wire_bytes, b"identical-wire")
            self.assertEqual(sent.signature, context.signature)

    def test_hedged_does_not_call_secondary_before_delay_when_primary_acks(self):
        primary, secondary = FakeSubmitter("a"), FakeSubmitter("b")
        router = SubmissionRouter(
            [primary, secondary], mode=BroadcastMode.HEDGED, hedge_delay_ms=20
        )
        result = asyncio.run(router.submit(*values()))
        self.assertEqual(result.provider_id, "a")
        self.assertFalse(secondary.calls)

    def test_hedged_relays_same_wire_after_delay(self):
        async def run():
            primary = FakeSubmitter("a", delay=0.05)
            secondary = FakeSubmitter("b")
            router = SubmissionRouter(
                [primary, secondary], mode=BroadcastMode.HEDGED, hedge_delay_ms=1
            )
            result = await router.submit(*values())
            await router.drain()
            return result, primary, secondary

        result, primary, secondary = asyncio.run(run())
        self.assertEqual(result.provider_id, "b")
        self.assertEqual(
            primary.calls[0][0].wire_bytes, secondary.calls[0][0].wire_bytes
        )

    def test_fallback_uses_secondary_only_for_safe_transport_failure(self):
        primary = FakeSubmitter(
            "a",
            accepted=False,
            classification=ErrorClassification.PROVIDER_UNAVAILABLE,
        )
        secondary = FakeSubmitter("b")
        router = SubmissionRouter([primary, secondary], mode=BroadcastMode.FALLBACK)
        result = asyncio.run(router.submit(*values()))
        self.assertEqual(result.provider_id, "b")

    def test_fallback_stops_on_ambiguous_duplicate_signature(self):
        primary = FakeSubmitter(
            "a",
            accepted=False,
            classification=ErrorClassification.DUPLICATE_SIGNATURE,
        )
        secondary = FakeSubmitter("b")
        router = SubmissionRouter([primary, secondary], mode=BroadcastMode.FALLBACK)
        result = asyncio.run(router.submit(*values()))
        self.assertEqual(result.provider_id, "a")
        self.assertFalse(secondary.calls)

    def test_context_signature_mismatch_is_rejected_before_submission(self):
        transaction, context = values()
        transaction = SignedTransaction(transaction.wire_bytes, "different")
        with self.assertRaises(ValueError):
            asyncio.run(
                SubmissionRouter([FakeSubmitter("a")]).submit(transaction, context)
            )

    def test_single_uses_health_only_after_every_provider_has_enough_samples(self):
        slow, fast = FakeSubmitter("slow"), FakeSubmitter("fast")
        router = SubmissionRouter([slow, fast])
        for index in range(20):
            router.health.record_submission(
                SubmissionResult(
                    f"slow-{index}",
                    "slow",
                    "slow-endpoint",
                    submit_started_mono_ns=0,
                    acknowledged_mono_ns=50_000_000,
                )
            )
            router.health.record_submission(
                SubmissionResult(
                    f"fast-{index}",
                    "fast",
                    "fast-endpoint",
                    submit_started_mono_ns=0,
                    acknowledged_mono_ns=5_000_000,
                )
            )

        result = asyncio.run(router.submit(*values()))

        self.assertEqual(result.provider_id, "fast")
        self.assertFalse(slow.calls)


if __name__ == "__main__":
    unittest.main()
