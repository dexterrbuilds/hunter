"""Fail-closed confidence assessment for Pump.fun zero-RPC construction."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from interfaces.core import Platform, TokenInfo


class FastPathConfidence(StrEnum):
    """Whether a creation event contains enough authoritative trade state."""

    AUTHORITATIVE_EVENT_STATE = "authoritative_event_state"
    AUTHORITATIVE_WITH_CACHED_STATIC_STATE = "authoritative_with_cached_static_state"
    REQUIRES_REFRESH = "requires_refresh"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class FastPathAssessment:
    """Confidence plus explicit missing fields for telemetry and fallback."""

    confidence: FastPathConfidence
    missing_fields: tuple[str, ...] = ()

    @property
    def may_skip_hot_path_reads(self) -> bool:
        return self.confidence in {
            FastPathConfidence.AUTHORITATIVE_EVENT_STATE,
            FastPathConfidence.AUTHORITATIVE_WITH_CACHED_STATIC_STATE,
        }


def assess_fast_path(token_info: TokenInfo) -> FastPathAssessment:
    """Assess only event state; never guess absent dynamic Pump.fun fields."""
    if token_info.platform != Platform.PUMP_FUN:
        return FastPathAssessment(FastPathConfidence.UNSUPPORTED)
    required = {
        "bonding_curve": token_info.bonding_curve,
        "associated_bonding_curve": token_info.associated_bonding_curve,
        "creator": token_info.creator,
        "creator_vault": token_info.creator_vault,
        "token_program_id": token_info.token_program_id,
        "quote_mint": token_info.quote_mint,
        "quote_token_program_id": token_info.quote_token_program_id,
    }
    missing = tuple(name for name, value in required.items() if value is None)
    if not token_info.state_from_event or missing:
        return FastPathAssessment(FastPathConfidence.REQUIRES_REFRESH, missing)
    return FastPathAssessment(FastPathConfidence.AUTHORITATIVE_EVENT_STATE)
