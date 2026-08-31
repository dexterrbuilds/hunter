"""Token-launch and operator wallet-fleet domain models."""

# ruff: noqa: TC001, TC002, TC003, TRY003

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from solders.pubkey import Pubkey

from domain.amounts import QuoteAmountRaw


class FleetWalletRole(StrEnum):
    CREATOR = "creator"
    PARTICIPANT = "participant"
    TREASURY = "treasury"


class LaunchState(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    PREPARING = "preparing"
    SIGNED = "signed"
    SUBMITTED = "submitted"
    LANDED = "landed"
    ACTIVE = "active"
    EXIT_REQUESTED = "exit_requested"
    EXIT_SUBMITTED = "exit_submitted"
    CLOSED = "closed"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class ComponentState(StrEnum):
    PLANNED = "planned"
    SIGNED = "signed"
    SUBMITTED = "submitted"
    LANDED = "landed"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"


class FleetExecutionPolicy(StrEnum):
    BUNDLE = "bundle"
    PARALLEL_FAST = "parallel_fast"
    SEQUENTIAL = "sequential"


class FleetExitType(StrEnum):
    MANUAL = "manual"
    PROFIT_TARGET = "profit_target"
    TIME_BASED = "time_based"
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    EMERGENCY = "emergency"


@dataclass(frozen=True, slots=True)
class SignerReference:
    """Non-secret handle resolved by a signer registry at runtime."""

    signer_id: str
    expected_public_key: Pubkey

    def __post_init__(self) -> None:
        if not self.signer_id.strip():
            raise ValueError("signer_id cannot be empty")


@dataclass(frozen=True, slots=True)
class FleetWallet:
    wallet_id: str
    signer: SignerReference
    role: FleetWalletRole

    def __post_init__(self) -> None:
        if not self.wallet_id.strip():
            raise ValueError("wallet_id cannot be empty")


@dataclass(frozen=True, slots=True)
class LaunchBuy:
    wallet_id: str
    quote_amount: QuoteAmountRaw


@dataclass(frozen=True, slots=True)
class FleetExitPolicy:
    exit_type: FleetExitType = FleetExitType.MANUAL
    target_bps: int | None = None
    after_seconds: int | None = None
    execution_policy: FleetExecutionPolicy = FleetExecutionPolicy.PARALLEL_FAST
    exclude_invalid_positions: bool = False

    def __post_init__(self) -> None:
        if (
            self.exit_type
            in {
                FleetExitType.PROFIT_TARGET,
                FleetExitType.TAKE_PROFIT,
                FleetExitType.STOP_LOSS,
            }
            and self.target_bps is None
        ):
            raise ValueError("percentage exit requires target_bps")
        if self.exit_type == FleetExitType.TIME_BASED and (
            self.after_seconds is None or self.after_seconds <= 0
        ):
            raise ValueError("time-based exit requires positive after_seconds")


@dataclass(frozen=True, slots=True)
class TokenLaunchRequest:
    """Typed on-chain launch request; contains no private key material."""

    name: str
    symbol: str
    uri: str
    mint: Pubkey
    mint_signer: SignerReference
    creator_wallet_id: str
    creator_buy: QuoteAmountRaw
    additional_wallet_buys: tuple[LaunchBuy, ...] = ()
    is_mayhem_mode: bool = False
    is_cashback_enabled: bool = False
    execution_policy: FleetExecutionPolicy = FleetExecutionPolicy.BUNDLE
    exit_policy: FleetExitPolicy = field(default_factory=FleetExitPolicy)
    marry_mode: bool = False
    launch_id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.symbol.strip() or not self.uri.strip():
            raise ValueError("launch name, symbol, and URI are required")
        if not self.uri.startswith(("https://", "ipfs://", "ar://")):
            raise ValueError("metadata URI must use https, ipfs, or ar")
        if self.creator_buy.value <= 0:
            raise ValueError("creator buy must be positive")
        wallet_ids = [item.wallet_id for item in self.additional_wallet_buys]
        if self.creator_wallet_id in wallet_ids or len(wallet_ids) != len(
            set(wallet_ids)
        ):
            raise ValueError("launch wallet IDs must be unique")
        if any(item.quote_amount.value <= 0 for item in self.additional_wallet_buys):
            raise ValueError("additional wallet buys must be positive")
        if any(
            item.quote_amount.mint != self.creator_buy.mint
            or item.quote_amount.decimals != self.creator_buy.decimals
            for item in self.additional_wallet_buys
        ):
            raise ValueError(
                "all launch buys must use the creator buy quote mint and decimals"
            )


@dataclass(frozen=True, slots=True)
class LaunchComponent:
    component_id: str
    sequence_index: int
    wallet_id: str
    wallet_role: FleetWalletRole
    action: str
    required_signer_ids: tuple[str, ...]
    logical_execution_id: str
    quote_amount_raw: int | None = None


@dataclass(frozen=True, slots=True)
class LaunchExecutionPlan:
    plan_id: str
    launch_id: str
    mint: Pubkey
    execution_policy: FleetExecutionPolicy
    components: tuple[LaunchComponent, ...]
    exit_policy: FleetExitPolicy
    marry_mode: bool
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: Mapping[str, str | int | bool] = field(default_factory=dict)

    @classmethod
    def from_request(cls, request: TokenLaunchRequest) -> LaunchExecutionPlan:
        """Freeze ordered economic identities before any component is signed."""
        plan_id = f"launch:{request.launch_id}"
        components = [
            LaunchComponent(
                component_id="create",
                sequence_index=0,
                wallet_id=request.creator_wallet_id,
                wallet_role=FleetWalletRole.CREATOR,
                action="create",
                required_signer_ids=(
                    request.creator_wallet_id,
                    request.mint_signer.signer_id,
                ),
                logical_execution_id=f"{plan_id}:create",
            ),
            LaunchComponent(
                component_id="creator_buy",
                sequence_index=1,
                wallet_id=request.creator_wallet_id,
                wallet_role=FleetWalletRole.CREATOR,
                action="buy",
                required_signer_ids=(request.creator_wallet_id,),
                logical_execution_id=f"{plan_id}:creator_buy",
                quote_amount_raw=request.creator_buy.value,
            ),
        ]
        for index, buy in enumerate(request.additional_wallet_buys, start=2):
            components.append(
                LaunchComponent(
                    component_id=f"wallet_buy_{index - 1}",
                    sequence_index=index,
                    wallet_id=buy.wallet_id,
                    wallet_role=FleetWalletRole.PARTICIPANT,
                    action="buy",
                    required_signer_ids=(buy.wallet_id,),
                    logical_execution_id=f"{plan_id}:wallet:{buy.wallet_id}:buy",
                    quote_amount_raw=buy.quote_amount.value,
                )
            )
        return cls(
            plan_id=plan_id,
            launch_id=request.launch_id,
            mint=request.mint,
            execution_policy=request.execution_policy,
            components=tuple(components),
            exit_policy=request.exit_policy,
            marry_mode=request.marry_mode,
            metadata={
                "name": request.name,
                "symbol": request.symbol,
                "is_mayhem_mode": request.is_mayhem_mode,
                "is_cashback_enabled": request.is_cashback_enabled,
            },
        )
