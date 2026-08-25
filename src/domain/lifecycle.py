"""Persistable position and execution lifecycle state machines."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from execution.errors import ErrorClassification


class PositionStatus(StrEnum):
    OPEN = "open"
    EXIT_REQUESTED = "exit_requested"
    SELL_SUBMITTED = "sell_submitted"
    SELL_CONFIRMED = "sell_confirmed"
    SELL_FAILED_RETRYABLE = "sell_failed_retryable"
    SELL_FAILED_PERMANENT = "sell_failed_permanent"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    CLOSED = "closed"


class ExecutionState(StrEnum):
    PLANNED = "planned"
    BUILDING = "building"
    SIGNED = "signed"
    RPC_ACCEPTED = "rpc_accepted"
    SIGNATURE_RECEIVED = "signature_received"
    PROCESSED = "processed"
    CONFIRMED = "confirmed"
    FINALIZED = "finalized"
    FAILED_ON_CHAIN = "failed_on_chain"
    EXPIRED = "expired"
    NOT_OBSERVED = "not_observed"
    TIMED_OUT = "timed_out"
    DROPPED_UNKNOWN = "dropped_unknown"


_POSITION_TRANSITIONS: dict[PositionStatus, frozenset[PositionStatus]] = {
    PositionStatus.OPEN: frozenset(
        {PositionStatus.EXIT_REQUESTED, PositionStatus.RECONCILIATION_REQUIRED}
    ),
    PositionStatus.EXIT_REQUESTED: frozenset(
        {
            PositionStatus.SELL_SUBMITTED,
            PositionStatus.SELL_FAILED_RETRYABLE,
            PositionStatus.SELL_FAILED_PERMANENT,
            PositionStatus.RECONCILIATION_REQUIRED,
        }
    ),
    PositionStatus.SELL_SUBMITTED: frozenset(
        {
            PositionStatus.SELL_CONFIRMED,
            PositionStatus.SELL_FAILED_RETRYABLE,
            PositionStatus.SELL_FAILED_PERMANENT,
            PositionStatus.RECONCILIATION_REQUIRED,
        }
    ),
    PositionStatus.SELL_CONFIRMED: frozenset(
        {
            PositionStatus.OPEN,
            PositionStatus.CLOSED,
            PositionStatus.RECONCILIATION_REQUIRED,
        }
    ),
    PositionStatus.SELL_FAILED_RETRYABLE: frozenset(
        {PositionStatus.EXIT_REQUESTED, PositionStatus.RECONCILIATION_REQUIRED}
    ),
    PositionStatus.SELL_FAILED_PERMANENT: frozenset(
        {PositionStatus.RECONCILIATION_REQUIRED}
    ),
    PositionStatus.RECONCILIATION_REQUIRED: frozenset(
        {PositionStatus.OPEN, PositionStatus.EXIT_REQUESTED, PositionStatus.CLOSED}
    ),
    PositionStatus.CLOSED: frozenset(),
}


RETRYABLE_ERRORS = frozenset(
    {
        ErrorClassification.RPC_TRANSPORT_FAILURE,
        ErrorClassification.RPC_RATE_LIMIT,
        ErrorClassification.BLOCKHASH_EXPIRED,
        ErrorClassification.CONFIRMATION_TIMEOUT,
        ErrorClassification.ACCEPTED_BUT_NOT_OBSERVED,
    }
)


def can_transition(current: PositionStatus, target: PositionStatus) -> bool:
    """Return whether a position lifecycle transition is legal."""
    return target in _POSITION_TRANSITIONS[current]


def require_transition(current: PositionStatus, target: PositionStatus) -> None:
    """Reject impossible or unsafe lifecycle changes."""
    if not can_transition(current, target):
        raise ValueError(
            f"invalid position transition: {current.value} -> {target.value}"
        )


def is_retryable(classification: ErrorClassification) -> bool:
    """Central safe retry policy for sell attempts."""
    return classification in RETRYABLE_ERRORS


@dataclass(frozen=True, slots=True)
class LifecycleTransition:
    position_id: str
    from_status: PositionStatus
    to_status: PositionStatus
    reason: str
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
