"""Typed error taxonomy for the future Hunter execution layer."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ErrorClassification(StrEnum):
    """Stable categories used for retry policy and execution telemetry."""

    CONFIGURATION_ERROR = "configuration_error"
    SIGNING_FAILURE = "signing_failure"
    RPC_TRANSPORT_FAILURE = "rpc_transport_failure"
    RPC_RATE_LIMIT = "rpc_rate_limit"
    RPC_REJECTION = "rpc_rejection"
    SIMULATION_FAILURE = "simulation_failure"
    ON_CHAIN_PROGRAM_FAILURE = "on_chain_program_failure"
    BLOCKHASH_EXPIRED = "blockhash_expired"
    TRANSACTION_DROPPED = "transaction_dropped"
    CONFIRMATION_TIMEOUT = "confirmation_timeout"
    ACCEPTED_BUT_NOT_OBSERVED = "accepted_but_not_observed"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    FEE_RENT_INSUFFICIENCY = "fee_rent_insufficiency"
    UNSUPPORTED_QUOTE_TOKEN = "unsupported_quote_token"
    MALFORMED_EVENT_STATE = "malformed_event_state"
    RISK_LIMIT_EXCEEDED = "risk_limit_exceeded"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_AUTHENTICATION_FAILURE = "provider_authentication_failure"
    BUNDLE_REJECTED = "bundle_rejected"
    TIP_TOO_LOW = "tip_too_low"
    DUPLICATE_SIGNATURE = "duplicate_signature"
    LEADER_ROUTING_FAILURE = "leader_routing_failure"
    UNSUPPORTED_PROVIDER = "unsupported_provider"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class ExecutionError(Exception):
    """Normalized execution error without changing the current exception path."""

    classification: ErrorClassification
    message: str
    code: str | int | None = None
    retryable: bool = False

    def __str__(self) -> str:
        """Render a concise non-provider-specific error."""
        code = f" ({self.code})" if self.code is not None else ""
        return f"{self.classification.value}{code}: {self.message}"
