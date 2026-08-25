"""Provider-neutral transaction observation semantics."""

from __future__ import annotations

from dataclasses import dataclass

from domain.lifecycle import ExecutionState
from execution.errors import ErrorClassification


@dataclass(frozen=True, slots=True)
class TransactionObservation:
    """One unambiguous interpretation of RPC status and transaction data."""

    signature: str
    state: ExecutionState
    slot: int | None = None
    confirmation_status: str | None = None
    meta_error: object | None = None
    error_classification: ErrorClassification | None = None

    @property
    def succeeded(self) -> bool:
        return (
            self.state
            in {
                ExecutionState.PROCESSED,
                ExecutionState.CONFIRMED,
                ExecutionState.FINALIZED,
            }
            and self.meta_error is None
        )

    @property
    def terminal(self) -> bool:
        return self.state in {
            ExecutionState.FINALIZED,
            ExecutionState.FAILED_ON_CHAIN,
            ExecutionState.EXPIRED,
            ExecutionState.DROPPED_UNKNOWN,
        }
