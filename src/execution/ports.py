"""Provider-neutral execution contracts used by Hunter's delivery layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol

from solders.instruction import Instruction
from solders.pubkey import Pubkey

from execution.errors import ErrorClassification
from execution.telemetry import ExecutionTelemetry


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Raw account state observed in one RPC response context."""

    address: Pubkey
    owner: Pubkey
    lamports: int
    data: bytes
    slot: int | None


@dataclass(frozen=True, slots=True)
class BlockhashContext:
    """Blockhash plus the context required to enforce expiry."""

    blockhash: str
    last_valid_block_height: int
    observed_slot: int | None = None
    provider_id: str | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    fetched_mono: float = 0.0

    @classmethod
    def observed(
        cls,
        blockhash: str,
        last_valid_block_height: int,
        *,
        observed_slot: int | None = None,
        provider_id: str | None = None,
    ) -> "BlockhashContext":
        return cls(
            blockhash=blockhash,
            last_valid_block_height=last_valid_block_height,
            observed_slot=observed_slot,
            provider_id=provider_id,
            fetched_at=datetime.now(UTC),
            fetched_mono=monotonic(),
        )

    def age_seconds(self, now_mono: float | None = None) -> float:
        if self.fetched_mono <= 0:
            return 0.0
        return (monotonic() if now_mono is None else now_mono) - self.fetched_mono

    def age_ms(self, now_mono: float | None = None) -> float:
        """Return monotonic age in milliseconds."""
        return self.age_seconds(now_mono) * 1_000

    def is_acceptable_age(
        self, maximum_age_ms: float, now_mono: float | None = None
    ) -> bool:
        """Whether the cache entry is within an explicit freshness budget."""
        if maximum_age_ms < 0:
            raise ValueError("maximum blockhash age must be non-negative")
        return self.age_ms(now_mono) <= maximum_age_ms

    def is_expired(self, current_block_height: int) -> bool:
        return current_block_height > self.last_valid_block_height


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
class ExecutionContext:
    """Immutable identity and fee context shared by delivery transports."""

    logical_trade_id: str
    execution_id: str
    execution_variant: str
    blockhash: BlockhashContext
    signature: str
    compute_unit_limit: int | None = None
    compute_unit_price_micro_lamports: int | None = None
    base_network_fee_lamports: int | None = None
    priority_fee_lamports: int | None = None
    jito_tip_lamports: int = 0
    rent_lamports: int = 0
    other_known_cost_lamports: int = 0
    detection_slot: int | None = None
    launch_slot: int | None = None
    detected_mono_ns: int | None = None
    metadata: dict[str, str | int | float | bool] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    """Immediate provider response; not proof of landing or success."""

    signature: str
    provider_id: str
    endpoint_id: str
    execution_variant: str = "standard"
    accepted: bool = True
    acknowledgement: str = "signature"
    bytes_sent: int = 0
    connection_reused: bool | None = None
    connection_session_generation: int | None = None
    connection_session_created: bool | None = None
    submit_started_mono_ns: int | None = None
    acknowledged_mono_ns: int | None = None
    response_wall_time: datetime | None = None
    submitted_slot: int | None = None
    provider_reference: str | None = None
    error_classification: ErrorClassification | None = None
    error_code: str | int | None = None
    diagnostic: str | None = None

    @property
    def submit_rtt_ms(self) -> float | None:
        """Transport acknowledgement latency measured monotonically."""
        if self.submit_started_mono_ns is None or self.acknowledged_mono_ns is None:
            return None
        return (self.acknowledged_mono_ns - self.submit_started_mono_ns) / 1_000_000

    @property
    def acceptable_acknowledgement(self) -> bool:
        """Whether routing may treat this response as transport acceptance."""
        return self.accepted and self.signature != ""


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

    @property
    def provider_id(self) -> str: ...

    async def submit(
        self,
        transaction: SignedTransaction,
        execution_context: ExecutionContext,
    ) -> SubmissionResult: ...

    async def close(self) -> None: ...


class ConfirmationService(Protocol):
    """Observe landing and execution independently from submission."""

    async def confirm(
        self, signature: str, blockhash: BlockhashContext, commitment: str
    ) -> ConfirmationResult: ...


class TelemetrySink(Protocol):
    """Persist or emit non-blocking, credential-safe execution telemetry."""

    async def record(self, telemetry: ExecutionTelemetry) -> None: ...
