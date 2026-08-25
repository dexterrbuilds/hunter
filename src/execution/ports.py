"""Protocols for Hunter's future provider-neutral execution boundary.

Milestone 1 defines these contracts for review. The existing ``SolanaClient``
continues to build, sign, submit, and confirm transactions unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from solders.instruction import Instruction
from solders.pubkey import Pubkey

from execution.telemetry import ExecutionTelemetry


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Raw account state observed in one RPC response context."""

    address: Pubkey
    owner: Pubkey
    lamports: int
    data: bytes
    slot: int


@dataclass(frozen=True, slots=True)
class BlockhashContext:
    """Blockhash plus the context required to enforce expiry."""

    blockhash: str
    last_valid_block_height: int
    observed_slot: int | None = None
    provider_id: str | None = None


@dataclass(frozen=True, slots=True)
class PriorityFeeEstimate:
    """Estimated compute-unit price and maximum total fee."""

    micro_lamports_per_compute_unit: int
    compute_unit_limit: int
    maximum_lamports: int
    observed_slot: int | None = None
    source: str | None = None


@dataclass(frozen=True, slots=True)
class UnsignedTransaction:
    """Canonical unsigned transaction payload and build metadata."""

    message_bytes: bytes
    transaction_size_bytes: int
    required_signers: tuple[Pubkey, ...]


@dataclass(frozen=True, slots=True)
class SignedTransaction:
    """Opaque signed transaction bytes ready for submission."""

    wire_bytes: bytes
    signature: str


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    """Immediate provider response; not proof of landing or success."""

    signature: str
    provider_id: str
    endpoint_id: str
    submitted_slot: int | None = None


@dataclass(frozen=True, slots=True)
class ConfirmationResult:
    """Observed commitment and on-chain execution outcome."""

    signature: str
    landed_slot: int | None
    processed: bool
    confirmed: bool
    finalized: bool
    meta_error: object | None = None


class AccountReader(Protocol):
    """Read account data without coupling callers to an RPC client."""

    async def get_account(
        self, address: Pubkey, commitment: str
    ) -> AccountSnapshot: ...

    async def get_multiple_accounts(
        self, addresses: list[Pubkey], commitment: str
    ) -> list[AccountSnapshot | None]: ...


class BlockhashProvider(Protocol):
    """Provide blockhashes with explicit validity context."""

    async def get_blockhash(self) -> BlockhashContext: ...


class PriorityFeeEstimator(Protocol):
    """Estimate a compute-unit price for the exact writable account set."""

    async def estimate(
        self, writable_accounts: list[Pubkey], compute_unit_limit: int
    ) -> PriorityFeeEstimate: ...


class TransactionBuilder(Protocol):
    """Build deterministic unsigned transactions from ordered instructions."""

    def build(
        self,
        instructions: list[Instruction],
        fee_payer: Pubkey,
        blockhash: BlockhashContext,
    ) -> UnsignedTransaction: ...


class Signer(Protocol):
    """Sign canonical messages without exposing secret key material."""

    @property
    def public_key(self) -> Pubkey: ...

    async def sign(self, transaction: UnsignedTransaction) -> SignedTransaction: ...


class TransactionSubmitter(Protocol):
    """Submit one signed transaction through one delivery adapter."""

    async def submit(self, transaction: SignedTransaction) -> SubmissionResult: ...


class ConfirmationService(Protocol):
    """Observe landing and execution independently from submission."""

    async def confirm(
        self, signature: str, blockhash: BlockhashContext, commitment: str
    ) -> ConfirmationResult: ...


class TelemetrySink(Protocol):
    """Persist or emit non-blocking, credential-safe execution telemetry."""

    async def record(self, telemetry: ExecutionTelemetry) -> None: ...
