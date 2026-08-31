"""Domain models for explicit, operator-configured wallet tracking."""

# ruff: noqa: PLR2004, TC002, TRY003

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic_ns

from solders.pubkey import Pubkey

from domain.amounts import BasisPoints, QuoteAmountRaw, TokenAmountRaw


class WalletActivityType(StrEnum):
    CREATE = "create"
    BUY = "buy"


class DuplicatePolicy(StrEnum):
    IGNORE_EXISTING_POSITION = "ignore_existing_position"
    ALLOW_ADDITIONAL_COPY = "allow_additional_copy"
    AGGREGATE_POSITION = "aggregate_position"


class CopySizingMode(StrEnum):
    FIXED = "fixed"
    PERCENTAGE_OF_SOURCE = "percentage_of_source"
    PERCENTAGE_OF_WALLET = "percentage_of_wallet"


@dataclass(frozen=True, slots=True)
class TrackedWalletAction:
    enabled: bool
    sizing_mode: CopySizingMode = CopySizingMode.FIXED
    fixed_quote_amount: QuoteAmountRaw | None = None
    percentage_bps: BasisPoints | None = None
    slippage: BasisPoints = field(default_factory=lambda: BasisPoints(100))

    def __post_init__(self) -> None:
        if not self.enabled:
            return
        if self.sizing_mode == CopySizingMode.FIXED:
            if self.fixed_quote_amount is None:
                raise ValueError("fixed wallet action requires fixed_quote_amount")
        elif self.percentage_bps is None:
            raise ValueError("percentage sizing requires percentage_bps")


@dataclass(frozen=True, slots=True)
class TrackedWallet:
    address: Pubkey
    watch_create: bool
    watch_buy: bool
    create_action: TrackedWalletAction
    copy_action: TrackedWalletAction
    label: str | None = None


@dataclass(frozen=True, slots=True)
class WalletActivity:
    """One successful, authoritative Pump.fun CREATE or BUY event."""

    activity_type: WalletActivityType
    wallet: Pubkey
    mint: Pubkey
    signature: str
    slot: int
    program_id: Pubkey
    quote_mint: Pubkey | None = None
    source_quote_amount: QuoteAmountRaw | None = None
    source_token_amount: TokenAmountRaw | None = None
    token_decimals: int | None = None
    source: str = "unknown"
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    observed_mono_ns: int = field(default_factory=monotonic_ns)
    decoded_mono_ns: int = field(default_factory=monotonic_ns)

    @property
    def event_id(self) -> str:
        return f"{self.signature}:{self.activity_type.value}:{self.wallet}:{self.mint}"


@dataclass(frozen=True, slots=True)
class WalletTrackingConfig:
    enabled: bool = False
    duplicate_policy: DuplicatePolicy = DuplicatePolicy.IGNORE_EXISTING_POSITION
    wallets: tuple[TrackedWallet, ...] = ()
    maximum_pending_events: int = 256
    decoder_workers: int = 2

    def __post_init__(self) -> None:
        if self.maximum_pending_events < 1:
            raise ValueError("maximum_pending_events must be positive")
        if not 1 <= self.decoder_workers <= 32:
            raise ValueError("decoder_workers must be between 1 and 32")
        addresses = [wallet.address for wallet in self.wallets]
        if len(addresses) > 128:
            raise ValueError("at most 128 tracked wallets are supported per process")
        if len(set(addresses)) != len(addresses):
            raise ValueError("tracked wallet addresses must be unique")
