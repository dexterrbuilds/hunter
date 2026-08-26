"""Offline tests for the current Solana transaction construction path."""

from __future__ import annotations

import asyncio
import struct
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from solders.compute_budget import ID as COMPUTE_BUDGET_PROGRAM_ID  # noqa: E402
from solders.hash import Hash  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.signature import Signature  # noqa: E402
from solders.system_program import TransferParams, transfer  # noqa: E402

from core.client import SolanaClient  # noqa: E402
from core.pubkeys import SystemAddresses  # noqa: E402
from execution.errors import ErrorClassification, ExecutionError  # noqa: E402


class _NoWaitRateLimiter:
    async def acquire(self) -> None:
        return None


class _CapturingRpc:
    def __init__(self) -> None:
        self.transaction = None
        self.options = None

    async def send_transaction(self, transaction, options):
        self.transaction = transaction
        self.options = options
        return SimpleNamespace(value=transaction.signatures[0])


class _MismatchingRpc(_CapturingRpc):
    async def send_transaction(self, transaction, options):
        await super().send_transaction(transaction, options)
        return SimpleNamespace(value=Signature.default())


class _TransactionClientHarness(SolanaClient):
    def __init__(self) -> None:
        self.rpc = _CapturingRpc()
        self._rate_limiter = _NoWaitRateLimiter()

    async def get_client(self):
        return self.rpc

    async def get_cached_blockhash(self) -> Hash:
        return Hash.default()


class TransactionConstructionTests(unittest.TestCase):
    """Lock compute-budget placement, signing, and encoded arguments."""

    def test_compute_budget_precedes_protocol_instructions(self) -> None:
        client = _TransactionClientHarness()
        signer = Keypair.from_seed(bytes(range(32)))
        recipient = Pubkey.from_string("5wyFsNExysbXf2hTtcn8Tqd3urs9Nv85Zx1zNdAfTMmX")
        protocol_instruction = transfer(
            TransferParams(
                from_pubkey=signer.pubkey(),
                to_pubkey=recipient,
                lamports=123,
            )
        )

        submitted = []
        signature = asyncio.run(
            client.build_and_send_transaction(
                [protocol_instruction],
                signer,
                skip_preflight=True,
                max_retries=1,
                priority_fee=321_000,
                compute_unit_limit=180_000,
                account_data_size_limit=12_500_000,
                submission_callback=lambda signature, context: submitted.append(
                    (str(signature), context)
                ),
            )
        )

        transaction = client.rpc.transaction
        self.assertIsNotNone(transaction)
        self.assertEqual(signature, transaction.signatures[0])
        self.assertEqual(submitted, [(str(transaction.signatures[0]), None)])
        transaction.verify()
        message = transaction.message
        compiled = list(message.instructions)
        program_ids = [
            message.account_keys[instruction.program_id_index]
            for instruction in compiled
        ]
        self.assertEqual(
            program_ids,
            [
                COMPUTE_BUDGET_PROGRAM_ID,
                COMPUTE_BUDGET_PROGRAM_ID,
                COMPUTE_BUDGET_PROGRAM_ID,
                SystemAddresses.SYSTEM_PROGRAM,
            ],
        )

        self.assertEqual(struct.unpack("<BI", bytes(compiled[0].data)), (4, 12_500_000))
        self.assertEqual(struct.unpack("<BI", bytes(compiled[1].data)), (2, 180_000))
        self.assertEqual(struct.unpack("<BQ", bytes(compiled[2].data)), (3, 321_000))
        self.assertEqual(bytes(compiled[3].data), bytes(protocol_instruction.data))
        self.assertEqual(message.recent_blockhash, Hash.default())
        self.assertEqual(message.signer_keys(), [signer.pubkey()])
        self.assertTrue(client.rpc.options.skip_preflight)
        telemetry = client.last_execution_telemetry
        self.assertIsNotNone(telemetry.trade_requested_at)
        self.assertIsNotNone(telemetry.build_started_at)
        self.assertIsNotNone(telemetry.build_completed_at)
        self.assertIsNotNone(telemetry.signing_started_at)
        self.assertIsNotNone(telemetry.signing_completed_at)
        self.assertIsNotNone(telemetry.submission_started_at)
        self.assertIsNotNone(telemetry.rpc_responded_at)
        self.assertIsNotNone(telemetry.signature_received_at)
        self.assertEqual(
            telemetry.transaction_signature, str(transaction.signatures[0])
        )
        self.assertGreater(telemetry.transaction_size_bytes, 0)
        self.assertGreaterEqual(telemetry.attributes["serialization_ms"], 0)
        self.assertEqual(telemetry.compute_unit_limit, 180_000)
        self.assertEqual(telemetry.priority_fee_lamports, 57_780)

    def test_no_compute_settings_preserves_protocol_instruction_as_first(self) -> None:
        client = _TransactionClientHarness()
        signer = Keypair.from_seed(bytes(reversed(range(32))))
        protocol_instruction = transfer(
            TransferParams(
                from_pubkey=signer.pubkey(),
                to_pubkey=Pubkey.default(),
                lamports=1,
            )
        )

        asyncio.run(
            client.build_and_send_transaction(
                [protocol_instruction],
                signer,
                max_retries=1,
            )
        )

        message = client.rpc.transaction.message
        self.assertEqual(len(message.instructions), 1)
        self.assertEqual(
            message.account_keys[message.instructions[0].program_id_index],
            SystemAddresses.SYSTEM_PROGRAM,
        )

    def test_explicit_jito_tip_is_one_distinct_instruction_variant(self) -> None:
        client = _TransactionClientHarness()
        client.execution_variant = "jito_tipped"
        client.jito_tip_lamports = 1_000
        client.jito_tip_account = Pubkey.new_unique()
        signer = Keypair.from_seed(bytes(range(32)))
        protocol_instruction = transfer(
            TransferParams(
                from_pubkey=signer.pubkey(),
                to_pubkey=Pubkey.new_unique(),
                lamports=7,
            )
        )

        asyncio.run(
            client.build_and_send_transaction(
                [protocol_instruction],
                signer,
                max_retries=1,
                priority_fee=1,
                compute_unit_limit=10,
            )
        )

        message = client.rpc.transaction.message
        programs = [
            message.account_keys[item.program_id_index] for item in message.instructions
        ]
        self.assertEqual(programs.count(SystemAddresses.SYSTEM_PROGRAM), 2)
        self.assertEqual(programs[0], SystemAddresses.SYSTEM_PROGRAM)
        self.assertEqual(client.last_execution_telemetry.jito_tip_lamports, 1_000)
        self.assertEqual(
            client.last_execution_telemetry.attributes["tip_instruction_count"], 1
        )

    def test_standard_rpc_signature_mismatch_is_rejected_and_measured(self) -> None:
        client = _TransactionClientHarness()
        client.rpc = _MismatchingRpc()
        signer = Keypair.from_seed(bytes(range(32)))
        instruction = transfer(
            TransferParams(
                from_pubkey=signer.pubkey(),
                to_pubkey=Pubkey.new_unique(),
                lamports=1,
            )
        )

        with self.assertRaises(ExecutionError):
            asyncio.run(
                client.build_and_send_transaction([instruction], signer, max_retries=1)
            )

        attempt = client.last_execution_telemetry.provider_attempts[0]
        self.assertFalse(attempt.accepted)
        self.assertEqual(
            attempt.error_classification, ErrorClassification.RPC_REJECTION
        )


if __name__ == "__main__":
    unittest.main()
