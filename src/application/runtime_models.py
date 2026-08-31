"""Typed, framework-neutral Hunter runtime lifecycle and status models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class ApplicationState(StrEnum):
    """Observable application lifecycle states."""

    CREATED = "created"
    CONFIG_VALIDATED = "config_validated"
    PERSISTENCE_READY = "persistence_ready"
    RECOVERY_RUNNING = "recovery_running"
    INFRASTRUCTURE_WARMING = "infrastructure_warming"
    INFRASTRUCTURE_READY = "infrastructure_ready"
    SERVICES_STARTING = "services_starting"
    READY = "ready"
    DEGRADED = "degraded"
    NOT_READY = "not_ready"
    FAILED = "failed"
    SHUTTING_DOWN = "shutting_down"
    STOPPED = "stopped"


class RuntimeComponentState(StrEnum):
    """Connection and service state reported without provider secrets."""

    CONFIGURED = "configured"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECEIVING = "receiving"
    READY = "ready"
    WARM = "warm"
    DEGRADED = "degraded"
    FAILED = "failed"
    STOPPED = "stopped"


class TaskFailurePolicy(StrEnum):
    """How a supervised background-task failure affects the application."""

    CRITICAL = "critical"
    RESTARTABLE = "restartable"
    OPTIONAL = "optional"


@dataclass(frozen=True, slots=True)
class ComponentStatus:
    """Safe status for one configured runtime dependency."""

    name: str
    state: RuntimeComponentState
    required: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class StartupPhaseTiming:
    """Wall/monotonic-derived duration for one startup phase."""

    phase: str
    duration_ms: float


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    """Framework-neutral status consumed by future interfaces."""

    application_state: ApplicationState
    trading_enabled: bool
    kill_switch: bool
    recovery_complete: bool
    database_ready: bool
    blockhash_ready: bool | None
    fee_cache_ready: bool | None
    tip_cache_ready: bool | None
    listeners: tuple[ComponentStatus, ...]
    providers: tuple[ComponentStatus, ...]
    tracked_wallet: ComponentStatus | None
    active_bots: tuple[str, ...]
    open_positions: int
    active_fleets: int
    pending_executions: int
    pending_reconciliation: int
    startup_timings: tuple[StartupPhaseTiming, ...]
    dropped_events: int
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class ApplicationEvent:
    """Non-durable notification event; economic truth remains in SQLite."""

    event_type: str
    bot_id: str
    attributes: dict[str, str | int | float | bool | None] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
