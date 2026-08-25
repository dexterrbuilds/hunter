"""Concrete single-provider boundaries backed by the existing SolanaClient."""

from __future__ import annotations

from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from core.client import SolanaClient
from execution.confirmation import TransactionObservation
from execution.ports import (
    AccountSnapshot,
    BlockhashContext,
    SignedTransaction,
    UnsignedTransaction,
)


class SolanaRpcAccountReader:
    """Account-read boundary using the configured standard JSON-RPC client."""

    def __init__(self, client: SolanaClient):
        self.client = client

    async def get_account(
        self, address: Pubkey, commitment: str = "confirmed"
    ) -> AccountSnapshot:
        account = await self.client.get_account_info(address, commitment=commitment)
        return AccountSnapshot(
            address=address,
            owner=account.owner,
            lamports=account.lamports,
            data=bytes(account.data),
            slot=None,
        )


class SolanaRpcBlockhashProvider:
    """Expiry-aware blockhash boundary."""

    def __init__(self, client: SolanaClient):
        self.client = client

    async def get_blockhash(self) -> BlockhashContext:
        return await self.client.get_latest_blockhash_context()


class LegacyTransactionConstructor:
    """Construct the same legacy Solana message shape as Milestone 1."""

    def build(
        self,
        instructions: list[Instruction],
        fee_payer: Pubkey,
        blockhash: BlockhashContext,
    ) -> UnsignedTransaction:
        message = Message.new_with_blockhash(
            instructions, fee_payer, Hash.from_string(blockhash.blockhash)
        )
        message_bytes = bytes(message)
        return UnsignedTransaction(
            message_bytes=message_bytes,
            transaction_size_bytes=len(message_bytes),
            required_signers=(fee_payer,),
        )


class LocalKeypairSigner:
    """Keypair signer boundary that never exposes secret bytes."""

    def __init__(self, keypair: Keypair):
        self.keypair = keypair

    @property
    def public_key(self) -> Pubkey:
        return self.keypair.pubkey()

    def sign_legacy(
        self,
        instructions: list[Instruction],
        blockhash: BlockhashContext,
    ) -> SignedTransaction:
        message = Message(instructions, self.public_key)
        transaction = Transaction(
            [self.keypair], message, Hash.from_string(blockhash.blockhash)
        )
        signature = str(transaction.signatures[0])
        return SignedTransaction(bytes(transaction), signature)


class SolanaRpcTransactionSubmitter:
    """One-provider standard sendTransaction boundary."""

    def __init__(self, client: SolanaClient):
        self.client = client

    async def submit_wire(
        self, transaction: SignedTransaction, *, skip_preflight: bool = True
    ) -> str:
        return await self.client.submit_wire_transaction(
            transaction.wire_bytes, skip_preflight=skip_preflight
        )


class SolanaRpcConfirmationService:
    """Confirmation boundary preserving ambiguous/terminal outcomes."""

    def __init__(self, client: SolanaClient):
        self.client = client

    async def observe(
        self,
        signature: str,
        *,
        commitment: str = "confirmed",
        last_valid_block_height: int | None = None,
    ) -> TransactionObservation:
        return await self.client.observe_transaction(
            signature,
            commitment=commitment,
            last_valid_block_height=last_valid_block_height,
        )


class SolanaRpcTransactionInspector:
    """Read authoritative transaction metadata separately from confirmation."""

    def __init__(self, client: SolanaClient):
        self.client = client

    async def get(self, signature: str) -> dict | None:
        return await self.client.get_transaction_result(signature)
