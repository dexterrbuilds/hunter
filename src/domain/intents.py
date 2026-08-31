"""Interface-neutral reasons and requirements for Hunter transactions."""

# Runtime dataclass types are kept visible for introspection and documentation.
# ruff: noqa: S105, TC001, TC002, TC003, TRY003

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic_ns
from types import MappingProxyType
from uuid import uuid4

from solders.pubkey import Pubkey

from domain.amounts import BasisPoints, QuoteAmountRaw, TokenAmountRaw


class TradeAction(StrEnum):
    """Economic action requested by a trigger."""

    BUY = "buy"
    SELL = "sell"
    LAUNCH = "launch"


class EconomicActionClass(StrEnum):
    """Whether an intent increases or reduces Hunter-managed exposure."""

    ENTRY = "entry"
    EXIT = "exit"


class TradeIntentSource(StrEnum):
    """Why Hunter is executing; never which transport it should use."""

    LAUNCH_SNIPE = "launch_snipe"
    TRACKED_WALLET_CREATE = "tracked_wallet_create"
    TRACKED_WALLET_BUY = "tracked_wallet_buy"
    COPY_TRADE = "copy_trade"
    MANUAL_BUY = "manual_buy"
    MANUAL_SELL = "manual_sell"
    YOLO = "yolo"
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TIMED_EXIT = "timed_exit"
    EMERGENCY_EXIT = "emergency_exit"
    TOKEN_LAUNCH = "token_launch"
    LAUNCH_BUNDLE = "launch_bundle"
    WALLET_FLEET_EXIT = "wallet_fleet_exit"

    @property
    def benchmark_category(self) -> str:
        """Return the stable source name used by benchmark/report exports."""
        aliases = {
            self.MANUAL_BUY: "MANUAL",
            self.MANUAL_SELL: "MANUAL",
            self.TAKE_PROFIT: "TP",
            self.STOP_LOSS: "SL",
            self.TIMED_EXIT: "TIME_EXIT",
            self.WALLET_FLEET_EXIT: "FLEET_EXIT",
        }
        return aliases.get(self, self.value.upper())

    @property
    def economic_action_class(self) -> EconomicActionClass:
        """Return the explicit exposure effect used by runtime authorization."""
        if self in {
            self.LAUNCH_SNIPE,
            self.TRACKED_WALLET_CREATE,
            self.TRACKED_WALLET_BUY,
            self.COPY_TRADE,
            self.MANUAL_BUY,
            self.YOLO,
            self.TOKEN_LAUNCH,
            self.LAUNCH_BUNDLE,
        }:
            return EconomicActionClass.ENTRY
        if self in {
            self.MANUAL_SELL,
            self.TAKE_PROFIT,
            self.STOP_LOSS,
            self.TIMED_EXIT,
            self.EMERGENCY_EXIT,
            self.WALLET_FLEET_EXIT,
        }:
            return EconomicActionClass.EXIT
        raise ValueError(f"unclassified trade intent source: {self.value}")


def classify_economic_action(
    source: TradeIntentSource, action: TradeAction
) -> EconomicActionClass:
    """Classify and validate one typed intent without heuristic matching."""
    classification = source.economic_action_class
    if classification == EconomicActionClass.EXIT and action != TradeAction.SELL:
        raise ValueError(f"exit source {source.value} requires a sell action")
    launch_sources = {
        TradeIntentSource.TOKEN_LAUNCH,
        TradeIntentSource.LAUNCH_BUNDLE,
    }
    if source in launch_sources and action != TradeAction.LAUNCH:
        raise ValueError(f"launch source {source.value} requires a launch action")
    if (
        classification == EconomicActionClass.ENTRY
        and source not in launch_sources
        and action != TradeAction.BUY
    ):
        raise ValueError(f"entry source {source.value} requires a buy action")
    if action == TradeAction.LAUNCH and source not in launch_sources:
        raise ValueError(f"source {source.value} cannot authorize a token launch")
    return classification


class ExecutionUrgency(StrEnum):
    """Scheduling hint; risk and fee caps remain absolute."""

    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    BACKGROUND = "background"


@dataclass(frozen=True, slots=True)
class TradeIntent:
    """A stable economic request independent of listeners and providers.

    Exactly one of ``quote_amount`` and ``token_amount`` is normally supplied:
    quote input for a buy, token input for a sell. A launch plan can omit both
    because its component amounts are explicit in the launch request.
    """

    action: TradeAction
    source: TradeIntentSource
    mint: Pubkey
    wallet_id: str
    slippage: BasisPoints
    quote_mint: Pubkey | None = None
    token_decimals: int | None = None
    quote_amount: QuoteAmountRaw | None = None
    token_amount: TokenAmountRaw | None = None
    position_id: str | None = None
    source_signature: str | None = None
    source_slot: int | None = None
    urgency: ExecutionUrgency = ExecutionUrgency.NORMAL
    metadata: Mapping[str, str | int | float | bool] = field(default_factory=dict)
    intent_id: str = field(default_factory=lambda: str(uuid4()))
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    received_mono_ns: int = field(default_factory=monotonic_ns)

    def __post_init__(self) -> None:
        if not self.wallet_id.strip():
            raise ValueError("wallet_id cannot be empty")
        if not self.intent_id.strip():
            raise ValueError("intent_id cannot be empty")
        if self.action == TradeAction.BUY:
            if self.quote_amount is None or self.token_amount is not None:
                raise ValueError("buy intent requires quote_amount only")
            if self.quote_mint is None:
                raise ValueError("buy intent requires quote_mint")
            if self.token_decimals is None or self.token_decimals < 0:
                raise ValueError("buy intent requires explicit token_decimals")
        elif self.action == TradeAction.SELL:
            if self.token_amount is None or self.quote_amount is not None:
                raise ValueError("sell intent requires token_amount only")
            if self.position_id is None or self.quote_mint is None:
                raise ValueError("sell intent requires position_id and quote_mint")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def logical_execution_id(self) -> str:
        """Use one identifier through risk, persistence, and submission."""
        return self.intent_id


def default_urgency(source: TradeIntentSource) -> ExecutionUrgency:
    """Return the conservative default scheduling urgency for a source."""
    if source in {
        TradeIntentSource.EMERGENCY_EXIT,
        TradeIntentSource.STOP_LOSS,
    }:
        return ExecutionUrgency.CRITICAL
    if source in {
        TradeIntentSource.LAUNCH_SNIPE,
        TradeIntentSource.TRACKED_WALLET_CREATE,
        TradeIntentSource.TRACKED_WALLET_BUY,
        TradeIntentSource.COPY_TRADE,
        TradeIntentSource.TAKE_PROFIT,
        TradeIntentSource.TIMED_EXIT,
        TradeIntentSource.LAUNCH_BUNDLE,
        TradeIntentSource.WALLET_FLEET_EXIT,
    }:
        return ExecutionUrgency.HIGH
    return ExecutionUrgency.NORMAL
