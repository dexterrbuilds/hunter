"""Framework-neutral terminal snapshots, controls, and display states."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from application.runtime_models import ApplicationEvent, RuntimeStatus


class TerminalView(StrEnum):
    """Navigable operator views."""

    OVERVIEW = "overview"
    RUNTIME = "runtime"
    FEEDS = "feeds"
    PROVIDERS = "providers"
    POSITIONS = "positions"
    POSITION_DETAILS = "position_details"
    EXECUTIONS = "executions"
    TRANSACTION = "transaction"
    WALLETS = "wallets"
    TRACKED_WALLETS = "tracked_wallets"
    WALLET_FLEET = "wallet_fleet"
    TOKEN_LAUNCHES = "token_launches"  # noqa: S105
    RISK = "risk"
    EVENTS = "events"
    LATENCY = "latency"
    SETTINGS = "settings"


class OperatorAction(StrEnum):
    """Consequential controls that require a fresh confirmation."""

    ENABLE_TRADING = "enable_trading"
    DISABLE_TRADING = "disable_trading"
    ACTIVATE_KILL_SWITCH = "activate_kill_switch"
    CLEAR_KILL_SWITCH = "clear_kill_switch"


class ValueState(StrEnum):
    """Prevent missing measurements from being displayed as numeric zero."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNMEASURED = "unmeasured"
    STALE = "stale"
    INSUFFICIENT = "insufficient_samples"


@dataclass(frozen=True, slots=True)
class DisplayValue:
    """One explicitly qualified dashboard value."""

    value: str | int | float | None
    state: ValueState
    samples: int = 0


@dataclass(frozen=True, slots=True)
class OperatorConfirmation:
    """Short-lived server-side confirmation with no executable payload."""

    nonce: str
    action: OperatorAction
    summary: str
    created_mono: float
    expires_mono: float


@dataclass(frozen=True, slots=True)
class PerformanceSnapshot:
    """Measured execution timing; missing values remain explicitly missing."""

    detection_to_decision_ms: DisplayValue
    build_ms: DisplayValue
    sign_ms: DisplayValue
    submit_ms: DisplayValue
    acknowledgement_ms: DisplayValue
    landing_ms: DisplayValue
    same_slot: int | None
    next_slot: int | None
    later_slot: int | None
    observations: int
    percentiles: dict[str, dict[str, DisplayValue]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TerminalSnapshot:
    """Immutable data supplied to the renderer without provider objects."""

    status: RuntimeStatus
    positions: tuple[object, ...]
    executions: tuple[object, ...]
    wallets: tuple[dict[str, object], ...]
    tracked_wallets: tuple[str, ...]
    risk: dict[str, Any]
    settings: dict[str, Any]
    recent_events: tuple[ApplicationEvent, ...]
    telemetry: tuple[dict[str, Any], ...]
    performance: PerformanceSnapshot
    telemetry_freshness: ValueState
    wallet_freshness: ValueState
    capabilities: dict[str, bool]
    routing_mode: str
    position_offset: int
    execution_offset: int
    event_drops: int
    refresh_coalesced: int
    data_errors: tuple[str, ...] = ()
    refreshed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
