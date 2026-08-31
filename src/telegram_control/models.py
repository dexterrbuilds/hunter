"""Framework-neutral Telegram messages, actions, status, and metrics."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from time import monotonic
from typing import Any


class TelegramServiceState(StrEnum):
    DISABLED = "disabled"
    STARTING = "starting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    FAILED = "failed"
    STOPPED = "stopped"


class PendingActionState(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"


class PendingActionType(StrEnum):
    BUY = "buy"
    SELL = "sell"
    ENABLE = "enable"
    DISABLE = "disable"
    KILL = "kill"
    FLEET_EXIT = "fleet_exit"
    LAUNCH = "launch"


class NotificationPriority(IntEnum):
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    DEBUG = 3


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    user_id: int
    chat_id: int
    chat_type: str
    text: str
    username: str | None = None


@dataclass(frozen=True, slots=True)
class IncomingCallback:
    callback_id: str
    user_id: int
    chat_id: int
    chat_type: str
    data: str
    username: str | None = None


@dataclass(frozen=True, slots=True)
class MessageButton:
    label: str
    callback_data: str


@dataclass(frozen=True, slots=True)
class PendingAction:
    nonce: str
    owner_user_id: int
    chat_id: int
    action_type: PendingActionType
    payload: Any
    summary: str
    created_mono: float
    expires_mono: float
    state: PendingActionState = PendingActionState.PENDING


@dataclass(frozen=True, slots=True)
class TelegramNotification:
    text: str
    priority: NotificationPriority
    category: str
    event_id: str | None = None


@dataclass(slots=True)
class TelegramMetrics:
    commands_received: int = 0
    commands_rejected: int = 0
    unauthorized_attempts: int = 0
    callbacks_received: int = 0
    callbacks_replayed: int = 0
    pending_expired: int = 0
    notifications_queued: int = 0
    notifications_sent: int = 0
    notifications_dropped: int = 0
    telegram_errors: int = 0
    started_mono: float = field(default_factory=monotonic)

    def snapshot(self, pending_actions: int) -> dict[str, int | float]:
        return {
            "commands_received": self.commands_received,
            "commands_rejected": self.commands_rejected,
            "unauthorized_attempts": self.unauthorized_attempts,
            "callbacks_received": self.callbacks_received,
            "callbacks_replayed": self.callbacks_replayed,
            "pending_actions": pending_actions,
            "pending_expired": self.pending_expired,
            "notifications_queued": self.notifications_queued,
            "notifications_sent": self.notifications_sent,
            "notifications_dropped": self.notifications_dropped,
            "telegram_errors": self.telegram_errors,
            "uptime_seconds": max(0.0, monotonic() - self.started_mono),
        }


@dataclass(frozen=True, slots=True)
class LaunchPendingPayload:
    request: Any
    submission_values: dict[str, object]
    plan_id: str
