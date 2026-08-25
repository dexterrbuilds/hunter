from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from solders.signature import Signature  # noqa: E402

from core.client import SolanaClient  # noqa: E402
from domain.lifecycle import ExecutionState  # noqa: E402
from execution.errors import ErrorClassification  # noqa: E402


class NoWait:
    async def acquire(self):
        return None


class RpcConfirm:
    def __init__(self, error=None):
        self.error = error

    async def confirm_transaction(self, *_args, **_kwargs):
        if self.error:
            raise self.error


class ConfirmationHarness(SolanaClient):
    def __init__(self, result, confirm_error=None, height=0):
        self._rate_limiter = NoWait()
        self.rpc = RpcConfirm(confirm_error)
        self.result = result
        self.height = height
        self.last_execution_telemetry = None

    async def get_client(self):
        return self.rpc

    async def _get_transaction_result(self, _signature):
        return self.result

    async def get_block_height(self):
        return self.height


class ConfirmationStateTests(unittest.TestCase):
    def test_confirmed_success(self):
        client = ConfirmationHarness({"slot": 9, "meta": {"err": None}})
        observation = asyncio.run(
            client.observe_transaction(Signature.default(), metadata_grace_seconds=0)
        )
        self.assertEqual(observation.state, ExecutionState.CONFIRMED)
        self.assertTrue(observation.succeeded)
        self.assertEqual(observation.slot, 9)

    def test_failed_on_chain_is_distinct(self):
        client = ConfirmationHarness(
            {"slot": 9, "meta": {"err": {"InstructionError": [1, 2]}}}
        )
        observation = asyncio.run(
            client.observe_transaction(Signature.default(), metadata_grace_seconds=0)
        )
        self.assertEqual(observation.state, ExecutionState.FAILED_ON_CHAIN)
        self.assertEqual(
            observation.error_classification,
            ErrorClassification.ON_CHAIN_PROGRAM_FAILURE,
        )

    def test_confirmed_status_but_metadata_not_visible_is_ambiguous(self):
        client = ConfirmationHarness(None)
        observation = asyncio.run(
            client.observe_transaction(Signature.default(), metadata_grace_seconds=0)
        )
        self.assertEqual(observation.state, ExecutionState.NOT_OBSERVED)
        self.assertEqual(
            observation.error_classification,
            ErrorClassification.ACCEPTED_BUT_NOT_OBSERVED,
        )

    def test_expired_blockhash_is_distinct_from_timeout(self):
        client = ConfirmationHarness(None, confirm_error=TimeoutError(), height=101)
        observation = asyncio.run(
            client.observe_transaction(
                Signature.default(),
                last_valid_block_height=100,
                metadata_grace_seconds=0,
            )
        )
        self.assertEqual(observation.state, ExecutionState.EXPIRED)
        self.assertEqual(
            observation.error_classification, ErrorClassification.BLOCKHASH_EXPIRED
        )

    def test_bool_compatibility_adapter_rejects_ambiguous(self):
        client = ConfirmationHarness(None)
        self.assertFalse(asyncio.run(client.confirm_transaction(Signature.default())))


if __name__ == "__main__":
    unittest.main()
